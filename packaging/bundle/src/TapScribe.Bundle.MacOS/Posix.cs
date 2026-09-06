using System.Runtime.InteropServices;

namespace TapScribe.Bundle.MacOS;

/// <summary>
/// The libc calls the reaper needs, and nothing else.
///
/// Kept as thin as <c>JobObject</c>'s Win32 block, and for the same reason: every one of
/// these is a syscall whose contract is the kernel's, so the interesting part is which ones
/// are called and in what order — which lives in the types above, where it can be read.
///
/// Classic <c>DllImport</c> rather than the source-generated <c>LibraryImport</c>: the
/// generated stubs require <c>AllowUnsafeBlocks</c>, and turning unsafe code on across the
/// assembly to save five hand-written declarations is the same bad trade the Windows half
/// declined.
/// </summary>
internal static class Posix
{
    /// <summary>The kqueue filter for process events.</summary>
    internal const short EvfiltProc = -5;

    /// <summary>EV_ADD | EV_ENABLE | EV_ONESHOT — register, arm, and fire exactly once.</summary>
    internal const ushort EvAdd = 0x0001;
    internal const ushort EvEnable = 0x0004;
    internal const ushort EvOneShot = 0x0010;

    /// <summary>EV_ERROR — set on a RETURNED event when the registration in the same call
    /// failed. kevent reports that failure through the eventlist rather than through its return
    /// value whenever there is room for it, so a caller that reads only the return value counts
    /// a refused registration as a fired watch.</summary>
    internal const ushort EvError = 0x4000;

    /// <summary>NOTE_EXIT — "the watched process exited". The whole point of the watch.</summary>
    internal const uint NoteExit = 0x8000_0000;

    internal const int Sigterm = 15;
    internal const int Sigkill = 9;

    /// <summary>
    /// A <c>struct kevent</c> as macOS lays it out on arm64: <c>uintptr_t ident</c>,
    /// <c>int16 filter</c>, <c>uint16 flags</c>, <c>uint32 fflags</c>, <c>intptr_t data</c>,
    /// <c>void *udata</c>. Sequential and explicitly sized — the default marshalling of a
    /// struct this is passed by reference has to match the C ABI exactly, and getting the
    /// field widths wrong reads as a watch that never fires.
    /// </summary>
    [StructLayout(LayoutKind.Sequential)]
    internal struct KEvent
    {
        internal nuint Ident;
        internal short Filter;
        internal ushort Flags;
        internal uint FFlags;
        internal nint Data;
        internal nint UData;
    }

    [DllImport("libc", SetLastError = true)]
    internal static extern int kqueue();

    [DllImport("libc", SetLastError = true)]
    internal static extern int kevent(
        int kq,
        ref KEvent changelist,
        int nchanges,
        ref KEvent eventlist,
        int nevents,
        IntPtr timeout);

    [DllImport("libc", SetLastError = true)]
    internal static extern int close(int fd);

    /// <summary>Put a process into a process group. <c>setpgid(0, 0)</c> makes the calling
    /// process a group leader of its own new group.</summary>
    [DllImport("libc", SetLastError = true)]
    internal static extern int setpgid(int pid, int pgid);

    [DllImport("libc", SetLastError = true)]
    internal static extern int getpgrp();

    /// <summary>Start a new session, which also creates a new process group. What the
    /// watchdog calls so it is not a member of the group it is going to kill.</summary>
    [DllImport("libc", SetLastError = true)]
    internal static extern int setsid();

    /// <summary>Signal every process in a group.</summary>
    [DllImport("libc", SetLastError = true)]
    internal static extern int killpg(int pgrp, int sig);

    /// <summary>Signal one process. <c>kill(pid, 0)</c> is the liveness probe: it delivers
    /// nothing and reports whether the process exists.</summary>
    [DllImport("libc", SetLastError = true)]
    internal static extern int kill(int pid, int sig);
}
