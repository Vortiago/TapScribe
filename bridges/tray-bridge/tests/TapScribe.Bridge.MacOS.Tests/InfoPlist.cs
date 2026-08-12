using System.Xml;
using System.Xml.Linq;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// The macOS shell app's <c>Info.plist</c>, read out of the working tree rather than from a
/// copy, so the tests over it gate the very file the <c>.app</c> ships. A plist dict is a
/// flat alternation of a <c>&lt;key&gt;</c> and its value element; this flattens that into
/// a lookup and leaves reading the value to each test, since the shapes differ (a
/// <c>&lt;true/&gt;</c> flag is an element NAME, a string is element text).
/// </summary>
internal static class InfoPlist
{
    private static readonly Lazy<IReadOnlyDictionary<string, XElement>> Lazily = new(Load);

    internal static IReadOnlyDictionary<string, XElement> Entries => Lazily.Value;

    /// <summary>A boolean plist entry, which is spelled as an empty element named
    /// <c>true</c> or <c>false</c> rather than as text.</summary>
    internal static bool Flag(string key) => Entries[key].Name.LocalName == "true";

    private static IReadOnlyDictionary<string, XElement> Load()
    {
        // DtdProcessing.Ignore, never Parse: the plist carries Apple's DOCTYPE, and
        // resolving it would mean an outbound fetch and an XXE surface to read a file whose
        // grammar we do not need.
        var settings = new XmlReaderSettings { DtdProcessing = DtdProcessing.Ignore };
        using XmlReader reader = XmlReader.Create(ShellProject.InfoPlistPath, settings);

        XElement dict = XDocument.Load(reader).Root!.Element("dict")!;
        XElement[] children = [.. dict.Elements()];

        var entries = new Dictionary<string, XElement>(StringComparer.Ordinal);
        for (int i = 0; i + 1 < children.Length; i += 2)
            entries[children[i].Value] = children[i + 1];
        return entries;
    }
}
