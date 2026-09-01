using AppKit;
using Foundation;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// The menu bar: an <see cref="NSStatusItem"/> with a status header line, Start meeting / End
/// meeting / Connect to live / Disconnect / Past meetings / Settings… / Quit. It owns the AppKit half of the Bridge and nothing
/// else. Every decision about what a meeting DOES belongs to <see cref="BridgeRuntime"/>, which is
/// written once and tested without AppKit; this class is its <see cref="ITrayView"/> and its menu,
/// the same split the WinForms <c>TrayContext</c> makes (ADR-0020).
///
/// Nothing here can carry a unit test: constructing any NSObject-derived type under the test host
/// throws inside ObjCRuntime. So the rule this file lives by is that it must not be worth testing.
/// Every judgement has moved out already: the glyph per state to <see cref="StatusSymbols"/>, the
/// notice line to <see cref="MenuNotice"/>, the status text to Core's <see cref="StatusView"/>,
/// the meeting lifecycle to the runtime. What is left is widget construction and forwarding.
/// </summary>
internal sealed class TrayShell : NSApplicationDelegate, ITrayView, INSMenuDelegate
{
    private readonly BridgeDependencies _deps;
    private readonly MainQueueDispatcher _dispatcher = new();
    private readonly NSMenu _menu = new();
    private readonly NSMenu _pastMeetingsMenu = new();
    private readonly NSMenuItem _statusHeader;
    private readonly NSMenuItem _notice;
    private readonly NSMenuItem _start;
    private readonly NSMenuItem _end;
    private readonly NSMenuItem _connect;
    private readonly NSMenuItem _disconnect;
    private readonly NSMenuItem _pastMeetings;

    // Held so the managed wrappers outlive the native windows they drive: a window whose only
    // remaining reference is AppKit's own would have its Closed handler collected out from
    // under the runtime that is still polling into it.
    private readonly HashSet<MeetingWindow> _windows = [];

    private NSStatusItem? _statusItem;
    private BridgeRuntime? _runtime;
    /// <summary>The Recorder section, or null on a bridge-only install — in which case the
    /// menu is exactly what it was before the host role existed (ADR-0022).</summary>
    private MacTrayHost? _host;
    private SettingsWindow? _settingsWindow;
    private bool _uiReleased;

    /// <summary>Build the menu bar over an explicit outside world.</summary>
    /// <param name="dependencies">What a meeting needs: the enumerator, the session mint and the
    /// three stores. Passed straight to <see cref="BridgeRuntime"/>.</param>
    internal TrayShell(BridgeDependencies dependencies)
    {
        ArgumentNullException.ThrowIfNull(dependencies);
        _deps = dependencies;

        // Empty on purpose. DidFinishLaunching renders the idle status through ShowStatus before
        // the status item exists, so anything seeded here is overwritten before it can be seen.
        _statusHeader = new NSMenuItem("") { Enabled = false };
        _notice = new NSMenuItem("") { Enabled = false, Hidden = true };
        _start = new NSMenuItem("Start meeting", (_, _) => OnRuntime(runtime => runtime.Start()));
        _end = new NSMenuItem("End meeting", (_, _) => OnRuntime(runtime => runtime.End())) { Enabled = false };
        // Connect to live (ADR-0025): stream into whatever session the Recorder has open,
        // rather than minting one. Beside Start/End rather than under a submenu — it is the
        // other way to do the one thing this tray is for.
        _connect = new NSMenuItem("Connect to live", (_, _) => OnRuntime(runtime => runtime.Connect()));
        _disconnect = new NSMenuItem("Disconnect", (_, _) => OnRuntime(runtime => runtime.Disconnect())) { Enabled = false };
        _pastMeetings = new NSMenuItem("Past meetings") { Submenu = _pastMeetingsMenu };
        // Seeded rather than left empty until menuWillOpen: fills it. AppKit will not open a
        // submenu with no items, so an empty one never fires the delegate that would populate it.
        // The placeholder is also what an empty history shows.
        _pastMeetingsMenu.AddItem(NoPastMeetings());
        // Guarded, not OnRuntime: both are live before the runtime exists. Quit must still shut
        // the app down then, and OpenSettings decides for itself that there is nothing to edit.
        var settings = new NSMenuItem("Settings…", (_, _) => Guarded(OpenSettings));
        var quit = new NSMenuItem("Quit", "q", (_, _) => Guarded(() => _ = QuitAsync()));

        // AppKit's automatic enablement asks each item's target whether it is live and overrides
        // Enabled every time the menu opens, which would undo every SetCommands the runtime makes.
        // Both menus: the Past-meetings items carry a managed handler rather than a target/action
        // pair, so auto-enablement would grey out every meeting in it.
        _menu.AutoEnablesItems = false;
        _pastMeetingsMenu.AutoEnablesItems = false;
        _menu.AddItem(_statusHeader);
        _menu.AddItem(_notice);
        _menu.AddItem(NSMenuItem.SeparatorItem);
        _menu.AddItem(_start);
        _menu.AddItem(_end);
        _menu.AddItem(_connect);
        _menu.AddItem(_disconnect);
        _menu.AddItem(_pastMeetings);

        // The host role's own section, when this install carries a payload (ADR-0022). Between
        // the tap commands and Settings/Quit: it is a different lifecycle, and the separator it
        // brings with it says so. TryAttach answers null for a bridge-only tray, and then not one
        // item of this is added — which is what makes that menu byte-identical to the one before
        // the role existed.
        _host = MacTrayHost.TryAttach(_dispatcher.Post, ShowNotice);
        if (_host is not null)
        {
            foreach (NSMenuItem item in _host.MenuItems)
                _menu.AddItem(item);
        }

        _menu.AddItem(NSMenuItem.SeparatorItem);
        _menu.AddItem(settings);
        _menu.AddItem(quit);

        // Past meetings (#168) is rebuilt each time the submenu opens, so it reflects meetings
        // ended since it was last shown, including ones another process ended. On the SUBMENU, not
        // the whole menu: AppKit fires menuWillOpen: for whichever menu carries the delegate, so
        // hanging it on _menu would re-parse the history file on every status-item click.
        // WeakDelegate rather than Delegate: this is already an NSApplicationDelegate and cannot
        // also derive from NSMenuDelegate, and the exported selector is what AppKit looks for.
        _pastMeetingsMenu.WeakDelegate = this;
    }

