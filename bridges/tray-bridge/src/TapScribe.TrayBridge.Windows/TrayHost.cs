using System.Diagnostics;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
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
    private readonly Action<string, string, NoticeKind> _notify;

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
        if (!BundleLayout.HostPayloadPresent(program))
            return null;

        BundleLayout layout = BundleLayout.Resolve(
            program, Environment.GetFolderPath(Environment.SpecialFolder.UserProfile));
        return new TrayHost(layout, post, notify);
    }

    private TrayHost(BundleLayout layout, Action<Action> post, Action<string, string, NoticeKind> notify)
    {
        _layout = layout;
        _notify = notify;
        _log = new RotatingLogWriter(layout);

        // Created BEFORE anything is spawned: a process started by a process already in a
        // job lands in that job automatically, which is what makes the WhisperLiveKit
        // grandchild reapable without ever holding its handle (see JobObject).
        _reaper = JobObject.TryCreate(_log.Write);

        _statusItem = new ToolStripMenuItem("Starting…") { Enabled = false };
        _startItem = new ToolStripMenuItem("Start Recorder", null, (_, _) => Guarded(() => _controller!.StartRecorder()));
        // Separate from Quit, deliberately: stopping the server is not quitting the tray,
        // and an operator who wants the port free should not have to lose their bridge.
        _stopItem = new ToolStripMenuItem("Stop Recorder", null, (_, _) => Guarded(() => _controller!.StopRecorder()));
        _dashboardItem = new ToolStripMenuItem("Open dashboard", null, (_, _) => Guarded(OpenDashboard));
        _passwordItem = new ToolStripMenuItem("Copy password", null, (_, _) => Guarded(CopyPassword));
        _logItem = new ToolStripMenuItem("Show log", null, (_, _) => Guarded(ShowLog));

        _log.Write("--- TapScribe host role starting ---");
        _controller = HostController.Attach(
            this, post, layout, _reaper, _log.Write, RecorderAnswers);
    }

    /// <summary>The items to splice into the tray menu, in order. The shell owns where
    /// they go; this owns what they are.</summary>
    internal IEnumerable<ToolStripItem> MenuItems =>
        [_separator, _statusItem, _startItem, _stopItem, _dashboardItem, _passwordItem, _logItem];

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
    /// </summary>
    private void OpenDashboard() => ShellOpen(BundleDefaults.DashboardUrl.TrimEnd('/') + MintLoginPath());

    private string MintLoginPath()
    {
        PasswordLookup lookup = PasswordFile.Read(_layout.PasswordFile);
        if (!lookup.IsOk || lookup.Password is null)
        {
            // Never the file-derived text: only the status. Same anti-leak rule as
            // CopyPassword below (CodeQL cs/cleartext-storage-of-sensitive-information).
            _log.Write($"login link: password unavailable ({lookup.Status}) — opening the dashboard signed out.");
            return "/";
        }

        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
            http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue(
                "Basic",
                Convert.ToBase64String(Encoding.UTF8.GetBytes($"admin:{lookup.Password}")));
            using HttpResponseMessage response = http
                .PostAsync(new Uri(BundleDefaults.DashboardUrl.TrimEnd('/') + "/api/login-link"), content: null)
                .GetAwaiter()
                .GetResult();
            response.EnsureSuccessStatusCode();
            using JsonDocument body = JsonDocument.Parse(
                response.Content.ReadAsStringAsync().GetAwaiter().GetResult());
            string? path = body.RootElement.GetProperty("path").GetString();
            return string.IsNullOrEmpty(path) ? "/" : path;
        }
        catch (Exception error) when (
            error is HttpRequestException or TaskCanceledException or JsonException or KeyNotFoundException)
        {
            // The Recorder is not up yet, or is an older build with no /api/login-link.
            // Not fatal and not worth a balloon: the dashboard still opens, and the
            // operator signs in the way they always could.
            _log.Write($"login link: {error.Message} — opening the dashboard signed out.");
            return "/";
        }
    }

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

        _notify("TapScribe", $"{lookup.Message} Username: admin.", NoticeKind.Information);
    }

    private void ShowLog() => ShellOpen(_log.Path);

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
            _log.Write($"could not open {target}: {error.Message}");
            _notify("TapScribe", $"Could not open {target}.", NoticeKind.Warning);
        }
    }

    /// <summary>Whether SOMETHING is serving the Recorder's port. The discriminator
    /// between "someone else's Recorder holds 8001" and "this install is broken".</summary>
    private bool RecorderAnswers()
    {
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
            using HttpResponseMessage response = http
                .GetAsync(new Uri(BundleDefaults.DashboardUrl.TrimEnd('/') + "/health"))
                .GetAwaiter()
                .GetResult();
            return response.IsSuccessStatusCode;
        }
        catch (Exception error) when (error is HttpRequestException or TaskCanceledException)
        {
            // Nothing listening, or it did not answer in time. Either way: not somebody
            // else's healthy Recorder.
            return false;
        }
    }

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
        _log.Write("--- TapScribe host role stopped ---");
        _log.Dispose();
        _reaper?.Dispose();
    }
}
