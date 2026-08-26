namespace TapScribe.TrayBridge.MacOS.Tests;

/// <summary>
/// Reading a number back out of a Settings text field (#419). Three fields on the Mac
/// Settings window are integers the operator types freely (the port and the two shared gate
/// timings), and a text field will hand back anything at all, so what a nonsense value does is
/// a decision rather than a formality: it keeps what was already saved instead of writing a
/// zero the operator never chose.
/// </summary>
public class SettingsFieldsTests
{
    [Fact]
    public void Int_ReadsANumberTheOperatorTyped()
    {
        Assert.Equal(8001, SettingsFields.Int("8001", fallback: 1, min: 1, max: 65535));
    }

    [Fact]
    public void Int_IgnoresSurroundingSpace()
    {
        Assert.Equal(8001, SettingsFields.Int("  8001 ", fallback: 1, min: 1, max: 65535));
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("localhost")]
    [InlineData("80o1")]
    [InlineData("8001.5")]
    [InlineData("8,001")]
    public void Int_ThatIsNotANumber_KeepsWhatWasSaved(string typed)
    {
        // Including the thousands separator: a port is not a quantity, and accepting "8,001"
        // as 8001 would depend on the operator's locale for whether it parsed at all.
        Assert.Equal(8001, SettingsFields.Int(typed, fallback: 8001, min: 1, max: 65535));
    }

    [Fact]
    public void Int_WithNoTextAtAll_KeepsWhatWasSaved()
    {
        Assert.Equal(8001, SettingsFields.Int(null, fallback: 8001, min: 1, max: 65535));
    }

    [Theory]
    [InlineData("0")]
    [InlineData("-1")]
    [InlineData("65536")]
    public void Int_OutsideTheRange_KeepsWhatWasSaved(string typed)
    {
        // Kept rather than clamped: a mistyped port is a typo, and silently saving the
        // nearest legal value tells the operator their entry was accepted.
        Assert.Equal(8001, SettingsFields.Int(typed, fallback: 8001, min: 1, max: 65535));
    }

    [Fact]
    public void Int_AtEitherEndOfTheRange_IsAccepted()
    {
        Assert.Equal(1, SettingsFields.Int("1", fallback: 8001, min: 1, max: 65535));
        Assert.Equal(65535, SettingsFields.Int("65535", fallback: 8001, min: 1, max: 65535));
    }

    [Fact]
    public void TryInt_SeparatesAUsableEntryFromOneThatFellBack()
    {
        // The distinction the window acts on: it snaps the field back to the value in force and
        // refuses to close, so an entry that was DISCARDED is not reported by a closing dialog as
        // one that was saved. A fallback that happens to equal the typed number is why this is
        // the same read as Int rather than a comparison after it.
        Assert.True(SettingsFields.TryInt("8001", min: 1, max: 65535, out int usable));
        Assert.Equal(8001, usable);

        Assert.False(SettingsFields.TryInt("8,001", min: 1, max: 65535, out _));
        Assert.False(SettingsFields.TryInt("0", min: 1, max: 65535, out _));
        Assert.False(SettingsFields.TryInt(null, min: 1, max: 65535, out _));
    }
}
