using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;
using TapScribe.Bridge.Windows;

namespace TapScribe.TrayBridge;

/// <summary>
/// The tray shell: a NotifyIcon with Start tap / Stop tap / Settings / Quit.
/// Connection settings are edited in a small dialog and persisted to %APPDATA%
/// (env vars only seed the first-run defaults). Start..Stop runs one capture
/// pipeline; the core's level gate opens/closes an Utterance per speech segment
/// within it, with reconnect + Drain. The richer tray UX (device picker,
/// loopback capture, end-meeting) lands in later PRD #99 slices (#105–#107); the
/// depth lives in the cross-platform core.
/// </summary>
internal sealed class TrayContext : ApplicationContext
{
    private readonly NotifyIcon _icon;
    private readonly ToolStripMenuItem _startItem;
    private readonly ToolStripMenuItem _stopItem;
    private readonly object _gate = new();
    private BridgeSettings _settings = BridgeSettingsStore.Load();
    private TapSession? _session;

    public TrayContext()
    {
        _startItem = new ToolStripMenuItem("Start tap", null, (_, _) => Start());
        // Fire-and-forget (not async void): a teardown fault can't escape onto
        // the UI thread and crash the tray. DisposeAsync is throw-free anyway.
        _stopItem = new ToolStripMenuItem("Stop tap", null, (_, _) => _ = StopAsync()) { Enabled = false };
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
        lock (_gate)
        {
            if (_session is not null)
                return;

            // Start runs as a menu-click handler — on the WinForms UI thread while
            // the message loop is pumping — so Current is the
            // WindowsFormsSynchronizationContext and Post() marshals callbacks
            // back to the UI thread. (Capturing it in the ctor would be too early:
            // Application.Run hasn't installed the context yet, and a NotifyIcon /
            // ToolStripMenuItem are Components, not Controls, so constructing them
            // doesn't install it either.)
            SynchronizationContext ui = SynchronizationContext.Current
                ?? throw new InvalidOperationException("Start must run on the WinForms UI thread.");

            TapConnectionOptions options = _settings.ToConnectionOptions();
            try
            {
                // Construct the WASAPI capture here so a device-open failure is
                // caught synchronously below; the session takes ownership of it.
                var capture = new WasapiAudioCapture();
                _session = TapSession.Begin(
                    capture,
                    options,
                    onConnected: () => ui.Post(_ => SetTooltip($"streaming as {options.Identity}"), null),
                    onFailed: ex => ui.Post(_ => OnSessionFailed(ex), null));
            }
            catch (Exception ex) when (
                ex is COMException
                    or InvalidOperationException
                    or NotSupportedException
                    or ArgumentException)
            {
                // The capture device couldn't be opened (no mic, unsupported mix
                // format, device busy). Surface it; the menu stays idle since
                // _session is null. The filter also keeps this off CodeQL's
                // catch-of-all radar.
                ShowBalloon("Could not start capture", ex.Message);
                return;
            }

            _startItem.Enabled = false;
            _stopItem.Enabled = true;
            SetTooltip("connecting…");
        }
    }

    private async Task StopAsync()
    {
        TapSession? session = TakeSessionAndResetUi();
        if (session is not null)
            await session.DisposeAsync();
    }

    private void OnSessionFailed(Exception ex)
    {
        TapSession? session = TakeSessionAndResetUi();
        ShowBalloon("Tap stopped", ex.Message);
        if (session is not null)
            _ = session.DisposeAsync(); // best-effort teardown of the failed session
    }

    private TapSession? TakeSessionAndResetUi()
    {
        lock (_gate)
        {
            TapSession? session = _session;
            _session = null;
            _startItem.Enabled = true;
            _stopItem.Enabled = false;
            SetTooltip("idle");
            return session;
        }
    }

    private void Quit()
    {
        TapSession? session;
        lock (_gate)
        {
            session = _session;
            _session = null;
        }

        // Tear the session down: DisposeAsync drains buffered frames and closes
        // the WS, both bounded, and is throw-free — so this blocking wait can't
        // deadlock or surface an AggregateException. The timeout is a backstop;
        // a sub-second tail may be dropped on a hard quit.
        session?.DisposeAsync().AsTask().Wait(TimeSpan.FromSeconds(5));

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
        // Editing while a tap is live is allowed; changes apply on the next Start
        // (the active session captured its options at Begin). Persist on Save so
        // the settings survive restarts.
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
            // Couldn't write %APPDATA% (permissions, full disk): keep the new
            // settings for this session and tell the user they won't persist.
            ShowBalloon("Settings not saved", ex.Message);
        }
    }
}
