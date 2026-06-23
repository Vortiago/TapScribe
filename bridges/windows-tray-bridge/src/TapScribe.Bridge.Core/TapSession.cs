namespace TapScribe.Bridge.Core;

/// <summary>
/// One capture pipeline for one speaker: a capture device →
/// <see cref="Resampler"/> → <see cref="LevelGate"/> → a fresh
/// <see cref="TapStream"/> per gated Utterance. The level gate is the Bridge-side
/// Mute — a loopback device has no mute event, so the session opens an Utterance
/// when the level crosses the threshold and closes it after the hangover. Each
/// Utterance mints its own <c>utterance_id</c> (stable across reconnects within
/// the Utterance, per the wire contract), so the Recorder writes one WAV per
/// speech segment with blip resilience for free.
///
/// Resample → gate → chunk runs synchronously on the capture thread (all cheap,
/// no I/O); the WebSocket send, reconnect, and Drain happen on each
/// <see cref="TapStream"/>'s own pump. The capture is injected
/// (<see cref="IAudioCapture"/>) and the per-utterance connection is injectable
/// too, so the whole pipeline is testable with a fake capture and a fake transport
/// — no real microphone, Windows audio stack, or socket required.
/// </summary>
public sealed class TapSession : IAsyncDisposable
{
    private static readonly TimeSpan DisposeDrainTimeout = TimeSpan.FromSeconds(2);

    private readonly IAudioCapture _capture;
    private readonly TapConnectionOptions _options;
    private readonly TapStreamOptions _streamOptions;
    private readonly Func<TapConnectionOptions, ITapConnection> _connectionFactory;
    private readonly Action _onConnected;
    private readonly Action<Exception> _onFailed;

    private readonly Resampler _resampler;
    private readonly LevelGate _gate;
    private readonly object _lock = new();
    private readonly List<Task> _draining = [];

    private TapStream? _current;
    private readonly bool _captureStarted; // set once in the ctor (Start succeeded)
    private bool _disposed;

    private TapSession(IAudioCapture capture, TapConnectionOptions options, GateOptions gate,
                       TapStreamOptions stream, Func<TapConnectionOptions, ITapConnection> connectionFactory,
                       Action onConnected, Action<Exception> onFailed)
    {
        _capture = capture;
        _options = options;
        _streamOptions = stream;
        _connectionFactory = connectionFactory;
        _onConnected = onConnected;
        _onFailed = onFailed;
        _resampler = new Resampler(capture.Format);
        _gate = new LevelGate(gate);

        // Capture must run continuously so the gate can hear speech start; unlike
        // the pre-gate tracer bullet there is no WS to wait for. A device-open /
        // Start failure surfaces synchronously to the caller (the tray catches it).
        _capture.DataAvailable += OnData;
        try
        {
            _capture.Start();
            _captureStarted = true;
        }
        catch
        {
            _capture.DataAvailable -= OnData;
            throw;
        }
    }

    /// <summary>
    /// Start a gated capture pipeline over <paramref name="capture"/>.
    /// <paramref name="onConnected"/> fires when an Utterance's WS connects;
    /// <paramref name="onFailed"/> fires if an Utterance can't reach the Recorder on
    /// its first connect (refused token / unreachable / unknown session). The
    /// session takes ownership of <paramref name="capture"/> and disposes it.
    /// Construct the capture before calling so a device-open failure surfaces to the
    /// caller. <paramref name="connectionFactory"/> defaults to a real
    /// <see cref="TapClient"/>; tests inject a fake.
    /// </summary>
    public static TapSession Begin(IAudioCapture capture, TapConnectionOptions options,
                                   Action onConnected, Action<Exception> onFailed,
                                   GateOptions? gate = null, TapStreamOptions? stream = null,
                                   Func<TapConnectionOptions, ITapConnection>? connectionFactory = null) =>
        new(capture, options, gate ?? new GateOptions(), stream ?? new TapStreamOptions(),
            connectionFactory ?? (static o => new TapClient(o)), onConnected, onFailed);

    /// <summary>
    /// Re-tune the live <see cref="LevelGate"/> (sensitivity / hangover / pre-roll)
    /// without tearing the pipeline down. Safe to call from another thread while the
    /// capture thread drives the gate; an in-flight utterance keeps streaming and the
    /// new tuning governs every frame from here on. Forwarded straight to the gate,
    /// which publishes the change atomically.
    /// </summary>
    public void UpdateGate(GateOptions gate) => _gate.UpdateTuning(gate);

    private void OnData(object? sender, AudioCapturedEventArgs e)
    {
        byte[] pcm = _resampler.Process(e.Data.Span);
        if (pcm.Length == 0)
            return;
        // The gate's output is already 640-byte frame-aligned, so each frame goes
        // straight to the stream — no second chunking.
        foreach (GateEvent ev in _gate.Push(pcm))
        {
            switch (ev.Kind)
            {
                case GateEventKind.Opened:
                    OpenUtterance(ev.Frame);
                    break;
                case GateEventKind.Audio:
                    EnqueueToCurrent(ev.Frame);
                    break;
                case GateEventKind.Closed:
                    CloseUtterance();
                    break;
            }
        }
    }

    private void OpenUtterance(byte[] firstFrame)
    {
        lock (_lock)
        {
            if (_disposed)
                return;

            // A fresh TapStream mints its own utterance_id, so each speech segment
            // is a distinct Utterance / WAV.
            _current = TapStream.Begin(_options, _streamOptions, _onConnected, _onFailed, _connectionFactory);
            _current.Enqueue(firstFrame);
        }
    }

    private void EnqueueToCurrent(byte[] frame)
    {
        lock (_lock)
            _current?.Enqueue(frame);
    }

    private void CloseUtterance()
    {
        lock (_lock)
        {
            if (_current is null)
                return;
            // Drain + dispose off the capture thread so a slow flush never stalls
            // capture; prune finished drains so a long session doesn't accumulate.
            _draining.RemoveAll(static t => t.IsCompleted);
            _draining.Add(_current.DrainAndDisposeAsync());
            _current = null;
        }
    }

    public async ValueTask DisposeAsync()
    {
        _capture.DataAvailable -= OnData; // stop producing gate events

        TapStream? current;
        List<Task> draining;
        lock (_lock)
        {
            _disposed = true;
            current = _current;
            _current = null;
            draining = [.. _draining];
        }

        if (_captureStarted)
            _capture.Stop();

        if (current is not null)
            draining.Add(current.DrainAndDisposeAsync());

        // Bound teardown so Stop/Quit can't hang on an unreachable Recorder. Any
        // still-draining utterance self-disposes within its own drain budget.
        if (draining.Count > 0)
            await Task.WhenAny(Task.WhenAll(draining), Task.Delay(DisposeDrainTimeout)).ConfigureAwait(false);

        _capture.Dispose();
    }
}
