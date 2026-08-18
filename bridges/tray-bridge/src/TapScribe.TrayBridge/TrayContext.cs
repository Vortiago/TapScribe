using TapScribe.Bridge.Core;
using TapScribe.Bridge.Windows;

namespace TapScribe.TrayBridge;

/// <summary>
/// The tray shell: a NotifyIcon with a status header line, Start meeting / End meeting /
/// Past meetings / Settings / Quit. It owns the WinForms half of the bridge and nothing else.
/// Every decision about what a meeting DOES (resolve the device selection, mint the detached
/// session, run one capture pipeline per device, drain, trigger the end-of-meeting pipeline,
/// resume one across a restart) belongs to <see cref="BridgeRuntime"/>, which is written once
/// and tested without WinForms; this class is its <see cref="ITrayView"/> and its menu.
///
/// So the split is: menu construction, the Past-meetings submenu, the settings dialog and the
/// meeting window live here, because they are widgets. Status text, notices, which commands
/// are enabled and when a window opens arrive through <see cref="ITrayView"/>, already
/// marshalled onto this thread. The AppKit shell (ADR-0020) is the same class over an
/// NSStatusItem.
/// </summary>
internal sealed class TrayContext : ApplicationContext, ITrayView
{
    // Held so teardown can dispose it: NotifyIcon.Dispose does NOT dispose its ContextMenuStrip.
    private readonly ContextMenuStrip _menu;
    private readonly ITrayIndicator _indicator;
    private readonly ToolStripMenuItem _statusItem;
    private readonly ToolStripMenuItem _startItem;
    private readonly ToolStripMenuItem _endItem;
    private readonly ToolStripMenuItem _pastMeetingsItem;
    private readonly TrayDependencies _deps;
    // Held only until the message loop starts, which is when the runtime that owns them
    // thereafter is built. Read its Settings for the dialog rather than this.
    private readonly BridgeSettings _initialSettings;
    private BridgeRuntime? _runtime;
    private bool _uiReleased; // Shutdown disposes, and then so does whoever owns the context

    public TrayContext()
        : this(TrayStores.Settings.Load(), TrayDependencies.Production)
    {
    }

    /// <summary>
    /// Build the shell over an explicit outside world (<see cref="TrayDependencies"/>) and
    /// an explicit starting <paramref name="settings"/>: the seam the tray tests construct
    /// through, so they drive the real menu without a WASAPI endpoint, a Recorder, or the
    /// operator's %APPDATA%. The parameterless constructor is what the app runs; nothing else
    /// about the shell differs between the two.
    /// </summary>
    internal TrayContext(BridgeSettings settings, TrayDependencies dependencies)
    {
        ArgumentNullException.ThrowIfNull(settings);
        ArgumentNullException.ThrowIfNull(dependencies);
        _initialSettings = settings;
        _deps = dependencies;

        // The idle header from the same StatusView every later change comes through, rather
        // than a hand-copy of its output: nothing renders a status until the operator acts,
        // so this string IS the menu header on a normal launch.
        _statusItem = new ToolStripMenuItem(StatusView.For(new TrayStatus.Idle()).Header) { Enabled = false };
        _startItem = new ToolStripMenuItem("Start meeting", null, (_, _) => OnRuntime(r => r.Start()));
        _endItem = new ToolStripMenuItem("End meeting", null, (_, _) => OnRuntime(r => r.End())) { Enabled = false };
        // Past meetings (#168): rebuilt from the persisted history each time it opens, so it
        // reflects meetings ended since it was last shown. Each item opens that meeting's
        // own window; the submenu never touches the live status line or Start/End controls.
        _pastMeetingsItem = new ToolStripMenuItem("Past meetings");
        _pastMeetingsItem.DropDownOpening += (_, _) => RebuildPastMeetingsMenu();
        var settingsItem = new ToolStripMenuItem("Settings…", null, (_, _) => OpenSettings());
        var quitItem = new ToolStripMenuItem("Quit", null, (_, _) => _ = QuitAsync());

        _menu = new ContextMenuStrip();
        _menu.Items.Add(_statusItem);
        _menu.Items.Add(new ToolStripSeparator());
        _menu.Items.Add(_startItem);
        _menu.Items.Add(_endItem);
        _menu.Items.Add(_pastMeetingsItem);
        _menu.Items.Add(new ToolStripSeparator());
        _menu.Items.Add(settingsItem);
        _menu.Items.Add(quitItem);

        _indicator = _deps.CreateIndicator();
        _indicator.Attach(_menu);

        _deps.ScheduleOnLoopStart(Startup);
    }

