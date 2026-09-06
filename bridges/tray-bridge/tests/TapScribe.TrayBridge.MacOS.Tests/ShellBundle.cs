namespace TapScribe.TrayBridge.MacOS.Tests;

/// <summary>
/// Where to find the shell's two manifests: the <c>Info.plist</c> inside the <c>.app</c> a
/// build just produced, and the source file a contributor edits.
///
/// The built one is the interesting one, and the reason these tests live in a project that
/// references the shell: the ProjectReference makes the <c>.app</c> exist before they run,
/// so an assertion about it is an assertion about what ships. The SDK rewrites some keys on
/// the way in (it stamps <c>LSMinimumSystemVersion</c> from
/// <c>SupportedOSPlatformVersion</c> and fills the version keys), so reading the source file
/// would gate a value the bundle does not have.
/// </summary>
internal static class ShellBundle
{
    /// <summary>The project on disk — <c>src/&lt;name&gt;/&lt;name&gt;.csproj</c>.</summary>
    private const string ProjectName = "TapScribe.TrayBridge.MacOS";

    /// <summary>What the built bundle is CALLED, which is <c>AssemblyName</c> and no longer
    /// the project name (ADR-0024): one tray per OS means the bridge-only build and the
    /// Bundle install the same <c>TapScribe.app</c>, so a Bundle upgrades a bridge-only
    /// install in place. Separate from <see cref="ProjectName"/> deliberately — they were
    /// one constant, and the day they diverged this search would have looked for a bundle
    /// that no longer exists and failed every test in the project with it.</summary>
    private const string AppName = "TapScribe";

    /// <summary>The manifest inside the built <c>.app</c>: what the operator's Mac reads.
    /// </summary>
    internal static string BuiltManifestPath => Locate();

    /// <summary>The manifest as committed, for the few claims that are about the tree rather
    /// than about the product.</summary>
    internal static string SourceManifestPath => Path.Join(ProjectDirectory, "Info.plist");

    // Both projects build under bin/<configuration>/..., and this assembly is running out of
    // its own. Deriving the configuration from that path rather than from a compile-time
    // constant keeps a Debug run from asserting over a stale Release bundle.
    private static string Configuration
    {
        get
        {
            for (DirectoryInfo? dir = new(AppContext.BaseDirectory); dir?.Parent is not null; dir = dir.Parent)
            {
                if (dir.Parent.Name == "bin")
                    return dir.Name;
            }

            throw new DirectoryNotFoundException($"no bin/<configuration>/ above {AppContext.BaseDirectory}");
        }
    }

    private static string ProjectDirectory
    {
        get
        {
            for (DirectoryInfo? dir = new(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
            {
                string candidate = Path.Join(dir.FullName, "src", ProjectName);
                if (File.Exists(Path.Join(candidate, $"{ProjectName}.csproj")))
                    return candidate;
            }

            throw new DirectoryNotFoundException($"no src/{ProjectName} above {AppContext.BaseDirectory}");
        }
    }

    // Globbed rather than spelled out, so the RID and TFM path segments the SDK chooses are
    // not restated here. Exactly one match is required, and anything else THROWS rather than
    // skipping: the ProjectReference builds the shell before these tests run, so a missing
    // bundle means something is wrong, and a skip would be a vacuous pass dressed as a green
    // run. More than one match means the search is ambiguous and could assert over the wrong
    // bundle, which is the same failure wearing a nicer hat.
    private static string Locate()
    {
        string root = Path.Join(ProjectDirectory, "bin", Configuration);
        string tail = Path.Join($"{AppName}.app", "Contents", "Info.plist");
        // Only the BUILD output counts, so anything under publish/ is skipped. A publish
        // artifact is a second bundle this search has no way to rank against the first, and
        // two matches fail the exactly-one rule below and take every test in this project
        // with them. The build output is the one the ProjectReference guarantees is current.
        string published = $"{Path.DirectorySeparatorChar}publish{Path.DirectorySeparatorChar}";
        string[] found = Directory.Exists(root)
            ? [.. Directory.GetFiles(root, "Info.plist", SearchOption.AllDirectories)
                .Where(p => p.EndsWith(tail, StringComparison.Ordinal))
                .Where(p => !p.Contains(published, StringComparison.Ordinal))]
            : [];

        return found.Length == 1
            ? found[0]
            : throw new FileNotFoundException(
                $"expected exactly one built bundle manifest at {Path.Join(root, "<rid>", tail)}, found {found.Length}. "
                + $"The shell is a ProjectReference of this test project, so it should already be built.");
    }
}
