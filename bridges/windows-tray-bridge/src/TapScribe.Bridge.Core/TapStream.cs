using System.Diagnostics;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Runtime.InteropServices;

namespace TapScribe.Bridge.Core;

/// <summary>
/// One resilient Utterance over <c>/tap</c>: a buffer of 640-byte frames drained
/// by a single pump that keeps a <see cref="TapClient"/> connected, recovering
/// across blips. The Bridge mints one <c>utterance_id</c> at the start of a
/// speech segment (carried on <see cref="TapConnectionOptions.UtteranceId"/>) and
/// this stream keeps it stable across every reconnect, so the Recorder appends to
/// the same WAV (per the wire contract's RESUME_WINDOW) instead of producing a
/// second file.
///
/// Three resilience behaviours, all bounded:
/// <list type="bullet">
/// <item><b>Reconnect.</b> A mid-utterance WS failure (the stream had connected at
/// least once) reopens <c>/tap</c> with the same <c>utterance_id</c> after a
/// jittered backoff. A failure on the <em>first</em> connect — unreachable
/// Recorder, refused token, unknown session — is not a transient blip: it is
/// surfaced via <c>onTerminalFailure</c> and the stream stops, matching the wire
/// contract's fail-loudly stance.</item>
/// <item><b>Gap buffer.</b> Frames captured while disconnected are buffered and
/// flushed (oldest first) once a WS lands, bounded by
/// <see cref="TapStreamOptions.MaxBufferBytes"/> with drop-oldest past the cap.</item>
/// <item><b>Drain.</b> <see cref="BeginDrain"/> (or <see cref="DrainAndDisposeAsync"/>)
/// ends the utterance: the pump flushes whatever is still buffered, bounded by
/// <see cref="TapStreamOptions.DrainBudget"/>, then closes cleanly — never blocking
/// forever against a wedged Recorder.</item>
/// </list>
///
/// The pump is the single consumer (<see cref="TapClient"/> forbids concurrent
/// sends); producers only <see cref="Enqueue"/>. Testable against an in-process
/// /tap server with synthetic PCM — no real audio device.
/// </summary>
public sealed class TapStream : IAsyncDisposable
{
    private readonly TapConnectionOptions _options;
    private readonly TapStreamOptions _stream;
    private readonly Func<TapConnectionOptions, ITapConnection> _connect;
    private readonly Action? _onConnected;
    private readonly Action<Exception>? _onTerminalFailure;

    private readonly object _lock = new();
    private readonly LinkedList<byte[]> _buffer = new();
    private long _bufferBytes;
    private bool _stopped;
    private bool _draining;
    private long _drainDeadlineTicks; // Stopwatch timestamp the drain budget elapses at

    // Auto-reset, coalescing wake: Release past a count of 1 is swallowed, so the
    // pump never accumulates stale permits while it's busy sending a batch.
    private readonly SemaphoreSlim _wake = new(0, 1);
    private readonly CancellationTokenSource _cts = new();
    private readonly Task _pump;

    /// <summary>Frames the pump has confirmed sent (across all reconnects).</summary>
    public long FramesSent { get; private set; }

    /// <summary>Frames dropped from the head because the gap buffer overflowed.</summary>
    public long DroppedFrames { get; private set; }

    private TapStream(TapConnectionOptions options, TapStreamOptions stream,
                      Func<TapConnectionOptions, ITapConnection> connect,
                      Action? onConnected, Action<Exception>? onTerminalFailure)
    {
        _options = options;
        _stream = stream;
        _connect = connect;
        _onConnected = onConnected;
        _onTerminalFailure = onTerminalFailure;
        _pump = Task.Run(RunAsync);
    }

    /// <summary>
    /// Start a resilient stream for one utterance. <paramref name="onConnected"/>
    /// fires on each successful (re)connect; <paramref name="onTerminalFailure"/>
    /// fires once if the very first connect fails (the stream then stops).
    /// <paramref name="connectionFactory"/> defaults to a real <see cref="TapClient"/>;
    /// tests inject a fake to drive the resilience paths deterministically.
    /// </summary>
    public static TapStream Begin(TapConnectionOptions options, TapStreamOptions? stream = null,
                                  Action? onConnected = null, Action<Exception>? onTerminalFailure = null,
                                  Func<TapConnectionOptions, ITapConnection>? connectionFactory = null) =>
        new(options, stream ?? new TapStreamOptions(), connectionFactory ?? (static o => new TapClient(o)),
            onConnected, onTerminalFailure);

