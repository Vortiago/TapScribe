using System.Diagnostics;
using System.Net.Http;
using TapScribe.Bridge.Core;
using TapScribe.Bundle.Core;
using TapScribe.Bundle.Windows;

namespace TapScribe.TrayBridge.Windows;

/// <summary>
/// The tray's Recorder section — the HOST ROLE (ADR-0022), present exactly when a host
/// payload sits beside the tray on disk. One tray per OS carries both roles; this is the
/// half that boots, supervises and reaps a co-located Recorder and is the way in to it.
///
/// A type of its own rather than more members on <c>TrayContext</c>, because the two roles
/// are genuinely separate lifecycles that happen to share a menu: everything here is null
/// and absent on a bridge-only install, and nothing here touches a tap.
///
/// It is the shell half of <see cref="HostController"/>, which owns the rules; this owns
/// the widgets.
/// </summary>
internal sealed class TrayHost : IHostView, IDisposable
{
    private readonly BundleLayout _layout;
    private readonly RotatingLogWriter _log;
    private readonly JobObject? _reaper;
    private readonly HostController _controller;
    // ONE client for the object's lifetime, the way ControlClient holds one: a fresh
    // HttpClient per call leaks its handler's socket for the pool's idle timeout, and
    // "Open dashboard" is a thing an operator clicks all day. Per-call timeouts come from
    // a CancellationTokenSource, not from HttpClient.Timeout, which is per-client.
    private readonly HttpClient _http = new();
    private readonly Action<string, string, NoticeKind> _notify;
    /// <summary>The shell's marshaller. Held because the host role does work OFF the UI
    /// thread (the login-link mint) and every menu and balloon touch has to come back.</summary>
    private readonly Action<Action> _post;
    private readonly ToolStripSeparator _separator = new();
    private readonly ToolStripMenuItem _statusItem;
    private readonly ToolStripMenuItem _startItem;
    private readonly ToolStripMenuItem _stopItem;
    private readonly ToolStripMenuItem _dashboardItem;
    private readonly ToolStripMenuItem _passwordItem;
    private readonly ToolStripMenuItem _logItem;

    /// <summary>
    /// Build the host role if this install carries one, or answer null — which is what a
    /// bridge-only tray gets, and why its menu is exactly what it was before the role
    /// existed. The probe is the payload on disk, never a flag or a build variant.
    /// </summary>
    /// <param name="post">The shell's marshaller: supervisor state arrives on a background
    /// thread and every menu touch below must reach the UI one.</param>
    /// <param name="notify">How the shell surfaces a balloon.</param>
    internal static TrayHost? TryAttach(Action<Action> post, Action<string, string, NoticeKind> notify)
    {
        string program = AppContext.BaseDirectory;
        try
        {
            if (!BundleLayout.HostPayloadPresent(program))
                return null;

            BundleLayout layout = BundleLayout.ForWindows(
                program, Environment.GetFolderPath(Environment.SpecialFolder.UserProfile));
            return new TrayHost(layout, post, notify);
        }
        catch (Exception error) when (error is ArgumentException or IOException)
        {
            // A profile-less session answers "" for UserProfile, and an invalid path throws
            // out of Path.GetFullPath. This runs from the TrayContext CONSTRUCTOR, before
            // Application.Run pumps anything, so an escaping throw takes the whole tray down
            // — the BRIDGE role with it — behind an unhandled-exception dialog. A dialog is
            // the only way to be heard here (no tray icon exists yet, so `notify` would NRE),
            // and degrading to a bridge-only tray keeps the half that needs no layout.
            MessageBox.Show(
                $"TapScribe could not work out where it is installed:\n\n{error.Message}",
                "TapScribe",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error);
            return null;
        }
    }

