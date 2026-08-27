using System.Security;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The description CoreAudio is handed to build the private aggregate device that carries one
/// process tap's audio (#420).
///
/// macOS has no loopback endpoint. System audio is a process tap, and a tap is not a device: it
/// becomes one an IOProc can run over only once it is listed inside an aggregate device bound to
/// the output endpoint the Mac is playing through. That aggregate is described by a CFDictionary of
/// CFStrings, CFNumbers and nested CFArrays.
///
/// Built here as a property list, the same dictionary spelled as text, rather than assembled
/// CFDictionaryCreate call by call:
///
/// <list type="bullet">
/// <item>It is REACHABLE FROM A TEST. Every key is a four-letter string CoreAudio matches exactly
/// and ignores silently otherwise: a misspelled "subdevices" produces an aggregate carrying no tap,
/// which is a meeting whose far side records as silence. Assembled through the CF calls it would
/// sit inside <see cref="CoreAudioHal"/>, which by design nothing on any lane executes.</item>
/// <item>It costs no second copy of the CF-collection scaffolding: no create calls, no kCFType
/// callback tables. The HAL parses this with two calls and holds one CF object.</item>
/// </list>
///
/// The value TYPES are as much of the contract as the keys: CoreAudio documents every flag below as
/// a CFNumber, and a plist <c>&lt;true/&gt;</c> parses to a CFBoolean it does not read. So the flags
/// are written as integers, and a test pins that they stay that way.
/// </summary>
public static class CoreAudioAggregateDescription
{
    /// <summary>The description of one tap-carrying aggregate device.</summary>
    /// <param name="aggregateUid">The UID the aggregate is published under. Must be unique per
    /// live aggregate: a second one claiming a UID that is still registered is refused, and a
    /// process killed mid-meeting can leave its own behind until CoreAudio reaps it, so the
    /// caller mints a fresh one per bind rather than naming a constant.</param>
    /// <param name="outputDeviceUid">The output endpoint the aggregate wraps: its clock source
    /// and its only sub-device. System audio is by definition what the Mac is playing, so this
    /// is the current default output.</param>
    /// <param name="tapUid">The tap object's own UID, as <c>kAudioTapPropertyUID</c> reports
    /// it.</param>
    /// <returns>The description, as property-list XML for the HAL to parse.</returns>
    public static string Plist(string aggregateUid, string outputDeviceUid, string tapUid)
    {
        ArgumentNullException.ThrowIfNull(aggregateUid);
        ArgumentNullException.ThrowIfNull(outputDeviceUid);
        ArgumentNullException.ThrowIfNull(tapUid);

        // Escaped because none of the three is a string this code chose: a device UID is a
        // vendor value, and one carrying & or < turns a valid description into a parse
        // failure, i.e. system audio that silently never records on that operator's Mac only.
        string aggregate = Escape(aggregateUid);
        string output = Escape(outputDeviceUid);
        string tap = Escape(tapUid);

        // No DOCTYPE: CFPropertyList parses the document without one, and the declaration
        // would name a URL fetched from a machine that may have no network.
        return "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            + "<plist version=\"1.0\"><dict>"
            + $"<key>{NameKey}</key><string>{AggregateName}</string>"
            + $"<key>{UidKey}</key><string>{aggregate}</string>"
            + $"<key>{MainSubDeviceKey}</key><string>{output}</string>"
            + $"<key>{IsPrivateKey}</key><integer>1</integer>"
            + $"<key>{IsStackedKey}</key><integer>0</integer>"
            + $"<key>{TapAutoStartKey}</key><integer>1</integer>"
            + $"<key>{SubDeviceListKey}</key><array><dict>"
            + $"<key>{UidKey}</key><string>{output}</string></dict></array>"
            + $"<key>{TapListKey}</key><array><dict>"
            + $"<key>{UidKey}</key><string>{tap}</string>"
            + $"<key>{DriftCompensationKey}</key><integer>1</integer></dict></array>"
            + "</dict></plist>";
    }

    /// <summary>What the aggregate calls itself. It is private, so this is never a picker label:
    /// it is what Audio MIDI Setup and a CoreAudio log show, and it wants to say which app to
    /// quit.</summary>
    private const string AggregateName = "TapScribe system audio";

    // CoreAudio's aggregate-description keys, as the header spells them. Each is matched exactly
    // and ignored silently otherwise, hence named constants with the header's symbol beside them.
    private const string NameKey = "name";                  // kAudioAggregateDeviceNameKey
    private const string UidKey = "uid";                    // kAudioAggregateDeviceUIDKey, and
                                                            // kAudioSubDeviceUIDKey / kAudioSubTapUIDKey
    private const string MainSubDeviceKey = "master";       // kAudioAggregateDeviceMainSubDeviceKey
    private const string IsPrivateKey = "private";          // kAudioAggregateDeviceIsPrivateKey
    private const string IsStackedKey = "stacked";          // kAudioAggregateDeviceIsStackedKey
    private const string TapAutoStartKey = "tapautostart";  // kAudioAggregateDeviceTapAutoStartKey
    private const string SubDeviceListKey = "subdevices";   // kAudioAggregateDeviceSubDeviceListKey
    private const string TapListKey = "taps";               // kAudioAggregateDeviceTapListKey
    private const string DriftCompensationKey = "drift";    // kAudioSubTapDriftCompensationKey

    // SecurityElement's escape rather than a hand-rolled one: it covers the five XML entities
    // and is already the framework's answer to this question. Non-null for a non-null input.
    private static string Escape(string value) => SecurityElement.Escape(value) ?? "";
}
