using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text.Json;
using TapScribe.Bridge.Core;
using TapScribe.Bridge.Windows;

namespace TapScribe.TrayBridge;

/// <summary>
/// The tray shell: a NotifyIcon with a status header line, Start meeting / End meeting /
/// Settings / Quit. Connection settings, the device selection, and the level-gate knobs
/// are edited in a tabbed dialog and persisted to %APPDATA% (env vars only seed the
/// first-run defaults). "Start meeting" resolves the operator's device selection against
/// the devices present now (<see cref="DeviceSelection.Resolve"/>), mints a detached
/// session on the Recorder, and runs one capture pipeline per resolved device — all
/// co-located in that one session so both sides of a meeting are recorded as
/// separately-attributed speakers.
///
/// "End meeting" (issue #107) closes every open tap (gate close + Drain) and then fires the
/// Recorder's end-of-meeting pipeline (strip → transcribe → summarize), polling per-stage
/// progress into the status line and popping the finished summary up with copy-to-clipboard
/// — all driven by the cross-platform, tested <see cref="MeetingController"/>; this shell
/// only renders its emissions. The active session id is persisted
/// (<see cref="MeetingStateStore"/>) so a restarted tray resumes showing an in-flight
/// pipeline or the finished summary. The depth lives in the core
/// (<see cref="CaptureOrchestrator"/>, <see cref="MeetingController"/>,
/// <see cref="PipelineView"/>, <see cref="StatusView"/>).
/// </summary>
internal sealed class TrayContext : ApplicationContext
{
    private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(1.5);

    private readonly NotifyIcon _icon;
    private readonly TrayIcons _icons = new();
    private readonly ToolStripMenuItem _statusItem;
    private readonly ToolStripMenuItem _startItem;
    private readonly ToolStripMenuItem _endItem;
    private readonly ToolStripMenuItem _pastMeetingsItem;
    private readonly System.Windows.Forms.Timer _resumeTimer;
    private readonly object _gate = new();
    private BridgeSettings _settings = BridgeSettingsStore.Load();
    private CaptureOrchestrator? _orchestrator;
    private WasapiDeviceEnumerator? _enumerator; // outlives the captures it opened; disposed at teardown
    private string? _sessionId; // the detached session the running meeting taps into
    private DateTimeOffset _startedAt; // wall-clock start of the running meeting, for Past-meetings history (#168)

