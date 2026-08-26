using System.Drawing;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// The tray icon for each <see cref="TrayIcon"/>, and the AppKit shell's
/// <c>StatusSymbolsTests</c> asked of the Windows side. The shapes differ in a way that matters:
/// the Mac maps through a switch that throws on a state it does not know, while this is a
/// DICTIONARY, so a state added in Core reaches the indexer and raises KeyNotFoundException from
/// inside a status render.
/// </summary>
public class TrayIconsTests
{
    [Fact]
    public void Indexer_GivesEveryTrayIconItsOwnColour()
    {
        // Rendered rather than declared: the colours live inside a private Dot(), so the pixel is
        // what says two states are distinguishable. Indexing every value also covers "answers at
        // all", which is the other half of the claim.
        using var icons = new TrayIcons();

        List<Color> centres = [];
        foreach (TrayIcon icon in Enum.GetValues<TrayIcon>())
        {
            using Bitmap bitmap = icons[icon].ToBitmap();
            centres.Add(bitmap.GetPixel(bitmap.Width / 2, bitmap.Height / 2));
        }

        Assert.Equal(centres.Count, centres.Distinct().Count());
    }

    [Fact]
    public void Indexer_AStateItDoesNotKnow_Refuses()
    {
        // The hazard the dictionary carries and the Mac's switch does not, pinned rather than
        // described: a state added in Core must fail here, not inside a status render.
        using var icons = new TrayIcons();

        Assert.Throws<KeyNotFoundException>(() => icons[(TrayIcon)999]);
    }
}
