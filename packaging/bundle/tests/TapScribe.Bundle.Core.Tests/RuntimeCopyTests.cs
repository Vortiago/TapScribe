namespace TapScribe.Bundle.Core.Tests;

/// <summary>
/// The macOS first-launch copy (ADR-0024), run against real directories on whatever CI
/// leg happens to be running — which is the point. ADR-0024 rejects a <c>pkg</c>
/// postinstall script precisely BECAUSE it would be untestable here, so a version of this
/// rule that only ran on macOS would give the decision away.
///
/// The payloads are a handful of files rather than 300 MB of interpreter: every rule under
/// test is about which directory exists when, and none of them is about size.
/// </summary>
public class RuntimeCopyTests
{
    [Fact]
    public void AWindowsLayoutCopiesNothing()
    {
        // The runtime IS the payload there, so the Windows tray never learns this type
        // exists. Asserted rather than assumed: a copy under Program Files would need
        // elevation and would silently do nothing useful.
        using var world = new Fake();

        RuntimeCopyResult result = RuntimeCopy.Ensure(
            BundleLayout.ForWindows(world.Payload, world.Home), world.Log);

        Assert.Equal(RuntimeCopyOutcome.NotNeeded, result.Outcome);
        Assert.False(result.BackendsLost);
        Assert.False(Directory.Exists(Path.Join(world.Home, "Library")));
    }

    [Fact]
    public void AFirstLaunchCopiesTheInterpreterAndTheWheel()
    {
        using var world = new Fake();
        BundleLayout layout = world.MacOS("1.3.0");

        RuntimeCopyResult result = RuntimeCopy.Ensure(layout, world.Log);

        Assert.Equal(RuntimeCopyOutcome.Fresh, result.Outcome);
        // Nothing was pip-installed into a runtime that did not exist, so there is nothing
        // to tell the operator about.
        Assert.False(result.BackendsLost);
        Assert.True(File.Exists(layout.Python));
        Assert.Equal("PK", File.ReadAllText(layout.ResolveWheel()));
    }

    [Fact]
    public void TheCopyLandsOutsideTheAppSoTheSignatureSurvives()
    {
        // The decision the whole ADR rests on: pip must never write inside the .app.
        using var world = new Fake();
        BundleLayout layout = world.MacOS("1.3.0");

        RuntimeCopy.Ensure(layout, world.Log);

        Assert.StartsWith(layout.DataDirectory, layout.Python, StringComparison.Ordinal);
        Assert.DoesNotContain(layout.PayloadDirectory, layout.Python, StringComparison.Ordinal);
    }

    [Fact]
    public void ASecondLaunchOfTheSameVersionCopiesNothingAgain()
    {
        using var world = new Fake();
        BundleLayout layout = world.MacOS("1.3.0");
        RuntimeCopy.Ensure(layout, world.Log);
        // Stands in for everything /setup pip-installed. It surviving IS the test: a
        // re-copy on every launch would throw the operator's model backends away daily.
        string backend = Path.Join(layout.PythonDirectory, "mlx_whisper.py");
        File.WriteAllText(backend, "installed by /setup");

        RuntimeCopyResult result = RuntimeCopy.Ensure(layout, world.Log);

        Assert.Equal(RuntimeCopyOutcome.Current, result.Outcome);
        Assert.True(File.Exists(backend));
    }

    [Fact]
    public void AnUpgradeRecopiesAndSaysTheBackendsAreGone()
    {
        // The drift ADR-0015's one-wheel rule exists to prevent: 1.4 installed over a
        // runtime copied from 1.3 leaves the Recorder serving 1.3's wheel, and
        // ResolveWheel cannot catch it because the stale runtime holds exactly one.
        using var world = new Fake();
        RuntimeCopy.Ensure(world.MacOS("1.3.0"), world.Log);
        world.ShipWheel("tapscribe-1.4.0-py3-none-any.whl");

        BundleLayout upgraded = world.MacOS("1.4.0");
        RuntimeCopyResult result = RuntimeCopy.Ensure(upgraded, world.Log);

        Assert.Equal(RuntimeCopyOutcome.Upgraded, result.Outcome);
        Assert.Equal("1.3.0", result.PreviousVersion);
        Assert.True(result.BackendsLost);
        Assert.EndsWith("tapscribe-1.4.0-py3-none-any.whl", upgraded.ResolveWheel(), StringComparison.Ordinal);
    }

    [Fact]
    public void TheSupersededRuntimeIsKeptUntilTheNewOneIsComplete_ThenDeleted()
    {
        using var world = new Fake();
        BundleLayout old = world.MacOS("1.3.0");
        RuntimeCopy.Ensure(old, world.Log);

        RuntimeCopy.Ensure(world.MacOS("1.4.0"), world.Log);

        Assert.False(Directory.Exists(old.RuntimeDirectory));
        Assert.Single(Directory.GetDirectories(old.RuntimeRoot));
    }

    [Fact]
    public void ACopyThatDiedPartWayIsNotMistakenForARuntime()
    {
        // The reason the rename is the completion marker. A crash during 300 MB otherwise
        // leaves a runtime/<version>/ that EXISTS, so "copy on first launch" never fires
        // again and the operator has a broken interpreter forever.
        using var world = new Fake();
        BundleLayout layout = world.MacOS("1.3.0");
        Directory.CreateDirectory(layout.RuntimeDirectory);
        Directory.CreateDirectory(Path.Join(layout.PythonDirectory, "lib"));

        RuntimeCopyResult result = RuntimeCopy.Ensure(layout, world.Log);

        Assert.Equal(RuntimeCopyOutcome.Repaired, result.Outcome);
        Assert.True(result.BackendsLost);
        Assert.True(File.Exists(layout.Python));
        // The outcome enum says WHAT happened; only the log says why, and a repair is the one
        // case an operator reading it has to be able to tell from a first launch.
        Assert.Contains(world.Logged, line => line.Contains("incomplete", StringComparison.Ordinal));
    }

