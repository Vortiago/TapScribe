namespace TapScribe.Bridge.Core;

/// <summary>
/// One <c>/tap</c> WebSocket from <see cref="TapStream"/>'s point of view: connect,
/// send 640-byte PCM frames, close. The concrete implementation is
/// <see cref="TapClient"/>; the seam exists so the reconnect / gap-buffer / drain
/// state machine can be driven against a fake whose failures are deterministic —
/// a real socket's blip timing (a send-only client only learns of a drop on its
/// next send) is impossible to script reliably. Mirrors how
/// <see cref="IAudioCapture"/> keeps the platform audio stack out of the core's tests.
/// </summary>
public interface ITapConnection : IAsyncDisposable
{
    /// <summary>Open the WebSocket. Throws on a refused upgrade / unreachable host.</summary>
    Task ConnectAsync(CancellationToken cancellationToken = default);

    /// <summary>Send one PCM frame as a single binary message. Throws if the link dropped.</summary>
    Task SendFrameAsync(ReadOnlyMemory<byte> frame, CancellationToken cancellationToken = default);

    /// <summary>Close cleanly (end of utterance). Best-effort and bounded.</summary>
    Task CloseAsync(CancellationToken cancellationToken = default);
}
