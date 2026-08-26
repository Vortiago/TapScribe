using System.Drawing;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// The tray icon for each <see cref="TrayIcon"/>, and the AppKit shell's
/// <c>StatusSymbolsTests</c> asked of the Windows side. It had no equivalent, and the shapes
/// differ in a way that matters: the Mac maps through a switch that throws on a state it does not
/// know, while this is a DICTIONARY, so a state added in Core reaches the indexer and raises
/// KeyNotFoundException from inside a status render, which is a tray that dies mid-meeting.
///
/// Distinctness is the same claim as the Mac's: the icon is the at-a-glance signal, and two
/// states wearing one colour is the same as having no signal.
/// </summary>
public class TrayIconsTests
{
    [Fact]
    public void Indexer_AnswersEveryTrayIcon()
    {
        using var icons = new TrayIcons();

        foreach (TrayIcon icon in Enum.GetValues<TrayIcon>())
            Assert.NotNull(icons[icon]);
    }

    [Fact]
    public void Indexer_GivesEveryTrayIconItsOwnColour()
    {
        using var icons = new TrayIcons();

        // Rendered rather than declared: the colours live inside Dot(), so the pixel is what
        // says two states are distinguishable. The centre of a 16x16 dot is its fill.
        List<Color> centres = [];
        foreach (TrayIcon icon in Enum.GetValues<TrayIcon>())
        {
            using Bitmap bitmap = icons[icon].ToBitmap();
            centres.Add(bitmap.GetPixel(bitmap.Width / 2, bitmap.Height / 2));
        }

        Assert.Equal(centres.Count, centres.Distinct().Count());
    }
}