    /// <summary>AppKit is about to show the Past-meetings submenu: rebuild it from the persisted
    /// history, so it reflects meetings ended since it was last shown.</summary>
    [Export("menuWillOpen:")]
    public void MenuWillOpen(NSMenu menu) => RebuildPastMeetings();

    /// <summary>Start AppKit and hand it the menu bar. Returns when the app stops, which is to
    /// say never on a normal run: <see cref="Shutdown"/> terminates the process.</summary>
    internal static void RunMenuBar()
    {
        NSApplication.Init();
        NSApplication app = NSApplication.SharedApplication;
        // Accessory, not Regular: no Dock icon and no Cmd-Tab entry. The bundle's LSUIElement says
        // so for a Finder launch; this says it again for the binary run straight out of bin/.
        app.ActivationPolicy = NSApplicationActivationPolicy.Accessory;
        app.Delegate = new TrayShell(TrayWiring.Production);
        app.Run();
    }

    /// <summary>Put the status item on screen and build the runtime behind it, in that order:
    /// <see cref="BridgeRuntime.Startup"/> renders through this view immediately. Nothing can be
    /// clicked in between, since a click is delivered by the run loop this has not returned to.
    /// </summary>
    public override void DidFinishLaunching(NSNotification notification)
    {
        _statusItem = NSStatusBar.SystemStatusBar.CreateStatusItem(NSStatusItemLength.Variable);
        _statusItem.Menu = _menu;
        ShowStatus(StatusView.For(new TrayStatus.Idle()));

        var runtime = new BridgeRuntime(this, _dispatcher, _deps, _deps.SettingsStore.Load());
        _runtime = runtime;
        runtime.Startup(); // resume a pipeline a previous session left running

        // After the Bridge half, deliberately: the tap state is what the operator watches, and
        // the host role's first act may be a 300 MB runtime copy (ADR-0024). Its own Startup
        // does that off the main thread, so this returns to the run loop either way.
        _host?.Startup();
    }

    // ---- ITrayView ----------------------------------------------------------------------
    // Everything the runtime shows the operator, already on the main thread. No decisions
    // here: the runtime suppresses repeats, names the notices and decides which commands are
    // live.