    public TrayContext()
    {
        _statusItem = new ToolStripMenuItem("○ Idle") { Enabled = false };
        _startItem = new ToolStripMenuItem("Start meeting", null, (_, _) => Start());
        _endItem = new ToolStripMenuItem("End meeting", null, (_, _) => End()) { Enabled = false };
        // Past meetings (#168): rebuilt from the persisted history each time it opens, so it
        // reflects meetings ended since it was last shown. Each item opens that meeting's
        // own window; the submenu never touches the live status line or Start/End controls.
        _pastMeetingsItem = new ToolStripMenuItem("Past meetings");
        _pastMeetingsItem.DropDownOpening += (_, _) => RebuildPastMeetingsMenu();
        var settingsItem = new ToolStripMenuItem("Settings…", null, (_, _) => OpenSettings());
        var quitItem = new ToolStripMenuItem("Quit", null, (_, _) => Quit());

        var menu = new ContextMenuStrip();
        menu.Items.Add(_statusItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(_startItem);
        menu.Items.Add(_endItem);
        menu.Items.Add(_pastMeetingsItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(settingsItem);
        menu.Items.Add(quitItem);

        _icon = new NotifyIcon
        {
            Icon = _icons[TrayIcon.Idle],
            Text = "TapScribe — idle",
            Visible = true,
            ContextMenuStrip = menu,
        };

        // Resume a pipeline left running by a previous tray session, once the message loop
        // is pumping (so SynchronizationContext.Current is the WinForms context — capturing
        // it in the ctor is too early). A one-shot UI-thread timer is the seam for that.
        _resumeTimer = new System.Windows.Forms.Timer { Interval = 200 };
        _resumeTimer.Tick += ResumeIfNeeded;
        _resumeTimer.Enabled = true;
    }

    private void Start()
    {
        BridgeSettings settings;
        lock (_gate)
        {
            if (_orchestrator is not null)
                return;
            settings = _settings;
        }

        // Start runs as a menu-click handler — on the WinForms UI thread while the
        // message loop is pumping — so Current is the WindowsFormsSynchronizationContext
        // and Post() marshals callbacks back to the UI thread. (Capturing it in the ctor
        // would be too early: Application.Run hasn't installed the context yet, and a
        // NotifyIcon / ToolStripMenuItem are Components, not Controls.)
        SynchronizationContext ui = SynchronizationContext.Current
            ?? throw new InvalidOperationException("Start must run on the WinForms UI thread.");

        // Disable Start now so a second click can't race a second meeting; the rest is
        // async (a network round-trip to mint the session) and resolves on the UI thread.
        _startItem.Enabled = false;
        ApplyStatus(new TrayStatus.Starting());
        _ = StartAsync(settings, ui);
    }

    private async Task StartAsync(BridgeSettings settings, SynchronizationContext ui)
    {
        WasapiDeviceEnumerator? enumerator = null;
        try
        {
            // 1) Resolve the operator's device selection against what's present RIGHT NOW
            //    (follow-default binds to the current default). A non-Ok verdict is a hard
            //    stop surfaced clearly BEFORE any network call or device open.
            enumerator = new WasapiDeviceEnumerator();
            ResolveResult resolution = DeviceSelection.Resolve(settings.EffectiveDevices, enumerator.List());
            if (resolution.Verdict != SelectionVerdict.Ok)
            {
                string reason = resolution.Verdict switch
                {
                    SelectionVerdict.NothingToCapture =>
                        "None of your selected devices are available. Check the Devices tab in Settings.",
                    SelectionVerdict.DuplicateIdentity =>
                        "Two devices share an identity. Give each a distinct identity in Settings.",
                    _ => "Cannot start with the current device selection.",
                };
                FailToIdle(ui, "Could not start meeting", reason);
                return; // the finally disposes the enumerator on this early exit
            }

            // 2) Mint a detached session — this doubles as the connection pre-flight: if the
            //    Recorder is unreachable or the token is rejected, it throws here, before any
            //    device is opened, and the catch classifies it into a clear message.
            string sessionId;
            using (var control = new ControlClient(
                settings.Host, settings.Port, settings.Tls, settings.Token,
                allowSelfSignedCert: settings.AllowSelfSignedCert))
            using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(20)))
                // Bound the round-trip: without a token HttpClient waits its 100 s default,
                // which would otherwise wedge the tray on "Starting…" against a host that
                // accepts the connection but never replies.
                sessionId = await control.CreateDetachedSessionAsync(cts.Token).ConfigureAwait(false);

            // 3) Build one tap per resolved device (each routing into the one session under
            //    its own identity/name) and open its capture. ToTapOptions preserves the
            //    Resolved order, so options[i] pairs with Resolved[i].
            IReadOnlyList<TapConnectionOptions> perDevice =
                resolution.ToTapOptions(sessionId, settings.ToConnectionOptions());
            var specs = new List<PipelineSpec>();
            for (int i = 0; i < resolution.Resolved.Count; i++)
            {
                ResolvedDevice resolved = resolution.Resolved[i];
                // Each pipeline is built with its OWN device's gate (#151): the resolved
                // selection carries a concrete per-device GateSettings.
                TryAddSpec(specs, enumerator, resolved.Device, perDevice[i], resolved.Gate.ToGateOptions(), ui);
            }
            if (specs.Count == 0)
                // Every resolved device failed to OPEN (in use, format unsupported, …).
                throw new InvalidOperationException("No selected device could be opened.");

            int total = specs.Count;
            var connected = new HashSet<string>(StringComparer.Ordinal);
            CaptureOrchestrator orchestrator = CaptureOrchestrator.StartAll(
                specs,
                onConnected: id => ui.Post(_ =>
                {
                    connected.Add(id);
                    ApplyStatus(new TrayStatus.Streaming(connected.Count, total));
                }, null),
                onFailed: (id, ex) => ui.Post(_ => ShowBalloon($"{id} stopped", ex.Message), null));
                // No shared gate arg: each spec already carries its own per-device gate.

            lock (_gate)
            {
                _orchestrator = orchestrator;
                _enumerator = enumerator;
                _sessionId = sessionId;
                _startedAt = DateTimeOffset.Now; // captured for the Past-meetings history at End (#168)
            }
            enumerator = null; // ownership transferred; the finally below must not dispose it

            // Devices that didn't resolve are a non-fatal warning — the meeting runs on the
            // ones that did.
            if (resolution.Missing.Count > 0)
            {
                string skipped = string.Join(", ", resolution.Missing.Select(DescribeSelection));
                ui.Post(_ => ShowBalloon("Some devices unavailable", $"Skipped: {skipped}"), null);
            }

            ui.Post(_ =>
            {
                SetMeetingControls(running: true);
                ApplyStatus(new TrayStatus.Streaming(connected.Count, total));
            }, null);
        }
        catch (Exception ex) when (
            ex is HttpRequestException
                or OperationCanceledException
                or JsonException
                or InvalidOperationException
                or COMException
                or NotSupportedException
                or ArgumentException)
        {
            // Pre-flight or device-open failure: tear down anything half-built, classify
            // the cause, and return the menu to idle with a clear message. Includes the
            // session-mint timeout (OperationCanceledException) and a malformed new-session
            // response (JsonException) so neither can escape and wedge the tray on
            // "Starting…". The filter keeps this off CodeQL's catch-of-all radar.
            StartFailure failure = StartFailure.Classify(ex, settings.Host, settings.Port);
            FailToIdle(ui, "Could not start meeting", failure.Message);
        }
        finally
        {
            // Dispose on every exit path — the non-Ok early return, an exception from
            // List()/Resolve() (whether or not the catch filter matches it), or normal
            // completion. Once the orchestrator owns the enumerator (line above), this is
            // null and the dispose is a no-op, so the running meeting keeps its devices.
            enumerator?.Dispose();
        }
    }

