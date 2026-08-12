using TapScribe.Bridge.MacOS;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// Tests for <see cref="MacOSVersionFloor"/> — the macOS 14.4 floor the Mac tray Bridge
/// refuses to launch below (#419). 14.4 is where Core Audio process taps became usable by
/// an ordinary app, so below it there is no system-audio capture at all and no degraded
/// mode worth offering (ADR-0020).
///
/// The running version is a PARAMETER here, never read from the ambient OS inside the
/// assertion: a floor check that only ever sees whatever this box happens to run tests
/// nothing. The ambient read is a separate seam, tested separately.
/// </summary>
public class MacOSVersionFloorTests
{
    [Fact]
    public void Refusal_ForAVersionBelowTheFloor_NamesTheFloor()
    {
        // The operator's whole remedy is "upgrade to 14.4", so the message has to say so.
        string? refusal = MacOSVersionFloor.Refusal(new Version(14, 3));

        Assert.NotNull(refusal);
        Assert.Contains("14.4", refusal);
    }

    // The two-component case is the one that bites: System.Version leaves an unstated
    // component at -1, so Version(14, 4) sorts BELOW Version(14, 4, 0). A floor compared
    // naively against a three-component reading of the same release would refuse it.
    [Theory]
    [InlineData(14, 4, -1)]  // the floor itself, as macOS spells a .0 release
    [InlineData(14, 4, 0)]
    [InlineData(14, 4, 1)]
    [InlineData(14, 7, 2)]
    [InlineData(15, 0, 0)]
    [InlineData(26, 2, 0)]
    public void Refusal_AtTheFloorAndAbove_IsNone(int major, int minor, int build)
    {
        var running = build < 0 ? new Version(major, minor) : new Version(major, minor, build);

        Assert.Null(MacOSVersionFloor.Refusal(running));
    }
}
