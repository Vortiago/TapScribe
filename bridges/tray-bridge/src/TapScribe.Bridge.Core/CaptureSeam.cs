using System.Runtime.InteropServices;

namespace TapScribe.Bridge.Core;

/// <summary>
/// The capture seam's declared failure set, in one place.
///
/// <see cref="IAudioDeviceEnumerator.Open"/> documents four exceptions and
/// <see cref="IAudioCapture.Start"/> a subset of the same four. Every caller that survives a
/// device it could not open filters on that set, and naming it by hand is how a caller ends up
/// naming a SUBSET of it: the Settings level meter listed two, and the other two escaped a
/// click handler, which on AppKit ends the tray.
///
/// A catch-all is the wrong repair for that. It also swallows the exceptions a device cannot
/// produce (a null dereference, a bad cast), turning a mistake a developer would meet in
/// testing into a dead level bar an operator cannot act on. Naming the set is right; naming it
/// four separate times is what was wrong.
/// </summary>
public static class CaptureSeam
{
    /// <summary>Whether <paramref name="ex"/> is one of the failures the capture seam declares,
    /// and therefore something a caller may report and carry on from.</summary>
    /// <remarks>Platform types that DERIVE from a declared one are included, which is the point
    /// of declaring <see cref="ExternalException"/> rather than a concrete type: Windows'
    /// <c>COMException</c> and the Mac layer's <c>CoreAudioException</c> both arrive as one.
    /// </remarks>
    public static bool IsDeclaredFailure(Exception ex) =>
        ex is ExternalException or NotSupportedException or InvalidOperationException or ArgumentException;
}
