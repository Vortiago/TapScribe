using System.Diagnostics;
using System.Xml;
using System.Xml.Linq;

namespace TapScribe.TrayBridge.MacOS.Tests;

/// <summary>
/// A parsed <c>Info.plist</c>. A plist dict is a flat alternation of a <c>&lt;key&gt;</c> and
/// its value element; this flattens that into a lookup and leaves reading the value to each
/// test, since the shapes differ (a <c>&lt;true/&gt;</c> flag is an element NAME, a string is
/// element text).
/// </summary>
internal sealed class InfoPlist
{
    private static readonly Lazy<InfoPlist> LazyBuilt = new(() => Read(ShellBundle.BuiltManifestPath));
    private static readonly Lazy<InfoPlist> LazySource = new(() => Read(ShellBundle.SourceManifestPath));

    private readonly IReadOnlyDictionary<string, XElement> _entries;

    private InfoPlist(IReadOnlyDictionary<string, XElement> entries) => _entries = entries;

    /// <summary>The manifest inside the built <c>.app</c>.</summary>
    internal static InfoPlist Built => LazyBuilt.Value;

    /// <summary>The manifest as committed.</summary>
    internal static InfoPlist Source => LazySource.Value;

    internal IEnumerable<string> Keys => _entries.Keys;

    internal bool Declares(string key) => _entries.ContainsKey(key);

    /// <summary>The text of a string entry.</summary>
    internal string Text(string key) => _entries[key].Value;

    /// <summary>A boolean entry, which is spelled as an empty element named <c>true</c> or
    /// <c>false</c> rather than as text.</summary>
    internal bool Flag(string key) => _entries[key].Name.LocalName == "true";

    private static InfoPlist Read(string path)
    {
        // DtdProcessing.Ignore, never Parse: the plist carries Apple's DOCTYPE, and
        // resolving it would mean an outbound fetch and an XXE surface to read a file whose
        // grammar we do not need.
        var settings = new XmlReaderSettings { DtdProcessing = DtdProcessing.Ignore };
        using var text = new StringReader(XmlTextOf(path));
        using XmlReader reader = XmlReader.Create(text, settings);

        XElement dict = XDocument.Load(reader).Root!.Element("dict")!;
        XElement[] children = [.. dict.Elements()];

        // The pairing is checked rather than assumed. A <key> whose value element is missing
        // shifts every pair after it, so a deleted permission string would surface as some
        // OTHER key holding a confusing wrong value instead of as the missing key these
        // tests exist to catch.
        if (children.Length % 2 != 0)
            throw new InvalidDataException($"malformed plist {path}: {children.Length} elements in the dict, expected pairs");

        var entries = new Dictionary<string, XElement>(StringComparer.Ordinal);
        for (int i = 0; i < children.Length; i += 2)
        {
            if (children[i].Name.LocalName != "key")
                throw new InvalidDataException($"malformed plist {path}: expected <key> at position {i}, found <{children[i].Name.LocalName}>");

            entries[children[i].Value] = children[i + 1];
        }

        return new InfoPlist(entries);
    }

    // The manifest inside a built .app is a BINARY plist, because the SDK converts it on the
    // way in, so handing the file straight to an XML parser gets bytes. plutil converts it
    // back and is always present on macOS, the only OS this project builds on. The committed
    // manifest is already XML, so a source-only test costs no subprocess.
    private static string XmlTextOf(string path)
    {
        if (!IsBinaryPlist(path))
            return File.ReadAllText(path);

        var plutil = new ProcessStartInfo("plutil")
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
        };

        // argv one element at a time, never a joined string and never through a shell.
        plutil.ArgumentList.Add("-convert");
        plutil.ArgumentList.Add("xml1");
        plutil.ArgumentList.Add("-o");
        plutil.ArgumentList.Add("-");
        plutil.ArgumentList.Add(path);

        using Process process = Process.Start(plutil)
            ?? throw new InvalidOperationException($"could not start plutil to convert {path}");

        // stderr is drained concurrently rather than after the wait: reading stdout to the
        // end first deadlocks if the error pipe fills while nobody is emptying it.
        Task<string> errors = process.StandardError.ReadToEndAsync();
        string xml = process.StandardOutput.ReadToEnd();
        process.WaitForExit();

        return process.ExitCode == 0
            ? xml
            : throw new InvalidDataException($"plutil could not read {path}: {errors.GetAwaiter().GetResult()}");
    }

    private static bool IsBinaryPlist(string path)
    {
        ReadOnlySpan<byte> magic = "bplist00"u8;
        using FileStream file = File.OpenRead(path);

        Span<byte> head = stackalloc byte[magic.Length];
        return file.ReadAtLeast(head, head.Length, throwOnEndOfStream: false) == head.Length
            && head.SequenceEqual(magic);
    }
}