    /// <summary>Render the at-a-glance state onto the menu header, the menu-bar glyph and its
    /// tooltip.</summary>
    public void ShowStatus(StatusView status)
    {
        ArgumentNullException.ThrowIfNull(status);
        _statusHeader.Title = status.Header;

        if (_statusItem?.Button is not { } button)
            return;

        StatusSymbol symbol = StatusSymbols.For(status.Icon);
        NSImage? image = NSImage.GetSystemSymbol(symbol.Name, status.Tooltip);
        if (image is not null)
            // A template image is recoloured by the menu bar itself, so the glyph follows a light
            // or dark menu bar and a highlighted item without being redrawn.
            image.Template = true;
        button.Image = image;

        // The badge rides BOTH paths. On a system with no such symbol the text stand-in is all
        // there is (an empty menu-bar item is indistinguishable from a Bridge that is not
        // running), and dropping the badge there would lose the count on exactly the Macs with
        // the least to go on.
        //
        // Said rather than inherited: the badge is only BESIDE the glyph if the button draws both,
        // and a default that put the image over the title would leave the count invisible.
        button.ImagePosition = NSCellImagePosition.ImageLeading;
        button.Title = image is null ? symbol.Fallback + status.Badge : status.Badge;
        button.ToolTip = status.Tooltip;
    }

    /// <summary>Show a notice as the line under the status header. See <see cref="MenuNotice"/> for
    /// why that rather than a system notification.</summary>
    public void ShowNotice(string title, string message, NoticeKind kind)
    {
        _notice.Title = MenuNotice.Line(title, message, kind);
        _notice.Hidden = false;
    }

    /// <summary>Take the notice line back out of the menu. Needed here because the notice is a menu
    /// item rather than a balloon: shown once, it stays until something removes it. See
    /// <see cref="ITrayView.ClearNotice"/> for when the runtime removes it.</summary>
    public void ClearNotice()
    {
        _notice.Title = "";
        _notice.Hidden = true;
    }

    /// <summary>Enable or disable the tap commands.</summary>
    public void SetCommands(TrayCommands commands)
    {
        ArgumentNullException.ThrowIfNull(commands);
        _start.Enabled = commands.CanStart;
        _end.Enabled = commands.CanEnd;
        _connect.Enabled = commands.CanConnect;
        _disconnect.Enabled = commands.CanDisconnect;
    }

    /// <summary>A new window per call, on screen immediately in its Loading state so an empty
    /// frame is never shown. The runtime renders into what it gets back.</summary>
    public IMeetingWindow OpenMeetingWindow()
    {
        var window = new MeetingWindow();
        _windows.Add(window);
        window.Closed += () =>
        {
            _windows.Remove(window);
            // Posted rather than disposed inline: this runs from inside -[NSWindow close], and
            // releasing the window while AppKit is unwinding that call is how a close crashes.
            _dispatcher.Post(window.Dispose);
        };
        window.Show();
        return window;
    }

    /// <summary>Teardown has finished: release the UI and stop the app. Nothing is streaming and no
    /// callback is in flight by the time the runtime calls this.</summary>
    public void Shutdown()
    {
        ReleaseUi();
        NSApplication.SharedApplication.Terminate(this);
    }

    // ---- Commands ------------------------------------------------------------------------

    /// <summary>Quit, awaitable so the teardown the operator's click starts can be waited on.
    /// Ends in <see cref="Shutdown"/>, which the runtime marshals back here.</summary>
    private Task QuitAsync()
    {
        if (_runtime is not { } runtime)
        {
            // Quit before the app finished launching. Nothing was ever started, so there is no
            // meeting to tear down and no runtime to ask: release the UI directly.
            Shutdown();
            return Task.CompletedTask;
        }
        return runtime.QuitAsync();
    }

