using TapScribe.Bundle.Core;

namespace TapScribe.Bundle.Core.Tests;

/// <summary>
/// Tests for <see cref="BundleLayout"/> — the Bundle's on-disk shape (ADR-0015):
/// program dir (embedded interpreter + wheel, under %LOCALAPPDATA%\Programs\TapScribe)
/// and a SEPARATE data dir (%USERPROFILE%\TapScribe) so uninstalling the program can
/// never delete someone's meeting recordings.
///
/// Path assertions are written with <see cref="Path.Join"/> rather than literal
/// backslashes: the layout is Windows-shaped at runtime but the Core is cross-platform
/// and these tests run on the Linux CI leg, where the separator is '/'.
///
/// The fixture ROOTS go through <see cref="Rooted"/> for the other half of that, which
/// the separator alone does not cover: <see cref="BundleLayout"/> normalises what it is
/// handed through <see cref="Path.GetFullPath"/>, and a POSIX-rooted literal like
/// "/opt/prog" is not absolute on Windows — it is drive-relative, so it comes back as
/// "D:\opt\prog" off whatever the current drive happens to be. Comparing the layout's
/// answer against the raw literal therefore failed on Windows alone, which CI never
/// sees because it runs this project on the ubuntu leg only. Both sides normalise now,
/// so the assertion is about the layout's own shape on every OS.
/// </summary>
public class BundleLayoutTests
{
    /// <summary>Where a macOS <c>.app</c> carries its read-only payload. Three things have
    /// to agree on it — this layout, the tray's role probe, and the package script — so it
    /// is written out once here rather than derived.</summary>
    private const string MacPayload = "/Applications/TapScribe.app/Contents/Resources";

    /// <summary>A fixture path as the layout will answer it: the same
    /// <see cref="Path.GetFullPath"/> the layout applies, so an expectation is never
    /// asserting .NET's own drive-qualification rules. Note what this does NOT weaken:
    /// the folder names, the joins, and data-outside-program all still fail if wrong.
    /// Only the root's spelling is delegated to the platform.</summary>
    private static string Rooted(string path) => Path.GetFullPath(path);

    private static BundleLayout Layout(string program = "/opt/prog", string profile = "/home/op") =>
        BundleLayout.ForWindows(program, profile);

    private static BundleLayout MacLayout(string version = "1.3.0") =>
        BundleLayout.ForMacOS(MacPayload, "/Users/op", version);

    [Fact]
    public void Resolve_PutsTheEmbeddedInterpreterUnderTheProgramDirectory()
    {
        BundleLayout layout = Layout();

        // Interpreters at the ROOT of python/, not under Scripts/: the Bundle ships a
        // python-build-standalone install_only distribution and pip-installs into it
        // directly (release.yml stages staging/python/python.exe) — there is no venv, so
        // there is no Scripts\python.exe to run.
        Assert.Equal(Path.Join(Rooted("/opt/prog"), "python"), layout.PythonDirectory);
        Assert.Equal(Path.Join(Rooted("/opt/prog"), "python", "python.exe"), layout.Python);
        Assert.Equal(Path.Join(Rooted("/opt/prog"), "python", "pythonw.exe"), layout.Pythonw);
        Assert.Equal(Path.Join(Rooted("/opt/prog"), "wheel"), layout.WheelDirectory);
    }

    [Fact]
    public void Resolve_KeepsOperatorDataOutsideTheProgramDirectory()
    {
        BundleLayout layout = Layout();

        // %USERPROFILE%\TapScribe — never under the program dir (ADR-0015: an
        // uninstall must not be able to delete recordings).
        Assert.Equal(Path.Join(Rooted("/home/op"), "TapScribe"), layout.DataDirectory);
        Assert.DoesNotContain(layout.PayloadDirectory, layout.DataDirectory, StringComparison.Ordinal);
    }

    [Fact]
    public void Resolve_PointsAtTheRecordersOwnAuthPasswordFile()
    {
        // config.AUTH_PASSWORD_FILE is BASE_DIR / ".auth-password", and the tray
        // sets TAPSCRIBE_BASE_DIR to the data dir — so the two must agree.
        Assert.Equal(Path.Join(Rooted("/home/op"), "TapScribe", ".auth-password"), Layout().PasswordFile);
    }

