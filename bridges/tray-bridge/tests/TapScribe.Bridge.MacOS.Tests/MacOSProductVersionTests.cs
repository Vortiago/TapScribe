using TapScribe.Bridge.MacOS;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// Tests for <see cref="MacOSProductVersion"/> — the thin ambient edge that tells
/// <see cref="MacOSVersionFloor"/> what this Mac actually runs (#419). Split in two so the
/// interesting half is pure: <see cref="MacOSProductVersion.Parse"/> turns the string the
/// OS hands back into a <see cref="Version"/> and is tested exhaustively here, while
/// <see cref="MacOSProductVersion.Current"/> is the one untestable line that fetches it.
/// </summary>
public class MacOSProductVersionTests
{
    // sysctl hands back a C string, so the reading can arrive NUL-terminated depending on
    // how the buffer is sized. Trimming is the parser's job, not every caller's.
    [Theory]
    [InlineData("14.4.1", "14.4.1")]
    [InlineData("14.4", "14.4")]
    [InlineData("26.2", "26.2")]
    [InlineData("14.4.1\0", "14.4.1")]
    [InlineData("  15.0 \n", "15.0")]
    public void Parse_ReadsTheProductVersionString(string reading, string expected)
    {
        Assert.Equal(Version.Parse(expected), MacOSProductVersion.Parse(reading));
    }
}
