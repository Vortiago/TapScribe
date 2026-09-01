using System.Diagnostics;
using System.Runtime.InteropServices;
using TapScribe.Bundle.Core;

namespace TapScribe.Bundle.MacOS;

/// <summary>
/// The macOS <see cref="IProcessReaper"/>: a POSIX process group the tray leads, plus a
/// watchdog process holding a kqueue parent-death watch (ADR-0024).
///
/// <para><b>Both halves are required, and neither is sufficient.</b> The group alone reaps
/// only when the tray gets to run <see cref="Dispose"/> — an ordinary Quit. A tray that is
/// killed, faults, or is logged out from under runs no teardown at all, and what survives is
/// the Recorder plus the <c>whisperlivekit-server</c> grandchild it spawned, holding port
/// 8001 with nothing left to stop them. That is the crash path Windows gets from the kernel
/// (<c>JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE</c> fires on process DEATH); macOS has no
/// equivalent and no <c>PDEATHSIG</c>, so the watch has to live in a process that OUTLIVES
/// the tray. The watchdog is that process.</para>
///
/// <para><b>Why the tray leads the group rather than assigning each child.</b> A child
/// inherits its parent's process group, and so does a grandchild — so leading the group once,
/// before any spawn, closes the window in which the Recorder forks
/// <c>whisperlivekit-server</c> before the tray could have enrolled it. It is the exact
/// analogue of the Windows job object's self-enrolment, for the exact same reason, and
/// <see cref="Adopt"/> is the same narrower fallback.</para>
///
/// <b>Behaviour is macOS-only; the assembly is not.</b> It compiles everywhere (see the
/// csproj), which is what keeps the shell's one buildable platform from being the only place
/// any of this is a compile error.
/// </summary>
public sealed class ProcessGroupReaper : IProcessReaper
{
    private readonly int _group;
    private readonly Action<string> _log;
    private readonly Process? _watchdog;
    private bool _disposed;

    private ProcessGroupReaper(int group, Action<string> log, Process? watchdog)
    {
        _group = group;
        _log = log;
        _watchdog = watchdog;
    }

    /// <summary>
    /// Lead a new process group and start the watchdog over it.
    ///
    /// Answers null — after logging why — rather than throwing, the same contract
    /// <c>JobObject.TryCreate</c> keeps: no reaper is a degraded tray, not a broken one, and
    /// refusing to start the Recorder over a missing backstop would be the worse trade.
    /// </summary>
    /// <param name="trayExecutable">The tray's own binary, re-invoked as the watchdog.
    /// Nullable because <c>Environment.ProcessPath</c> is: an unknown path is answered here,
    /// as a degraded tray, rather than made a throw at the call site — which would take the
    /// whole HOST ROLE down (the Recorder vanishing from the menu) over a missing backstop.</param>
    public static ProcessGroupReaper? TryCreate(string? trayExecutable, Action<string> log)
    {
        ArgumentNullException.ThrowIfNull(log);

        if (string.IsNullOrWhiteSpace(trayExecutable))
        {
            log("reaper: this process has no known path, so the parent-death watch cannot be "
                + "started; a crash may leave whisperlivekit-server running.");
            return null;
        }

        int self = Environment.ProcessId;
        if (Posix.setpgid(0, 0) != 0)
        {
            // Already a session leader is the ordinary reason, and it is not fatal: we then
            // lead nothing new but still sit in SOME group, which children still inherit.
            // Fall back to whatever group that is rather than giving up the backstop.
            log($"reaper: could not lead a new process group (errno {Marshal.GetLastPInvokeError()}); " +
                "using the one this process is already in.");
        }

        int group = Posix.getpgrp();
        if (group <= 0)
        {
            log("reaper: could not read this process's group; a crash may leave whisperlivekit-server running.");
            return null;
        }

        Process? watchdog = StartWatchdog(trayExecutable, new ReapRequest(self, group), log);
        return new ProcessGroupReaper(group, log, watchdog);
    }

    /// <summary>
    /// True: a child inherits its parent's process group, and so does a grandchild. This is
    /// what makes the WhisperLiveKit grandchild reapable without the tray ever holding its
    /// handle.
    /// </summary>
    public bool CoversChildrenByInheritance => true;

    /// <summary>
    /// Move one already-started child into the group. Reached only when
    /// <see cref="CoversChildrenByInheritance"/> is false, which it is not here — kept
    /// because the seam has it and because a child that called <c>setsid</c> on its own
    /// would need it.
    /// </summary>
    public bool Adopt(IChildProcess child)
    {
        ArgumentNullException.ThrowIfNull(child);

        return child.ProcessId > 0 && Posix.setpgid(child.ProcessId, _group) == 0;
    }

    /// <summary>
    /// Kill the group, then stop the watchdog.
    ///
    /// Order is load-bearing, and it is the OPPOSITE of the Windows half's: there, releasing
    /// the job is what does the killing, so it goes last. Here the watchdog exists only to
    /// do this if we could not, so once we have, it must be stopped — otherwise a Quit would
    /// leave a stray process behind on every run, watching a tray that is already gone.
    ///
    /// The tray is itself in this group, so <c>killpg</c> would signal it too. SIGTERM
    /// first, which .NET's default handling turns into an orderly shutdown of a process
    /// already on its way out, and never SIGKILL on this path — that is the watchdog's, for
    /// a tray that is no longer running to care.
    /// </summary>
    public void Dispose()
    {
        if (_disposed)
            return;
        _disposed = true;

        StopWatchdog();

        if (Posix.killpg(_group, Posix.Sigterm) != 0)
            _log($"reaper: could not signal process group {_group} (errno {Marshal.GetLastPInvokeError()}).");
    }

    private static Process? StartWatchdog(string trayExecutable, ReapRequest request, Action<string> log)
    {
        BundleProcess command = RecorderCommand.Watchdog(trayExecutable, request);
        var info = new ProcessStartInfo(command.Executable)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        foreach (string argument in command.Arguments)
            info.ArgumentList.Add(argument);

        try
        {
            return Process.Start(info);
        }
        catch (Exception error) when (
            error is System.ComponentModel.Win32Exception or InvalidOperationException
                or FileNotFoundException)
        {
            // The tray's own binary was not startable — an odd install, or a sandbox. The
            // group still reaps on a clean Quit; only the crash path is lost, and saying so
            // is better than refusing to run the Recorder at all.
            log($"reaper: could not start the parent-death watch ({error.Message}); "
                + "a crash may leave whisperlivekit-server running.");
            return null;
        }
    }

    private void StopWatchdog()
    {
        // No run-once dance of its own: Dispose is the only caller and already guards with
        // _disposed, so a second mechanism tracking "the watchdog has been dealt with" would
        // only make a reader check whether either can fire without the other.
        if (_watchdog is not { } watchdog)
            return;

        try
        {
            if (!watchdog.HasExited)
                watchdog.Kill();
        }
        catch (Exception error) when (
            error is InvalidOperationException or System.ComponentModel.Win32Exception or NotSupportedException)
        {
            // It exited between the check and the kill, or we lost the right to signal it.
            // Both mean the thing we wanted (no watchdog running) is already true.
            _log($"reaper: the parent-death watch had already gone ({error.Message}).");
        }
        finally
        {
            watchdog.Dispose();
        }
    }
}