    [Fact]
    public void Resolve_PutsTheHostLogInItsOwnFolderUnderTheDataDirectory()
    {
        BundleLayout layout = Layout();

        Assert.Equal(Path.Join(Rooted("/home/op"), "TapScribe", "logs"), layout.LogDirectory);
        Assert.Equal(Path.Join(layout.LogDirectory, BundleLayout.LogFileName), layout.LogFile);
    }

    [Fact]
    public void Resolve_MakesRelativeInputsAbsolute()
    {
        // pip runs with a different cwd than the tray, so every path handed
        // onward has to be absolute (install_target.resolve_install_spec absolutises
        // the wheel too, but a relative TAPSCRIBE_BASE_DIR would silently follow cwd).
        BundleLayout layout = BundleLayout.ForWindows("prog", "profile");

        Assert.True(Path.IsPathRooted(layout.PayloadDirectory));
        Assert.True(Path.IsPathRooted(layout.DataDirectory));
        Assert.True(Path.IsPathRooted(layout.Python));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    public void Resolve_RejectsABlankDirectory(string? blank)
    {
        // ThrowsAny: null surfaces as ArgumentNullException, a derived type.
        Assert.ThrowsAny<ArgumentException>(() => BundleLayout.ForWindows(blank!, "/home/op"));
        Assert.ThrowsAny<ArgumentException>(() => BundleLayout.ForWindows("/opt/prog", blank!));
    }

    // ---- wheel resolution -------------------------------------------------
    // A Bundle carries exactly one tapscribe-*.whl. Zero or two is a PACKAGING
    // bug the operator must see, not something to paper over with a "pick the
    // newest" heuristic that would ship the wrong version silently.

    [Fact]
    public void ResolveWheel_ReturnsTheSingleBundledWheel()
    {
        using var dir = new TempDir();
        BundleLayout layout = dir.Layout();
        string wheel = dir.Wheel("tapscribe-1.0.0-py3-none-any.whl");

        Assert.Equal(wheel, layout.ResolveWheel());
    }

    [Fact]
    public void ResolveWheel_IgnoresNonWheelFilesBesideIt()
    {
        using var dir = new TempDir();
        BundleLayout layout = dir.Layout();
        string wheel = dir.Wheel("tapscribe-1.0.0-py3-none-any.whl");
        File.WriteAllText(Path.Join(layout.WheelDirectory, "README.txt"), "not a wheel");

        Assert.Equal(wheel, layout.ResolveWheel());
    }

    [Fact]
    public void ResolveWheel_FailsLoudly_WhenTheWheelDirectoryIsMissing()
    {
        using var dir = new TempDir();
        BundleLayout layout = dir.Layout(); // never creates wheel/

        var error = Assert.Throws<BundleLayoutException>(() => layout.ResolveWheel());

        Assert.Contains(layout.WheelDirectory, error.Message, StringComparison.Ordinal);
        Assert.Contains("wheel", error.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void ResolveWheel_FailsLoudly_WhenNoWheelIsPresent()
    {
        using var dir = new TempDir();
        BundleLayout layout = dir.Layout();
        Directory.CreateDirectory(layout.WheelDirectory);

        var error = Assert.Throws<BundleLayoutException>(() => layout.ResolveWheel());

        Assert.Contains(layout.WheelDirectory, error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void ResolveWheel_FailsLoudly_AndNamesThemAll_WhenSeveralArePresent()
    {
        using var dir = new TempDir();
        BundleLayout layout = dir.Layout();
        dir.Wheel("tapscribe-1.0.0-py3-none-any.whl");
        dir.Wheel("tapscribe-1.1.0-py3-none-any.whl");

        var error = Assert.Throws<BundleLayoutException>(() => layout.ResolveWheel());

        // Both names in the message: the operator has to know WHICH two shipped.
        Assert.Contains("tapscribe-1.0.0-py3-none-any.whl", error.Message, StringComparison.Ordinal);
        Assert.Contains("tapscribe-1.1.0-py3-none-any.whl", error.Message, StringComparison.Ordinal);
    }

    /// <summary>A throwaway program dir with a matching data dir, cleaned up on dispose.</summary>
    [Fact]
    public void HostPayloadPresent_IsFalseForABridgeOnlyInstall()
    {
        // The bridge-only artifact: the same tray executable, with nothing beside it. Its
        // menu must be exactly what it was before the host role existed.
        using var dir = new TempDir();
        Directory.CreateDirectory(dir.Layout().PayloadDirectory);

        Assert.False(BundleLayout.HostPayloadPresent(dir.Layout().PayloadDirectory));
    }

    [Fact]
    public void HostPayloadPresent_IsTrueForABundle()
    {
        using var dir = new TempDir();
        dir.Wheel("tapscribe-1.0.0-py3-none-any.whl");
        Directory.CreateDirectory(dir.Layout().PythonDirectory);

        Assert.True(BundleLayout.HostPayloadPresent(dir.Layout().PayloadDirectory));
    }

    [Fact]
    public void HostPayloadPresent_StillClaimsTheRoleWhenHalfThePayloadIsMissing()
    {
        // The point of the OR. A Bundle whose wheel/ was wiped must NOT silently degrade
        // to a bridge-only tray — the operator's Recorder would just vanish from the
        // menu, with no error anywhere. It keeps the role, and ResolveWheel's own loud
        // failure is what they see instead. Mirrored for a wiped interpreter, which is
        // the same bug with the halves swapped.
        using var wheelOnly = new TempDir();
        wheelOnly.Wheel("tapscribe-1.0.0-py3-none-any.whl");
        Assert.True(BundleLayout.HostPayloadPresent(wheelOnly.Layout().PayloadDirectory));

        using var pythonOnly = new TempDir();
        Directory.CreateDirectory(pythonOnly.Layout().PythonDirectory);
        Assert.True(BundleLayout.HostPayloadPresent(pythonOnly.Layout().PayloadDirectory));
    }

    [Fact]
    public void HostPayloadPresent_DoesNotSwallowAPackagingBugItClaimedTheRoleOver()
    {
        // The pairing that makes the OR safe: claiming the role with half a payload is
        // only correct because resolution then FAILS loudly inside it.
        using var dir = new TempDir();
        Directory.CreateDirectory(dir.Layout().PythonDirectory);
        BundleLayout layout = dir.Layout();

        Assert.True(BundleLayout.HostPayloadPresent(layout.PayloadDirectory));
        Assert.Throws<BundleLayoutException>(() => layout.ResolveWheel());
    }

    private sealed class TempDir : IDisposable
    {
        private readonly string _root = Path.Join(
            Path.GetTempPath(), "tapscribe-bundle-" + Guid.NewGuid().ToString("n"));

        public BundleLayout Layout() =>
            BundleLayout.ForWindows(Path.Join(_root, "program"), Path.Join(_root, "profile"));

        public string Wheel(string name)
        {
            string dir = Layout().WheelDirectory;
            Directory.CreateDirectory(dir);
            string path = Path.Join(dir, name);
            File.WriteAllText(path, "PK");
            return path;
        }

        public void Dispose()
        {
            if (Directory.Exists(_root))
                Directory.Delete(_root, recursive: true);
        }
    }

    [Fact]
    public void ForMacOS_PutsTheDataRootUnderApplicationSupport()
    {
        // ADR-0024. Bridge.MacOS/TrayStores already keeps the tray's settings here, so
        // settings, recordings/, config/, .auth-password, .tap-token and the runtime share
        // one folder: one thing to back up, one to delete, and a bridge-only operator who
        // later installs the Bundle keeps their settings. Never ~/Documents, ~/Desktop or
        // ~/Downloads, which are TCC-protected.
        BundleLayout layout = MacLayout();

        Assert.Equal(
            Path.Join(Rooted("/Users/op"), "Library", "Application Support", "TapScribe"),
            layout.DataDirectory);
    }

    [Fact]
    public void ForMacOS_TargetsPipAtACopyOutsideTheApp()
    {
        // The decision the whole ADR turns on: /setup pip-installs at runtime and writing
        // inside a signed .app invalidates its signature, so nothing pip touches may live
        // under the payload.
        BundleLayout layout = MacLayout();

        Assert.True(layout.RuntimeIsACopy);
        Assert.Equal(
            Path.Join(layout.DataDirectory, "runtime", "1.3.0"), layout.RuntimeDirectory);
        Assert.StartsWith(layout.DataDirectory, layout.PythonDirectory, StringComparison.Ordinal);
        Assert.StartsWith(layout.DataDirectory, layout.WheelDirectory, StringComparison.Ordinal);
    }

    [Fact]
    public void ForMacOS_ReadsThePayloadFromInsideTheApp()
    {
        // The copy's SOURCE, and the only two paths that may point into the bundle.
        BundleLayout layout = MacLayout();

        Assert.Equal(Path.Join(Rooted(MacPayload), "python"), layout.PayloadPythonDirectory);
        Assert.Equal(Path.Join(Rooted(MacPayload), "wheel"), layout.PayloadWheelDirectory);
    }

    [Fact]
    public void ForMacOS_UsesThePosixInterpreterAndHasNoSeparateWindowlessOne()
    {
        // python-build-standalone's install_only tree is POSIX-shaped on macOS: bin/python3,
        // with the console-script entry points beside it. And there is no pythonw twin —
        // a child of a GUI app gets no terminal to begin with — so Pythonw is deliberately
        // the same binary rather than a second name that would have to exist.
        BundleLayout layout = MacLayout();

        Assert.Equal(Path.Join(layout.PythonDirectory, "bin", "python3"), layout.Python);
        Assert.Equal(layout.Python, layout.Pythonw);
    }

    [Fact]
    public void ForMacOS_StampsTheRuntimeWithTheVersionSoAnUpgradeRecopies()
    {
        // Without the stamp, installing 1.4 over a runtime copied from 1.3 leaves the
        // installer saying 1.4 while the Recorder serves 1.3 — drift ResolveWheel cannot
        // catch, because the stale runtime holds exactly one (wrong) wheel.
        Assert.NotEqual(MacLayout("1.3.0").RuntimeDirectory, MacLayout("1.4.0").RuntimeDirectory);
    }

    [Theory]
    [InlineData("../../../etc")]
    [InlineData("1.3.0/../../evil")]
    [InlineData("..")]
    public void ForMacOS_CannotBeSteeredOutOfTheRuntimeRootByAVersion(string version)
    {
        // The version reaching a macOS build is whatever `-p:Version=` was given — a git
        // tag, and therefore external text — and it becomes a directory name under the
        // operator's home. Reduced to one safe segment rather than trusted.
        BundleLayout layout = MacLayout(version);

        Assert.Equal(
            Path.GetFullPath(layout.RuntimeRoot),
            Path.GetFullPath(Path.Join(layout.RuntimeDirectory, "..")));
    }

    [Fact]
    public void ForMacOS_TellsAMacOperatorHowToRepairAMacInstall()
    {
        // "reinstall from TapScribe-Setup-win-x64.exe" is worse than saying nothing to
        // someone holding a .pkg.
        Assert.Contains("osx-arm64.pkg", MacLayout().ReinstallAdvice, StringComparison.Ordinal);
        Assert.Contains("win-x64.exe", Layout().ReinstallAdvice, StringComparison.Ordinal);
    }

    [Fact]
    public void MacOSPayload_ClimbsToContentsResourcesFromWhereverTheAssembliesSit()
    {
        // The SDK has moved managed assemblies between Contents/MonoBundle and
        // Contents/MacOS before, and a wrong guess reads downstream as "no host payload" —
        // a Bundle silently demoted to a bridge-only tray.
        string expected = Path.Join(Rooted("/Applications/TapScribe.app"), "Contents", "Resources");

        Assert.Equal(
            expected,
            BundleLayout.MacOSPayload("/Applications/TapScribe.app/Contents/MonoBundle"));
        Assert.Equal(
            expected,
            BundleLayout.MacOSPayload("/Applications/TapScribe.app/Contents/MacOS"));
    }

    [Fact]
    public void MacOSPayload_AnswersAMissingFolderRatherThanThrowingOutsideABundle()
    {
        // `dotnet run`, or a test host: there is no Contents above. The caller's very next
        // step is the role probe, and "no host payload" is the right answer there — a throw
        // would take down a tray that has a perfectly good bridge role.
        string payload = BundleLayout.MacOSPayload("/tmp/not-a-bundle");

        Assert.False(BundleLayout.HostPayloadPresent(payload));
    }
}
