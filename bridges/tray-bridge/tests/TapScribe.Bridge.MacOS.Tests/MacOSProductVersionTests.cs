using TapScribe.Bridge.MacOS;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// Tests for <see cref="MacOSProductVersion"/>, the thin ambient edge that tells
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

    // A Mac that will not say what it runs is not one this Bridge supports, and the honest
    // place to say so is the floor's refusal - not an exception out of the launch path,
    // which would read as a crash rather than as "your macOS is too old".
    [Theory]
    [InlineData("")]
    [InlineData("\0")]
    [InlineData("Sonoma")]
    public void Parse_AReadingItCannotUnderstand_YieldsAVersionTheFloorRefuses(string reading)
    {
        Assert.NotNull(MacOSVersionFloor.Refusal(MacOSProductVersion.Parse(reading)));
    }

    [Fact]
    public void Current_ReadsARealMacOSVersionFromThisHost()
    {
        // Deliberately not compared against the floor: that would assert about whatever
        // this box happens to run rather than about the reader. Every macOS since 2001 is
        // major 10 or above, so this is the weakest claim that still proves the OS
        // answered - and, with it, that a P/Invoke works in the VSTest host at all, which
        // is what rules out doing this through the managed ObjC bindings.
        Version current = MacOSProductVersion.Current();

        Assert.True(current.Major >= 10, $"expected a real macOS version, read {current}");
    }
}
