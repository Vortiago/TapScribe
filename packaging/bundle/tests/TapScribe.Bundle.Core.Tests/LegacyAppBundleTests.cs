namespace TapScribe.Bundle.Core.Tests;

/// <summary>
/// The first-launch removal of the pre-rename <c>.app</c> (ADR-0024). `installer`
/// overwrites by PATH and never removes what an older receipt put elsewhere, so without
/// this an upgrade leaves two trays in the Applications folder — the outcome the rename to
/// <c>TapScribe.app</c> exists to prevent.
///
/// Real directories, on whatever leg runs: every rule here is about which path exists and
/// which one is running, and none of them is about macOS.
/// </summary>
public class LegacyAppBundleTests
{
    [Fact]
    public void TheSupersededBundleIsRemovedOnTheSuccessorsFirstLaunch()
    {
        using var apps = new Applications(legacyInstalled: true);

        string? removed = LegacyAppBundle.RemoveSuperseded(apps.Current, apps.Log, apps.Root);

        Assert.Equal(apps.Legacy, removed);
        Assert.False(Directory.Exists(apps.Legacy));
    }

    [Fact]
    public void ARunFromAnywhereButTheInstalledPathRemovesNothing()
    {
        // The one that would hurt: a contributor with the old app installed, running a debug
        // build out of bin/, must not have the operator's install deleted under them. And a
        // tray started from a Downloads folder is not the successor to anything.
        using var apps = new Applications(legacyInstalled: true);

        string? removed = LegacyAppBundle.RemoveSuperseded(
            "/Users/dev/repo/bin/Release/TapScribe.app", apps.Log, apps.Root);

        Assert.Null(removed);
        Assert.True(Directory.Exists(apps.Legacy));
    }

    [Fact]
    public void ALaunchFromNoBundleAtAllRemovesNothing()
    {
        // `dotnet run`, or a test host: ContainingApp answered null.
        using var apps = new Applications(legacyInstalled: true);

        Assert.Null(LegacyAppBundle.RemoveSuperseded(null, apps.Log, apps.Root));
        Assert.True(Directory.Exists(apps.Legacy));
    }

    [Fact]
    public void AFreshInstallWithNoOldBundleIsSilent()
    {
        // The overwhelmingly common case, and it must not log noise on every launch.
        using var apps = new Applications(legacyInstalled: false);

        Assert.Null(LegacyAppBundle.RemoveSuperseded(apps.Current, apps.Log, apps.Root));
        Assert.Empty(apps.Logged);
    }

    [Fact]
    public void ATrailingSeparatorStillCountsAsTheInstalledPath()
    {
        // The successor failing to recognise ITSELF would leave the orphan behind forever,
        // and a path with a trailing separator is the ordinary way that happens.
        using var apps = new Applications(legacyInstalled: true);

        string? removed = LegacyAppBundle.RemoveSuperseded(
            apps.Current + Path.DirectorySeparatorChar, apps.Log, apps.Root);

        Assert.Equal(apps.Legacy, removed);
    }

    [Theory]
    [InlineData("/Applications/TapScribe.app/Contents/MonoBundle", "/Applications/TapScribe.app")]
    [InlineData("/Applications/TapScribe.app/Contents/MacOS", "/Applications/TapScribe.app")]
    [InlineData("/Users/dev/repo/bin/Release/net10.0-macos/osx-arm64", null)]
    public void ContainingApp_FindsTheBundleAroundTheRunningAssemblies(string baseDir, string? expected)
    {
        Assert.Equal(expected, LegacyAppBundle.ContainingApp(baseDir));
    }

    /// <summary>A stand-in Applications folder holding the new app and, optionally, the one
    /// it replaced.</summary>
    private sealed class Applications : IDisposable
    {
        private readonly string _root = Directory.CreateTempSubdirectory("tapscribe-apps-").FullName;

        public Applications(bool legacyInstalled)
        {
            Directory.CreateDirectory(Path.Join(Current, "Contents", "MacOS"));
            if (legacyInstalled)
                Directory.CreateDirectory(Path.Join(Legacy, "Contents", "MacOS"));
        }

        public string Root => _root;

        public string Current => Path.Join(_root, LegacyAppBundle.CurrentName);

        public string Legacy => Path.Join(_root, LegacyAppBundle.LegacyName);

        public List<string> Logged { get; } = [];

        public void Log(string line) => Logged.Add(line);

        public void Dispose() => Directory.Delete(_root, recursive: true);
    }
}
