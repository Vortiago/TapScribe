namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Fitting operator-facing text to a platform's budget (#419).
///
/// Pinned here rather than in a shell because the rule is shared: the Windows tooltip's cut and
/// the macOS menu item's cut are one Unicode decision. It also gets the Windows side covered at
/// all for the first time, since a WinForms tray icon carries no unit tests.
/// </summary>
public class DisplayTextTests
{
    [Fact]
    public void Clamp_TextWithinTheBudget_IsLeftWhole()
    {
        Assert.Equal("Streaming 2/2", DisplayText.Clamp("Streaming 2/2", 63));
    }

    [Fact]
    public void Clamp_TextOverTheBudget_KeepsTheBudget()
    {
        Assert.Equal("Could not", DisplayText.Clamp("Could not open MacBook Pro Microphone", 9));
    }

    [Fact]
    public void Clamp_CutMidSurrogatePair_DropsTheWholeCharacter()
    {
        // A device name or a path can carry an emoji or a rarer CJK glyph, and one of those is
        // two chars: a cut between the halves leaves a lone surrogate, which both platforms draw
        // as a replacement box rather than as a truncation. The budget is odd against a run of
        // pairs on purpose, since that is the only arrangement the naive cut gets wrong.
        string clamped = DisplayText.Clamp(string.Concat(Enumerable.Repeat("🎤", 8)), 5);

        Assert.Equal("🎤🎤", clamped);
    }

    [Fact]
    public void Clamp_ToNothing_ReturnsEmpty()
    {
        // The degenerate budget, because backing off a surrogate reads one char below the limit
        // and a zero limit is the index that would be out of range.
        Assert.Equal("", DisplayText.Clamp("🎤", 0));
    }
}