    /// <summary>
    /// Completes when the pump stops: a clean drained close, a drain give-up, or a
    /// hard stop. In steady streaming (no drain/stop) it stays pending.
    /// </summary>
    public Task Completion => _pump;

    /// <summary>Queue one 640-byte PCM frame. Owns <paramref name="frame"/> (the
    /// caller must not mutate it afterwards). No-op once stopped.</summary>
    public void Enqueue(byte[] frame)
    {
        lock (_lock)
        {
            if (_stopped)
                return;
            _buffer.AddLast(frame);
            _bufferBytes += frame.Length;
            while (_bufferBytes > _stream.MaxBufferBytes && _buffer.First is not null)
            {
                _bufferBytes -= _buffer.First.Value.Length;
                _buffer.RemoveFirst();
                DroppedFrames++;
            }
        }
        Wake();
    }

    /// <summary>
    /// Mark the utterance ended: the pump flushes the remaining buffer and closes,
    /// bounded by the drain budget. Non-blocking; await <see cref="Completion"/> or
    /// use <see cref="DrainAndDisposeAsync"/> to wait for it.
    /// </summary>
    public void BeginDrain()
    {
        lock (_lock)
        {
            if (_stopped || _draining)
                return;
            _draining = true;
            _drainDeadlineTicks = Stopwatch.GetTimestamp() +
                (long)(_stream.DrainBudget.TotalSeconds * Stopwatch.Frequency);
        }
        Wake();
    }

    /// <summary>
    /// Drain the buffered tail (bounded by the drain budget), close cleanly, then
    /// dispose. Throw-free: the pump is observed without rethrow so a fire-and-forget
    /// caller (e.g. a session tearing down many utterances) can't be faulted.
    /// </summary>
    public async Task DrainAndDisposeAsync()
    {
        BeginDrain();
        // Observe the pump's completion (drained-close or give-up) without rethrow.
        await _pump.ContinueWith(static t => _ = t.Exception, TaskScheduler.Default).ConfigureAwait(false);
        await DisposeAsync().ConfigureAwait(false);
    }

