namespace TapScribe.Bridge.Core;

/// <summary>Carries a chunk of raw, device-format PCM from a capture device.</summary>
public sealed class AudioCapturedEventArgs : EventArgs
{
    public AudioCapturedEventArgs(ReadOnlyMemory<byte> data) => Data = data;

    /// <summary>
    /// Raw bytes in the device's <see cref="IAudioCapture.Format"/>. The buffer
    /// may be reused by the capture backend after the handler returns, so a
    /// handler that needs to retain the bytes must copy them. The reference
    /// pipeline consumes them synchronously (resample -> chunk -> enqueue).
    /// </summary>
    public ReadOnlyMemory<byte> Data { get; }
}

/// <summary>
/// The capture seam. The cross-platform core depends only on this interface;
/// WASAPI (Windows) is one implementation, kept in the Windows project so the
/// core takes no platform dependency. A future macOS/Linux backend implements
/// the same interface.
/// </summary>
public interface IAudioCapture : IDisposable
{
    /// <summary>The device's native format. Valid once the instance is constructed.</summary>
    AudioFormat Format { get; }

    /// <summary>Raised on the capture thread with device-format PCM as it arrives.</summary>
    event EventHandler<AudioCapturedEventArgs>? DataAvailable;

    /// <summary>
    /// Whether this endpoint is muted at the OS/device level right now. A muted
    /// CAPTURE (mic) endpoint still delivers <see cref="DataAvailable"/> frames — a
    /// residual noise floor, a DC offset, periodic device blips — so the
    /// <see cref="LevelGate"/> alone cannot tell "muted" from "quiet" and will
    /// occasionally open a tap on a transient: the recurring "quiet" tap of issue
    /// #159. Honouring this turns "muted" into a hard gate-closed, independent of
    /// level. A RENDER (loopback) endpoint has no mute event and reports
    /// <c>false</c> permanently — there the level gate IS the mute, by design.
    /// </summary>
    bool IsMuted { get; }

    /// <summary>
    /// Raised when <see cref="IsMuted"/> transitions, so the pipeline can close an
    /// open utterance the instant the mic mutes rather than waiting out the gate's
    /// hangover on whatever residual the device keeps delivering. The handler reads
    /// <see cref="IsMuted"/> for the current state (the event carries no payload, so
    /// the property is the single source of truth). May fire on an arbitrary thread.
    /// A loopback backend never raises it.
    /// </summary>
    event EventHandler? MuteChanged;

    /// <summary>Begin capturing. <see cref="DataAvailable"/> fires until <see cref="Stop"/>.</summary>
    void Start();

    /// <summary>Stop capturing. Safe to call when not started.</summary>
    void Stop();
}
