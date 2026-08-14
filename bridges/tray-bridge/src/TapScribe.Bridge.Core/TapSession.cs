using System.Runtime.InteropServices;

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

    // The device's mute state, mirrored from IAudioCapture.MuteChanged. Written on the
    // capture backend's volume-notification thread, read on the capture (OnData) thread
    // and under _lock — volatile so neither misses a transition. A muted CAPTURE endpoint
    // still delivers a residual (noise floor / blips) that crosses the level gate, so
    // honouring mute is what stops the recurring "quiet" tap of #159; a loopback endpoint
    // never mutes (IAudioCapture.IsMuted stays false there), so this is permanently false
    // for it and the level gate remains its only mute.
    private volatile bool _muted;
    // Set on the volume thread when a mute arrives, consumed once on the capture thread:
    // the gate must be reset (the only thread allowed to touch it) so an utterance open at
    // mute time doesn't leave the gate IsOpen and swallow the first resumed frame. Decoupled
    // from _muted's edge so the reset still happens even if the capture delivers NO frames
    // during the muted interval — it lands on the first frame after the mute, whenever that is.
    private volatile bool _gateResetPending;

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
        // Subscribe BEFORE seeding _muted so a mute toggled during construction can't slip
        // through the gap between the read and the subscribe — the handler catches it and
        // the seed then reads the reconciled current state.
        AttachCaptureEvents();
        _muted = _capture.IsMuted; // seed from the device's current state before any frame
        try
        {
            _capture.Start();
            _captureStarted = true;
        }
        catch
        {
            DetachCaptureEvents();
            throw;
        }
    }

    // The capture-event wiring, hand-listed in one place so subscribe and unsubscribe
    // stay symmetric by construction: the ctor attaches, and every teardown path — the
    // ctor-catch on a failed Start, DisposeAsync, and DrainAllAsync — detaches. Detach is
    // idempotent (removing an already-removed handler is a no-op), so the End path's
    // belt-and-braces double-unsubscribe (DrainAllAsync then DisposeAsync) is safe. A new
    // capture event is added or removed here once, keeping the four call sites in lockstep.
    private void AttachCaptureEvents()
    {
        _capture.DataAvailable += OnData;
        _capture.MuteChanged += OnMuteChanged;
        _capture.Failed += OnFailed;
    }

    private void DetachCaptureEvents()
    {
        _capture.DataAvailable -= OnData;
        _capture.MuteChanged -= OnMuteChanged;
        _capture.Failed -= OnFailed;
    }

    /// <summary>
    /// Start a gated capture pipeline over <paramref name="capture"/>.
    /// <paramref name="onConnected"/> fires when an Utterance's WS connects;
    /// <paramref name="onFailed"/> fires on a capture-pipeline failure: an Utterance that
    /// can't reach the Recorder on its first connect (refused token / unreachable / unknown
    /// session), or a mid-stream capture loss — the endpoint invalidated after Start
    /// (unplugged / disabled / default-device switch), forwarded from
    /// <see cref="IAudioCapture.Failed"/>. The
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
        if (_gateResetPending)
        {
            // A mute landed; resync the gate on THIS (the capture) thread — the only thread
            // allowed to touch it — so an utterance that was open when the mute hit doesn't
            // leave the gate IsOpen and swallow the first resumed frame as a continuation
            // into a tap that's already gone. Closing that open tap promptly is OnMuteChanged's
            // job; this only resyncs the gate, on the first frame after the mute (muted residual
            // or post-unmute audio — works either way, so a device that stops delivering frames
            // while muted still resumes cleanly). The Resampler is deliberately NOT reset: its
            // sub-sample carry-over across a mute is inaudible (mute is a hard cut anyway), and
            // it only matters for frame ALIGNMENT, which the gate's FrameChunker reset covers.
            _gate.Reset();
            _gateResetPending = false;
        }
        if (_muted)
            return; // muted is a hard gate-closed: drop the residual a muted endpoint keeps delivering (#159)

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
            // Re-check mute under the lock: a frame can clear the gate's open decision on
            // the capture thread just as OnMuteChanged flips _muted on the volume thread.
            // Bailing here means no tap is ever born muted, closing that race definitively.
            if (_disposed || _muted)
                return;

            // A fresh TapStream mints its own utterance_id, so each speech segment
            // is a distinct Utterance / WAV.
            _current = TapStream.Begin(_options, _streamOptions, _onConnected, _onFailed, _connectionFactory);
            _current.Enqueue(firstFrame);
        }
    }

    // The device muted or unmuted. On mute, close any open utterance NOW rather than
    // streaming the residual until the gate's hangover elapses on it — so an in-progress
    // recording stops the instant the mic mutes — and flag the gate for a resync. The gate
    // itself is reset on the capture thread (OnData), the only thread that may touch it.
    // Fires on the capture backend's volume-notification thread; CloseUtterance is
    // _lock-guarded, so it is safe from here. A no-op on unmute (OnData resumes feeding the
    // gate; the pending reset, set when the mute arrived, lands on the next frame).
    private void OnMuteChanged(object? sender, EventArgs e)
    {
        if (_capture.IsMuted)
        {
            // Publish the pending-reset BEFORE _muted so the capture thread, on seeing muted,
            // is guaranteed to also see the reset request (volatile release/acquire ordering).
            _gateResetPending = true;
            _muted = true;
            CloseUtterance();
        }
        else
        {
            _muted = false;
        }
    }

    private void EnqueueToCurrent(byte[] frame)
    {
        lock (_lock)
            _current?.Enqueue(frame);
    }

    private void OnFailed(object? sender, Exception? ex)
    {
        if (ex is not null)
            _onFailed(ex);
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
        DetachCaptureEvents(); // stop producing gate events, and a late device-loss Failed

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
        {
            try
            {
                _capture.Stop();
            }
            catch (Exception ex) when (ex is ExternalException or InvalidOperationException)
            {
                // The endpoint was invalidated while the meeting ran (unplugged / disabled /
                // default-device switch): stopping a WASAPI client for a device that is gone
                // raises AUDCLNT_E_DEVICE_INVALIDATED. There is nothing left to stop, and the
                // one thing that still matters — RELEASING the device below — must not be
                // skipped over it. Swallowed rather than surfaced because teardown has no
                // caller who could act on it: this runs from End meeting (whose drain callback
                // disposes the device enumerator on the next line) and from Quit (which blocks
                // on DisposeAsync and is documented to rely on it being throw-free). What is
                // lost is the stop error's detail; the device loss itself already reached the
                // operator through IAudioCapture.Failed -> onFailed. The filter is what the
                // capture seam lets Stop raise: its declared native failure, or
                // InvalidOperationException. An unexpected exception still escapes.
            }
        }

        if (current is not null)
            draining.Add(current.DrainAndDisposeAsync());

        // Bound teardown so Stop/Quit can't hang on an unreachable Recorder. Any
        // still-draining utterance self-disposes within its own drain budget.
        if (draining.Count > 0)
            await Task.WhenAny(Task.WhenAll(draining), Task.Delay(DisposeDrainTimeout)).ConfigureAwait(false);

        _capture.Dispose();
    }

    /// <summary>
    /// Drain every currently-draining utterance to completion (no 2 s Quit cap)
    /// — bounded only by each stream's own <see cref="TapStreamOptions.DrainBudget"/>.
    /// Also closes any currently-open utterance and drains its tail. This is the
    /// end-of-meeting teardown path: it must flush buffered tails fully so the
    /// Recorder does not strip / transcribe a truncated WAV, so the 2 s bound on
    /// <see cref="DisposeAsync"/> is wrong and a no-cap drain is needed.
    /// </summary>
    public async Task DrainAllAsync()
    {
        // Detach capture events FIRST: the tail drain below is awaited
        // un-capped (up to each stream's DrainBudget), and with OnData still
        // attached a gate close→reopen during that window would mint a NEW
        // utterance streaming post-End PCM into the session — the exact harm
        // the End barrier exists to prevent. DisposeAsync (which follows on
        // the End path) unsubscribes again; removing an already-removed
        // handler is a no-op.
        DetachCaptureEvents();

        List<Task> draining;
        lock (_lock)
        {
            draining = [.. _draining];
            if (_current is not null)
                draining.Add(_current.DrainAndDisposeAsync());
            _current = null;
        }

        if (draining.Count > 0)
            await Task.WhenAll(draining).ConfigureAwait(false);
    }
}
