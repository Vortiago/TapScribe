using AppKit;
using Foundation;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// Paints Core's <see cref="SummaryLayout"/> runs into an <see cref="NSTextView"/>: the Mac
/// sibling of WinForms' <c>SummaryRichText</c>, and the last piece of the meeting card (#422).
///
/// It decides nothing. Where a blank line goes, how tight a list is, what marker a bullet gets
/// and how far a heading steps up are all Core's answers, so the two shells cannot drift on
/// them. What is here is the mapping from a run to an <see cref="NSFont"/>, which is the only
/// part that is about AppKit.
///
/// Sizes are the system body size plus the run's step, rather than fixed points: a summary
/// should follow the operator's text size, and Core deliberately expresses the ramp as a step
/// so each shell resolves it against its own baseline.
/// </summary>
internal static class SummaryAttributedText
{
    internal static NSAttributedString Build(IReadOnlyList<MarkdownBlock> blocks)
    {
        nfloat bodySize = NSFont.SystemFontSize;
        var painted = new NSMutableAttributedString();

        foreach (SummaryRun run in SummaryLayout.Flatten(blocks))
        {
            nfloat size = bodySize + run.SizePlus;
            var attributes = new NSMutableDictionary
            {
                // labelColor, never a literal: the window follows the system appearance, and a
                // hard-coded black body would be invisible in dark mode.
                [NSStringAttributeKey.ForegroundColor] = NSColor.Label,
                [NSStringAttributeKey.Font] = Face(run, size),
            };
            if (run.Mono)
                attributes[NSStringAttributeKey.BackgroundColor] = NSColor.UnderPageBackground;

            painted.Append(new NSAttributedString(run.Text, attributes));
        }

        return painted;
    }

    // Bold and italic go on through the font MANAGER's trait conversion rather than by asking
    // for a named face: "Segoe UI Bold" has no macOS equivalent to name, and the system font
    // is not addressable by family name at all on recent versions. Mono asks for the
    // monospaced system face, which is the one guaranteed present.
    private static NSFont Face(SummaryRun run, nfloat size)
    {
        // Every one of these is typed as nullable by the bindings and none of them can
        // actually answer null: the system font at a valid size always resolves, and a null
        // here would mean AppKit had no system font at all. Coalescing to the plain system
        // font keeps the summary painted rather than throwing inside a window Apply, which
        // runs from a poll tick and would take the meeting card down with it.
        NSFont fallback = NSFont.SystemFontOfSize(size) ?? NSFont.UserFontOfSize(size)!;

        if (run.Mono)
            return NSFont.MonospacedSystemFont(size, NSFontWeight.Regular) ?? fallback;

        NSFont font = run.Emphasis.HasFlag(SummaryEmphasis.Bold)
            ? NSFont.BoldSystemFontOfSize(size) ?? fallback
            : fallback;

        if (!run.Emphasis.HasFlag(SummaryEmphasis.Italic))
            return font;

        // Italic is a trait conversion because there is no BoldItalicSystemFont, and the
        // converted font is what carries BOTH when a heading contains an emphasised word.
        // ConvertFont answers the ORIGINAL font when the family has no italic face, which is
        // the correct degradation: the word keeps the heading's weight instead of vanishing.
        return NSFontManager.SharedFontManager.ConvertFont(font, NSFontTraitMask.Italic) ?? font;
    }
}
