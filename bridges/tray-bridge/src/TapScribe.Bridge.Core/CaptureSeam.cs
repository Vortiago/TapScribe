using System.Runtime.InteropServices;

namespace TapScribe.Bridge.Core;

/// <summary>
/// The capture seam's declared failure sets, in one place.
///
/// There are two, and they are not the same size. Listing either per call site is how a caller
/// ends up listing a subset; picking the wrong one is how a caller ends up swallowing what no
/// device can produce. A catch-all is the wrong repair for both: it hides a bug behind an
/// operator-facing message.
///
/// <list type="bullet">
/// <item><see cref="IsDeclaredOpenFailure"/> covers <see cref="IAudioDeviceEnumerator.Open"/>:
/// four types, because opening a device can answer "no format I can take" or "no such
/// endpoint".</item>
/// <item><see cref="IsDeclaredCaptureFailure"/> covers <see cref="IAudioCapture.Start"/> and
/// <see cref="IAudioCapture.Stop"/>: two types. A capture that already holds an endpoint
/// cannot raise the other two, so a teardown filtering on the open set would swallow a bug.
/// </item>
/// </list>
///
/// <see cref="IAudioDeviceEnumerator.List"/> gets no predicate on purpose. It declares exactly
/// one type, so <c>catch (ExternalException)</c> already names the whole set and reads better
/// than a call; a predicate there could only widen it.
/// </summary>
public static class CaptureSeam
{
    /// <summary>Whether <paramref name="ex"/> is one of the failures
    /// <see cref="IAudioDeviceEnumerator"/> declares, and therefore something a caller may
    /// report and carry on from.</summary>
    /// <remarks>Derived platform types count, which is why the seam declares
    /// <see cref="ExternalException"/>: <c>COMException</c> and <c>CoreAudioException</c> are
    /// both one.</remarks>
    public static bool IsDeclaredOpenFailure(Exception ex) =>
        ex is ExternalException or NotSupportedException or InvalidOperationException or ArgumentException;

    /// <summary>Whether <paramref name="ex"/> is one of the failures
    /// <see cref="IAudioCapture.Start"/> and <see cref="IAudioCapture.Stop"/> declare.</summary>
    /// <remarks>The narrower of the two sets. Teardown paths filter on this one: they must
    /// release the endpoint over a device that went away mid-capture, and must not go quiet
    /// over anything else.</remarks>
    public static bool IsDeclaredCaptureFailure(Exception ex) =>
        ex is ExternalException or InvalidOperationException;
}
