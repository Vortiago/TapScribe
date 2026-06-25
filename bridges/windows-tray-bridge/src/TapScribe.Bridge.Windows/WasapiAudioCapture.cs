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
    /// <summary>Capture the default microphone (shared mode). No MMDevice handle is held,
    /// so this overload can't observe the endpoint mute — the tray always opens a specific
    /// endpoint via the enumerator, which is the mute-aware path below.</summary>
    public WasapiAudioCapture() : base(new WasapiCapture()) { }

    /// <summary>Capture a specific microphone endpoint (from the enumerator). The base owns
    /// the device and observes its endpoint mute, so an OS-level mute on this mic stops it
    /// being recorded (#159) instead of streaming a residual the level gate would tap.</summary>
    public WasapiAudioCapture(MMDevice device) : base(new WasapiCapture(device), device, observeMute: true) { }
}
