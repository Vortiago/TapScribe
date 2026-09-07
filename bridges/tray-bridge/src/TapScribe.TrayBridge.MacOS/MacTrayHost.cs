using System.Net.Http;
using AppKit;
using Foundation;
using TapScribe.Bridge.Core;
using TapScribe.Bundle.Core;
using TapScribe.Bundle.MacOS;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// The tray's Recorder section on macOS — the HOST ROLE (ADR-0022), present exactly when a
/// host payload sits inside the <c>.app</c>. The Mac sibling of
/// <c>TapScribe.TrayBridge.Windows.TrayHost</c>, and deliberately its shape: same rules, same
/// order, same seams, with only the widgets and the two platform calls different.
///
/// Everything that is a DECISION lives in <see cref="HostController"/>, <see cref="LoginLink"/>,
/// <see cref="RuntimeCopy"/> and <see cref="BundleLayout"/> — all in <c>Bundle.Core</c>, all
/// tested on the Linux leg. What is left here is genuinely the shell's: an NSMenu, the
/// pasteboard, NSWorkspace, and reading a file that is on THIS machine.
/// </summary>
internal sealed class MacTrayHost : IHostView, IDisposable
{
    private readonly BundleLayout _layout;
    private readonly RotatingLogWriter _log;
    private readonly IProcessReaper? _reaper;
    private readonly HostController _controller;
    // ONE client for the object's lifetime, the way the Windows half holds one: a fresh
    // HttpClient per call leaks its handler's socket for the pool's idle timeout, and
    // "Open dashboard" is a thing an operator clicks all day.
    private readonly HttpClient _http = new();
    private readonly Action<string, string, NoticeKind> _notify;
    /// <summary>The shell's marshaller. Held because the host role does work OFF the main
    /// thread (the login-link mint, the runtime copy) and every menu and notice touch has to
    /// come back to it.</summary>
    private readonly Action<Action> _post;

    private readonly NSMenuItem _separator = NSMenuItem.SeparatorItem;
    private readonly NSMenuItem _statusItem;
    private readonly NSMenuItem _startItem;
    private readonly NSMenuItem _stopItem;
    private readonly NSMenuItem _dashboardItem;
    private readonly NSMenuItem _passwordItem;
    private readonly NSMenuItem _revealItem;
    private readonly NSMenuItem _logItem;

    /// <summary>
    /// Build the host role if this install carries one, or answer null — which is what a
    /// bridge-only tray gets, and why its menu is exactly what it was before the role
    /// existed. The probe is the payload inside the bundle, never a flag or a build variant.
    /// </summary>
    internal static MacTrayHost? TryAttach(Action<Action> post, Action<string, string, NoticeKind> notify)
    {
        ArgumentNullException.ThrowIfNull(post);
        ArgumentNullException.ThrowIfNull(notify);

        try
        {
            string payload = BundleLayout.MacOSPayload(AppContext.BaseDirectory);
            if (!BundleLayout.HostPayloadPresent(payload))
                return null;

            BundleLayout layout = BundleLayout.ForMacOS(
                payload,
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                BundleVersion.Current());
            return new MacTrayHost(layout, post, notify);
        }
        catch (Exception error) when (error is ArgumentException or IOException)
        {
            // A home directory that could not be read, or a path that will not resolve. This
            // runs from DidFinishLaunching, so an escaping throw takes the whole tray down —
            // the BRIDGE role with it — for a fault in the half that only supervises a
            // Recorder. Degrade to a bridge-only tray and say so where a menu-bar app can:
            // the notice line, via the caller's own notifier.
            notify("TapScribe", $"Could not work out where TapScribe is installed: {error.Message}", NoticeKind.Warning);
            return null;
        }
    }

