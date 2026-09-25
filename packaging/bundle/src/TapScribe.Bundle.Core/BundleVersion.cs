using System.Reflection;

namespace TapScribe.Bundle.Core;

/// <summary>
/// The version this build of TapScribe is, as the runtime stamp (ADR-0024).
///
/// It has one job and it is a narrow one: name the folder a macOS runtime copy lands in, so
/// that installing 1.4 over a runtime copied from 1.3 re-copies rather than serving the
/// previous release's wheel. So what matters is that it CHANGES between releases and is
/// stable within one — not that it is pretty.
///
/// Read from the assembly rather than passed in from the shell, because the release job's
/// single <c>-p:Version=</c> from the git tag is what stamps it, and that reaches the
/// assembly on every platform. Trimmed of the <c>+&lt;sha&gt;</c> build metadata the SDK
/// appends from source-link, which would otherwise make every commit a different runtime and
/// have a contributor's Mac re-copy 300 MB on every build.
/// </summary>
public static class BundleVersion
{
    /// <summary>What an unstamped build reports. A local <c>dotnet build</c> gets the SDK's
    /// own <c>1.0.0</c>, which is a perfectly good stamp — one folder, reused every time.</summary>
    public const string Unknown = "0.0.0";

    /// <summary>This assembly's version, ready to be a path segment.</summary>
    public static string Current() =>
        Normalise(typeof(BundleVersion).Assembly
            .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion);

    /// <summary>
    /// The part of an informational version that identifies the RELEASE.
    ///
    /// Split out from <see cref="Current"/> so the rule is testable without building an
    /// assembly per case — the reflection is not the interesting half.
    /// </summary>
    public static string Normalise(string? informational)
    {
        if (string.IsNullOrWhiteSpace(informational))
            return Unknown;

        // "1.4.0+9cd62f9…" — everything from the '+' is build metadata (SemVer), which
        // source-link fills with the commit sha. Keeping it would make every commit its own
        // runtime folder.
        string trimmed = informational.Split('+', 2)[0].Trim();
        return trimmed.Length == 0 ? Unknown : trimmed;
    }
}