    /// <summary>
    /// Build the runtime and hand it the running loop. This is the ONLY place the shell reads
    /// <see cref="SynchronizationContext.Current"/>: it is the WinForms context from the message
    /// loop's first turn (Application.Run installs it, so a constructor is too early), and the
    /// one <see cref="SynchronizationContextDispatcher"/> built from it is what every marshalled
    /// callback goes through afterwards. The four hand-rolled "Current, or throw" captures this
    /// replaced were the same read repeated per entry point, and on macOS, where Current is null
    /// everywhere, none of them would have been true.
    /// </summary>
    private void Startup()
    {
        SynchronizationContext ui = SynchronizationContext.Current
            ?? throw new InvalidOperationException("The tray must start on the WinForms UI thread.");
        var runtime = new BridgeRuntime(
            this, new SynchronizationContextDispatcher(ui), _deps.Bridge, _initialSettings);
        _runtime = runtime;
        runtime.Startup(); // resume a pipeline a previous tray session left running
    }

    /// <summary>
    /// Run a command against the runtime, or do nothing if it does not exist yet.
    ///
    /// That window is real, not theoretical: the icon goes visible in the indicator's
    /// constructor and the menu is built before <c>Application.Run</c>, while the runtime is
    /// built from a 200 ms one-shot timer (the earliest point
    /// <c>SynchronizationContext.Current</c> is the WinForms one). So the tray is on screen and
    /// clickable for about a fifth of a second with no runtime behind it. Throwing there would
    /// surface an unhandled-exception dialog out of a click handler; doing nothing matches what
    /// the operator already believes, which is that they clicked a tray that had not finished
    /// starting.
    /// </summary>
    private void OnRuntime(Action<BridgeRuntime> command)
    {
        if (_runtime is { } runtime)
            command(runtime);
    }

    // ---- Test-visible state (read-only) -------------------------------------------------
    // The tray's observable surface, so TapScribe.TrayBridge.Tests can assert on what the
    // operator would see without a message loop or a visible window. Nothing here mutates.

    internal ContextMenuStrip Menu => _menu;
    internal ToolStripMenuItem StartItem => _startItem;
    internal ToolStripMenuItem EndItem => _endItem;
    internal ToolStripMenuItem PastMeetingsItem => _pastMeetingsItem;
    internal string StatusHeader => _statusItem.Text ?? "";

    // ---- ITrayView ----------------------------------------------------------------------
    // Everything the runtime shows the operator, already on this thread. No decisions here:
    // the runtime suppresses repeats, names the notices and decides which commands are live.

    public void ShowStatus(StatusView status)
    {
        ArgumentNullException.ThrowIfNull(status);
        _statusItem.Text = status.Header;
        _indicator.Show(status);
    }

    public void ShowNotice(string title, string message, NoticeKind kind)
    {
        if (kind == NoticeKind.Information)
            _indicator.Inform(title, message);
        else
            _indicator.Warn(title, message);
    }

    /// <summary>Nothing to do here, and that is the whole implementation: a notice on Windows
    /// is a balloon, which the shell already takes down on its own timer, and there is no API
    /// to retract one early. The seam declares this for the shells whose notice is shown in
    /// place - the macOS menu's status line - where it stays until something removes it.
    /// </summary>
    public void ClearNotice()
    {
    }

    public void SetMenuState(bool canStart, bool canEnd)
    {
        _startItem.Enabled = canStart;
        _endItem.Enabled = canEnd;
    }

    /// <summary>A new window per call, shown immediately in its Loading state so an empty frame
    /// is never on screen, and released by its own FormClosed handler. The runtime renders into
    /// what it gets back.</summary>
    public IMeetingWindow OpenMeetingWindow()
    {
        var form = new MeetingForm();
        form.Show();
        return form;
    }

    /// <summary>Teardown has finished: release the UI and stop the message loop. Nothing is
    /// streaming and no callback is in flight by the time the runtime calls this.</summary>
    public void Shutdown()
    {
        Dispose();
        ExitThread();
    }

