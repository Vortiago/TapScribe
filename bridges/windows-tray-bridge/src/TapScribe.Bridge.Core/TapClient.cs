using System.Net.WebSockets;

namespace TapScribe.Bridge.Core;

/// <summary>
/// A single `/tap` WebSocket: connect (offering the tap-token subprotocol when
/// authenticated), send 640-byte binary PCM frames, close. Per ADR-0002 the
/// Bridge does nothing else on this socket — no JSON, no control messages, and
/// it never talks to WhisperLiveKit. One TapClient == one Utterance.
///
/// Sends are serialised: ClientWebSocket forbids concurrent SendAsync calls, and
/// the reference pipeline drives sends from a single consumer anyway.
/// </summary>
public sealed class TapClient : ITapConnection
{
    private readonly TapConnectionOptions _options;
    private readonly ClientWebSocket _ws = new();
    private readonly SemaphoreSlim _sendLock = new(1, 1);

    public TapClient(TapConnectionOptions options) => _options = options;

    /// <summary>The subprotocol the server echoed back after a successful upgrade.</summary>
    public string? NegotiatedSubProtocol => _ws.SubProtocol;

    public WebSocketState State => _ws.State;

    /// <summary>
    /// Open the WebSocket. Offers `tapscribe.v1.tap.&lt;token&gt;` when a token is
    /// configured; under --no-auth no subprotocol is offered. Throws
    /// <see cref="WebSocketException"/> if the Recorder refuses the upgrade
    /// (bad token -> close 4401; unknown session -> HTTP 404).
    /// </summary>
    public Task ConnectAsync(CancellationToken cancellationToken = default)
    {
        string? subprotocol = _options.BuildSubprotocol();
        if (subprotocol is not null)
            _ws.Options.AddSubProtocol(subprotocol);
        return _ws.ConnectAsync(_options.BuildTapUri(), cancellationToken);
    }

    /// <summary>Send one PCM frame as a single binary WebSocket message.</summary>
    public async Task SendFrameAsync(ReadOnlyMemory<byte> frame, CancellationToken cancellationToken = default)
    {
        await _sendLock.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            await _ws.SendAsync(frame, WebSocketMessageType.Binary, endOfMessage: true, cancellationToken)
                .ConfigureAwait(false);
        }
        finally
        {
            _sendLock.Release();
        }
    }

    /// <summary>
    /// After <see cref="ConnectAsync"/>, wait briefly for the server to send a
    /// close frame. Returns true if the server closed the socket within
    /// <paramref name="window"/> (an accept-then-reject), false if it stayed open
    /// for the whole window (accepted). Used by the connection probe so a Recorder
    /// that accepts then closes 4401 isn't mistaken for "token accepted". The
    /// Bridge never reads frames in normal operation — this is probe-only.
    /// </summary>
    public async Task<bool> WaitForServerCloseAsync(TimeSpan window, CancellationToken cancellationToken = default)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(window);
        var buffer = new byte[256];
        try
        {
            WebSocketReceiveResult result = await _ws.ReceiveAsync(buffer, timeout.Token).ConfigureAwait(false);
            return result.MessageType == WebSocketMessageType.Close;
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            return false; // our window elapsed with the socket still open => accepted
        }
        catch (WebSocketException)
        {
            return true; // socket faulted/closed under us => treat as not accepted
        }
    }

    /// <summary>
    /// Close the WebSocket cleanly (end of Utterance). Best-effort and bounded:
    /// a torn-down or silent peer never hangs or throws out of here.
    /// </summary>
    public async Task CloseAsync(CancellationToken cancellationToken = default)
    {
        if (_ws.State is not (WebSocketState.Open or WebSocketState.CloseReceived))
            return;

        // CloseOutputAsync only SENDS the close frame; unlike CloseAsync it does
        // not block waiting for the peer's close reply, so a wedged or
        // black-holed Recorder can't hang teardown. Per ADR-0002 the Bridge's
        // end-of-utterance job is just "stop sending and close" — it has no
        // reason to await the peer. Still bounded with a short timeout in case
        // the send itself stalls.
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(TimeSpan.FromSeconds(2));
        try
        {
            await _ws.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "utterance end", timeout.Token)
                .ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is WebSocketException or OperationCanceledException)
        {
            // Peer already dropped the connection, or the close send timed out.
            // Nothing the caller can do — the WAV is finalised Recorder-side on
            // disconnect regardless. Swallow best-effort.
        }
    }

    public async ValueTask DisposeAsync()
    {
        await CloseAsync().ConfigureAwait(false);
        _ws.Dispose();
        _sendLock.Dispose();
    }
}