    // Open one resolved device behind the capture seam and add a pipeline for it.
    // Best-effort: a device that fails to OPEN is surfaced and skipped, so a dead loopback
    // doesn't stop the mic from recording. (Opening a device is Windows-side, so the
    // cross-platform CaptureOrchestrator can't own it; it owns the symmetric START-failure
    // half — capture.Start throwing inside TapSession.Begin.)
    private void TryAddSpec(List<PipelineSpec> into, WasapiDeviceEnumerator enumerator,
                            CaptureDevice device, TapConnectionOptions options, GateOptions gate,
                            SynchronizationContext ui)
    {
        try
        {
            into.Add(new PipelineSpec(enumerator.Open(device), options, gate));
        }
        catch (Exception ex) when (
            ex is COMException or NotSupportedException or ArgumentException or InvalidOperationException)
        {
            ui.Post(_ => ShowBalloon($"Could not open {device.Name}", ex.Message), null);
        }
    }

    private static string DescribeSelection(DeviceSelection selection) => selection switch
    {
        DeviceSelection.FollowDefault { Flow: DeviceFlow.Capture } => "default microphone",
        DeviceSelection.FollowDefault { Flow: DeviceFlow.Render } => "default system audio",
        DeviceSelection.Pinned pinned => string.IsNullOrEmpty(pinned.Name) ? pinned.DeviceId : pinned.Name,
        _ => selection.Identity,
    };

    // End meeting (issue #107): close the open taps and run the end-of-meeting pipeline,
    // showing progress and the finished summary. Detach the running meeting atomically so
    // Quit/Settings can't race it; if nothing is running there's nothing to end.
    private void End()
    {
        SynchronizationContext ui = SynchronizationContext.Current
            ?? throw new InvalidOperationException("End must run on the WinForms UI thread.");

        (CaptureOrchestrator? orchestrator, WasapiDeviceEnumerator? enumerator, string? sessionId, DateTimeOffset startedAt) = TakeMeeting();
        if (orchestrator is null || sessionId is null)
            return;

        BridgeSettings settings;
        lock (_gate)
            settings = _settings;

        // Busy guard: both Start and End disabled for the whole pipeline, so a second
        // End-meeting click can't fire a second pipeline.
        SetBusyControls();
        ApplyStatus(new TrayStatus.Ending());
        _ = EndAsync(settings, sessionId, startedAt, orchestrator, enumerator, ui);
    }

    private Task EndAsync(BridgeSettings settings, string sessionId, DateTimeOffset startedAt,
        CaptureOrchestrator orchestrator, WasapiDeviceEnumerator? enumerator, SynchronizationContext ui)
    {
        // Persist the session so a tray restart mid-pipeline resumes showing it; cleared
        // when the flow reaches a terminal state (RunPipelineFlowAsync's finally).
        MeetingStateStore.Save(new MeetingState { SessionId = sessionId });
        // Record the meeting in the local Past-meetings history (#168), beside the resume
        // state, at End time — best-effort: a failed history write never breaks the pipeline.
        MeetingHistoryStore.Append(new MeetingRecord { SessionId = sessionId, StartedAt = startedAt });
        return RunPipelineFlowAsync(
            settings, sessionId, ui,
            run: controller => controller.EndAsync(),
            // Close every open tap (gate close + Drain) BEFORE the pipeline strips; the
            // controller awaits this to completion before it triggers the pipeline.
            drainAsync: async () =>
            {
                await orchestrator.DisposeAsync().ConfigureAwait(false);
                enumerator?.Dispose();
            });
    }

