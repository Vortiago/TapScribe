namespace TapScribe.Bundle.Core.Tests;

/// <summary>
/// What the supervisor DECIDES, over fake children and a stubbed health probe. The spawn
/// itself is not the subject — <see cref="ChildProcess"/> is forwarding, and a fake that
/// also faked process start would be testing the double.
///
/// The decisions here are the ones an operator meets when something is wrong, and before
/// the seams existed the only way to reach them was to install a Bundle on Windows and
/// break it by hand.
/// </summary>
public class RecorderSupervisorTests
{
    [Fact]
    public void AHealthyBootReachesRunning_AndTheTrayOwnsTheRecorder()
    {
        using var world = new Fake();

        world.Boot();

        Assert.Equal(RecorderState.Running, world.LastState);
        Assert.True(world.Supervisor.Manages, "the tray did not record ownership at spawn");
    }

    [Fact]
    public void ARecorderThatDiesWithSomethingElseOnThePort_IsShownAsUnmanaged()
    {
        // Unmanaged is decided by the SPAWN ATTEMPT, not by probing first: the tray starts
        // its child, the child exits because the port is taken, and /health answering says
        // whose it is. A start.sh in a terminal, or another install.
        using var world = new Fake { PortAnswers = true };
        world.Boot();

        world.Recorder!.ExitWith(1);

        Assert.Equal(RecorderState.Unmanaged, world.LastState);
        Assert.Contains("already running", world.LastMessage, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void AnUnmanagedRecorderSurvivesQuit()
    {
        // The whole reason ownership is recorded rather than configured: the Recorder the
        // operator launched from a terminal must outlive this tray's Quit.
        using var world = new Fake { PortAnswers = true };
        world.Boot();
        IChildProcess recorder = world.Recorder!;
        world.Recorder!.ExitWith(1);

        world.Supervisor.Stop();

        Assert.False(world.Supervisor.Manages);
        Assert.False(((FakeChild)recorder).Killed, "Quit killed a Recorder this tray does not own");
    }

    [Fact]
    public void ARecorderThatDiesWithNothingOnThePort_IsReportedAsFailedNotAdopted()
    {
        // A crash-loop must look like a crash-loop. Adopting it would leave the tray
        // claiming a Recorder is running when the dashboard is dead.
        using var world = new Fake { PortAnswers = false };
        world.Boot();

        world.Recorder!.ExitWith(3);

        Assert.Equal(RecorderState.Stopped, world.LastState);
        Assert.Contains("exit 3", world.LastMessage, StringComparison.Ordinal);
    }

    [Fact]
    public void TheExitDecisionNeverReadsTheChildsOutput()
    {
        // Matching an "address already in use" string would mean owning uvicorn's wording
        // forever. The probe answers the question directly, so the classification is the
        // same whatever the child said on its way out.
        using var world = new Fake { PortAnswers = true };
        world.Boot();
        world.Recorder!.Say("Traceback (most recent call last): something entirely unrelated");

        world.Recorder!.ExitWith(0);

        Assert.Equal(RecorderState.Unmanaged, world.LastState);
    }

    [Fact]
    public void StopThenStartBootsAgainRatherThanWedging()
    {
        // Stop() is the operator's "Stop Recorder" as well as Quit's teardown (ADR-0022).
        // While the quitting flag was a one-way latch, the next Start spawned a preflight,
        // killed it on its own quit-race check and returned with no state report — leaving
        // the menu on "Preparing TapScribe…" with Start AND Stop both disabled, forever.
        using var world = new Fake();
        world.Boot();

        world.Supervisor.Stop();
        world.Boot();

        Assert.Equal(RecorderState.Running, world.LastState);
        Assert.Equal(4, world.Spawned.Count);
    }

    [Fact]
    public void QuitStopsARecorderTheTrayDidStart()
    {
        using var world = new Fake();
        world.Boot();
        var recorder = (FakeChild)world.Recorder!;

        world.Supervisor.Stop();

        Assert.True(recorder.Killed, "the tray left its own Recorder running");
    }

    [Fact]
    public void AQuitDuringPreflightNeverStartsARecorder()
    {
        // Preflight is an unbounded pip install. A Quit during it must not be followed by
        // a spawn nobody is left to reap.
        using var world = new Fake { StopDuringPreflight = true };

        world.Boot();

        Assert.Null(world.Recorder);
        Assert.NotEqual(RecorderState.Running, world.LastState);
    }

    [Fact]
    public void AMissingWheelFailsBeforeAnythingIsSpawned()
    {
        using var world = new Fake { ShipWheel = false };

        world.Boot();

        Assert.Equal(RecorderState.Failed, world.LastState);
        Assert.Empty(world.Spawned);
    }

    [Fact]
    public void TheChildIsEnrolledOnlyWhenTheReaperDoesNotCoverItAlready()
    {
        using var inherited = new Fake { Reaper = new FakeReaper { CoversChildrenByInheritance = true } };
        inherited.Boot();
        Assert.Empty(((FakeReaper)inherited.Reaper!).Adopted);

        using var perChild = new Fake { Reaper = new FakeReaper { CoversChildrenByInheritance = false } };
        perChild.Boot();
        Assert.Single(((FakeReaper)perChild.Reaper!).Adopted);
    }

    // ---- doubles -------------------------------------------------------------------

    private sealed class FakeReaper : IProcessReaper
    {
        public bool CoversChildrenByInheritance { get; init; }

        public List<IChildProcess> Adopted { get; } = [];

        public bool Adopt(IChildProcess child)
        {
            Adopted.Add(child);
            return true;
        }

        public void Dispose()
        {
        }
    }

    private sealed class FakeChild : IChildProcess
    {
        private readonly List<string> _said = [];

        public required string Executable { get; init; }

        public bool HasExited { get; private set; }

        public int ExitCode { get; private set; }

        public IntPtr NativeHandle => IntPtr.Zero;

        public int ProcessId => 4242;

        public bool Killed { get; private set; }

        public event EventHandler? Exited;

        /// <summary>Runs inside <see cref="WaitForExit()"/>: how a test lands a Quit
        /// while preflight is still blocked.</summary>
        public Action? OnWait { get; init; }

        public void Say(string line) => _said.Add(line);

        public void ExitWith(int code)
        {
            ExitCode = code;
            HasExited = true;
            Exited?.Invoke(this, EventArgs.Empty);
        }

        public void Kill()
        {
            Killed = true;
            HasExited = true;
        }

        public bool WaitForExit(int milliseconds) => true;

        public void WaitForExit()
        {
            OnWait?.Invoke();
            HasExited = true;
        }

        public void Dispose()
        {
        }
    }

    /// <summary>A supervisor over a temp Bundle, with every child faked.</summary>
    private sealed class Fake : IDisposable
    {
        private readonly string _root = Path.Join(
            Path.GetTempPath(), "tapscribe-supervisor-" + Guid.NewGuid().ToString("n"));

        public bool ShipWheel { get; init; } = true;

        public bool PortAnswers { get; init; }

        /// <summary>Quit lands while preflight is blocked, the race RunCore guards.</summary>
        public bool StopDuringPreflight { get; init; }

        public IProcessReaper? Reaper { get; init; }

        public List<string> Spawned { get; } = [];

        public FakeChild? Recorder { get; private set; }

        public RecorderState LastState { get; private set; } = RecorderState.Preflight;

        public string LastMessage { get; private set; } = "";

        private RecorderSupervisor? _supervisor;

        public RecorderSupervisor Supervisor => _supervisor ??= Build();

        private RecorderSupervisor Build()
        {
            BundleLayout layout = BundleLayout.ForWindows(
                Path.Join(_root, "program"), Path.Join(_root, "profile"));
            if (ShipWheel)
            {
                Directory.CreateDirectory(layout.WheelDirectory);
                File.WriteAllText(
                    Path.Join(layout.WheelDirectory, "tapscribe-1.0.0-py3-none-any.whl"), "PK");
            }

            return new RecorderSupervisor(
                layout,
                Reaper,
                log: _ => { },
                onState: (state, message) =>
                {
                    LastState = state;
                    LastMessage = message;
                },
                spawn: command =>
                {
                    Spawned.Add(command.Executable);
                    // Every boot spawns preflight then the Recorder, so the ODD spawns are
                    // the preflights — a second Boot() after a Stop is a supported sequence
                    // and `Count == 1` would mislabel its preflight as the Recorder.
                    bool isPreflight = Spawned.Count % 2 == 1;
                    var child = new FakeChild
                    {
                        Executable = command.Executable,
                        OnWait = isPreflight && StopDuringPreflight ? () => Supervisor.Stop() : null,
                    };
                    if (!isPreflight)
                        Recorder = child;
                    return child;
                },
                recorderAnswers: () => PortAnswers);
        }

        /// <summary>Await the boot rather than racing the thread pool. `Start()` returns
        /// its task for exactly this.</summary>
        public void Boot() => Supervisor.Start().GetAwaiter().GetResult();

        public void Dispose()
        {
            _supervisor?.Dispose();
            if (Directory.Exists(_root))
                Directory.Delete(_root, recursive: true);
        }
    }
}
