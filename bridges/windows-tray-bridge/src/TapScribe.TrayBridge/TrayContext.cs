using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text.Json;
using TapScribe.Bridge.Core;
using TapScribe.Bridge.Windows;

namespace TapScribe.TrayBridge;

/// <summary>
/// The tray shell: a NotifyIcon with a status header line, Start meeting / Stop meeting /
/// Settings / Quit. Connection settings, the device selection, and the level-gate knobs
/// are edited in a tabbed dialog and persisted to %APPDATA% (env vars only seed the
/// first-run defaults). "Start meeting" resolves the operator's device selection against
/// the devices present now (<see cref="DeviceSelection.Resolve"/>), mints a detached
/// session on the Recorder, and runs one capture pipeline per resolved device — all
/// co-located in that one session so both sides of a meeting are recorded as
/// separately-attributed speakers. Status (idle / streaming / error) is event-driven:
/// it reflects the Start pre-flight and the per-device connect/fail callbacks, with no
/// idle polling. The depth lives in the cross-platform core
/// (<see cref="CaptureOrchestrator"/>, <see cref="DeviceSelection"/>, <see cref="StatusView"/>).
/// </summary>
internal sealed class TrayContext : ApplicationContext
{
    private readonly NotifyIcon _icon;
    private readonly TrayIcons _icons = new();
    private readonly ToolStripMenuItem _statusItem;
    private readonly ToolStripMenuItem _startItem;
    private readonly ToolStripMenuItem _stopItem;
    private readonly object _gate = new();
    private BridgeSettings _settings = BridgeSettingsStore.Load();
    private CaptureOrchestrator? _orchestrator;
    private WasapiDeviceEnumerator? _enumerator; // outlives the captures it opened; disposed at teardown

    public TrayContext()
    {
        _statusItem = new ToolStripMenuItem("○ Idle") { Enabled = false };
        _startItem = new ToolStripMenuItem("Start meeting", null, (_, _) => Start());
        // Fire-and-forget (not async void): a teardown fault can't escape onto the UI
        // thread and crash the tray. DisposeAsync is throw-free anyway.
        _stopItem = new ToolStripMenuItem("Stop meeting", null, (_, _) => _ = StopAsync()) { Enabled = false };
        var settingsItem = new ToolStripMenuItem("Settings…", null, (_, _) => OpenSettings());
        var quitItem = new ToolStripMenuItem("Quit", null, (_, _) => Quit());

        var menu = new ContextMenuStrip();
        menu.Items.Add(_statusItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(_startItem);
        menu.Items.Add(_stopItem);
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

    private async Task StopAsync()
    {
        (CaptureOrchestrator? orchestrator, WasapiDeviceEnumerator? enumerator) = TakeAndResetUi();
        if (orchestrator is not null)
            await orchestrator.DisposeAsync();
        enumerator?.Dispose();
    }

    private (CaptureOrchestrator?, WasapiDeviceEnumerator?) TakeAndResetUi()
    {
        (CaptureOrchestrator?, WasapiDeviceEnumerator?) taken = Take();
        ResetIdleUi();
        return taken;
    }

    // Atomically detach the running meeting's orchestrator + enumerator, leaving both
    // null. The shared claim/null-out for Stop (which also resets the UI) and Quit
    // (which tears down without touching the menu, since it's exiting).
    private (CaptureOrchestrator?, WasapiDeviceEnumerator?) Take()
    {
        lock (_gate)
        {
            CaptureOrchestrator? orchestrator = _orchestrator;
            WasapiDeviceEnumerator? enumerator = _enumerator;
            _orchestrator = null;
            _enumerator = null;
            return (orchestrator, enumerator);
        }
    }

    private void SetMeetingControls(bool running)
    {
        _startItem.Enabled = !running;
        _stopItem.Enabled = running;
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
        (CaptureOrchestrator? orchestrator, WasapiDeviceEnumerator? enumerator) = Take();

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
