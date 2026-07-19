using System.Diagnostics;
using System.Runtime.InteropServices;
using TapScribe.Bundle.Core;

namespace TapScribe.Bundle.Launcher;

/// <summary>
/// The tray shell of the Windows Bundle's <b>Launcher</b> (CONTEXT.md, ADR-0015): a
/// NotifyIcon with a status header, <b>Open dashboard</b>, <b>Copy password</b>,
/// <b>Show log</b> and <b>Quit</b>. It is not the Recorder — it starts one, points
/// <c>TAPSCRIBE_BASE_DIR</c> at the operator's data directory, and keeps it reaped.
///
/// Everything decidable without a Windows API already happened in
/// <see cref="BundleLayout"/>, <see cref="RecorderCommand"/>, <see cref="LogRotation"/>
/// and <see cref="PasswordFile"/> — all unit-tested on Linux. What is left here is
/// menu wiring, the clipboard, and two shell-executes.
///
/// <b>Not unit-tested — WinForms, needs a desktop session.</b>
/// </summary>
internal sealed class LauncherContext : ApplicationContext
{
    private readonly BundleLayout _layout;
    private readonly RotatingLogWriter _log;
    private readonly LauncherIcons _icons = new();
    private readonly NotifyIcon _icon;
    private readonly ToolStripMenuItem _statusItem;
    private readonly JobObject? _job;
    private readonly RecorderSupervisor _supervisor;

    /// <summary>
    /// Captured on the UI thread in the ctor. This — NOT
    /// <c>ContextMenuStrip.InvokeRequired</c> — is how supervisor callbacks get onto the
    /// UI thread. A ContextMenuStrip is a parentless top-level ToolStripDropDown whose
    /// handle is not created until it is first SHOWN, and WinForms' InvokeRequired
    /// returns FALSE for a handle-less control with no marshalling parent. So every state
    /// change before the operator's first right-click — Preflight, Running, and the
    /// Failed/Stopped paths raised from Process.Exited — took the unmarshalled branch and
    /// touched NotifyIcon/ToolStrip from a thread-pool thread. The balloon is a Bundle's
    /// only error surface, so one that intermittently fails to appear reads as "the app
    /// silently did nothing".
    /// </summary>
    private readonly SynchronizationContext _ui;