    private MacTrayHost(BundleLayout layout, Action<Action> post, Action<string, string, NoticeKind> notify)
    {
        _layout = layout;
        _notify = notify;
        _post = post;
        _log = new RotatingLogWriter(layout);

        // Created BEFORE anything is spawned: a child inherits its parent's process group, so
        // leading the group once, here, is what makes the WhisperLiveKit GRANDCHILD reapable
        // without ever holding its handle (ProcessGroupReaper).
        _reaper = ProcessGroupReaper.TryCreate(Environment.ProcessPath, _log.Write);

        _statusItem = new NSMenuItem("Starting…") { Enabled = false };
        // Both DISABLED until the first ShowHost, like the Bridge half's End/Disconnect. The
        // menu is built and the status item shown before Startup runs, and an operator who
        // clicked Start Recorder in that window would boot a second preflight and a second
        // Recorder on top of the one Startup is about to boot.
        _startItem = new NSMenuItem("Start Recorder", (_, _) => Guarded(() => _controller!.StartRecorder()))
        { Enabled = false };
        // Separate from Quit, deliberately: stopping the server is not quitting the tray, and
        // an operator who wants the port free should not have to lose their bridge.
        _stopItem = new NSMenuItem("Stop Recorder", (_, _) => Guarded(() => _controller!.StopRecorder()))
        { Enabled = false };
        _dashboardItem = new NSMenuItem("Open dashboard", (_, _) => Guarded(OpenDashboard));
        _passwordItem = new NSMenuItem("Copy password", (_, _) => Guarded(CopyPassword));
        // macOS only, and not a nicety: the data root is under ~/Library/Application Support,
        // which Finder HIDES (ADR-0024). Without this an operator has no way to reach their own
        // recordings that does not involve typing a path into Go → Go to Folder.
        _revealItem = new NSMenuItem("Reveal recordings in Finder", (_, _) => Guarded(RevealRecordings));
        _logItem = new NSMenuItem("Show log", (_, _) => Guarded(ShowLog));

        MenuItems =
            [_separator, _statusItem, _startItem, _stopItem, _dashboardItem, _passwordItem, _revealItem, _logItem];

        _log.Write("--- TapScribe host role starting ---");
        _controller = HostController.Attach(
            this, post, layout, _reaper, _log.Write, RecorderAnswers,
            notice: message => _notify("TapScribe", message, NoticeKind.Warning));
    }

    /// <summary>The items to splice into the tray menu, in order. The shell owns where they
    /// go; this owns what they are.</summary>
    internal IReadOnlyList<NSMenuItem> MenuItems { get; }

    /// <summary>Boot the Recorder, which on macOS begins by copying the runtime out of the
    /// <c>.app</c> (ADR-0024). Both are the SUPERVISOR's, on its own background thread: doing
    /// the copy here instead put a rule the Linux leg tests into a shell only a Mac can
    /// build, and left "Start Recorder" unable to retry a copy that had failed.</summary>
    internal void Startup() => _controller.Start();

    public void ShowHost(HostView? host)
    {
        bool present = host is not null;
        foreach (NSMenuItem item in MenuItems)
            item.Hidden = !present;
        if (host is null)
            return;

        _statusItem.Title = host.Header;
        _startItem.Enabled = host.CanStart;
        _stopItem.Enabled = host.CanStop;

        // The Recorder's state lives in the MENU, which an operator only sees when they open
        // it — and the menu-bar glyph stays the Bridge's tap state by design (ADR-0022). So a
        // crash or a failed boot has no ambient signal at all unless it says so out loud.
        // Whether to say it, and whether it has already been said, are HostController's.
        if (host.Alert)
            _notify("TapScribe", host.Header, NoticeKind.Warning);
    }

    /// <summary>
    /// Open the dashboard already signed in: mint a single-use login link (ADR-0023) and hand
    /// the browser that, so the Basic dialog never appears.
    ///
    /// Off the main thread, and that is not an optimisation: the mint is a loopback round-trip
    /// with a 5 s deadline, and the state an operator is most likely to click in — a Recorder
    /// still grinding through preflight's pip install, or one that accepted the socket and
    /// went quiet — is exactly the one that spends the whole budget. On AppKit's main thread
    /// that is a frozen menu bar.
    /// </summary>
    private void OpenDashboard() => _ = Task.Run(() =>
    {
        string url = BundleDefaults.DashboardUrl;
        try
        {
            url = LoginLink.DashboardUrlFor(_http, _layout, _log.Write);
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            // The link is the convenience; the signed-out dashboard is what the whole feature
            // degrades to, so open THAT rather than nothing. Logged here rather than notified
            // because a notice needs the main thread and this is not it.
            _log.Write($"open dashboard: {error}");
        }

        _post(() => Guarded(() => Open(NSUrl.FromString(url))));
    });

