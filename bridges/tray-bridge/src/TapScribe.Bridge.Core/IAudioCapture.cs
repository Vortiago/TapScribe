using System.Runtime.InteropServices;

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
///
/// <see cref="IDisposable.Dispose"/> releases the endpoint and MUST NOT THROW: every
/// teardown path reaches it from a finally or from the tray's bounded Quit, so a throw
/// there strands the device for the process lifetime. It is NOT required to be idempotent
/// - exactly one owner releases a capture, which is why the tests count the releases.
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

    /// <summary>
    /// Raised when capture ends unexpectedly mid-stream — the endpoint was invalidated
    /// (unplugged/disabled/default-device switch) after <see cref="Start"/>, so
    /// <see cref="DataAvailable"/> silently stops. Carries the failure exception, or
    /// <c>null</c> for a clean stop (which is NOT a failure). Lets the pipeline surface
    /// "microphone lost — audio not being captured" instead of going quietly dead.
    /// May fire on an arbitrary thread. A backend that can't detect mid-stream loss
    /// never raises it.
    /// </summary>
    event EventHandler<Exception?>? Failed;

    /// <summary>Begin capturing. <see cref="DataAvailable"/> fires until <see cref="Stop"/>.</summary>
    /// <exception cref="ExternalException">The platform refused to start the endpoint. This is
    /// the seam's declared failure type for a native/driver error (Windows' <c>COMException</c>
    /// derives from it), and a backend must not leak a platform-specific exception type above
    /// the seam: <see cref="CaptureOrchestrator.StartAll"/> filters on this one to skip a dead
    /// device without sinking the meeting.</exception>
    /// <exception cref="InvalidOperationException">The device is already started, or
    /// closed.</exception>
    void Start();

    /// <summary>Stop capturing. Safe to call when not started.</summary>
    /// <exception cref="ExternalException">The endpoint was invalidated while capture ran
    /// (unplugged / disabled / default-device switch), so there is nothing left to stop. Same
    /// declared type as <see cref="Start"/>: teardown swallows it and releases the device
    /// anyway, so a backend that raises anything else strands the endpoint.</exception>
    void Stop();
}
