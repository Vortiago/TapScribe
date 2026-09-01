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
/// </summary>
public class BundleLayoutTests
{
    private static BundleLayout Layout(string program = "/opt/prog", string profile = "/home/op") =>
        BundleLayout.Resolve(program, profile);

    [Fact]
    public void Resolve_PutsTheEmbeddedInterpreterUnderTheProgramDirectory()
    {
        BundleLayout layout = Layout();

        // Interpreters at the ROOT of python/, not under Scripts/: the Bundle ships a
        // python-build-standalone install_only distribution and pip-installs into it
        // directly (release.yml stages staging/python/python.exe) — there is no venv, so
        // there is no Scripts\python.exe to run.
        Assert.Equal(Path.Join("/opt/prog", "python"), layout.PythonDirectory);
        Assert.Equal(Path.Join("/opt/prog", "python", "python.exe"), layout.Python);
        Assert.Equal(Path.Join("/opt/prog", "python", "pythonw.exe"), layout.Pythonw);
        Assert.Equal(Path.Join("/opt/prog", "wheel"), layout.WheelDirectory);
    }

    [Fact]
    public void Resolve_KeepsOperatorDataOutsideTheProgramDirectory()
    {
        BundleLayout layout = Layout();

        // %USERPROFILE%\TapScribe — never under the program dir (ADR-0015: an
        // uninstall must not be able to delete recordings).
        Assert.Equal(Path.Join("/home/op", "TapScribe"), layout.DataDirectory);
        Assert.DoesNotContain(layout.ProgramDirectory, layout.DataDirectory, StringComparison.Ordinal);
    }

    [Fact]
    public void Resolve_PointsAtTheRecordersOwnAuthPasswordFile()
    {
        // config.AUTH_PASSWORD_FILE is BASE_DIR / ".auth-password", and the Launcher
        // sets TAPSCRIBE_BASE_DIR to the data dir — so the two must agree.
        Assert.Equal(Path.Join("/home/op", "TapScribe", ".auth-password"), Layout().PasswordFile);
    }

    [Fact]
    public void Resolve_PutsTheLauncherLogInItsOwnFolderUnderTheDataDirectory()
    {
        BundleLayout layout = Layout();

        Assert.Equal(Path.Join("/home/op", "TapScribe", "logs"), layout.LogDirectory);
        Assert.Equal(Path.Join(layout.LogDirectory, BundleLayout.LogFileName), layout.LogFile);
    }

    [Fact]
    public void Resolve_MakesRelativeInputsAbsolute()
    {
        // pip runs with a different cwd than the Launcher, so every path handed
        // onward has to be absolute (install_target.resolve_install_spec absolutises
        // the wheel too, but a relative TAPSCRIBE_BASE_DIR would silently follow cwd).
        BundleLayout layout = BundleLayout.Resolve("prog", "profile");

        Assert.True(Path.IsPathRooted(layout.ProgramDirectory));
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
        Assert.ThrowsAny<ArgumentException>(() => BundleLayout.Resolve(blank!, "/home/op"));
        Assert.ThrowsAny<ArgumentException>(() => BundleLayout.Resolve("/opt/prog", blank!));
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
    private sealed class TempDir : IDisposable
    {
        private readonly string _root = Path.Join(
            Path.GetTempPath(), "tapscribe-bundle-" + Guid.NewGuid().ToString("n"));

        public BundleLayout Layout() =>
            BundleLayout.Resolve(Path.Join(_root, "program"), Path.Join(_root, "profile"));

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
}