    [Fact]
    public void ALeftOverPartialIsCleanedUpRatherThanCopiedInto()
    {
        // What the crashed copy above actually leaves behind. Copying INTO it would merge
        // a dead tree with a live one, which is the one state no check downstream looks for.
        using var world = new Fake();
        BundleLayout layout = world.MacOS("1.3.0");
        string partial = layout.RuntimeDirectory + RuntimeCopy.PartialSuffix;
        Directory.CreateDirectory(partial);
        File.WriteAllText(Path.Join(partial, "half-written.bin"), "junk");

        RuntimeCopy.Ensure(layout, world.Log);

        Assert.False(Directory.Exists(partial));
        Assert.False(File.Exists(Path.Join(layout.RuntimeDirectory, "half-written.bin")));
    }

    [Fact]
    public void APartialIsNeverCountedAsTheSupersededRuntime()
    {
        // Otherwise an interrupted upgrade reports the PARTIAL's name as the version the
        // operator came from, and the "your backends are gone" notice names a release that
        // never ran.
        using var world = new Fake();
        BundleLayout layout = world.MacOS("1.4.0");
        Directory.CreateDirectory(Path.Join(layout.RuntimeRoot, "1.3.0" + RuntimeCopy.PartialSuffix));

        RuntimeCopyResult result = RuntimeCopy.Ensure(layout, world.Log);

        Assert.Equal(RuntimeCopyOutcome.Fresh, result.Outcome);
        Assert.Null(result.PreviousVersion);
    }

    [Fact]
    public void ACopyThatFailsLeavesNoRuntimeBehind()
    {
        // The atomic rename's actual claim, and the one the others cannot make: whatever
        // goes wrong mid-copy, `runtime/<version>/` must not come into existence — because
        // its existence is the ONLY thing "already copied" is decided on. Forced with a
        // FILE where the partial's directory has to go, which is the one failure that can
        // be provoked identically on every leg.
        using var world = new Fake();
        BundleLayout layout = world.MacOS("1.3.0");
        Directory.CreateDirectory(layout.RuntimeRoot);
        File.WriteAllText(layout.RuntimeDirectory + RuntimeCopy.PartialSuffix, "in the way");

        Assert.ThrowsAny<IOException>(() => RuntimeCopy.Ensure(layout, world.Log));

        Assert.False(Directory.Exists(layout.RuntimeDirectory));
    }

    [Fact]
    public void AMissingInterpreterIsAPackagingBugTheOperatorSees()
    {
        // Loud, and phrased like every other packaging failure in this assembly — with the
        // platform's OWN reinstall advice, since a Mac cannot act on "TapScribe-Setup-win-x64.exe".
        using var world = new Fake(shipPython: false);
        BundleLayout layout = world.MacOS("1.3.0");

        var error = Assert.Throws<BundleLayoutException>(() => RuntimeCopy.Ensure(layout, world.Log));

        Assert.Contains(layout.PayloadPythonDirectory, error.Message, StringComparison.Ordinal);
        Assert.Contains(".pkg", error.Message, StringComparison.Ordinal);
    }

    /// <summary>A payload on disk plus a home to copy it into. Real directories: what is
    /// under test is which of them exists when.</summary>
    private sealed class Fake : IDisposable
    {
        private readonly string _root = Directory.CreateTempSubdirectory("tapscribe-runtime-").FullName;

        public Fake(bool shipPython = true)
        {
            Directory.CreateDirectory(Home);
            Directory.CreateDirectory(Payload);
            if (shipPython)
            {
                // The shape python-build-standalone's install_only tree has on macOS, which
                // is what BundleLayout.Python points into.
                Directory.CreateDirectory(Path.Join(Payload, "python", "bin"));
                File.WriteAllText(Path.Join(Payload, "python", "bin", "python3"), "#!/bin/sh");
                Directory.CreateDirectory(Path.Join(Payload, "python", "lib"));
                File.WriteAllText(Path.Join(Payload, "python", "lib", "os.py"), "import sys");
            }
            ShipWheel("tapscribe-1.3.0-py3-none-any.whl");
        }

        public string Home => Path.Join(_root, "home");

        public string Payload => Path.Join(_root, "TapScribe.app", "Contents", "Resources");

        public List<string> Logged { get; } = [];

        public void Log(string line) => Logged.Add(line);

        public BundleLayout MacOS(string version) => BundleLayout.ForMacOS(Payload, Home, version);

        /// <summary>Replace the shipped wheel, the way an upgraded <c>.app</c> carries a
        /// new one. Exactly one, always — two is a packaging bug ResolveWheel throws on.</summary>
        public void ShipWheel(string name)
        {
            string dir = Path.Join(Payload, "wheel");
            Directory.CreateDirectory(dir);
            foreach (string stale in Directory.GetFiles(dir, "*.whl"))
                File.Delete(stale);
            File.WriteAllText(Path.Join(dir, name), "PK");
        }

        public void Dispose() => Directory.Delete(_root, recursive: true);
    }
}