    private TrayHost(BundleLayout layout, Action<Action> post, Action<string, string, NoticeKind> notify)
    {
        _layout = layout;
        _notify = notify;
        _post = post;
        _log = new RotatingLogWriter(layout);

        // Created BEFORE anything is spawned: a process started by a process already in a
        // job lands in that job automatically, which is what makes the WhisperLiveKit
        // grandchild reapable without ever holding its handle (see JobObject).
        _reaper = JobObject.TryCreate(_log.Write);

        _statusItem = new ToolStripMenuItem("Starting…") { Enabled = false };
        // Both DISABLED until the first ShowHost, like the Bridge half's End/Disconnect. The
        // menu is spliced in and the icon made visible by the shell's constructor, while
        // Startup lands a message-loop tick later: an operator who clicked Start Recorder in
        // that window would boot a second preflight and a second Recorder on top of the one
        // Startup is about to boot, and only the last would be reachable by Stop or Quit.
        _startItem = new ToolStripMenuItem("Start Recorder", null, (_, _) => Guarded(() => _controller!.StartRecorder()))
        { Enabled = false };
        // Separate from Quit, deliberately: stopping the server is not quitting the tray,
        // and an operator who wants the port free should not have to lose their bridge.
        _stopItem = new ToolStripMenuItem("Stop Recorder", null, (_, _) => Guarded(() => _controller!.StopRecorder()))
        { Enabled = false };
        _dashboardItem = new ToolStripMenuItem("Open dashboard", null, (_, _) => Guarded(OpenDashboard));
        _passwordItem = new ToolStripMenuItem("Copy password", null, (_, _) => Guarded(CopyPassword));
        _logItem = new ToolStripMenuItem("Show log", null, (_, _) => Guarded(ShowLog));

        MenuItems =
            [_separator, _statusItem, _startItem, _stopItem, _dashboardItem, _passwordItem, _logItem];

        _log.Write("--- TapScribe host role starting ---");
        _controller = HostController.Attach(
            this, post, layout, _reaper, _log.Write, RecorderAnswers,
            notice: message => _notify("TapScribe", message, NoticeKind.Warning));
    }

    /// <summary>The items to splice into the tray menu, in order. The shell owns where
    /// they go; this owns what they are. Built once — the seven fields are readonly and the
    /// set is read on every ShowHost, i.e. on every Recorder state change.</summary>
    internal IReadOnlyList<ToolStripItem> MenuItems { get; }

    /// <summary>Boot the Recorder. Called once the message loop is pumping, like the
    /// Bridge runtime's own startup.</summary>
    internal void Startup() => _controller.Start();

    public void ShowHost(HostView? host)
    {
        bool present = host is not null;
        foreach (ToolStripItem item in MenuItems)
            item.Visible = present;
        if (host is null)
            return;

        _statusItem.Text = host.Header;
        _startItem.Enabled = host.CanStart;
        _stopItem.Enabled = host.CanStop;

        // The Recorder's state lives in the MENU, which an operator only sees when they open
        // it — and the tray ICON stays the Bridge's tap state by design (ADR-0022). So a
        // crash or a failed boot has no ambient signal at all unless it says so out loud.
        // WHETHER to say it, and whether it has already been said, are HostController's —
        // both trays had grown the same de-dup field, and a rule about what the operator
        // hears should not have one implementation per shell.
        if (host.Alert)
            _notify("TapScribe", host.Header, NoticeKind.Warning);
    }