    private async Task RunAsync()
    {
        int attempt = 0;
        bool everConnected = false;

        while (true)
        {
            if (StoppedSnapshot())
                return;

            ITapConnection client = _connect(_options);
            try
            {
                await client.ConnectAsync(_cts.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                await client.DisposeAsync().ConfigureAwait(false);
                return;
            }
            catch (Exception ex) when (IsTransport(ex))
            {
                await client.DisposeAsync().ConfigureAwait(false);
                if (!everConnected)
                {
                    // First connect failed: unreachable Recorder, refused token, or
                    // unknown session. Not a transient blip — surface and stop
                    // rather than retry forever against a wall.
                    _onTerminalFailure?.Invoke(ex);
                    return;
                }
                if (!await BackoffOrGiveUpAsync(attempt++).ConfigureAwait(false))
                    return;
                continue; // reconnect with the same utterance_id; the buffer survives
            }

            everConnected = true;
            attempt = 0;
            _onConnected?.Invoke();

            try
            {
                bool drained = await StreamUntilDoneAsync(client).ConfigureAwait(false);
                // Drained (utterance ended, buffer flushed) closes cleanly; a hard
                // stop just disposes. CloseAsync uses an uncancelled token because
                // the drain path never cancels _cts.
                if (drained)
                    await client.CloseAsync(CancellationToken.None).ConfigureAwait(false);
                await client.DisposeAsync().ConfigureAwait(false);
                return;
            }
            catch (OperationCanceledException)
            {
                await client.DisposeAsync().ConfigureAwait(false);
                return; // hard stop
            }
            catch (Exception ex) when (IsTransport(ex))
            {
                // Mid-utterance failure. The unsent head frame stays buffered.
                await client.DisposeAsync().ConfigureAwait(false);
                if (!await BackoffOrGiveUpAsync(attempt++).ConfigureAwait(false))
                    return;
                // loop: reconnect, flush the buffer (incl. the head frame), continue
            }
        }
    }

    // Sends buffered frames oldest-first while connected. Returns true when drained
    // (utterance ended, buffer empty — close cleanly), false when hard-stopped.
    // Throws a transport exception on a send failure so the caller reconnects.
    private async Task<bool> StreamUntilDoneAsync(ITapConnection client)
    {
        while (true)
        {
            _cts.Token.ThrowIfCancellationRequested();

            byte[]? frame = PeekOldest();
            if (frame is not null)
            {
                await client.SendFrameAsync(frame, _cts.Token).ConfigureAwait(false);
                PopOldestAndCount(); // only after a confirmed send
                continue;
            }

            // Buffer empty.
            if (StoppedSnapshot())
                return false;
            if (DrainingSnapshot())
                return true; // drained: nothing left to flush
            await _wake.WaitAsync(_stream.PollInterval, _cts.Token).ConfigureAwait(false);
        }
    }

    // Returns true to retry (after waiting the backoff), false to give up.
    private async Task<bool> BackoffOrGiveUpAsync(int attempt)
    {
        if (StoppedSnapshot())
            return false;

        TimeSpan delay = NextBackoff(attempt);

        bool draining;
        long deadline;
        lock (_lock)
        {
            draining = _draining;
            deadline = _drainDeadlineTicks;
        }
        if (draining)
        {
            long now = Stopwatch.GetTimestamp();
            if (now >= deadline)
                return false; // drain budget exhausted: give up, dropping the tail
            var remaining = TimeSpan.FromSeconds((deadline - now) / (double)Stopwatch.Frequency);
            if (remaining < delay)
                delay = remaining;
        }

        try
        {
            await Task.Delay(delay, _cts.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            return false; // hard stop during backoff
        }
        return true;
    }

    private TimeSpan NextBackoff(int attempt)
    {
        IReadOnlyList<TimeSpan> schedule = _stream.Backoff;
        TimeSpan baseDelay = schedule.Count == 0
            ? _stream.BackoffCap
            : (attempt < schedule.Count ? schedule[attempt] : _stream.BackoffCap);
        if (baseDelay > _stream.BackoffCap)
            baseDelay = _stream.BackoffCap;

        if (_stream.BackoffJitter > 0)
        {
            double factor = 1 + ((Random.Shared.NextDouble() * 2) - 1) * _stream.BackoffJitter;
            baseDelay *= factor;
            if (baseDelay < TimeSpan.Zero)
                baseDelay = TimeSpan.Zero;
            if (baseDelay > _stream.BackoffCap)
                baseDelay = _stream.BackoffCap;
        }
        return baseDelay;
    }

    private byte[]? PeekOldest()
    {
        lock (_lock)
            return _buffer.First?.Value;
    }

    private void PopOldestAndCount()
    {
        lock (_lock)
        {
            if (_buffer.First is null)
                return;
            _bufferBytes -= _buffer.First.Value.Length;
            _buffer.RemoveFirst();
            FramesSent++;
        }
    }

    private bool StoppedSnapshot()
    {
        lock (_lock)
            return _stopped;
    }

    private bool DrainingSnapshot()
    {
        lock (_lock)
            return _draining;
    }

    private void Wake()
    {
        try
        {
            _wake.Release();
        }
        catch (SemaphoreFullException)
        {
            // A wake is already pending; coalesce. The pump re-reads all state
            // under the lock on its next loop, so one pending permit suffices.
        }
    }

    private static bool IsTransport(Exception ex) =>
        ex is WebSocketException
            or IOException
            or SocketException
            or InvalidOperationException
            or FormatException
            or COMException;

    public async ValueTask DisposeAsync()
    {
        lock (_lock)
            _stopped = true;
        Wake();
        if (!_cts.IsCancellationRequested)
            _cts.Cancel();

        // Observe the pump without rethrowing: terminal failures were already
        // surfaced via onTerminalFailure, so Dispose stays throw-free for a
        // fire-and-forget caller.
        await _pump.ContinueWith(static t => _ = t.Exception, TaskScheduler.Default).ConfigureAwait(false);

        _cts.Dispose();
        _wake.Dispose();
    }
}
