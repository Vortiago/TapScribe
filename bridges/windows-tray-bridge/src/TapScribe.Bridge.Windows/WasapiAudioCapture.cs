using System.Runtime.InteropServices;
using NAudio.CoreAudioApi;
using NAudio.Wave;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Windows;

/// <summary>
/// <see cref="IAudioCapture"/> over the default WASAPI capture device (the
/// default microphone), in shared mode. Hands the core raw device-format bytes;
/// the core <see cref="Resampler"/> does the conversion to the wire format.
///
/// System-audio loopback capture (the "other side" of a meeting) is a later PRD
/// slice and would be a sibling backend (WasapiLoopbackCapture) behind the same
/// interface.
/// </summary>
public sealed class WasapiAudioCapture : IAudioCapture
{
    private readonly WasapiCapture _capture;

    public AudioFormat Format { get; }

    public event EventHandler<AudioCapturedEventArgs>? DataAvailable;

    public WasapiAudioCapture()
    {
        _capture = new WasapiCapture(); // default capture endpoint, shared mode

        // Shared-mode capture usually reports a WaveFormatExtensible; reduce it
        // to a plain WaveFormat so we can classify the sample encoding. The raw
        // bytes are unchanged — only the label is normalised.
        WaveFormat wf = _capture.WaveFormat;
        WaveFormat standard = wf is WaveFormatExtensible ext ? ext.ToStandardWaveFormat() : wf;

        SampleKind kind = standard.Encoding switch
        {
            WaveFormatEncoding.IeeeFloat => SampleKind.Float32,
            WaveFormatEncoding.Pcm when standard.BitsPerSample == 16 => SampleKind.Int16,
            _ => throw new NotSupportedException(
                $"Unsupported capture format: {standard.Encoding} {standard.BitsPerSample}-bit. " +
                "Expected 32-bit float or 16-bit PCM (the usual WASAPI shared-mode mix formats)."),
        };

        Format = new AudioFormat(standard.SampleRate, standard.Channels, kind);
        _capture.DataAvailable += OnDataAvailable;
    }

    private void OnDataAvailable(object? sender, WaveInEventArgs e) =>
        DataAvailable?.Invoke(this, new AudioCapturedEventArgs(e.Buffer.AsMemory(0, e.BytesRecorded)));

    public void Start() => _capture.StartRecording();

    public void Stop()
    {
        try
        {
            _capture.StopRecording();
        }
        catch (COMException)
        {
            // The device was invalidated/removed mid-tap (mic unplugged or
            // disabled): AUDCLNT_E_DEVICE_INVALIDATED surfaces as a COMException
            // from the WASAPI client. There's nothing left to stop and nothing
            // the caller can do; teardown proceeds. Stop is documented safe.
        }
    }

    public void Dispose()
    {
        _capture.DataAvailable -= OnDataAvailable;
        try
        {
            _capture.Dispose();
        }
        catch (COMException)
        {
            // Same device-invalidation case as Stop(): releasing the WASAPI
            // client for a removed device can throw, but we're disposing anyway.
        }
    }
}
