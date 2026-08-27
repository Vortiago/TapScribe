using AppKit;
using Foundation;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// Paints Core's <see cref="SummaryLayout"/> runs into an <see cref="NSTextView"/>: the Mac sibling
/// of WinForms' <c>SummaryRichText</c>, and the last piece of the meeting card (#422).
///
/// It decides nothing. Where a blank line goes, how tight a list is, what marker a bullet gets and
/// how far a heading steps up are Core's answers, so the two shells cannot drift. What is here is
/// the mapping from a run to an <see cref="NSFont"/>.
///
/// Sizes are the system body size plus the run's step rather than fixed points: a summary should
/// follow the operator's text size, which is why Core expresses the ramp as a step.
/// </summary>
internal static class SummaryAttributedText
{
    // The handful of distinct faces a summary uses, resolved once: a body of forty spans is forty
    // NSFont lookups for the five or six faces it really has.
    private static readonly Dictionary<(bool Mono, SummaryEmphasis Emphasis, double Size), NSFont> Faces = [];

    internal static NSAttributedString Build(IReadOnlyList<MarkdownBlock> blocks)
    {
        nfloat bodySize = NSFont.SystemFontSize;
        var painted = new NSMutableAttributedString();

        foreach (SummaryRun run in SummaryLayout.Flatten(blocks))
        {
            nfloat size = bodySize + run.SizePlus;
            NSFont face = CachedFace(run, size);
            var attributes = new NSMutableDictionary
            {
                // labelColor, never a literal: the window follows the system appearance, and a
                // hard-coded black body would be invisible in dark mode.
                [NSStringAttributeKey.ForegroundColor] = NSColor.Label,
                [NSStringAttributeKey.Font] = face,
            };
            if (run.Mono)
                attributes[NSStringAttributeKey.BackgroundColor] = NSColor.UnderPageBackground;

            painted.Append(new NSAttributedString(run.Text, attributes));
        }

        return painted;
    }

    // Keyed on the run's face, so two spans differing only in text share a font. On the RESOLVED
    // size, not on SizePlus: keying on the step would hand back fonts at the old size after an
    // operator changed their text size.
    private static NSFont CachedFace(SummaryRun run, nfloat size)
    {
        (bool, SummaryEmphasis, double) key = (run.Mono, run.Emphasis, (double)size);
        if (!Faces.TryGetValue(key, out NSFont? face))
        {
            face = Face(run, size);
            Faces[key] = face;
        }

        return face;
    }

    // Bold and italic go on through the font MANAGER's trait conversion rather than a named face:
    // "Segoe UI Bold" has no macOS equivalent, and the system font is not addressable by family name
    // at all on recent versions. Mono asks for the monospaced system face.
    private static NSFont Face(SummaryRun run, nfloat size)
    {
        // All nullable by the bindings and none can answer null: the system font at a valid size
        // always resolves. Coalescing keeps the summary painted rather than throwing inside an
        // Apply that runs from a poll tick and would take the meeting card down. Resolved lazily, so
        // a mono or bold run does not pay for a plain face it discards.
        if (run.Mono)
            return NSFont.MonospacedSystemFont(size, NSFontWeight.Regular) ?? Fallback(size);

        NSFont font = run.Emphasis.HasFlag(SummaryEmphasis.Bold)
            ? NSFont.BoldSystemFontOfSize(size) ?? Fallback(size)
            : Fallback(size);

        if (!run.Emphasis.HasFlag(SummaryEmphasis.Italic))
            return font;

        // Italic is a trait conversion because there is no BoldItalicSystemFont, and the converted
        // font is what carries BOTH in an emphasised word inside a heading. ConvertFont answers the
        // ORIGINAL font when the family has no italic face, which is the right degradation.
        return NSFontManager.SharedFontManager.ConvertFont(font, NSFontTraitMask.Italic) ?? font;
    }

    private static NSFont Fallback(nfloat size) => NSFont.SystemFontOfSize(size) ?? NSFont.UserFontOfSize(size)!;
}
