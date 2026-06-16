using NAudio.CoreAudioApi;
using NAudio.Wave;

namespace TapScribe.Bridge.Windows;

/// <summary>
/// <see cref="TapScribe.Bridge.Core.IAudioCapture"/> over WASAPI loopback — it records
/// a RENDER endpoint's output mix (the system audio, the "other side" of a meeting) as
/// if it were a capture device. Behind the same interface as the mic backend, so the
/// core sees only another capture source; the loopback mix (typically 48 kHz stereo
/// float) is converted to the 16 kHz mono int16 wire format by the core
/// <c>Resampler</c> like any other source. The level gate is the bridge-side Mute,
/// since a render endpoint has no mute event.
/// </summary>
public sealed class WasapiLoopbackAudioCapture : WasapiCaptureBase
{
    /// <summary>Loopback over the default render endpoint (the system default speakers).</summary>
    public WasapiLoopbackAudioCapture() : base(new WasapiLoopbackCapture()) { }

    /// <summary>Loopback over a specific render endpoint (from the enumerator).</summary>
    public WasapiLoopbackAudioCapture(MMDevice renderDevice) : base(new WasapiLoopbackCapture(renderDevice)) { }
}
