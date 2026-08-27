using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS.Tests;

/// <summary>
/// The menu-bar glyph for each <see cref="TrayIcon"/> (#419). The status item itself cannot
/// be constructed under a test host, so the mapping is kept out of it: what is asserted here
/// is that every state the runtime can report has a glyph, that no two states share one (the
/// icon is the at-a-glance signal, and two states wearing the same face is the same as having
/// no signal), and that a new state added in Core fails loudly rather than quietly reading as
/// idle.
/// </summary>
public class StatusSymbolsTests
{
    [Fact]
    public void For_AnswersEveryTrayIconWithAGlyphAndAFallback()
    {
        foreach (TrayIcon icon in Enum.GetValues<TrayIcon>())
        {
            StatusSymbol symbol = StatusSymbols.For(icon);

            Assert.False(string.IsNullOrWhiteSpace(symbol.Name), $"{icon} has no SF Symbol name");
            Assert.False(string.IsNullOrWhiteSpace(symbol.Fallback), $"{icon} has no fallback glyph");
        }
    }

    [Fact]
    public void For_GivesEveryTrayIconItsOwnGlyph()
    {
        TrayIcon[] icons = Enum.GetValues<TrayIcon>();

        int distinct = icons.Select(icon => StatusSymbols.For(icon).Name).Distinct(StringComparer.Ordinal).Count();

        Assert.Equal(icons.Length, distinct);
    }

    [Fact]
    public void For_AStateItDoesNotKnow_Refuses()
    {
        // TrayIcon lives in Core, so a state can be added there without this file being
        // touched. A silent fallback would show the operator an idle menu bar through a
        // failure; refusing turns it into a crash on the first status, which is the loud end
        // of a bad trade but the honest one.
        Assert.Throws<ArgumentOutOfRangeException>(() => StatusSymbols.For((TrayIcon)999));
    }
}
