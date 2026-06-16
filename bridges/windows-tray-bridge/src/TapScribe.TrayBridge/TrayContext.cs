using System.Net.Http;
using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;
using TapScribe.Bridge.Windows;

namespace TapScribe.TrayBridge;

/// <summary>
/// The tray shell: a NotifyIcon with Start meeting / Stop meeting / Settings / Quit.
/// Connection settings are edited in a small dialog and persisted to %APPDATA% (env
/// vars only seed the first-run defaults). "Start meeting" mints a detached session
/// on the Recorder and runs one capture pipeline PER device — the default microphone
/// (under the operator's identity) and the system audio loopback (under "system") —
/// co-located in that one session, so both sides of a meeting are recorded as
/// separately-attributed speakers. The core's level gate opens/closes an Utterance per
/// speech segment within each pipeline, with reconnect + Drain. The richer tray UX (a
/// device-picker UI, end-meeting pipeline) lands in later PRD #99 slices (#106–#107);
/// the depth lives in the cross-platform core (<see cref="CaptureOrchestrator"/>).
/// </summary>
internal sealed class TrayContext : ApplicationContext
{
    private readonly NotifyIcon _icon;
    private readonly ToolStripMenuItem _startItem;
    private readonly ToolStripMenuItem _stopItem;
    private readonly object _gate = new();
    private BridgeSettings _settings = BridgeSettingsStore.Load();
    private CaptureOrchestrator? _orchestrator;
    private WasapiDeviceEnumerator? _enumerator; // outlives the captures it opened; disposed at teardown

