using TapScribe.Bundle.Core;

namespace TapScribe.Bundle.MacOS;

/// <summary>
/// The watchdog's whole life: leave the group it is about to kill, wait for the tray to
/// exit, kill the group, exit (ADR-0024).
///
/// This runs in the SECOND invocation of the tray binary — the one
/// <see cref="ReapRequest.Parse"/> recognised — and it is deliberately the smallest program
/// in the repo. It opens no window, reads no settings, resolves no paths and holds no
/// Recorder handle. It exists because macOS cannot express "kill these when I die" any other
/// way.
/// </summary>
public static class ParentDeathWatch
{
    /// <summary>How long the group is given to go down on SIGTERM before SIGKILL. The
    /// Recorder's own shutdown is a few hundred milliseconds; this is generous, and nobody
    /// is waiting on it — the tray is already gone.</summary>
    private static readonly TimeSpan GraceBeforeKill = TimeSpan.FromSeconds(5);

    /// <summary>
    /// Run the watch to completion. Returns the process exit code.
    /// </summary>
    /// <param name="request">Who to watch, and what to kill.</param>
    /// <param name="complaints">Where a failure is said. The watchdog has no log file of its
    /// own on purpose: it must not race the tray for the rotating one.</param>
    public static int Run(ReapRequest request, TextWriter complaints)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentNullException.ThrowIfNull(complaints);

        // FIRST, before anything can go wrong: leave the process group. The watchdog was
        // spawned by the tray and therefore INHERITED the very group it is here to kill, so
        // without this it signals itself and the Recorder outlives it — the exact leak this
        // process exists to prevent, in the one shape nothing else would catch.
        if (Posix.setsid() == -1)
        {
            complaints.WriteLine("reap: could not leave the tray's process group; refusing to signal it.");
            return 1;
        }

        WaitForExit(request.TrayPid);

        // SIGTERM, then SIGKILL for whatever ignored it. The Recorder handles SIGTERM and
        // shuts its own children down; WhisperLiveKit, when it is the one left, does not
        // always.
        Posix.killpg(request.GroupId, Posix.Sigterm);
        Thread.Sleep(GraceBeforeKill);
        Posix.killpg(request.GroupId, Posix.Sigkill);
        return 0;
    }

    /// <summary>
    /// Block until the process is gone, through a kqueue <c>EVFILT_PROC</c>/<c>NOTE_EXIT</c>
    /// watch — the one macOS primitive that reports another process's exit without polling.
    ///
    /// Falls back to polling <c>kill(pid, 0)</c> when the watch cannot be registered, which
    /// is not merely defensive: the tray may ALREADY have exited by the time the watchdog
    /// gets here (a crash during startup is the likeliest crash of all), and registering a
    /// watch on a dead pid fails with ESRCH. Returning immediately in that case is exactly
    /// right — the thing being waited for has happened.
    /// </summary>
    private static void WaitForExit(int pid)
    {
        int queue = Posix.kqueue();
        if (queue < 0)
            return; // no kqueue: fall through to the poll below.

        try
        {
            var change = new Posix.KEvent
            {
                Ident = (nuint)pid,
                Filter = Posix.EvfiltProc,
                Flags = (ushort)(Posix.EvAdd | Posix.EvEnable | Posix.EvOneShot),
                FFlags = Posix.NoteExit,
            };
            var fired = default(Posix.KEvent);

            // Registration and wait in one call, which is what makes this race-free: if the
            // process died between the caller's spawn and here, the register itself fails
            // and we are done.
            if (Posix.kevent(queue, ref change, 1, ref fired, 1, IntPtr.Zero) >= 0)
                return;
        }
        finally
        {
            Posix.close(queue);
        }

        // ESRCH (already gone), or a kqueue that would not take the filter. Poll rather than
        // give up: giving up here means never killing the group.
        while (Posix.kill(pid, 0) == 0)
            Thread.Sleep(TimeSpan.FromSeconds(1));
    }
}
