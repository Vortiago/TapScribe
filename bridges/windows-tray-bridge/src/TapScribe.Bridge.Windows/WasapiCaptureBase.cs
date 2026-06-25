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

    // The endpoint volume we observe for mute, or null when this backend has no mute to
    // honour (loopback render endpoints, and the default-mic ctor that holds no MMDevice).
    // Cached so a read on the capture thread never re-enters COM; refreshed from the
    // endpoint's OnVolumeNotification callback.
    private readonly AudioEndpointVolume? _endpointVolume;
    private volatile bool _muted;

    public AudioFormat Format { get; }

    public event EventHandler<AudioCapturedEventArgs>? DataAvailable;

    /// <summary>True while the observed CAPTURE endpoint is muted at the OS level. Always
    /// false for a backend constructed without a mute source (loopback / default mic) —
    /// there the level gate remains the only mute (#159).</summary>
    public bool IsMuted => _muted;

    public event EventHandler? MuteChanged;

    /// <summary>Wrap an already-constructed WASAPI capture (the subclass picks the
    /// endpoint/mode). The WaveFormat is read eagerly, so an unsupported mix format
    /// surfaces from construction — the caller builds the capture before streaming.
    /// <paramref name="muteSource"/> is the MMDevice whose endpoint mute is honoured
    /// (the mic itself); pass null for a loopback endpoint (no mute event) or when no
    /// MMDevice is held. The endpoint volume is observed for the instance's lifetime and
    /// released in <see cref="Dispose"/>.</summary>
    protected WasapiCaptureBase(WasapiCapture capture, MMDevice? muteSource = null)
    {
        ArgumentNullException.ThrowIfNull(capture);
        _capture = capture;
        Format = ToAudioFormat(capture.WaveFormat);
        _capture.DataAvailable += OnDataAvailable;

        if (muteSource is not null)
        {
            try
            {
                _endpointVolume = muteSource.AudioEndpointVolume;
                // Subscribe BEFORE reading the initial Mute so a toggle during construction
                // isn't lost in the gap; the seed then reads the reconciled current state.
                _endpointVolume.OnVolumeNotification += OnVolumeNotification;
                _muted = _endpointVolume.Mute;
            }
            catch (COMException)
            {
                // The endpoint doesn't expose volume/mute control (some virtual or
                // capture-only devices don't). Degrade to no mute awareness — exactly
                // today's level-only behaviour — rather than failing the whole capture:
                // the mic still records, it just can't honour an OS mute. _endpointVolume
                // stays null so Dispose has nothing to unsubscribe.
                _endpointVolume = null;
            }
        }
    }

    // Fires on a WASAPI volume-notification (COM) thread on any volume OR mute change;
    // forward only true mute transitions so a volume tweak doesn't churn the pipeline.
    private void OnVolumeNotification(AudioVolumeNotificationData data)
    {
        if (data.Muted == _muted)
            return;
        _muted = data.Muted;
        MuteChanged?.Invoke(this, EventArgs.Empty);
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
        if (_endpointVolume is not null)
        {
            try
            {
                _endpointVolume.OnVolumeNotification -= OnVolumeNotification;
                // Dispose the endpoint-volume COM wrapper too: it owns the native
                // RegisterControlChangeNotify registration we made, which detaching the
                // managed handler alone does NOT release. Without this the tray (a
                // long-lived process opening a fresh capture per meeting / meter refresh)
                // leaks an endpoint-volume callback every cycle.
                _endpointVolume.Dispose();
            }
            catch (COMException)
            {
                // The endpoint was invalidated/removed (unplugged, disabled): unsubscribing
                // from or releasing its volume callback can throw the same
                // AUDCLNT_E_DEVICE_INVALIDATED as the capture teardown below. Nothing left to
                // detach; disposal proceeds.
            }
        }
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
