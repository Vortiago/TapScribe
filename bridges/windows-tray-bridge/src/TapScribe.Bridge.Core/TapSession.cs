using System.Net.Sockets;
using System.Net.WebSockets;
using System.Runtime.InteropServices;
using System.Threading.Channels;

namespace TapScribe.Bridge.Core;

/// <summary>
/// One live tap: a capture source -> <see cref="Resampler"/> -> <see cref="FrameChunker"/>
/// -> 640-byte frames over a single <see cref="TapClient"/> WebSocket. Capture
/// frames are queued on a channel and drained by one async pump so sends stay
/// serialised and off the capture thread. One TapSession == one Utterance for
/// this tracer bullet (the level gate that opens/closes utterances on speech is
/// a later PRD slice).
///
/// The capture is injected (<see cref="IAudioCapture"/>), so the whole pipeline
/// is integration-testable with a fake capture against an in-process /tap server
/// — no real microphone or Windows audio stack required. The WASAPI
/// implementation lives in the Windows project; the tray supplies it.
/// </summary>
public sealed class TapSession : IAsyncDisposable
{
    private readonly IAudioCapture _capture;
    private readonly Resampler _resampler;
    private readonly FrameChunker _chunker;
    private readonly Channel<byte[]> _frames;
    private readonly TapClient _tap;
    private readonly CancellationTokenSource _cts = new();
    private readonly Task _pump;
    private volatile bool _captureStarted;

    private static readonly TimeSpan DrainTimeout = TimeSpan.FromSeconds(2);

    private TapSession(IAudioCapture capture, TapConnectionOptions options,
                       Action onConnected, Action<Exception> onFailed)
    {
        _capture = capture;
        _resampler = new Resampler(capture.Format);
        _chunker = new FrameChunker();
        _frames = Channel.CreateUnbounded<byte[]>(new UnboundedChannelOptions { SingleReader = true });
        _tap = new TapClient(options);
        _capture.DataAvailable += OnData;
        _pump = Task.Run(() => RunAsync(onConnected, onFailed));
    }

    /// <summary>
    /// Start a tap over the given <paramref name="capture"/>: connect the /tap WS,
    /// then start capturing and stream frames. Connection/streaming failures
    /// arrive via <paramref name="onFailed"/>. The session takes ownership of
    /// <paramref name="capture"/> and disposes it. Construct the capture (which
    /// may throw if the device can't be opened) before calling, so the caller can
    /// surface that synchronously.
    /// </summary>
    public static TapSession Begin(IAudioCapture capture, TapConnectionOptions options,
                                   Action onConnected, Action<Exception> onFailed) =>
        new(capture, options, onConnected, onFailed);

    private void OnData(object? sender, AudioCapturedEventArgs e)
    {
        byte[] pcm = _resampler.Process(e.Data.Span);
        if (pcm.Length == 0)
            return;
        foreach (byte[] frame in _chunker.Push(pcm))
            _frames.Writer.TryWrite(frame); // unbounded: never fails; completed channel just drops
    }

    private async Task RunAsync(Action onConnected, Action<Exception> onFailed)
    {
        try
        {
            await _tap.ConnectAsync(_cts.Token).ConfigureAwait(false);
            _capture.Start();
            _captureStarted = true;
            onConnected();
            await foreach (byte[] frame in _frames.Reader.ReadAllAsync(_cts.Token).ConfigureAwait(false))
                await _tap.SendFrameAsync(frame, _cts.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            // Expected: Stop()/Dispose cancelled the pump. Nothing to report.
        }
        catch (Exception ex) when (
            ex is WebSocketException
                or IOException
                or SocketException
                or InvalidOperationException
                or FormatException
                or COMException)
        {
            // Expected operational failures at the connect/stream/device boundary
            // (Recorder unreachable, refused token, mic removed, or a malformed
            // host that slipped past NormalizeHost -> UriFormatException). The
            // filter keeps this off CodeQL's catch-of-all radar AND lets a genuine
            // bug fault the task loudly instead of being silently swallowed.
            // onFailed surfaces it to the user and tears the session down.
            onFailed(ex);
        }
    }

    public async ValueTask DisposeAsync()
    {
        _capture.DataAvailable -= OnData; // stop producing frames

        // Graceful drain: complete the writer and let the pump send the 640-byte
        // frames still buffered, bounded by DrainTimeout. Only cancel (hard stop)
        // if the drain stalls — cancelling first would abort ReadAllAsync and drop
        // the buffered tail (worst case a sub-second tail of an utterance).
        _frames.Writer.TryComplete();
        Task finished = await Task.WhenAny(_pump, Task.Delay(DrainTimeout)).ConfigureAwait(false);
        if (finished != _pump)
            _cts.Cancel();

        // Observe the pump's completion without rethrowing: expected errors were
        // already surfaced via onFailed, and marking any unexpected fault observed
        // here keeps DisposeAsync throw-free so a fire-and-forget caller can't crash.
        await _pump.ContinueWith(static t => _ = t.Exception, TaskScheduler.Default).ConfigureAwait(false);

        if (_captureStarted)
            _capture.Stop();
        await _tap.DisposeAsync().ConfigureAwait(false);
        _capture.Dispose();
        _cts.Dispose();
    }
}