    // ---- Commands ------------------------------------------------------------------------

    /// <summary>Quit, awaitable so a test can wait for the teardown the operator's click
    /// starts. Ends in <see cref="Shutdown"/>, which the runtime marshals back here.</summary>
    internal Task QuitAsync()
    {
        BridgeRuntime? runtime = _runtime;
        if (runtime is null)
        {
            // Quit before the message loop's first turn. Nothing was ever started, so there is
            // no meeting to tear down and no runtime to ask: release the UI directly.
            Shutdown();
            return Task.CompletedTask;
        }
        return runtime.QuitAsync();
    }

    // Rebuild the Past-meetings submenu from the persisted history each time it opens (#168):
    // newest-first, one item per meeting. An empty (or unreadable, which the store degrades to
    // empty) history shows a single disabled placeholder rather than a bare submenu.
    internal void RebuildPastMeetingsMenu()
    {
        // Dispose the previous items before rebuilding: DropDownItems.Clear() detaches them but
        // does NOT dispose, so without this each submenu open leaks the prior menu items (the
        // tray lives for days). Snapshot first: Dispose() detaches from the collection, which
        // would mutate it mid-iteration.
        ToolStripItem[] previous = [.. _pastMeetingsItem.DropDownItems.Cast<ToolStripItem>()];
        _pastMeetingsItem.DropDownItems.Clear();
        foreach (ToolStripItem item in previous)
            item.Dispose();

        IReadOnlyList<MeetingRecord> meetings = _runtime?.PastMeetings() ?? [];
        if (meetings.Count == 0)
        {
            _pastMeetingsItem.DropDownItems.Add(new ToolStripMenuItem("(No past meetings)") { Enabled = false });
            return;
        }
        foreach (MeetingRecord record in meetings)
            _pastMeetingsItem.DropDownItems.Add(
                new ToolStripMenuItem(record.MenuLabel(), null, (_, _) => OnRuntime(r => r.OpenPastMeeting(record))));
    }

    private void OpenSettings()
    {
        // Editing while a meeting is live is allowed, and what that means for the running
        // pipelines is the runtime's business (connection and device changes bind at the next
        // Start; the per-device level-gate knobs re-tune in place). This owns the dialog only.
        //
        // The dialog's live level meters (#152) open a second, display-only shared-mode capture
        // per device; this enumerator opens them and outlives those captures (the form disposes
        // them on close). Declared before the form so it disposes AFTER it, captures first.
        // Before the loop's first turn there is no runtime and so no settings to edit; the
        // dialog would seed itself from nothing.
        if (_runtime is not { } runtime)
            return;
        using IAudioDeviceEnumerator meterEnumerator = _deps.Bridge.OpenEnumerator();
        using var form = new SettingsForm(runtime.Settings, ListDevices, meterEnumerator.Open);
        if (form.ShowDialog() != DialogResult.OK)
            return;
        OnRuntime(r => r.ApplySettings(form.Result));
    }

    private IReadOnlyList<CaptureDevice> ListDevices()
    {
        using IAudioDeviceEnumerator enumerator = _deps.Bridge.OpenEnumerator();
        return enumerator.List();
    }

    /// <summary>
    /// Release the tray's own UI: the menu (including the Past-meetings drop-down) and the
    /// notification-area indicator. <see cref="Shutdown"/> is the operator's route here; the
    /// override exists so the shell honours the IDisposable it inherits: an ApplicationContext
    /// that leaks its entire menu when disposed is a real bug, and leaving the release only in
    /// the quit path would leave the same hole one level up.
    /// </summary>
    protected override void Dispose(bool disposing)
    {
        if (disposing && !_uiReleased)
        {
            _uiReleased = true;
            // NotifyIcon.Dispose does NOT dispose the ContextMenuStrip it renders, so the
            // whole menu outlived the tray. A ToolStrip disposes the items it owns, so take
            // the Past-meetings drop-down (itself a ToolStrip) explicitly first: its LAST
            // rebuilt set is the one RebuildPastMeetingsMenu never gets to dispose, since it
            // only ever disposes the PREVIOUS set on the next open, and then the strip.
            _pastMeetingsItem.DropDown.Dispose();
            _menu.Dispose();
            _indicator.Dispose();
        }
        base.Dispose(disposing);
    }
}
