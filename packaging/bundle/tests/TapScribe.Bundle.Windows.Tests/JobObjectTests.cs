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
    [RequiresWindows("create a Win32 job object")]
    public void TryCreate_PutsThisProcessInTheJob_SoChildrenJoinByInheritance()
    {
        // Not disposed: releasing a KILL_ON_JOB_CLOSE job this process is a member of
        // terminates the test run from inside Dispose. The handle goes when the process
        // does, which is the same lifetime the tray gives it.
        JobObject? job = JobObject.TryCreate(_ => { });

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
        JobObject? job = JobObject.TryCreate(_ => { });

        Assert.IsAssignableFrom<IProcessReaper>(job);
    }

    [RequiresWindows("enrol a running process in a job object")]
    public void Adopt_RefusesAProcessAlreadyInTheJob_RatherThanThrowing()
    {
        // The fallback path's contract: it answers false rather than throwing, because a
        // caller that cannot enrol a child still has a meeting to record. Enrolling the
        // CURRENT process — already a member by TryCreate — is the cheapest way to get a
        // refusal without spawning anything.
        JobObject? job = JobObject.TryCreate(_ => { });
        Assert.NotNull(job);

        using var self = new CurrentProcess();

        Assert.False(job!.Adopt(self));
    }

    /// <summary>This process, as the reaper's seam sees one. Already a job member by
    /// TryCreate, which is the cheapest way to get a refusal without spawning
    /// anything.</summary>
    private sealed class CurrentProcess : IChildProcess
    {
        private readonly Process _process = Process.GetCurrentProcess();

        public bool HasExited => false;

        public int ExitCode => 0;

        public IntPtr NativeHandle => _process.Handle;

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
