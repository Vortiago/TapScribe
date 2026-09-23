using System.Diagnostics;
using TapScribe.Bundle.Core;

namespace TapScribe.Bundle.Windows.Tests;

/// <summary>
/// The Windows reaper, on Windows. What matters is not that the P/Invoke returns a handle
/// but that the job is created with the two limit flags the Bundle depends on, and that
/// the tray ends up INSIDE it — the property that makes a grandchild reapable without the
/// tray ever holding its handle.
///
/// Deliberately does not assert the kill itself: proving KILL_ON_JOB_CLOSE means letting
/// a real job close, which takes the test process with it. CI's launch proof on
/// windows-latest is where that behaviour is exercised end to end.
/// </summary>
public class JobObjectTests
{
    /// <summary>
    /// Every job these tests create, rooted for the life of the test run — the lifetime the
    /// tray gives its own, which it keeps in a field for as long as it runs.
    ///
    /// Never disposed, and never left to a local either: releasing a KILL_ON_JOB_CLOSE job
    /// this process is a member of terminates the test run, and an unreachable job gets
    /// released by the GC — a SafeHandle's finalizer closes the handle, and it is the last
    /// one. Each TryCreate also nests this process in one more job, so every one of them is
    /// such a job, and the run would end wherever the collector happened to run.
    /// </summary>
    private static readonly List<JobObject> Rooted = [];

    private static JobObject? Create()
    {
        JobObject? created = JobObject.TryCreate(_ => { });
        if (created is not null)
        {
            lock (Rooted)
                Rooted.Add(created);
        }
        return created;
    }

    [RequiresWindows("create a Win32 job object")]
    public void TryCreate_PutsThisProcessInTheJob_SoChildrenJoinByInheritance()
    {
        // Rooted, not disposed — see Rooted.
        JobObject? job = Create();

        Assert.NotNull(job);
        Assert.True(
            job!.CoversChildrenByInheritance,
            "the tray was not enrolled, so a child that forks a grandchild in its first "
                + "instants can escape the job");
    }

    [RequiresWindows("create a Win32 job object")]
    public void TryCreate_AnswersTheReaperSeam()
    {
        // The supervisor is written against IProcessReaper and never against this type;
        // this is what says the Windows half actually satisfies it.
        JobObject? job = Create();

        Assert.IsAssignableFrom<IProcessReaper>(job);
    }

    [RequiresWindows("enrol a running process in a job object")]
    public void Adopt_AnswersFalseRatherThanThrowing_WhenTheKernelRefuses()
    {
        // The fallback path's whole contract: a caller that cannot enrol a child still has
        // a meeting to record, so a refusal is an answer, never an exception. A handle the
        // kernel rejects outright is the one refusal that needs no process to be in a
        // particular state — every OTHER way to be refused depends on Windows version or
        // on someone else's job, which is what made the first version of this test wrong.
        JobObject? job = Create();
        Assert.NotNull(job);

        using var refused = new NoSuchProcess();

        Assert.False(job!.Adopt(refused));
    }

    [RequiresWindows("enrol a running process in a job object")]
    public void Adopt_IsAcceptedForAProcessAlreadyInTheJob()
    {
        // Recorded because it is the opposite of what the API's older documentation says,
        // and this test asserted the opposite until CI said otherwise. Since Windows 8 a
        // process may belong to nested jobs, so re-assigning one to a job it is already in
        // SUCCEEDS. Nothing depends on that — self-enrolment is preferred for the
        // grandchild race, not to avoid a refusal — but a reader who assumes the old
        // behaviour writes `Adopt`'s fallback around a failure that never comes.
        JobObject? job = Create();
        Assert.NotNull(job);

        using var self = new CurrentProcess();

        Assert.True(job!.Adopt(self));
    }

    /// <summary>A child the kernel will not take: handle zero is invalid, which is the
    /// version-independent way to make AssignProcessToJobObject answer false.</summary>
    private sealed class NoSuchProcess : CurrentProcess
    {
        public override IntPtr NativeHandle => IntPtr.Zero;
    }

    /// <summary>This process, as the reaper's seam sees one.</summary>
    private class CurrentProcess : IChildProcess
    {
        private readonly Process _process = Process.GetCurrentProcess();

        public bool HasExited => false;

        public int ExitCode => 0;

        public virtual IntPtr NativeHandle => _process.Handle;

        public int ProcessId => _process.Id;

        public event EventHandler? Exited
        {
            add { }
            remove { }
        }

        public void Kill() => throw new NotSupportedException("not killing the test run");

        public bool WaitForExit(int milliseconds) => false;

        public void WaitForExit() => throw new NotSupportedException("not waiting on the test run");

        public void Dispose() => _process.Dispose();
    }
}
