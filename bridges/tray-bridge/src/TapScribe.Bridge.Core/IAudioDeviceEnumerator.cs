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
/// </summary>
public interface IAudioDeviceEnumerator
{
    /// <summary>
    /// The active endpoints: both capture devices (mics) and loopback-capable render
    /// devices, each carrying its <see cref="DeviceFlow"/> and default flag.
    /// </summary>
    IReadOnlyList<CaptureDevice> List();

    /// <summary>
    /// Open <paramref name="device"/> as a capture source — a mic capture for
    /// <see cref="DeviceFlow.Capture"/>, a loopback capture for
    /// <see cref="DeviceFlow.Render"/>. The returned capture is not started; the
    /// caller (a <see cref="TapSession"/>) takes ownership and disposes it.
    /// </summary>
    IAudioCapture Open(CaptureDevice device);
}
