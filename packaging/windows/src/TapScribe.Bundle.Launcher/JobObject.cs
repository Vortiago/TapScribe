using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace TapScribe.Bundle.Launcher;

/// <summary>
/// A Windows Job Object with <c>JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE</c>, used as the
/// Bundle's reaper: whatever the Launcher started dies with the Launcher, including
/// processes it never had a handle to.
///
/// That last part is the whole point. The Recorder spawns
/// <c>whisperlivekit-server</c> as its own child (<c>tapscribe/live.py</c>), and the repo
/// already ships an operator-facing diagnostic for the case where one of those is left
/// behind holding the live-channel port — "a leftover whisperlivekit-server from a
/// previous crash — find with <c>netstat -ano | findstr :PORT</c> then
/// <c>taskkill /PID &lt;pid&gt; /F</c>". A tray app that the operator closes from the
/// notification area would produce exactly that orphan on every quit; a job object makes
/// the kernel clean up instead of the operator.
///
/// <para><b>Why the Launcher assigns ITSELF rather than the child.</b> A process created
/// by a process already in a job is placed in that job automatically, so assigning
/// ourselves once, before any spawn, closes the window between <c>Process.Start</c> and
/// <c>AssignProcessToJobObject</c> in which the child could have already forked a
/// grandchild that then escapes the job. The cost is that the job contains the Launcher
/// too — which is the intended semantics ("when the tray goes, everything goes") and is
/// harmless because the handle is held for the process lifetime and only ever released
/// at exit. <see cref="AssignProcess"/> remains as the fallback when self-assignment is
/// refused (a Launcher already inside someone else's job that forbids nesting).</para>
///
/// <b>Not unit-tested — Windows-only kernel behaviour.</b> Verified on windows-latest.
/// </summary>
internal sealed class JobObject : IDisposable
{
    /// <summary>JOBOBJECTINFOCLASS.JobObjectExtendedLimitInformation.</summary>
    private const int ExtendedLimitInformationClass = 9;
    private const uint JobObjectLimitKillOnJobClose = 0x00002000;

    /// <summary>
    /// JOB_OBJECT_LIMIT_BREAKAWAY_OK — lets a child that explicitly asks (via
    /// CREATE_BREAKAWAY_FROM_JOB) leave the job. Nothing TapScribe spawns asks for it, so
    /// the Recorder and its WhisperLiveKit grandchild are still reaped as before; this
    /// only stops the kernel from refusing a breakaway that the shell may request when
    /// LauncherContext.ShellOpen hands a URL or a file to explorer.exe.
    /// </summary>
    private const uint JobObjectLimitBreakawayOk = 0x00000800;

    private readonly SafeFileHandle _handle;

    private JobObject(SafeFileHandle handle, bool selfAssigned)
    {
        _handle = handle;
        SelfAssigned = selfAssigned;
    }

    /// <summary>
    /// True when the Launcher itself is in the job, so every process it spawns is placed
    /// there by the kernel and no per-child assignment is needed (nor wanted — assigning
    /// a process that is already a member fails). False means the caller must fall back
    /// to <see cref="AssignProcess"/> for each child it starts.
    /// </summary>
    public bool SelfAssigned { get; }

    /// <summary>
    /// Create the job and put the current process in it. Returns <c>null</c> — after
    /// logging why — rather than throwing: no reaper is a degraded Launcher, not a
    /// broken one, and refusing to start the Recorder over it would be a worse trade.
    /// </summary>
    public static JobObject? TryCreate(Action<string> log)
    {
        SafeFileHandle handle = CreateJobObjectW(IntPtr.Zero, null);

        // ONE dispose point, in a finally, with an explicit ownership transfer. Every
        // step below can throw — Marshal.SizeOf, GetLastWin32Error, the caller's log
        // callback — and an escaping exception must not leak the kernel handle: the job
        // would stay alive, unowned, holding KILL_ON_JOB_CLOSE over nothing. `owned`
        // flips only once the JobObject exists and has taken responsibility for it.
        bool owned = false;
        try
        {
            if (handle.IsInvalid)
            {
                log($"job object: CreateJobObject failed (win32 {Marshal.GetLastWin32Error()}); " +
                    "a crash may leave whisperlivekit-server running.");
                return null;
            }

            var limits = new JobObjectExtendedLimitInformation();
            limits.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose | JobObjectLimitBreakawayOk;

            if (!SetInformationJobObject(handle, ExtendedLimitInformationClass, ref limits, (uint)Marshal.SizeOf<JobObjectExtendedLimitInformation>()))
            {
                log($"job object: SetInformationJobObject failed (win32 {Marshal.GetLastWin32Error()}); " +
                    "a crash may leave whisperlivekit-server running.");
                return null;
            }

            bool selfAssigned = AssignProcessToJobObject(handle, GetCurrentProcess());
            if (!selfAssigned)
            {
                log($"job object: could not assign the Launcher itself (win32 {Marshal.GetLastWin32Error()}); " +
                    "falling back to assigning the Recorder after spawn.");
            }

            var job = new JobObject(handle, selfAssigned);
            owned = true;
            return job;
        }
        finally
        {
            if (!owned)
                handle.Dispose();
        }
    }

    /// <summary>
    /// Fallback assignment for a single already-running process. Inherently racy against
    /// grandchildren spawned in the first instants of the child's life — see the type
    /// docs for why self-assignment is preferred.
    /// </summary>
    public bool AssignProcess(IntPtr processHandle) => AssignProcessToJobObject(_handle, processHandle);

    /// <summary>
    /// Releases the job. With <c>KILL_ON_JOB_CLOSE</c> this terminates every process
    /// still in it — the Recorder and its WhisperLiveKit grandchild — so it must only
    /// ever run at Launcher exit.
    /// </summary>
    public void Dispose() => _handle.Dispose();

    // Classic DllImport rather than the source-generated LibraryImport: the generated
    // stubs require <AllowUnsafeBlocks>, and turning unsafe code on across the whole
    // Launcher to save four hand-written declarations is a bad trade.
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateJobObjectW(IntPtr lpJobAttributes, string? lpName);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetInformationJobObject(
        SafeFileHandle hJob,
        int jobObjectInformationClass,
        ref JobObjectExtendedLimitInformation lpJobObjectInformation,
        uint cbJobObjectInformationLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool AssignProcessToJobObject(SafeFileHandle hJob, IntPtr hProcess);

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetCurrentProcess();

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectBasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public nuint MinimumWorkingSetSize;
        public nuint MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public nuint Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectExtendedLimitInformation
    {
        public JobObjectBasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public nuint ProcessMemoryLimit;
        public nuint JobMemoryLimit;
        public nuint PeakProcessMemoryUsed;
        public nuint PeakJobMemoryUsed;
    }
}
