using System.Xml.Linq;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// The macOS shell app project as it sits in the working tree: where its files are, and
/// what its csproj declares. Tests reach the real project through here rather than through
/// a fixture copy, so what they gate is what a build would use.
/// </summary>
internal static class ShellProject
{
    private const string ProjectName = "TapScribe.TrayBridge.MacOS";

    internal static string InfoPlistPath => Path.Combine(Directory, "Info.plist");

    /// <summary>The single value of an MSBuild property in the shell csproj, or
    /// <c>null</c> when the csproj does not set it.</summary>
    internal static string? Property(string name) =>
        XDocument.Load(Path.Combine(Directory, $"{ProjectName}.csproj"))
            .Descendants(name)
            .SingleOrDefault()
            ?.Value;

    // The test assembly runs from bin/<config>/<tfm>/<rid>/ inside the tests project, so
    // the shell project is a fixed walk up and back down. Anchoring on a file the project
    // must have means a moved or renamed project fails here, loudly, rather than leaving
    // the tests over it silently gating nothing.
    private static string Directory
    {
        get
        {
            for (DirectoryInfo? dir = new(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
            {
                string candidate = Path.Combine(dir.FullName, "src", ProjectName);
                if (File.Exists(Path.Combine(candidate, "Info.plist")))
                    return candidate;
            }

            throw new DirectoryNotFoundException($"no src/{ProjectName} above {AppContext.BaseDirectory}");
        }
    }
}