    // Build the controller, wire its emissions to the UI thread, run the flow (End or
    // Resume), and always clear the persisted state when it terminates. The one place the
    // End and Resume paths share — they differ only in the run delegate and the drain.
    private async Task RunPipelineFlowAsync(BridgeSettings settings, string sessionId,
        SynchronizationContext ui, Func<MeetingController, Task> run, Func<Task>? drainAsync)
    {
        using var control = new ControlClient(
            settings.Host, settings.Port, settings.Tls, settings.Token,
            allowSelfSignedCert: settings.AllowSelfSignedCert);
        var controller = new MeetingController(
            control, sessionId, pollDelay: ct => Task.Delay(PollInterval, ct), drainAsync: drainAsync);
        controller.Updated += view => ui.Post(_ => RenderPipeline(view), null);
        controller.OperatorNotice += message => ui.Post(_ => ShowBalloon("Meeting", message), null);

        try
        {
            await run(controller).ConfigureAwait(false);
        }
        catch (Exception ex) when (
            ex is HttpRequestException or OperationCanceledException or InvalidOperationException)
        {
            // The Recorder is unreachable / timed out / refused the trigger after the taps
            // drained: classify it and surface a clear error so the tray doesn't wedge on a
            // processing state. The filter keeps this off CodeQL's catch-all radar.
            StartFailure failure = StartFailure.Classify(ex, settings.Host, settings.Port);
            ui.Post(_ => FailPipeline(failure.Message), null);
        }
        finally
        {
            MeetingStateStore.Clear();
        }
    }

    // Once the message loop is running, resume a pipeline a previous tray session left
    // behind (the Recorder keeps it going across both restarts). No drain, no re-trigger.
    private void ResumeIfNeeded(object? sender, EventArgs e)
    {
        _resumeTimer.Stop();
        _resumeTimer.Dispose();

        MeetingState? state = MeetingStateStore.Load();
        if (state is null)
            return; // the common case: a fresh launch with no meeting to resume

        SynchronizationContext ui = SynchronizationContext.Current
            ?? throw new InvalidOperationException("Resume must run on the WinForms UI thread.");
        BridgeSettings settings;
        lock (_gate)
            settings = _settings;

        SetBusyControls();
        ApplyStatus(new TrayStatus.Processing("Resuming…"));
        _ = ResumeAsync(settings, state.SessionId, ui);
    }

    // Resume polls only — no drain, no re-trigger (RunPipelineFlowAsync with a null drain).
    private Task ResumeAsync(BridgeSettings settings, string sessionId, SynchronizationContext ui) =>
        RunPipelineFlowAsync(settings, sessionId, ui, run: controller => controller.ResumeAsync(), drainAsync: null);

    // Render a MeetingController emission on the UI thread: the status line tracks the
    // pipeline phase, and the terminal phases pop the summary / the failure.
    private void RenderPipeline(PipelineView view)
    {
        switch (view.Phase)
        {
            case PipelinePhase.Ending:
                ApplyStatus(new TrayStatus.Ending());
                break;
            case PipelinePhase.Running:
                ApplyStatus(new TrayStatus.Processing(view.Progress ?? "Processing…"));
                break;
            case PipelinePhase.Done:
                ApplyStatus(new TrayStatus.SummaryReady());
                ShowInfoBalloon("Meeting summary ready", "Your meeting notes are ready.");
                ShowSummary(view);
                SetMeetingControls(running: false);
                break;
            case PipelinePhase.Failed:
                FailPipeline(view.FailureReason ?? "The end-of-meeting pipeline failed.", view.FailureStage);
                break;
            default:
                // Idle / Recording — a resumed session that has no live pipeline; back to idle.
                ResetIdleUi();
                break;
        }
    }

    private void ShowSummary(PipelineView view)
    {
        var form = new MeetingForm();
        form.FormClosed += (_, _) => form.Dispose();
        form.Render(view); // opened straight at the finished summary (#107)
        form.Show();
    }

