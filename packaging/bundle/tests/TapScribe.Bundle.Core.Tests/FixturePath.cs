namespace TapScribe.Bundle.Core.Tests;

/// <summary>
/// Fixture paths as the code under test will answer them.
///
/// <see cref="BundleLayout"/> and <see cref="RecorderCommand"/> normalise through
/// <see cref="Path.GetFullPath"/>, and a POSIX-rooted literal like "/opt/prog" is
/// drive-RELATIVE on Windows, so it comes back as "D:\opt\prog". Asserting the raw literal
/// therefore failed on Windows alone, unseen because CI ran this project on the ubuntu leg
/// only. One home for the trick, so the next fixture is a call rather than a fourth
/// spelling. Folder names, joins and data-outside-program still fail if wrong.
/// </summary>
internal static class FixturePath
{
    internal static string Rooted(string path) => Path.GetFullPath(path);

    /// <summary>Null in, null out, for a <c>[Theory]</c> whose expectation is "no match":
    /// an <c>[InlineData]</c> literal cannot call a helper.</summary>
    internal static string? RootedOrNull(string? path) => path is null ? null : Path.GetFullPath(path);
}
