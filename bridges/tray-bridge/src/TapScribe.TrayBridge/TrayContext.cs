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

    /// <summary>How long Quit waits for a Start that is still in flight to reach the point
    /// where it can tear its own meeting down. A backstop, not a promise: the session mint
    /// it is usually blocked on carries its own 20 s timeout, and Quit must stay responsive
    /// against a Recorder that accepted the connection and then went quiet.</summary>
    private static readonly TimeSpan StartSettleTimeout = TimeSpan.FromSeconds(5);

    private readonly NotifyIcon _icon;
    private readonly TrayIcons _icons = new();
    private readonly ToolStripMenuItem _statusItem;
    private readonly ToolStripMenuItem _startItem;
    private readonly ToolStripMenuItem _endItem;
    private readonly ToolStripMenuItem _pastMeetingsItem;
    private readonly System.Windows.Forms.Timer _resumeTimer;
    private readonly object _gate = new();
    // Read and written under _gate, always — Start/End/Resume/OpenPastMeeting snapshot it
    // and then carry the snapshot into thread-pool continuations, so there is no thread
    // this field is private to.
    private BridgeSettings _settings = BridgeSettingsStore.Load();
    private CaptureOrchestrator? _orchestrator;
    private WasapiDeviceEnumerator? _enumerator; // outlives the captures it opened; disposed at teardown
    private string? _sessionId; // the detached session the running meeting taps into
    // The in-flight End/Resume flow's cancellation, published for Quit. Whoever takes it
    // out of this field owns it, so a Cancel can never race the Dispose.
    private CancellationTokenSource? _flowCancellation;
    // The in-flight StartAsync, if any. A meeting exists from the operator's first click,
    // not from the moment it is published, and Quit has to be able to see that window —
    // TakeMeeting reports "nothing running" throughout it.
    private Task? _startTask;
    // Set once Quit has run. A start that is mid-flight then tears its own meeting down
    // instead of publishing it into a shell whose message loop is gone.
    private bool _quitting;
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
            // Already running, already starting (the mint is a network round-trip long), or
            // on the way out.
            if (_orchestrator is not null || _startTask is { IsCompleted: false } || _quitting)
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
        // Publish the task, not just fire and forget it: Quit waits on this so a meeting
        // minted a moment before the operator quit is torn down instead of abandoned. Safe
        // to assign after the call — StartAsync yields at the first await and both this and
        // Quit run on the UI thread, so nothing can observe the gap.
        Task start = StartAsync(settings, ui);
        lock (_gate)
            _startTask = start;
    }

    private async Task StartAsync(BridgeSettings settings, SynchronizationContext ui)
    {
        WasapiDeviceEnumerator? enumerator = null;
        // Captures we have opened but not yet handed to the orchestrator. Nothing else can
        // reach them, so an exception in that window would strand every one of them for the
        // process lifetime — the finally below is their only owner until StartAll takes over.
        List<PipelineSpec>? unowned = null;
        // Non-null once the meeting is published, which is the LAST thing the try does.
        // Everything the shell still has to render then happens BELOW the catch, where it
        // cannot reach FailToIdle: a throw while balloon-ing a skipped device used to roll
        // a live, streaming meeting back to "idle" — Start re-enabled, End greyed out, and
        // no way left to end the meeting that was still recording.
        StartedMeeting? started = null;
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
            unowned = specs;
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

            // Which devices are actually streaming, and what that means for the status line,
            // is the core's DeviceTally — including the drop the shell used to show only as
            // a balloon while the header kept claiming a full house. Touched on the UI
            // thread only (both callbacks marshal first), which is the tally's contract.
            var tally = new DeviceTally(specs.Count);
            CaptureOrchestrator orchestrator = CaptureOrchestrator.StartAll(
                specs,
                onConnected: id => ui.Post(_ => ApplyStatus(tally.Connected(id)), null),
                onFailed: (id, ex) => ui.Post(_ =>
                {
                    ApplyStatus(tally.Dropped(id));
                    ShowBalloon($"{id} stopped", ex.Message);
                }, null));
                // No shared gate arg: each spec already carries its own per-device gate.
            unowned = null; // every capture now belongs to the orchestrator, including on its throw paths

            bool abandoned;
            lock (_gate)
            {
                // Quit ran while this start was in flight. Publishing now would hand the
                // meeting to a shell that has already torn down and stopped its message
                // loop: nobody would ever dispose it, the captures would keep streaming,
                // and the detached session would stay open on the Recorder. Take the
                // teardown ourselves instead — this is the same lock Quit's TakeMeeting
                // uses, so exactly one of the two runs it.
                abandoned = _quitting;
                if (!abandoned)
                {
                    _orchestrator = orchestrator;
                    _enumerator = enumerator;
                    _sessionId = sessionId;
                    _startedAt = DateTimeOffset.Now; // captured for the Past-meetings history at End (#168)
                }
            }
            if (abandoned)
            {
                // The 2 s-per-session bounded teardown, the same one Quit uses; the finally
                // then releases the enumerator, after the captures it opened.
                await orchestrator.DisposeAsync().ConfigureAwait(false);
                return;
            }

            enumerator = null; // ownership transferred; the finally below must not dispose it
            started = new StartedMeeting(tally, resolution.Missing); // the meeting is live from here
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
            // Same rule one level down for the captures themselves: any we opened but never
            // handed over. Null once StartAll has them (it releases what it refuses), so a
            // running meeting's devices are never touched here. Dispose is contract-bound
            // not to throw, so it needs no guard of its own.
            if (unowned is not null)
                foreach (PipelineSpec spec in unowned)
                    spec.Capture.Dispose();
        }

        if (started is null)
            return; // the catch (or the non-Ok early return) already surfaced the failure

        // From here the meeting IS streaming, and everything left is presentation. It sits
        // outside the try on purpose: a failure to render must never be classified as a
        // failure to START.
        //
        // Devices that didn't resolve are a non-fatal warning — the meeting runs on the
        // ones that did.
        if (started.Missing.Count > 0)
        {
            string skipped = string.Join(", ", started.Missing.Select(DescribeSelection));
            ui.Post(_ => ShowBalloon("Some devices unavailable", $"Skipped: {skipped}"), null);
        }

        ui.Post(_ =>
        {
            SetMeetingControls(running: true);
            ApplyStatus(started.Tally.Status);
        }, null);
    }

    /// <summary>A published, streaming meeting's presentation state — the live
    /// <see cref="DeviceTally"/> and the selections that didn't resolve. Its non-nullness
    /// is what tells <see cref="StartAsync"/> the meeting reached the point of no return.</summary>
    private sealed record StartedMeeting(DeviceTally Tally, IReadOnlyList<DeviceSelection> Missing);

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
        bool process = settings.ProcessOnEnd;
        // Only persist the resume state + the Past-meetings entry when a pipeline will actually
        // run: a record-only meeting (ProcessOnEnd == false) has no pipeline to resume across a
        // restart and no summary to re-open, so it stays out of both. Both writes are
        // best-effort — a failed write never breaks the drain.
        if (process)
        {
            // Persist the session so a tray restart mid-pipeline resumes showing it; cleared
            // when the flow reaches a terminal state (RunPipelineFlowAsync's finally).
            MeetingStateStore.Save(new MeetingState { SessionId = sessionId });
            // Record the meeting in the local Past-meetings history (#168), beside the resume
            // state, at End time.
            MeetingHistoryStore.Append(new MeetingRecord { SessionId = sessionId, StartedAt = startedAt });
        }
        return RunPipelineFlowAsync(
            settings, sessionId, ui,
            // Record-only (ProcessOnEnd == false) still drains below but skips the trigger/poll,
            // ending at a terminal Saved view; the default runs the full pipeline (issue #107).
            run: (controller, ct) => controller.EndAsync(triggerPipeline: process, cancellationToken: ct),
            // Close every open tap (gate close + Drain) BEFORE the pipeline strips; the
            // controller awaits this to completion before it triggers the pipeline.
            drainAsync: async () =>
            {
                try
                {
                    // End-meeting teardown is ONE call — drain every tap to completion
                    // THEN stop+dispose capture — so it can't be reduced to a drain that
                    // leaks the devices and streams past the barrier (see EndMeetingAsync).
                    await orchestrator.EndMeetingAsync().ConfigureAwait(false);
                }
                finally
                {
                    // Release the endpoints even if the teardown above failed. Sequenced
                    // after the await it was skipped on a throw, and nothing else holds
                    // this enumerator once End has detached the meeting — so the devices
                    // stayed open until the process exited.
                    enumerator?.Dispose();
                }
            });
    }

    // Build the controller, wire its emissions to the UI thread, run the flow (End or
    // Resume), and always clear the persisted state when it terminates. The one place the
    // End and Resume paths share — they differ only in the run delegate and the drain.
    private async Task RunPipelineFlowAsync(BridgeSettings settings, string sessionId,
        SynchronizationContext ui, Func<MeetingController, CancellationToken, Task> run, Func<Task>? drainAsync)
    {
        using var control = new ControlClient(
            settings.Host, settings.Port, settings.Tls, settings.Token,
            allowSelfSignedCert: settings.AllowSelfSignedCert);
        var controller = new MeetingController(
            control, sessionId, pollDelay: ct => Task.Delay(PollInterval, ct), drainAsync: drainAsync);
        controller.Updated += view => ui.Post(_ => RenderPipeline(view), null);
        controller.OperatorNotice += message => ui.Post(_ => ShowBalloon("Meeting", message), null);

        // The flow's poll loop runs on an uncancellable path today: Quit ends the message
        // loop and leaves it polling into a dead UI. Publish a source Quit can cancel —
        // the same seam OpenPastMeeting already owns for its window. Ownership is the
        // field: whoever takes the CTS out of it is responsible for it, so Quit's Cancel
        // and this method's Dispose can never race.
        var cancellation = new CancellationTokenSource();
        lock (_gate)
            _flowCancellation = cancellation;

        bool handled = false;
        try
        {
            await run(controller, cancellation.Token).ConfigureAwait(false);
            handled = true;
        }
        catch (Exception ex) when (
            ex is HttpRequestException or OperationCanceledException or InvalidOperationException)
        {
            // The Recorder is unreachable / timed out / refused the trigger after the taps
            // drained: classify it and surface a clear error so the tray doesn't wedge on a
            // processing state. The filter keeps this off CodeQL's catch-all radar.
            StartFailure failure = StartFailure.Classify(ex, settings.Host, settings.Port);
            ui.Post(_ => FailPipeline(failure.Message), null);
            handled = true;
        }
        finally
        {
            MeetingStateStore.Clear();
            bool owned;
            lock (_gate)
            {
                owned = ReferenceEquals(_flowCancellation, cancellation);
                if (owned)
                    _flowCancellation = null;
            }
            if (owned)
                cancellation.Dispose(); // Quit didn't take it, so disposing it can't race its Cancel
            if (!handled)
                // An exception OUTSIDE the filter above is escaping this fire-and-forget
                // task. Nobody observes it, and both menu items are disabled with the header
                // stuck on "● Ending meeting…" — the tray is unusable until it is restarted.
                // The exception still propagates (it is not this method's to classify); this
                // only returns the menu to a usable state on its way out.
                ui.Post(_ => FailPipeline("The meeting could not be completed."), null);
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
        RunPipelineFlowAsync(settings, sessionId, ui, run: (controller, ct) => controller.ResumeAsync(ct), drainAsync: null);

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
            case PipelinePhase.Saved:
                // Record-only End (ProcessOnEnd == false): the taps drained and the recordings
                // are saved on the Recorder, but nothing was transcribed/summarized. A brief cue
                // and straight back to idle — no summary window (there is none).
                ShowInfoBalloon("Recording saved",
                    "The meeting was recorded. Transcribe or summarize it from the dashboard.");
                ResetIdleUi();
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
        // Stop an in-flight End/Resume flow first: ExitThread below kills the message loop
        // it renders into, so an uncancelled poll loop would keep talking to the Recorder
        // and posting into a dead context for as long as the process lingers. Taking it out
        // of the field makes this the owner; the flow then leaves it alone (and we don't
        // dispose it — the process is on its way out).
        CancellationTokenSource? flow;
        Task? start;
        lock (_gate)
        {
            flow = _flowCancellation;
            _flowCancellation = null;
            _quitting = true; // an in-flight start must tear itself down, not publish
            start = _startTask;
        }
        flow?.Cancel();

        // Let a start that is mid-flight settle. Until it publishes, TakeMeeting below sees
        // nothing to take — so without this wait, quitting during the session mint left the
        // captures streaming, the meeting undisposed and the detached session open on the
        // Recorder, all the way until the process died. Bounded: the mint has its own 20 s
        // timeout, and Quit must not hang behind a Recorder that accepted the connection
        // and went quiet. Past the bound the start is abandoned exactly as it was before.
        if (start is { IsCompleted: false })
        {
            try
            {
                start.Wait(StartSettleTimeout);
            }
            catch (AggregateException)
            {
                // StartAsync faulted with something outside its own catch filter. The
                // meeting was never published, so there is nothing here left to tear down;
                // what is lost is the failure's detail, on a path that is exiting anyway.
            }
        }

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
        BridgeSettings current;
        lock (_gate)
            current = _settings;
        using var form = new SettingsForm(current, ListDevices, meterEnumerator.Open);
        if (form.ShowDialog() != DialogResult.OK)
            return;

        // Publish the edit under the same lock every other reader of _settings takes (Start,
        // End, Resume, OpenPastMeeting). Those read it from thread-pool continuations, so
        // "they all happen to run on the UI thread" was never true — and an unlocked write
        // of a reference field is not ordered against them.
        BridgeSettings updated = form.Result;
        lock (_gate)
            _settings = updated;
        try
        {
            BridgeSettingsStore.Save(updated);
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
        running?.UpdateGates(updated.ToGateOptionsByIdentity());
    }

    private static IReadOnlyList<CaptureDevice> ListDevices()
    {
        using var enumerator = new WasapiDeviceEnumerator();
        return enumerator.List();
    }
}
