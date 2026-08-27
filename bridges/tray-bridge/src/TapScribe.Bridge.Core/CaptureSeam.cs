using System.Runtime.InteropServices;

namespace TapScribe.Bridge.Core;

/// <summary>
/// The capture seam's two declared failure sets. Listing either by hand is how a caller ends up
/// with a subset; picking the wrong one is how it swallows what no device can produce.
///
/// <see cref="IAudioDeviceEnumerator.List"/> has no predicate on purpose: it declares one type,
/// so <c>catch (ExternalException)</c> is already the whole set.
/// </summary>
public static class CaptureSeam
{
    /// <summary>The four <see cref="IAudioDeviceEnumerator.Open"/> declares. Wider than the
    /// capture set because opening can answer "no format I can take" or "no such endpoint".
    /// <c>COMException</c> and <c>CoreAudioException</c> arrive through
    /// <see cref="ExternalException"/>, which is why the seam declares the base type.</summary>
    public static bool IsDeclaredOpenFailure(Exception ex) =>
        ex is ExternalException or NotSupportedException or InvalidOperationException or ArgumentException;

    /// <summary>The two <see cref="IAudioCapture.Start"/> and <see cref="IAudioCapture.Stop"/>
    /// declare. Teardown filters on this one: it must release over a device that went away
    /// mid-capture, and must not go quiet over anything else.</summary>
    public static bool IsDeclaredCaptureFailure(Exception ex) =>
        ex is ExternalException or InvalidOperationException;
}