    /// <summary>
    /// Open the dashboard already signed in: mint a single-use login link (ADR-0023) and
    /// hand the browser that, so the native Basic dialog never appears.
    ///
    /// Always the LOCAL Recorder, never whatever host the bridge settings point at: a tray
    /// may legitimately supervise one Recorder and tap into another, and a login link for
    /// the wrong one would be a password sent somewhere it does not belong.
    ///
    /// A failed mint falls back to the plain URL — the operator then meets the password
    /// prompt they met before this existed, with Copy password one item below.
    ///
    /// Off the UI thread, and that is not an optimisation: the mint is a loopback round-trip
    /// with a 5 s deadline, and the state an operator is most likely to click in — a Recorder
    /// still grinding through preflight's pip install, or one that accepted the socket and
    /// went quiet — is exactly the one that spends the whole budget. On the message loop that
    /// is a frozen menu, a "Not Responding" tray, and every marshalled Bridge status behind
    /// it for a meeting that may be streaming.
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
            // degrades to, so open THAT rather than nothing. Logged here rather than
            // balloon'd because the balloon needs the UI thread and this is not it.
            _log.Write($"open dashboard: {error}");
        }

        _post(() => Guarded(() => ShellOpen(url)));
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

        try
        {
            Clipboard.SetText(lookup.Password);
        }
        catch (System.Runtime.InteropServices.ExternalException error)
        {
            // Another app holds the clipboard (CLIPBRD_E_CANT_OPEN). Name the file so the
            // operator can read it themselves.
            _log.Write($"copy password: clipboard unavailable ({error.HResult:X})");
            _notify("TapScribe", $"Could not copy. The password is in {_layout.PasswordFile}.", NoticeKind.Warning);
            return;
        }

        _notify(
            "TapScribe",
            $"{lookup.Message} Username: {BundleDefaults.DashboardUser}.",
            NoticeKind.Information);
    }

    private void ShowLog()
    {
        // ShellOpen hands the path to explorer.exe, which returns 0 whether or not the target
        // exists — so without this the one case an operator most needs an answer in (the log
        // directory is unwritable, and RotatingLogWriter has been swallowing the IOException
        // by design) is a click that silently does nothing.
        if (!File.Exists(_log.Path))
        {
            _notify("TapScribe", $"No log yet at {_log.Path}.", NoticeKind.Information);
            return;
        }
        ShellOpen(_log.Path);
    }

    /// <summary>
    /// Hand a URL or a file to the shell's default handler, VIA EXPLORER.
    ///
    /// Not <c>UseShellExecute = true</c> directly: the tray self-enrols into a
    /// <see cref="JobObject"/> with KILL_ON_JOB_CLOSE, and for an ordinary
    /// <c>shell\open\command</c> verb ShellExecuteEx does its CreateProcess INSIDE the
    /// calling process — so the browser or Notepad becomes our child and the kernel puts
    /// it in the job, exactly as it does the WhisperLiveKit grandchild we actually want
    /// reaped. Quitting from the tray then killed the operator's browser, or every
    /// Notepad tab they had open (Win11 Notepad is single-process).
    ///
    /// Handing the target to <c>explorer.exe</c> sidesteps it: an explorer instance is
    /// already running outside our job, the one we start forwards to it and exits
    /// immediately, and the real application is spawned by THAT explorer — never a member
    /// of our job. The job also carries BREAKAWAY_OK (see JobObject) so the forwarding
    /// stub itself can leave.
    ///
    /// Cost: explorer returns before the target opens, so a bad URL/handler no longer
    /// surfaces as an exception here. Acceptable — the failure was already only advisory.
    /// </summary>
    private void ShellOpen(string target)
    {
        try
        {
            var info = new ProcessStartInfo("explorer.exe") { UseShellExecute = false, CreateNoWindow = true };
            info.ArgumentList.Add(target);
            using (Process.Start(info)) { }
        }
        catch (Exception error) when (
            error is System.ComponentModel.Win32Exception or InvalidOperationException or FileNotFoundException)
        {
            // No handler registered for http/.log, or the shell refused. Not fatal — the
            // operator can open it themselves, so say what we tried and carry on.
            string said = LoginLink.WithoutSecrets(target);
            _log.Write($"could not open {said}: {error.Message}");
            _notify("TapScribe", $"Could not open {said}.", NoticeKind.Warning);
        }
    }

    /// <summary>Whether SOMETHING is serving the Recorder's port. The rule — and the five
    /// ways of not being there — belong to <see cref="ConnectionTester"/>, which already owns
    /// what <c>GET /health</c> means; both shells had written it out identically.</summary>
    private bool RecorderAnswers() =>
        ConnectionTester.AnswersOnLoopback(BundleDefaults.RecorderPort, _http, TimeSpan.FromSeconds(2));

    /// <summary>The same boundary the Bridge half keeps around an operator action: a bug
    /// must not become a tray that vanishes, and WinForms answers an escaping handler with
    /// a dialog out of a click.</summary>
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
    /// Order is load-bearing: the reaper goes LAST, because releasing a KILL_ON_JOB_CLOSE
    /// job this process is a member of terminates the current process from inside that
    /// call — nothing after it runs.
    /// </summary>
    public void Dispose()
    {
        _controller.Dispose();
        foreach (ToolStripItem item in MenuItems)
            item.Dispose();
        _http.Dispose();
        _log.Write("--- TapScribe host role stopped ---");
        _log.Dispose();
        _reaper?.Dispose();
    }
}