    // Rebuild the Past-meetings submenu from the persisted history each time it opens (#168):
    // newest-first, one item per meeting. An empty (or unreadable → empty) history shows a
    // single disabled placeholder rather than a bare submenu.
    private void RebuildPastMeetingsMenu()
    {
        // Dispose the previous items before rebuilding: DropDownItems.Clear() detaches them but
        // does NOT dispose, so without this each submenu open leaks the prior menu items (the
        // tray lives for days). Snapshot first — Dispose() detaches from the collection, which
        // would mutate it mid-iteration.
        ToolStripItem[] previous = [.. _pastMeetingsItem.DropDownItems.Cast<ToolStripItem>()];
        _pastMeetingsItem.DropDownItems.Clear();
        foreach (ToolStripItem item in previous)
            item.Dispose();

        MeetingHistory history = MeetingHistoryStore.Load();
        if (history.Meetings.Count == 0)
        {
            _pastMeetingsItem.DropDownItems.Add(new ToolStripMenuItem("(No past meetings)") { Enabled = false });
            return;
        }
        foreach (MeetingRecord record in history.Meetings)
            _pastMeetingsItem.DropDownItems.Add(
                new ToolStripMenuItem(record.MenuLabel(), null, (_, _) => OpenPastMeeting(record)));
    }

    // Open a past meeting (#168) in its OWN window, isolated from the tray status line and the
    // Start/End controls — re-opening last week's notes must never disturb a live meeting. The
    // window shows Loading immediately, then a MeetingController.ResumeAsync rides the session
    // to its summary (or a "no longer available" failure). Read-only: never drains, never
    // re-triggers — so opening it alongside a live meeting (or its own in-flight End) is safe.
    private void OpenPastMeeting(MeetingRecord record)
    {
        SynchronizationContext ui = SynchronizationContext.Current
            ?? throw new InvalidOperationException("OpenPastMeeting must run on the WinForms UI thread.");
        BridgeSettings settings;
        lock (_gate)
            settings = _settings;

        var form = new MeetingForm();
        var cts = new CancellationTokenSource();
        form.FormClosed += (_, _) =>
        {
            cts.Cancel(); // stop the poll loop the instant the user closes the window
            cts.Dispose();
            form.Dispose();
        };
        form.Show();
        _ = OpenPastMeetingAsync(settings, record.SessionId, form, ui, cts.Token);
    }

    private async Task OpenPastMeetingAsync(BridgeSettings settings, string sessionId,
        MeetingForm form, SynchronizationContext ui, CancellationToken cancellationToken)
    {
        using var control = new ControlClient(
            settings.Host, settings.Port, settings.Tls, settings.Token,
            allowSelfSignedCert: settings.AllowSelfSignedCert);
        var controller = new MeetingController(control, sessionId, pollDelay: ct => Task.Delay(PollInterval, ct));
        // The render-marshaling + ride-to-summary lives in the cross-platform-tested Core
        // MeetingViewDriver (the form is the IMeetingView); this shell just supplies the
        // ControlClient, the WinForms SynchronizationContext, and the window.
        await MeetingViewDriver.DriveAsync(controller, form, ui, cancellationToken).ConfigureAwait(false);
    }

    private void FailPipeline(string reason, string? stage = null)
    {
        ApplyStatus(new TrayStatus.PipelineFailed(reason));
        ShowBalloon("Meeting summary failed", stage is null ? reason : $"{stage}: {reason}");
        SetMeetingControls(running: false);
    }

    // Atomically detach the running meeting's orchestrator + enumerator + session id,
    // leaving all three null. Shared by End (which then drains + triggers the pipeline)
    // and Quit (which tears down without touching the menu, since it's exiting).
    private (CaptureOrchestrator?, WasapiDeviceEnumerator?, string?, DateTimeOffset) TakeMeeting()
    {
        lock (_gate)
        {
            CaptureOrchestrator? orchestrator = _orchestrator;
            WasapiDeviceEnumerator? enumerator = _enumerator;
            string? sessionId = _sessionId;
            DateTimeOffset startedAt = _startedAt;
            _orchestrator = null;
            _enumerator = null;
            _sessionId = null;
            _startedAt = default; // cleared with the rest of the meeting state — no active meeting
            return (orchestrator, enumerator, sessionId, startedAt);
        }
    }

