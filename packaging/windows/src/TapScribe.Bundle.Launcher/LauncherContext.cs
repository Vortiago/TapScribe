using System.Diagnostics;
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

    public LauncherContext(BundleLayout layout)
    {
        _layout = layout;
        _log = new RotatingLogWriter(layout);

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
        if (_icon.ContextMenuStrip is { } menu && menu.InvokeRequired)
        {
            menu.BeginInvoke(() => OnState(state, message));
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
            Clipboard.SetText(password);
            _icon.ShowBalloonTip(5_000, "TapScribe", $"{lookup.Message} Username: admin.", ToolTipIcon.Info);
            return;
        }

        _log.Write($"copy password: {lookup.Message}");
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

    /// <summary>Hand a URL or a file to the shell's default handler.</summary>
    private void ShellOpen(string target)
    {
        try
        {
            using (Process.Start(new ProcessStartInfo(target) { UseShellExecute = true })) { }
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
            // LAST: KILL_ON_JOB_CLOSE fires here and terminates anything the Recorder
            // left behind (notably whisperlivekit-server), so it must outlive the
            // supervisor's own polite shutdown.
            _job?.Dispose();
            _log.Write("--- TapScribe Launcher stopped ---");
            _log.Dispose();
        }

        base.Dispose(disposing);
    }
}
