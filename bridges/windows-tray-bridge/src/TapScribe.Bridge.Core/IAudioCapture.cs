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

    /// <summary>Begin capturing. <see cref="DataAvailable"/> fires until <see cref="Stop"/>.</summary>
    void Start();

    /// <summary>Stop capturing. Safe to call when not started.</summary>
    void Stop();
}
