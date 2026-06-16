using System.Runtime.InteropServices;
using NAudio.CoreAudioApi;
using NAudio.Wave;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Windows;

/// <summary>
/// Shared <see cref="IAudioCapture"/> plumbing for the WASAPI backends. It owns one
/// NAudio <see cref="WasapiCapture"/> (a <see cref="WasapiLoopbackCapture"/> is one),
/// classifies its shared-mode WaveFormat into an <see cref="AudioFormat"/>, re-raises
/// device-format PCM, and survives the endpoint being invalidated mid-tap. The mic
/// backend (<see cref="WasapiAudioCapture"/>) and the loopback backend
/// (<see cref="WasapiLoopbackAudioCapture"/>) differ ONLY in which capture they
/// construct, so this is the single normalisation + lifecycle authority — no
/// copy-paste between them.
/// </summary>
public abstract class WasapiCaptureBase : IAudioCapture
{
    private readonly WasapiCapture _capture;

    public AudioFormat Format { get; }

    public event EventHandler<AudioCapturedEventArgs>? DataAvailable;

    /// <summary>Wrap an already-constructed WASAPI capture (the subclass picks the
    /// endpoint/mode). The WaveFormat is read eagerly, so an unsupported mix format
    /// surfaces from construction — the caller builds the capture before streaming.</summary>
    protected WasapiCaptureBase(WasapiCapture capture)
    {
        ArgumentNullException.ThrowIfNull(capture);
        _capture = capture;
        Format = ToAudioFormat(capture.WaveFormat);
        _capture.DataAvailable += OnDataAvailable;
    }

    // Reduce a (usually WaveFormatExtensible) WASAPI shared-mode format to the
    // AudioFormat the core resampler consumes. The raw bytes are unchanged — only the
    // sample encoding is classified.
    private static AudioFormat ToAudioFormat(WaveFormat waveFormat)
    {
        WaveFormat standard = waveFormat is WaveFormatExtensible ext ? ext.ToStandardWaveFormat() : waveFormat;
        SampleKind kind = standard.Encoding switch
        {
            WaveFormatEncoding.IeeeFloat => SampleKind.Float32,
            WaveFormatEncoding.Pcm when standard.BitsPerSample == 16 => SampleKind.Int16,
            _ => throw new NotSupportedException(
                $"Unsupported capture format: {standard.Encoding} {standard.BitsPerSample}-bit. " +
                "Expected 32-bit float or 16-bit PCM (the usual WASAPI shared-mode mix formats)."),
        };
        return new AudioFormat(standard.SampleRate, standard.Channels, kind);
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
            // The endpoint was invalidated/removed mid-tap (unplugged, disabled, or the
            // default render device switched): AUDCLNT_E_DEVICE_INVALIDATED surfaces as a
            // COMException from the WASAPI client. There's nothing left to stop and
            // nothing the caller can do; teardown proceeds. Stop is documented safe.
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
            // Same endpoint-invalidation case as Stop(): releasing the WASAPI client for
            // a removed device can throw, but we're disposing anyway.
        }
        GC.SuppressFinalize(this);
    }
}
