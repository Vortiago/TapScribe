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
}