    private void CopyPassword()
    {
        PasswordLookup lookup = PasswordFile.Read(_layout.PasswordFile);
        if (!lookup.IsOk || lookup.Password is null)
        {
            // Only the status is logged, never file-derived text.
            _log.Write($"copy password: {lookup.Status}");
            _notify("TapScribe", lookup.Message, NoticeKind.Warning);
            return;
        }

        Pasteboard.Put(lookup.Password);
        _notify("TapScribe", $"{lookup.Message} Username: {BundleDefaults.DashboardUser}.", NoticeKind.Information);
    }

    /// <summary>
    /// Open the recordings folder in Finder.
    ///
    /// Its own menu item because the data root is under
    /// <c>~/Library/Application Support</c>, which Finder hides from the sidebar and from a
    /// plain Home listing (ADR-0024) — chosen over <c>~/Documents</c> precisely because those
    /// are TCC-protected, and this item is the cost of that choice.
    /// </summary>
    private void RevealRecordings()
    {
        string recordings = _layout.RecordingsDirectory;
        // The data dir rather than a folder that may not exist yet: the Recorder creates
        // recordings/ on its first session, and opening a missing path silently does nothing.
        string target = Directory.Exists(recordings) ? recordings : _layout.DataDirectory;
        if (!Directory.Exists(target))
        {
            _notify("TapScribe", $"There is nothing at {target} yet.", NoticeKind.Information);
            return;
        }

        Open(NSUrl.FromFilename(target));
    }

    private void ShowLog()
    {
        // NSWorkspace answers false for a path that does not exist, but silently: without this
        // the one case an operator most needs an answer in (the log directory is unwritable,
        // and RotatingLogWriter has been swallowing the IOException by design) is a click that
        // does nothing.
        if (!File.Exists(_log.Path))
        {
            _notify("TapScribe", $"No log yet at {_log.Path}.", NoticeKind.Information);
            return;
        }

        Open(NSUrl.FromFilename(_log.Path));
    }

    /// <summary>
    /// Hand a URL or a file to the operator's default handler.
    ///
    /// Simply NSWorkspace, with none of the Windows half's <c>explorer.exe</c> indirection:
    /// that exists because the tray self-enrols into a KILL_ON_JOB_CLOSE job and
    /// ShellExecuteEx would make the browser a member of it. macOS has no such thing — the
    /// process group this tray leads is inherited by CHILDREN, and NSWorkspace does not make
    /// one.
    /// </summary>
    private void Open(NSUrl? url)
    {
        if (url is null)
        {
            _log.Write("could not open: the target was not a usable URL.");
            return;
        }

        if (!NSWorkspace.SharedWorkspace.OpenUrl(url))
        {
            // No handler registered, or the workspace refused. Not fatal — the operator can
            // open it themselves. Never the whole URL: the one this class opens is a login
            // link whose `?k=` IS a live single-use dashboard credential (ADR-0023), and this
            // goes to a rotating log file the operator is invited to open and paste.
            string said = LoginLink.WithoutSecrets(url.AbsoluteString);
            _log.Write($"could not open {said}.");
            _notify("TapScribe", $"Could not open {said}.", NoticeKind.Warning);
        }
    }

    /// <summary>Whether SOMETHING is serving the Recorder's port. The rule — and the five
    /// ways of not being there — belong to <see cref="ConnectionTester"/>, which already owns
    /// what <c>GET /health</c> means; both shells had written it out identically.</summary>
    private bool RecorderAnswers() =>
        ConnectionTester.AnswersOnLoopback(BundleDefaults.RecorderPort, _http, TimeSpan.FromSeconds(2));

    /// <summary>The same boundary the Bridge half keeps around an operator action: a bug must
    /// not become a menu bar that vanishes.</summary>
    private void Guarded(Action action)
    {
        try
        {
            action();
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            _log.Write($"menu command failed: {error}");
            _notify("TapScribe", "Something went wrong. See the log.", NoticeKind.Warning);
        }
    }

    /// <summary>
    /// Order mirrors the Windows half's, and for the mirrored reason: the reaper goes LAST,
    /// because disposing it signals the process group this process is itself in.
    /// </summary>
    public void Dispose()
    {
        _controller.Dispose();
        foreach (NSMenuItem item in MenuItems)
            item.Dispose();
        _http.Dispose();
        _log.Write("--- TapScribe host role stopped ---");
        _log.Dispose();
        _reaper?.Dispose();
    }
}
