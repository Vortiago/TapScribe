namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Reading a number back out of a Settings text field (#419). Three fields on the Mac
/// Settings window are integers the operator types freely (the port and the two shared gate
/// timings), and a text field will hand back anything at all, so whether an entry is usable is a
/// decision rather than a formality. The answer is one read, not a parse and a separate range
/// check: the window both TAKES the value and REPORTS the rejection, and two reads could
/// disagree about which happened.
/// </summary>
public class SettingsFieldsTests
{
    [Fact]
    public void TryInt_ReadsANumberTheOperatorTyped()
    {
        Assert.True(SettingsFields.TryInt("8001", min: 1, max: 65535, out int value));
        Assert.Equal(8001, value);
    }

    [Fact]
    public void TryInt_IgnoresSurroundingSpace()
    {
        Assert.True(SettingsFields.TryInt("  8001 ", min: 1, max: 65535, out int value));
        Assert.Equal(8001, value);
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("localhost")]
    [InlineData("80o1")]
    [InlineData("8001.5")]
    [InlineData("8,001")]
    public void TryInt_ThatIsNotANumber_IsRefused(string typed)
    {
        // Including the thousands separator: a port is not a quantity, and accepting "8,001"
        // as 8001 would depend on the operator's locale for whether it parsed at all.
        Assert.False(SettingsFields.TryInt(typed, min: 1, max: 65535, out _));
    }

    [Fact]
    public void TryInt_WithNoTextAtAll_IsRefused()
    {
        Assert.False(SettingsFields.TryInt(null, min: 1, max: 65535, out _));
    }

    [Theory]
    [InlineData("0")]
    [InlineData("-1")]
    [InlineData("65536")]
    public void TryInt_OutsideTheRange_IsRefused(string typed)
    {
        // Refused rather than clamped: a mistyped port is a typo, and silently saving the
        // nearest legal value tells the operator their entry was accepted.
        Assert.False(SettingsFields.TryInt(typed, min: 1, max: 65535, out _));
    }

    [Fact]
    public void TryInt_AtEitherEndOfTheRange_IsAccepted()
    {
        Assert.True(SettingsFields.TryInt("1", min: 1, max: 65535, out int low));
        Assert.Equal(1, low);

        Assert.True(SettingsFields.TryInt("65535", min: 1, max: 65535, out int high));
        Assert.Equal(65535, high);
    }
}
