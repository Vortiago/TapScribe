using System.Runtime.InteropServices;

namespace TapScribe.Bridge.Core;

/// <summary>
/// The capture seam's declared failure set, in one place.
///
/// <see cref="IAudioDeviceEnumerator.Open"/> documents four exceptions; every caller that
/// survives an unopenable device filters on them. Listing them per call site is how a caller
/// ends up listing a subset, which is what happened. A catch-all is the wrong repair: it also
/// swallows what no device can produce, hiding a bug behind an operator-facing message.
/// </summary>
public static class CaptureSeam
{
    /// <summary>Whether <paramref name="ex"/> is one of the failures the capture seam declares,
    /// and therefore something a caller may report and carry on from.</summary>
    /// <remarks>Derived platform types count, which is why the seam declares
    /// <see cref="ExternalException"/>: <c>COMException</c> and <c>CoreAudioException</c> are
    /// both one.</remarks>
    public static bool IsDeclaredFailure(Exception ex) =>
        ex is ExternalException or NotSupportedException or InvalidOperationException or ArgumentException;
}