    public TrayContext()
    {
        _startItem = new ToolStripMenuItem("Start meeting", null, (_, _) => Start());
        // Fire-and-forget (not async void): a teardown fault can't escape onto the UI
        // thread and crash the tray. DisposeAsync is throw-free anyway.
        _stopItem = new ToolStripMenuItem("Stop meeting", null, (_, _) => _ = StopAsync()) { Enabled = false };
        var settingsItem = new ToolStripMenuItem("Settings…", null, (_, _) => OpenSettings());
        var quitItem = new ToolStripMenuItem("Quit", null, (_, _) => Quit());

        var menu = new ContextMenuStrip();
        menu.Items.Add(_startItem);
        menu.Items.Add(_stopItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(settingsItem);
        menu.Items.Add(quitItem);

        _icon = new NotifyIcon
        {
            Icon = SystemIcons.Application,
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
        SetTooltip("starting…");
        _ = StartAsync(settings, ui);
    }

    private async Task StartAsync(BridgeSettings settings, SynchronizationContext ui)
    {
        WasapiDeviceEnumerator? enumerator = null;
        try
        {
            // Mint a detached session so this meeting is isolated from anything else the
            // Recorder is doing; every device's tap lands in it via ?session=<id>.
            string sessionId;
            using (var control = new ControlClient(settings.Host, settings.Port, settings.Tls, settings.Token))
                sessionId = await control.CreateDetachedSessionAsync().ConfigureAwait(false);

            // One device = one speaker. The mic streams under the operator's identity
            // (defaults to the OS username); the loopback streams under "system".
            TapConnectionOptions baseOptions = settings.ToConnectionOptions() with { Session = sessionId };
            TapConnectionOptions micOptions = baseOptions with
            {
                Name = string.IsNullOrEmpty(baseOptions.Name) ? baseOptions.Identity : baseOptions.Name,
            };
            TapConnectionOptions systemOptions = baseOptions with { Identity = "system", Name = "System Audio" };

            enumerator = new WasapiDeviceEnumerator();
            IReadOnlyList<CaptureDevice> devices = enumerator.List();
            var specs = new List<PipelineSpec>();
            TryAddSpec(specs, enumerator, PickDefault(devices, DeviceFlow.Capture), micOptions, "microphone", ui);
            TryAddSpec(specs, enumerator, PickDefault(devices, DeviceFlow.Render), systemOptions, "system audio", ui);
            if (specs.Count == 0)
                // Nothing opened — every candidate device failed (or there were none).
                throw new InvalidOperationException("No active microphone or system-audio device could be opened.");

            CaptureOrchestrator orchestrator = CaptureOrchestrator.StartAll(
                specs,
                onConnected: id => ui.Post(_ => SetTooltip($"streaming: {id}"), null),
                onFailed: (id, ex) => ui.Post(_ => ShowBalloon($"{id} stopped", ex.Message), null));

            lock (_gate)
            {
                _orchestrator = orchestrator;
                _enumerator = enumerator;
            }
            enumerator = null; // ownership transferred; the catch below must not dispose it
            ui.Post(_ =>
            {
                _stopItem.Enabled = true;
                SetTooltip($"recording {orchestrator.PipelineCount} device(s)");
            }, null);
        }
        catch (Exception ex) when (
            ex is HttpRequestException
                or InvalidOperationException
                or COMException
                or NotSupportedException
                or ArgumentException)
        {
            // The Recorder was unreachable / refused the session, or no device could be
            // opened. Tear down anything half-built and return the menu to idle. The
            // exception filter keeps this off CodeQL's catch-of-all radar.
            enumerator?.Dispose();
            ui.Post(_ =>
            {
                ResetIdleUi();
                ShowBalloon("Could not start meeting", ex.Message);
            }, null);
        }
    }

    // Open one device behind the capture seam and add a pipeline for it. Best-effort:
    // a device that fails to OPEN is surfaced and skipped, so a dead loopback doesn't
    // stop the mic from recording. This is the runner's half of the open-failure story
    // by design — opening a device is Windows-side (the enumerator), so the
    // cross-platform CaptureOrchestrator can't own it; it owns the symmetric
    // START-failure half (capture.Start throwing inside TapSession.Begin). The #106
    // picker slice should fold both into one open-failure authority rather than adding
    // a third best-effort layer.
    private void TryAddSpec(List<PipelineSpec> into, WasapiDeviceEnumerator enumerator,
                            CaptureDevice? device, TapConnectionOptions options, string label,
                            SynchronizationContext ui)
    {
        if (device is null)
            return;
        try
        {
            into.Add(new PipelineSpec(enumerator.Open(device), options));
        }
        catch (Exception ex) when (
            ex is COMException or NotSupportedException or ArgumentException or InvalidOperationException)
        {
            ui.Post(_ => ShowBalloon($"Could not open {label}", ex.Message), null);
        }
    }

    private static CaptureDevice? PickDefault(IReadOnlyList<CaptureDevice> devices, DeviceFlow flow) =>
        devices.FirstOrDefault(d => d.Flow == flow && d.IsDefault)
            ?? devices.FirstOrDefault(d => d.Flow == flow);

    private async Task StopAsync()
    {
        (CaptureOrchestrator? orchestrator, WasapiDeviceEnumerator? enumerator) = TakeAndResetUi();
        if (orchestrator is not null)
            await orchestrator.DisposeAsync();
        enumerator?.Dispose();
    }

    private (CaptureOrchestrator?, WasapiDeviceEnumerator?) TakeAndResetUi()
    {
        lock (_gate)
        {
            CaptureOrchestrator? orchestrator = _orchestrator;
            WasapiDeviceEnumerator? enumerator = _enumerator;
            _orchestrator = null;
            _enumerator = null;
            ResetIdleUi();
            return (orchestrator, enumerator);
        }
    }

    private void ResetIdleUi()
    {
        _startItem.Enabled = true;
        _stopItem.Enabled = false;
        SetTooltip("idle");
    }

    private void Quit()
    {
        CaptureOrchestrator? orchestrator;
        WasapiDeviceEnumerator? enumerator;
        lock (_gate)
        {
            orchestrator = _orchestrator;
            enumerator = _enumerator;
            _orchestrator = null;
            _enumerator = null;
        }

        // Tear every pipeline down: the orchestrator drains + closes all of them
        // CONCURRENTLY, each bounded, and DisposeAsync is throw-free — so this blocking
        // wait stays ~one drain budget (not N×) and can't deadlock or surface an
        // AggregateException. The timeout is a backstop; a sub-second tail may drop on a
        // hard quit.
        orchestrator?.DisposeAsync().AsTask().Wait(TimeSpan.FromSeconds(5));
        enumerator?.Dispose();

        _icon.Visible = false;
        _icon.Dispose();
        ExitThread();
    }

    private void SetTooltip(string state)
    {
        // NotifyIcon.Text is capped at 63 chars.
        string text = $"TapScribe — {state}";
        _icon.Text = text.Length <= 63 ? text : text[..63];
    }

    private void ShowBalloon(string title, string message) =>
        _icon.ShowBalloonTip(4000, title, message, ToolTipIcon.Warning);

    private void OpenSettings()
    {
        // Editing while a meeting is live is allowed; changes apply on the next Start
        // (active pipelines captured their options at Begin). Persist on Save so the
        // settings survive restarts.
        using var form = new SettingsForm(_settings);
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
    }
}
