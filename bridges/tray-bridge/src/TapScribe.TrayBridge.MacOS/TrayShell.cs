using AppKit;
using Foundation;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// The menu bar: an <see cref="NSStatusItem"/> with a status header line, Start meeting /
/// End meeting / Past meetings / Settings… / Quit. It owns the AppKit half of the Bridge and
/// nothing else. Every decision about what a meeting DOES belongs to
/// <see cref="BridgeRuntime"/>, which is written once and tested without AppKit; this class is
/// its <see cref="ITrayView"/> and its menu, the same split the WinForms <c>TrayContext</c>
/// makes (ADR-0020).
///
/// Nothing here can carry a unit test: constructing any NSObject-derived type under the test
/// host throws inside ObjCRuntime, because the bridge is never initialised. So the rule this
/// file lives by is that it must not be worth testing. Every branch that is a judgement has
/// been moved out already: the glyph per state is <see cref="StatusSymbols"/>, the notice line
/// is <see cref="MenuNotice"/>, the status text is Core's <see cref="StatusView"/>, and the
/// meeting lifecycle is the runtime's. What is left is widget construction and forwarding.
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
    private readonly NSMenuItem _pastMeetings;

    // Held so the managed wrappers outlive the native windows they drive: a window whose only
    // remaining reference is AppKit's own would have its Closed handler collected out from
    // under the runtime that is still polling into it.
    private readonly HashSet<MeetingWindow> _windows = [];

    private NSStatusItem? _statusItem;
    private BridgeRuntime? _runtime;
    private SettingsWindow? _settingsWindow;
    private bool _uiReleased;

    /// <summary>Build the menu bar over an explicit outside world.</summary>
    /// <param name="dependencies">What a meeting needs: the enumerator, the session mint and
    /// the three stores. Passed straight to <see cref="BridgeRuntime"/>; the shell neither
    /// reads nor writes any of it.</param>
    internal TrayShell(BridgeDependencies dependencies)
    {
        ArgumentNullException.ThrowIfNull(dependencies);
        _deps = dependencies;

        // Empty on purpose. DidFinishLaunching renders the idle status through ShowStatus
        // before the status item exists, so anything seeded here is overwritten before it can
        // be seen; the Windows sibling seeds it because its menu is built inside a live
        // message loop, which is the difference.
        _statusHeader = new NSMenuItem("") { Enabled = false };
        _notice = new NSMenuItem("") { Enabled = false, Hidden = true };
        _start = new NSMenuItem("Start meeting", (_, _) => OnRuntime(runtime => runtime.Start()));
        _end = new NSMenuItem("End meeting", (_, _) => OnRuntime(runtime => runtime.End())) { Enabled = false };
        _pastMeetings = new NSMenuItem("Past meetings") { Submenu = _pastMeetingsMenu };
        // Seeded rather than left empty until menuWillOpen: fills it. AppKit will not open a
        // submenu with no items, so an empty one is a Past-meetings entry that never fires the
        // delegate that would have populated it. The placeholder is also what an empty history
        // shows, so the seed is the same line RebuildPastMeetings writes.
        _pastMeetingsMenu.AddItem(NoPastMeetings());
        var settings = new NSMenuItem("Settings…", (_, _) => OpenSettings());
        var quit = new NSMenuItem("Quit", "q", (_, _) => _ = QuitAsync());

        // AppKit's automatic enablement asks each item's target whether it is live and
        // overrides Enabled every time the menu opens, which would undo every SetMenuState
        // the runtime makes. The runtime is the one deciding, so the menu stops guessing. Both
        // menus: the Past-meetings items carry a managed handler rather than a target/action
        // pair AppKit can interrogate, so auto-enablement would grey out every meeting in it.
        _menu.AutoEnablesItems = false;
        _pastMeetingsMenu.AutoEnablesItems = false;
        _menu.AddItem(_statusHeader);
        _menu.AddItem(_notice);
        _menu.AddItem(NSMenuItem.SeparatorItem);
        _menu.AddItem(_start);
        _menu.AddItem(_end);
        _menu.AddItem(_pastMeetings);
        _menu.AddItem(NSMenuItem.SeparatorItem);
        _menu.AddItem(settings);
        _menu.AddItem(quit);

        // Past meetings (#168) is rebuilt each time the submenu opens, so it reflects meetings
        // ended since it was last shown, including ones another process ended. On the
        // Past-meetings SUBMENU, not the whole menu: AppKit fires menuWillOpen: for whichever
        // menu carries the delegate, so hanging it on _menu would re-read and re-parse the
        // history file on every status-item click, whether or not the operator went near Past
        // meetings. The Windows sibling hooks the item's own DropDownOpening for the same
        // reason. WeakDelegate rather than Delegate: this is already an NSApplicationDelegate
        // and so cannot also derive from NSMenuDelegate, and the exported selector below is
        // what AppKit actually looks for.
        _pastMeetingsMenu.WeakDelegate = this;
    }

    /// <summary>AppKit is about to show the Past-meetings submenu: rebuild it from the
    /// persisted history, so it reflects meetings ended since it was last shown.</summary>
    /// <param name="menu">The menu being opened, which is the Past-meetings submenu.</param>
    [Export("menuWillOpen:")]
    public void MenuWillOpen(NSMenu menu) => RebuildPastMeetings();

    /// <summary>Start AppKit and hand it the menu bar. Returns when the app stops, which is to
    /// say never on a normal run: <see cref="Shutdown"/> terminates the process.</summary>
    internal static void RunMenuBar()
    {
        NSApplication.Init();
        NSApplication app = NSApplication.SharedApplication;
        // Accessory, not Regular: no Dock icon and no Cmd-Tab entry. The bundle's LSUIElement
        // already says so for a Finder launch, and this says it again for the binary run
        // straight out of bin/, which Launch Services never sees.
        app.ActivationPolicy = NSApplicationActivationPolicy.Accessory;
        app.Delegate = new TrayShell(TrayWiring.Production);
        app.Run();
    }

    /// <summary>Put the status item on screen and build the runtime behind it, in that order:
    /// <see cref="BridgeRuntime.Startup"/> renders through this view immediately, so the menu
    /// has to exist first. Nothing can be clicked in between, because a click is delivered by
    /// the run loop this method has not returned to yet.</summary>
    /// <param name="notification">AppKit's launch notification, unread.</param>
    public override void DidFinishLaunching(NSNotification notification)
    {
        _statusItem = NSStatusBar.SystemStatusBar.CreateStatusItem(NSStatusItemLength.Variable);
        _statusItem.Menu = _menu;
        ShowStatus(StatusView.For(new TrayStatus.Idle()));

        var runtime = new BridgeRuntime(this, _dispatcher, _deps, _deps.SettingsStore.Load());
        _runtime = runtime;
        runtime.Startup(); // resume a pipeline a previous session left running
    }

    // ---- ITrayView ----------------------------------------------------------------------
    // Everything the runtime shows the operator, already on the main thread. No decisions
    // here: the runtime suppresses repeats, names the notices and decides which commands are
    // live.

    /// <summary>Render the at-a-glance state onto the menu header, the menu-bar glyph and its
    /// tooltip.</summary>
    /// <param name="status">What the runtime wants shown.</param>
    public void ShowStatus(StatusView status)
    {
        ArgumentNullException.ThrowIfNull(status);
        _statusHeader.Title = status.Header;

        if (_statusItem?.Button is not { } button)
            return;

        StatusSymbol symbol = StatusSymbols.For(status.Icon);
        NSImage? image = NSImage.GetSystemSymbol(symbol.Name, status.Tooltip);
        if (image is not null)
        {
            // A template image is recoloured by the menu bar itself, so the glyph follows a
            // light or dark menu bar and a highlighted item without being redrawn.
            image.Template = true;
            button.Image = image;
            button.Title = "";
        }
        else
        {
            // The system has no such symbol. An empty menu-bar item is indistinguishable from
            // a Bridge that is not running, so fall back to the glyph's text stand-in.
            button.Image = null;
            button.Title = symbol.Fallback;
        }
        button.ToolTip = status.Tooltip;
    }

    /// <summary>Show a notice as the line under the status header. See
    /// <see cref="MenuNotice"/> for why that rather than a system notification.</summary>
    /// <param name="title">The notice's headline.</param>
    /// <param name="message">The detail behind it.</param>
    /// <param name="kind">Whether something went wrong or something went right.</param>
    public void ShowNotice(string title, string message, NoticeKind kind)
    {
        _notice.Title = MenuNotice.Line(title, message, kind);
        _notice.Hidden = false;
    }

    /// <summary>Take the notice line back out of the menu. The Mac shell needs this because
    /// its notice is a menu item rather than a balloon: shown once, it stays until something
    /// removes it. See <see cref="ITrayView.ClearNotice"/> for why the runtime removes it when
    /// a meeting starts and not when one ends.</summary>
    public void ClearNotice()
    {
        _notice.Title = "";
        _notice.Hidden = true;
    }

    /// <summary>Enable or disable the two meeting commands.</summary>
    /// <param name="canStart">Whether Start meeting is live.</param>
    /// <param name="canEnd">Whether End meeting is live.</param>
    public void SetMenuState(bool canStart, bool canEnd)
    {
        _start.Enabled = canStart;
        _end.Enabled = canEnd;
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
            // Posted rather than disposed inline: this runs from inside -[NSWindow close],
            // and releasing the window while AppKit is still unwinding that call is how a
            // close turns into a crash. The post lands after it returns.
            _dispatcher.Post(window.Dispose);
        };
        window.Show();
        return window;
    }

    /// <summary>Teardown has finished: release the UI and stop the app. Nothing is streaming
    /// and no callback is in flight by the time the runtime calls this.</summary>
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
        // Editing while a meeting is live is allowed, and what that means for the running
        // pipelines is the runtime's business (connection and device changes bind at the next
        // Start; the per-device level-gate knobs re-tune in place). This owns the window only.
        //
        // Before the app has finished launching there is no runtime and so no settings to
        // edit; the window would seed itself from nothing.
        if (_runtime is not { } runtime)
            return;

        if (_settingsWindow is { IsOpen: true } open)
        {
            open.Show(); // a second Settings… raises the one already open rather than stacking
            return;
        }

        // The previous window is closed but not freed (ReleaseWhenClosed is off, which is what
        // makes IsOpen answerable above), so dropping the reference alone would leave its whole
        // control graph behind once per Settings…, on a tray that runs for days.
        _settingsWindow?.Dispose();

        var window = new SettingsWindow(
            runtime.Settings, ListDevices, runtime.ApplySettings, _dispatcher);
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
    /// The window this covers is narrow rather than absent. The status item goes on screen a
    /// few statements before the runtime is built, both inside DidFinishLaunching, and a menu
    /// click cannot be delivered until that method returns to the run loop. So the guard is
    /// about the ORDER inside that method rather than about a fifth of a second of clickable
    /// tray, which is the shape the WinForms sibling has. Doing nothing still beats throwing:
    /// this runs from a menu action, where an exception reaches AppKit and takes the process.
    /// </summary>
    private void OnRuntime(Action<BridgeRuntime> command)
    {
        if (_runtime is { } runtime)
            command(runtime);
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
    }
}
