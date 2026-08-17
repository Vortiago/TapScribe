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
    ];

    // One fact over the whole set rather than a case each, so a run reports EVERY symbol that
    // moved. A rename usually arrives as a family, and learning them one CI round at a time
    // is the slow way to find that out.
    [RequiresMacOS("resolve symbols in a macOS framework")]
    public void EverySymbolTheHalBinds_ResolvesInItsFramework()
    {
        List<string> missing = [];
        foreach ((string framework, string symbol) in Bound)
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
