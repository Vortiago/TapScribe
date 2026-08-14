using System.Runtime.InteropServices;

namespace TapScribe.Bridge.Core;

/// <summary>
/// Lists the audio endpoints the bridge can tap and opens one as an
/// <see cref="IAudioCapture"/>. The cross-platform core depends only on this seam;
/// the Windows implementation (over NAudio <c>MMDeviceEnumerator</c>) lists capture
/// devices and loopback-capable render devices and maps each
/// <see cref="CaptureDevice"/> back to a WASAPI backend. A future macOS/Linux
/// backend implements the same interface. Tests use a fake that hands out
/// <c>FakeAudioCapture</c>s, so the multi-pipeline orchestration is exercised with
/// no real audio hardware.
///
/// <see cref="IDisposable"/> is part of the seam because every real backend holds
/// native handles for the device tree it walks. Disposal releases those handles, and
/// nothing else: the captures it handed out have their own owners. An enumerator hands
/// its endpoint over to each capture it opens, so it must OUTLIVE them - dispose the
/// captures first, then the enumerator. Same contract as
/// <see cref="IAudioCapture"/>'s release: it must not throw, since every caller reaches
/// it from a finally that has no other owner to fall back on.
/// </summary>
public interface IAudioDeviceEnumerator : IDisposable
{
    /// <summary>
    /// The active endpoints: both capture devices (mics) and loopback-capable render
    /// devices, each carrying its <see cref="DeviceFlow"/> and default flag.
    /// </summary>
    /// <exception cref="ExternalException">The platform could not walk the device tree (no
    /// audio service, a driver error). Same declared type as <see cref="Open"/>: both callers
    /// that survive a failed enumeration filter on it, and an empty list means "no endpoints",
    /// never "the question could not be asked".</exception>
    IReadOnlyList<CaptureDevice> List();

    /// <summary>
    /// Open <paramref name="device"/> as a capture source — a mic capture for
    /// <see cref="DeviceFlow.Capture"/>, a loopback capture for
    /// <see cref="DeviceFlow.Render"/>. The returned capture is not started; the
    /// caller (a <see cref="TapSession"/>) takes ownership and disposes it.
    /// </summary>
    /// <exception cref="ExternalException">The platform refused the endpoint (in use,
    /// invalidated). This is the seam's declared failure type for a native/driver error -
    /// Windows' <c>COMException</c> derives from it - and a backend must not leak a
    /// platform-specific exception type above the seam, since every caller that skips a dead
    /// device filters on this one.</exception>
    /// <exception cref="NotSupportedException">The endpoint offers no format the pipeline can
    /// take. Declared separately from the native failure because it is not one: the device
    /// answered, and the answer was unusable.</exception>
    /// <exception cref="InvalidOperationException">The endpoint cannot be opened in its
    /// current state.</exception>
    /// <exception cref="ArgumentException">The id names no active endpoint of the
    /// requested flow.</exception>
    IAudioCapture Open(CaptureDevice device);
}