    public LauncherContext(BundleLayout layout)
    {
        _layout = layout;
        _log = new RotatingLogWriter(layout);
        // WindowsFormsSynchronizationContext is installed by Application.Run's message
        // loop; if this runs before that (or under a bare context) fall back to a plain
        // one, which posts to the thread pool — no worse than the old behaviour.
        _ui = SynchronizationContext.Current ?? new SynchronizationContext();

        // Created BEFORE anything is spawned: a process started by a process already in a
        // job lands in that job automatically, which is what makes the WhisperLiveKit
        // grandchild reapable without ever holding its handle (see JobObject).
        _job = JobObject.TryCreate(_log.Write);

        _statusItem = new ToolStripMenuItem("Starting…") { Enabled = false };
        var menu = new ContextMenuStrip();
        menu.Items.Add(_statusItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(new ToolStripMenuItem("Open dashboard", null, (_, _) => OpenDashboard()));
        menu.Items.Add(new ToolStripMenuItem("Copy password", null, (_, _) => CopyPassword()));
        menu.Items.Add(new ToolStripMenuItem("Show log", null, (_, _) => ShowLog()));
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(new ToolStripMenuItem("Quit", null, (_, _) => Quit()));

        _icon = new NotifyIcon
        {
            Icon = _icons[RecorderState.Preflight],
            Text = "TapScribe — starting",
            Visible = true,
            ContextMenuStrip = menu,
        };
        _icon.DoubleClick += (_, _) => OpenDashboard();

        _log.Write("--- TapScribe Launcher starting ---");
        _supervisor = new RecorderSupervisor(_layout, _job, _log.Write, OnState);
        _supervisor.Start();
    }

    /// <summary>Marshal a supervisor state change onto the UI thread and render it.</summary>
    private void OnState(RecorderState state, string message)
    {
        if (SynchronizationContext.Current != _ui)
        {
            _ui.Post(_ => OnState(state, message), null);
            return;
        }

        _statusItem.Text = message;
        _icon.Icon = _icons[state];
        // NotifyIcon.Text is capped at 63 chars by the shell; longer throws.
        _icon.Text = Truncate($"TapScribe — {message}", 63);

        if (state is RecorderState.Failed or RecorderState.Stopped)
            _icon.ShowBalloonTip(10_000, "TapScribe", message, ToolTipIcon.Error);
    }

    private static string Truncate(string text, int max) =>
        text.Length <= max ? text : text[..(max - 1)] + "…";

    private void OpenDashboard() => ShellOpen(BundleDefaults.DashboardUrl);

    /// <summary>
    /// The Bundle's only door in. <c>start.sh</c> prints the generated password to its
    /// terminal; a Bundle has none, so on first run this menu item is the operator's
    /// ONLY way to learn it. Every failure path therefore says something.
    /// </summary>
    private void CopyPassword()
    {
        PasswordLookup lookup = PasswordFile.Read(_layout.PasswordFile);
        if (lookup is { IsOk: true, Password: { } password })
        {
            // Clipboard.SetText is the one call in this handler that genuinely
            // throws: another process holding the clipboard (RDP redirection, a
            // clipboard manager like Ditto) surfaces as ExternalException
            // CLIPBRD_E_CANT_OPEN. PasswordFile goes to great lengths never to
            // throw so a menu click can't die silently — letting it die HERE
            // instead would leave the operator locked out of their own dashboard
            // behind a generic WinForms crash dialog, which is exactly the
            // outcome that design was avoiding.
            try
            {
                Clipboard.SetText(password);
            }
            catch (ExternalException error)
            {
                _log.Write($"copy password: clipboard unavailable ({error.Message})");
                _icon.ShowBalloonTip(
                    10_000,
                    "TapScribe",
                    "Couldn't reach the clipboard — another app is holding it. Try again, "
                        + $"or read the password from {_layout.PasswordFile}.",
                    ToolTipIcon.Warning);
                return;
            }

            _icon.ShowBalloonTip(5_000, "TapScribe", $"{lookup.Message} Username: admin.", ToolTipIcon.Info);
            return;
        }

        // Log the STATUS, never text derived from the password file. Message is
        // documented as secret-free and this is the failure path (Password is null
        // here), but the log is a plaintext file the operator opens from the tray —
        // keeping file-derived text out of it entirely means a future edit to Message
        // cannot turn this into a password leak. Also what CodeQL's
        // cs/cleartext-storage-of-sensitive-information is asking for.
        _log.Write($"copy password: {lookup.Status}");
        _icon.ShowBalloonTip(10_000, "TapScribe", lookup.Message, ToolTipIcon.Warning);
    }

    private void ShowLog()
    {
        if (!File.Exists(_log.Path))
        {
            _icon.ShowBalloonTip(5_000, "TapScribe", $"No log yet at {_log.Path}.", ToolTipIcon.Info);
            return;
        }

        ShellOpen(_log.Path);
    }

    /// <summary>
    /// Hand a URL or a file to the shell's default handler, VIA EXPLORER.
    ///
    /// Not <c>UseShellExecute = true</c> directly: the Launcher self-assigns into a
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
    /// of our job. The job also now carries BREAKAWAY_OK (see JobObject) so the forwarding
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
        catch (Exception error) when (error is System.ComponentModel.Win32Exception or InvalidOperationException or FileNotFoundException)
        {
            // No handler registered for http/.log, or the shell refused. Not fatal — the
            // operator can open it themselves, so say what we tried and carry on.
            _log.Write($"could not open {target}: {error.Message}");
            _icon.ShowBalloonTip(10_000, "TapScribe", $"Could not open {target}.", ToolTipIcon.Warning);
        }
    }

    private void Quit()
    {
        _log.Write("--- quit requested ---");
        _icon.Visible = false;
        _supervisor.Stop();
        ExitThread();
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _supervisor.Dispose();
            _icon.Dispose();
            _icons.Dispose();
            // Log the clean-shutdown marker and close the writer BEFORE releasing
            // the job. `_job.Dispose()` closes the last handle to a
            // KILL_ON_JOB_CLOSE job that the Launcher ITSELF is a member of (it
            // self-assigns so children are inherited), so the kernel terminates
            // this process from inside that call — anything after it never runs.
            // With the old ordering the marker was silently never written, which
            // made "the operator quit" indistinguishable from "we were killed" in
            // the only diagnostic a Bundle leaves behind.
            _log.Write("--- TapScribe Launcher stopped ---");
            _log.Dispose();

            // LAST, and it does not return: KILL_ON_JOB_CLOSE fires here and
            // terminates anything the Recorder left behind (notably
            // whisperlivekit-server), so it must outlive the supervisor's own
            // polite shutdown.
            _job?.Dispose();
        }

        base.Dispose(disposing);
    }
}
