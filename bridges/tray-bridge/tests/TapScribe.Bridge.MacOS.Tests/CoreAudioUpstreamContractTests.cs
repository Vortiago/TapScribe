using System.Runtime.InteropServices;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// Pins every native symbol <see cref="CoreAudioHal"/> binds (#419), the CoreAudio sibling of
/// the Windows layer's <c>WasapiUpstreamContractTests</c>.
///
/// The facade is a dumb passthrough on purpose, so every decision above it is unit-tested
/// against a fake and NOTHING exercises these declarations. A mistyped entry point therefore
/// compiles, ships, and fails on an operator's Mac the first time they start a meeting. This
/// resolves each one against the real framework instead, which is the only check standing
/// between a typo and that.
///
/// It asks dyld whether the symbol exists rather than calling it: resolution is the whole
/// claim, and calling would need a real device, an IOProc and a microphone grant.
/// </summary>
public class CoreAudioUpstreamContractTests
{
    private const string CoreAudio = "/System/Library/Frameworks/CoreAudio.framework/CoreAudio";
    private const string CoreFoundation =
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation";
    private const string ObjC = "/usr/lib/libobjc.A.dylib";

    private static readonly (string Framework, string Symbol)[] Bound =
    [
        // The property walk: ask the size, read the value, ask whether a device carries the
        // property at all (the no-mute case), and watch it for changes.
        (CoreAudio, "AudioObjectGetPropertyDataSize"),
        (CoreAudio, "AudioObjectGetPropertyData"),
        (CoreAudio, "AudioObjectHasProperty"),
        (CoreAudio, "AudioObjectAddPropertyListener"),
        (CoreAudio, "AudioObjectRemovePropertyListener"),
        // The IOProc lifecycle, which is the capture itself.
        (CoreAudio, "AudioDeviceCreateIOProcID"),
        (CoreAudio, "AudioDeviceDestroyIOProcID"),
        (CoreAudio, "AudioDeviceStart"),
        (CoreAudio, "AudioDeviceStop"),
        // A device name arrives as a CFStringRef, so reading and releasing one is part of
        // listing.
        (CoreFoundation, "CFStringGetCString"),
        (CoreFoundation, "CFRelease"),
        // The system-audio process tap (#420): the tap object, and the private aggregate
        // device that carries its audio to an IOProc.
        (CoreAudio, "AudioHardwareCreateProcessTap"),
        (CoreAudio, "AudioHardwareDestroyProcessTap"),
        (CoreAudio, "AudioHardwareCreateAggregateDevice"),
        (CoreAudio, "AudioHardwareDestroyAggregateDevice"),
        // CATapDescription is the one ObjC class in the whole backend and is NOT bound by
        // Microsoft.macOS, which carries no CoreAudio namespace at all. It is reached through
        // the ObjC runtime's own C entry points rather than through a hand-written NSObject
        // binding: constructing any NSObject-derived type under the test host faults inside
        // ObjCRuntime, so a binding would make this class untestable even for its symbols.
        // Two objc_msgSend declarations, because the ABI needs the real signature at each
        // call: one for the no-argument selectors and one for the initialiser's array.
        (ObjC, "objc_getClass"),
        (ObjC, "sel_registerName"),
        (ObjC, "objc_msgSend"),
        (ObjC, "objc_msgSend"),
        // An empty NSArray for that initialiser (toll-free bridged), and the aggregate's
        // description, which is parsed from a property list rather than assembled call by
        // call - see CoreAudioAggregateDescription for why.
        (CoreFoundation, "CFArrayCreate"),
        (CoreFoundation, "CFDataCreate"),
        (CoreFoundation, "CFPropertyListCreateWithData"),
    ];

    // One fact over the whole set rather than a case each, so a run reports EVERY symbol that
    // moved. A rename usually arrives as a family, and learning them one CI round at a time
    // is the slow way to find that out.
    [RequiresMacOS("resolve symbols in a macOS framework")]
    public void EverySymbolTheHalBinds_ResolvesInItsFramework()
    {
        List<string> missing = [];
        // Distinct, because the table lists one row per DECLARATION and objc_msgSend is
        // declared twice under two signatures: dyld has one answer for it either way.
        foreach ((string framework, string symbol) in Bound.Distinct())
        {
            Assert.True(NativeLibrary.TryLoad(framework, out IntPtr handle), $"could not load {framework}");
            try
            {
                if (!NativeLibrary.TryGetExport(handle, symbol, out _))
                    missing.Add($"{symbol} (in {framework})");
            }
            finally
            {
                NativeLibrary.Free(handle);
            }
        }

        Assert.True(missing.Count == 0, $"no longer exported: {string.Join(", ", missing)}");
    }

    [RequiresMacOS("create a real Core Audio process tap")]
    public void TwoProcessTaps_CanBeLiveAtOnce()
    {
        // The one claim about this backend that symbol resolution cannot make: that the calls
        // WORK, not merely that they exist. Creating a tap goes through CATapDescription via
        // objc_msgSend against a class Microsoft.macOS does not bind, so a rename or an ABI
        // mistake in that path is invisible to every other test here and shows up as a meeting
        // that records one speaker.
        //
        // Two of them, because the system-audio level meter would open a second while a meeting
        // holds the first, and nothing else establishes that the OS permits it. Measured
        // PERMITTED on macOS 26.4; if a future release refuses, this is where that is learned.
        //
        // Deliberately does NOT run an IOProc: reading audio through a tap needs the Audio
        // Capture grant, which no CI runner can answer. Creating one does not, which is what
        // makes this runnable unattended.
        if (!OperatingSystem.IsMacOS())
            return;   // unreachable: [RequiresMacOS] skips first. Here for CA1416 only.

        using var hal = new CoreAudioHal();
        CoreAudioTapHandle first = hal.CreateProcessTap();
        try
        {
            CoreAudioTapHandle second = hal.CreateProcessTap();
            hal.DestroyProcessTap(second);
        }
        finally
        {
            hal.DestroyProcessTap(first);
        }
    }

    [Fact]
    public void TheBoundSet_MatchesWhatTheHalActuallyDeclares()
    {
        // The list above is hand-written, so on its own it pins whatever someone remembered
        // to add. This counts the real [LibraryImport] declarations by reflection and makes
        // the two agree, which is what stops a twelfth import from riding in unpinned. It
        // runs on every lane, because it is about this assembly rather than about the OS.
        int declared = typeof(CoreAudioHal)
            .GetMethods(System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static)
            .Count(m => m.GetCustomAttributes(typeof(DllImportAttribute), false).Length > 0
                || m.Attributes.HasFlag(System.Reflection.MethodAttributes.PinvokeImpl));

        Assert.Equal(Bound.Length, declared);
    }
}
