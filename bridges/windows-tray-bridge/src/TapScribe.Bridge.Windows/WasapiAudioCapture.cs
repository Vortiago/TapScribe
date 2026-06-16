using NAudio.CoreAudioApi;

namespace TapScribe.Bridge.Windows;

/// <summary>
/// <see cref="TapScribe.Bridge.Core.IAudioCapture"/> over a WASAPI capture device (a
/// microphone) in shared mode — the default endpoint, or a specific one resolved by
/// <see cref="WasapiDeviceEnumerator"/> so two pipelines can target two mics. The
/// shared WaveFormat normalisation and lifecycle live in <see cref="WasapiCaptureBase"/>;
/// the system-audio sibling is <see cref="WasapiLoopbackAudioCapture"/>.
/// </summary>
public sealed class WasapiAudioCapture : WasapiCaptureBase
{
    /// <summary>Capture the default microphone (shared mode).</summary>
    public WasapiAudioCapture() : base(new WasapiCapture()) { }

    /// <summary>Capture a specific microphone endpoint (from the enumerator).</summary>
    public WasapiAudioCapture(MMDevice device) : base(new WasapiCapture(device)) { }
}