    private void OpenSettings()
    {
        // Editing while a meeting is live is allowed, and what that means for the running pipelines
        // is the runtime's business (connection and device changes bind at the next Start; the
        // per-device gate knobs re-tune in place). This owns the window only. Before the app has
        // finished launching there is no runtime, so the window would seed itself from nothing.
        if (_runtime is not { } runtime)
            return;

        if (_settingsWindow is { IsOpen: true } open)
        {
            open.Show(); // a second Settings… raises the one already open rather than stacking
            return;
        }

        // The previous window is closed but not freed (ReleaseWhenClosed is off, which is what
        // makes IsOpen answerable above), so dropping the reference alone leaves its control graph
        // behind once per Settings…, on a tray that runs for days.
        //
        // Forgotten BEFORE it is released: the build below can throw, and a field still naming a
        // disposed window would have the next Settings… ask a freed NSWindow whether it IsOpen.
        SettingsWindow? stale = _settingsWindow;
        _settingsWindow = null;
        stale?.Dispose();

        // Applied through the same boundary the menu items use. Save is an NSButton on a window
        // of its own, so nothing else stands between ApplySettings and AppKit, and Core narrowed
        // that method's catches to the three a disk can produce on the strength of a shell that
        // contains the rest.
        var window = new SettingsWindow(
            runtime.Settings,
            ListDevices,
            _deps.OpenEnumerator,
            edited => OnRuntime(r => r.ApplySettings(edited)),
            _dispatcher);
        _settingsWindow = window;
        window.Show();
    }

    private IReadOnlyList<CaptureDevice> ListDevices()
    {
        using IAudioDeviceEnumerator enumerator = _deps.OpenEnumerator();
        return enumerator.List();
    }

    private void RebuildPastMeetings()
    {
        _pastMeetingsMenu.RemoveAllItems();
        IReadOnlyList<MeetingRecord> meetings = _runtime?.PastMeetings() ?? [];
        if (meetings.Count == 0)
        {
            // An empty (or unreadable, which the store degrades to empty) history shows a
            // placeholder rather than a submenu that opens onto nothing.
            _pastMeetingsMenu.AddItem(NoPastMeetings());
        }
        else
        {
            foreach (MeetingRecord record in meetings)
                _pastMeetingsMenu.AddItem(
                    new NSMenuItem(record.MenuLabel(), (_, _) => OnRuntime(r => r.OpenPastMeeting(record))));
        }
    }

    private static NSMenuItem NoPastMeetings() => new("(No past meetings)") { Enabled = false };

    /// <summary>
    /// Run a command against the runtime, or do nothing if it does not exist yet.
    ///
    /// The window is narrow rather than absent: the status item goes on screen a few statements
    /// before the runtime is built, both inside DidFinishLaunching, and a menu click cannot be
    /// delivered until that method returns to the run loop. Doing nothing still beats throwing,
    /// since an exception from a menu action reaches AppKit and takes the process.
    /// </summary>
    private void OnRuntime(Action<BridgeRuntime> command)
    {
        if (_runtime is not { } runtime)
            return;

        Guarded(() => command(runtime));
    }

    /// <summary>
    /// The boundary with the toolkit, around one operator action.
    ///
    /// An exception from an AppKit handler unwinds into AppKit and ends the process, so nothing
    /// may escape here; Core narrowed its own catches on the strength of this
    /// (<see cref="BridgeRuntime.ApplySettings"/>), and a bug that used to arrive as a wrong
    /// notice must not become a tray that vanishes. Every path an operator can reach goes
    /// through it: the menu items, and the Settings window's Save, which calls the runtime from
    /// an NSButton of its own rather than from the menu. Reported rather than swallowed, so it
    /// is still visible as something other than a click that did nothing. CodeQL flags the
    /// width (cs/catch-of-all-exceptions): this is the one place that width is the point.
    /// </summary>
    private void Guarded(Action action)
    {
        try
        {
            action();
        }
        catch (Exception ex)
        {
            ShowNotice("Something went wrong", ex.Message, NoticeKind.Warning);
        }
    }

    private void ReleaseUi()
    {
        if (_uiReleased)
            return;
        _uiReleased = true;

        // Snapshot: closing a window raises Closed, which removes it from the set.
        foreach (MeetingWindow window in _windows.ToArray())
            window.Close();
        _windows.Clear();
        _settingsWindow?.Close();
        // Safe to release inline, unlike a meeting window's: this is the caller of Close rather
        // than a handler AppKit is still unwinding through.
        _settingsWindow?.Dispose();
        _settingsWindow = null;

        if (_statusItem is not null)
        {
            // The status bar holds the item, so dropping the reference alone would leave the
            // glyph in the menu bar until the process actually went away.
            NSStatusBar.SystemStatusBar.RemoveStatusItem(_statusItem);
            _statusItem = null;
        }

        // LAST, and not for tidiness: disposing the host role stops the Recorder it started
        // and then signals the process group this process is itself in (ProcessGroupReaper).
        // Anything sequenced after it may not run.
        _host?.Dispose();
        _host = null;
    }
}
