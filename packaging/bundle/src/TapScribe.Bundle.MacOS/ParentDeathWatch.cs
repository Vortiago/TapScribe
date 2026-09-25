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
    /// Block until the process is gone: a kqueue <c>EVFILT_PROC</c>/<c>NOTE_EXIT</c> watch,
    /// or a poll when that could not be armed.
    ///
    /// Two named halves rather than one method with three exits. The shape this replaced had
    /// an early return, a return inside a <c>try</c>, and a loop after the <c>finally</c> —
    /// and it was already wrong: the "no kqueue" branch said "fall through to the poll below"
    /// and RETURNED, so a kqueue that could not be created meant the watchdog killed the
    /// Recorder's process group immediately, while the tray was still running.
    /// </summary>
    private static void WaitForExit(int pid)
    {
        if (!TryWatchForExit(pid))
            PollUntilGone(pid);
    }

    /// <summary>
    /// Arm a one-shot exit watch and block on it. False means it could not be armed at all —
    /// including because the process is ALREADY gone, which is the likeliest case of all (a
    /// tray that crashed during startup) and which the poll then answers immediately.
    /// </summary>
    private static bool TryWatchForExit(int pid)
    {
        int queue = Posix.kqueue();
        if (queue < 0)
            return false;

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

            // Registration and wait in ONE call, which is what makes this race-free: if the
            // process died between the caller's spawn and here, the register itself fails
            // rather than arming a watch that will never fire.
            //
            // BOTH halves of the answer are needed. kevent reports a failed REGISTRATION not
            // through its return value but as a returned event carrying EV_ERROR — there was
            // room in the eventlist for it, so the call answers 1, "one event", and reading
            // only the return value counts a refusal as an exit. That is the same shape of bug
            // as the early `return` this method replaced, one step narrower: for the likeliest
            // errno (ESRCH — the tray is already gone) killing the group is right by accident,
            // and for every other one the watchdog reaps a live tray's Recorder mid-meeting.
            // Answering false sends this to PollUntilGone, which settles the already-dead case
            // on its first kill(pid, 0) anyway.
            int events = Posix.kevent(queue, ref change, 1, ref fired, 1, IntPtr.Zero);
            return events > 0 && (fired.Flags & Posix.EvError) == 0;
        }
        finally
        {
            Posix.close(queue);
        }
    }

    /// <summary>The fallback, and never a give-up: giving up here means never killing the
    /// group, which is the whole reason this process exists.</summary>
    private static void PollUntilGone(int pid)
    {
        while (Posix.kill(pid, 0) == 0)
            Thread.Sleep(TimeSpan.FromSeconds(1));
    }
}