    private void SetMeetingControls(bool running)
    {
        _startItem.Enabled = !running;
        _endItem.Enabled = running;
    }

    // Both disabled: a meeting is being ended / a pipeline is in flight.
    private void SetBusyControls()
    {
        _startItem.Enabled = false;
        _endItem.Enabled = false;
    }

    private void ResetIdleUi()
    {
        SetMeetingControls(running: false);
        ApplyStatus(new TrayStatus.Idle());
    }

    private void FailToIdle(SynchronizationContext ui, string title, string message) =>
        ui.Post(_ =>
        {
            SetMeetingControls(running: false);
            ApplyStatus(new TrayStatus.Error(message));
            ShowBalloon(title, message);
        }, null);

    private void Quit()
    {
        (CaptureOrchestrator? orchestrator, WasapiDeviceEnumerator? enumerator, _, _) = TakeMeeting();

        // Tear every pipeline down: the orchestrator drains + closes all of them
        // CONCURRENTLY, each bounded, and DisposeAsync is throw-free — so this blocking
        // wait stays ~one drain budget (not N×) and can't deadlock or surface an
        // AggregateException. The timeout is a backstop; a sub-second tail may drop on a
        // hard quit.
        orchestrator?.DisposeAsync().AsTask().Wait(TimeSpan.FromSeconds(5));
        enumerator?.Dispose();

        _icon.Visible = false;
        _icon.Dispose();
        _icons.Dispose();
        ExitThread();
    }

    /// <summary>Apply a status to the menu header line, the icon, and the tooltip — the
    /// pure <see cref="StatusView"/> decides all three (issue #106 at-a-glance status).</summary>
    private void ApplyStatus(TrayStatus status)
    {
        StatusView view = StatusView.For(status);
        _statusItem.Text = view.Header;
        _icon.Icon = _icons[view.Icon];
        // NotifyIcon.Text is capped at 63 chars.
        _icon.Text = view.Tooltip.Length <= 63 ? view.Tooltip : view.Tooltip[..63];
    }

    private void ShowBalloon(string title, string message) =>
        _icon.ShowBalloonTip(4000, title, message, ToolTipIcon.Warning);

    private void ShowInfoBalloon(string title, string message) =>
        _icon.ShowBalloonTip(5000, title, message, ToolTipIcon.Info);

    private void OpenSettings()
    {
        // Editing while a meeting is live is allowed. Connection/device changes apply on
        // the next Start (those pipelines bound them at Begin); the per-device level-gate
        // knobs, however, are pushed to the running pipelines below so a sensitivity change
        // takes effect mid-meeting with no Stop/Start, re-tuning only the devices whose
        // tuning changed (issues #149, #153). The device list is supplied as a delegate so
        // the dialog can re-enumerate (Refresh) without owning the enumerator's lifecycle.
        // Persist on Save so the settings survive restarts.
        //
        // The dialog's live level meters (#152) open a second, display-only shared-mode
        // capture per device; this enumerator opens them and outlives those captures (the
        // form disposes them on close) — the same ownership shape as the meeting path.
        // Declared before the form so it disposes AFTER it (captures released first).
        using var meterEnumerator = new WasapiDeviceEnumerator();
        using var form = new SettingsForm(_settings, ListDevices, meterEnumerator.Open);
        if (form.ShowDialog() != DialogResult.OK)
            return;

        _settings = form.Result;
        try
        {
            BridgeSettingsStore.Save(_settings);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Couldn't write %APPDATA% (permissions, full disk): keep the new settings
            // for this session and tell the user they won't persist.
            ShowBalloon("Settings not saved", ex.Message);
        }

        // Re-tune a live meeting in place. Grab the orchestrator under the lock (StartAsync
        // may publish it from a thread-pool continuation), then call out WITHOUT holding
        // the lock — UpdateGates is a quick atomic fan-out and shouldn't run under _gate.
        // The per-identity map routes each device's new tuning to its own pipeline; one
        // whose identity isn't running is skipped. No meeting running -> null -> a no-op,
        // exactly as the AC requires. Applied even if the disk save above failed, so the
        // in-memory re-tune still reaches the pipelines for this session.
        CaptureOrchestrator? running;
        lock (_gate)
            running = _orchestrator;
        running?.UpdateGates(_settings.ToGateOptionsByIdentity());
    }

    private static IReadOnlyList<CaptureDevice> ListDevices()
    {
        using var enumerator = new WasapiDeviceEnumerator();
        return enumerator.List();
    }
}
