using System.Linq;

namespace TapScribe.Bridge.Core;

/// <summary>
/// One device to tap: a capture source paired with the <see cref="TapConnectionOptions"/>
/// its frames stream under and the per-device <see cref="GateOptions"/> its
/// <see cref="LevelGate"/> is built from. Each spec carries its own <c>Identity</c>/<c>Name</c>
/// (the per-speaker split) and its own <see cref="Gate"/> (the per-device tuning, ADR-0007)
/// while sharing the connection coordinates (<c>Host</c>/<c>Port</c>/<c>Token</c>/<c>Session</c>).
/// <see cref="Gate"/> is optional: when null the pipeline falls back to the shared gate
/// passed to <see cref="CaptureOrchestrator.StartAll"/> (and then to the gate defaults). The
/// orchestrator takes ownership of <see cref="Capture"/>.
/// </summary>
public sealed record PipelineSpec(IAudioCapture Capture, TapConnectionOptions Options, GateOptions? Gate = null);

/// <summary>
/// The devices one meeting taps, as ONE ownership unit: a <see cref="PipelineSpec"/> per
/// device and the <see cref="IAudioDeviceEnumerator"/> their endpoints came out of, where
/// there is one. They belong together because an enumerator hands its endpoint over to each
/// capture it opens and so has to outlive them: one value holding both is what makes that
/// ordering unwritable to get wrong, instead of a rule every teardown path restates for itself.
///
/// Deliberately NOT <see cref="IDisposable"/>. Ownership TRANSFERS at
/// <see cref="CaptureOrchestrator.StartAll"/>, which releases the set on every exit that is not
/// a handed-back orchestrator, so a scope-bound release would free what the orchestrator now
/// owns: a second release of seams contract-bound to be throw-free and nowhere promised to be
/// idempotent. A holder either hands the set on or calls <see cref="Release"/>, and there is no
/// third state.
/// </summary>
public sealed record CaptureSet(IReadOnlyList<PipelineSpec> Specs, IAudioDeviceEnumerator? Enumerator = null)
{
    /// <summary>
    /// Release the whole set: the captures first, then the enumerator behind them. For a set
    /// nothing has taken yet, so the captures are raw and this is a plain
    /// <see cref="IDisposable.Dispose"/> each, rather than the session teardown
    /// <see cref="CaptureOrchestrator.DisposeAsync"/> owes a pipeline that has begun. Both
    /// seams are contract-bound not to throw, which is what lets this run from a finally with
    /// no other owner to fall back on.
    /// </summary>
    public void Release()
    {
        foreach (PipelineSpec spec in Specs)
            spec.Capture.Dispose();
        Enumerator?.Dispose();
    }
}

/// <summary>
/// Runs N concurrent per-identity capture pipelines — one <see cref="TapSession"/>
/// per <see cref="PipelineSpec"/> — so one user can tap their mic and the system
/// loopback (and more) at once, each landing on the Recorder as its own speaker.
/// This is the multi-device piece of the meeting recorder: the coarse me-vs-them
/// split ahead of diarization.
///
/// A thin surface (<see cref="StartAll"/> / <see cref="PipelineCount"/> /
/// <see cref="DisposeAsync"/>) over the already-deep <see cref="TapSession"/>; the
/// depth it adds is the multi-pipeline failure and teardown semantics. Each spec
/// gets its own independent pipeline (own <see cref="Resampler"/> and
/// <see cref="LevelGate"/>), so one device's silence never closes another's
/// utterance and the streams stay separately attributed.
/// </summary>
public sealed class CaptureOrchestrator : IAsyncDisposable
{
    /// <summary>Backstop on unwinding a failed <see cref="StartAll"/>. Nothing is draining on
    /// that path, so this is a guard against a device hanging in teardown, not a wait the
    /// abandon expects to pay.</summary>
    private static readonly TimeSpan AbandonTeardownCap = TimeSpan.FromSeconds(5);

    // Running sessions keyed by their pipeline identity — the per-identity channel a
    // runtime re-tune fans out over (and that the later per-device-tuning slice keys
    // its updates by). Identities are unique here: StartAll rejects collisions up front.
    private readonly Dictionary<string, TapSession> _sessions;

    // The enumerator those sessions' endpoints came out of, released after them and exactly
    // once. Cleared on release rather than checked against a flag, so a second teardown call
    // cannot release it twice: IDisposable is contract-bound to be throw-free, never to be
    // idempotent.
    private IAudioDeviceEnumerator? _enumerator;

    private CaptureOrchestrator(Dictionary<string, TapSession> sessions, IAudioDeviceEnumerator? enumerator)
    {
        _sessions = sessions;
        _enumerator = enumerator;
    }

    /// <summary>
    /// Start one pipeline per spec. <paramref name="onConnected"/> /
    /// <paramref name="onFailed"/> are tagged with the firing pipeline's identity so
    /// the shell can show per-device state. Each pipeline's <see cref="LevelGate"/> is
    /// built from its spec's own <see cref="PipelineSpec.Gate"/>, falling back to the
    /// shared <paramref name="gate"/> when the spec carries none (the per-device tuning
    /// lives on the spec — ADR-0007). <paramref name="connectionFactory"/> defaults to a
    /// real <see cref="TapClient"/>; tests inject a fake.
    /// </summary>
    /// <param name="captures">The devices to tap and the enumerator their endpoints came out
    /// of, as the one value that holds both. Ownership passes AT THE CALL, all of it: unless
    /// this hands back an orchestrator, nothing in the set survives the call, and the enumerator
    /// is released AFTER every capture on every path. Taking the set rather than a spec list
    /// plus a loose enumerator is what leaves the caller nothing to hold across the transfer.
    /// </param>
    public static CaptureOrchestrator StartAll(
        CaptureSet captures,
        Action<string> onConnected,
        Action<string, Exception> onFailed,
        GateOptions? gate = null,
        TapStreamOptions? stream = null,
        Func<TapConnectionOptions, ITapConnection>? connectionFactory = null)
    {
        ArgumentNullException.ThrowIfNull(captures);
        ArgumentNullException.ThrowIfNull(onConnected);
        ArgumentNullException.ThrowIfNull(onFailed);

        IReadOnlyList<PipelineSpec> specs = captures.Specs;
        var sessions = new Dictionary<string, TapSession>(specs.Count, StringComparer.Ordinal);
        int considered = 0;   // specs whose capture now has an owner: a session, or the catch below
        bool handedOver = false;
        try
        {
            // Reject colliding identities before opening any device. The Recorder buckets
            // WAVs and attribution by the sanitised identity (safe_name(identity)[:10]),
            // so two pipelines under one identity cross-attribute into one speaker. The
            // core can't dedupe meaningfully, so it fails loudly here rather than record a
            // muddled session. (Raw equality only; a collision that survives only after the
            // Recorder's 10-char truncation is a caller responsibility — see README.)
            var duplicate = specs
                .GroupBy(s => s.Options.Identity, StringComparer.Ordinal)
                .FirstOrDefault(g => g.Count() > 1);
            if (duplicate is not null)
                throw new ArgumentException(
                    $"Duplicate pipeline identity '{duplicate.Key}'. Each device must stream " +
                    "under a distinct identity.", nameof(specs));

            foreach (PipelineSpec spec in specs)
            {
                string identity = spec.Options.Identity;
                Exception? skipped = null;
                try
                {
                    sessions[identity] = TapSession.Begin(
                        spec.Capture, spec.Options,
                        onConnected: () => onConnected(identity),
                        onFailed: ex => onFailed(identity, ex),
                        spec.Gate ?? gate, stream, connectionFactory);
                }
                catch (Exception ex) when (CaptureSeam.IsDeclaredCaptureFailure(ex))
                {
                    // A device that failed to OPEN is skipped, not fatal: the remaining ones
                    // still start, so one dead device doesn't sink the whole meeting.
                    // TapSession.Begin rethrows WITHOUT disposing the capture (it only
                    // unsubscribes), so release it here and surface the failure tagged by
                    // identity. Anything outside the seam's capture set is NOT a skippable
                    // device failure and goes to the unwind below, which owns that rule.
                    // Dispose is contract-bound not to throw.
                    spec.Capture.Dispose();
                    skipped = ex;
                }
                // This capture has an owner either way now: the session that begun, or the
                // release above. Counted BEFORE the notification, because onFailed is the
                // CALLER's callback and nothing binds it to be throw-free. A throw from it
                // would otherwise leave the unwind releasing a capture already released here,
                // and Dispose is nowhere promised to be idempotent.
                considered++;
                if (skipped is not null)
                    onFailed(identity, skipped);
            }

            // Zero pipelines is not a meeting. The per-device catch above is best-effort so
            // one dead device can't sink the others — but when EVERY device dies, handing
            // back an empty orchestrator lets the caller publish it as a live meeting:
            // "End meeting" goes live and the status line claims 0/N devices are streaming
            // while nothing is recorded. Refuse instead, the symmetric half of the caller's
            // own refusal when no device could be OPENED.
            if (sessions.Count == 0)
                throw new InvalidOperationException("No selected device could be started.");

            handedOver = true;
            return new CaptureOrchestrator(sessions, captures.Enumerator);
        }
        finally
        {
            // ONE total rule: unless the orchestrator was handed back, nothing this method
            // was given survives it. Stated as "every exit that isn't the success" rather
            // than as a list of the throws that exist today, because that list is not
            // knowable from here — TapSession's ctor validates the capture format and the
            // gate tuning BEFORE it starts the device, so an out-of-range gate raises an
            // ArgumentOutOfRangeException that the per-device filter above deliberately does
            // not catch. The caller handed ownership over with the specs (see PipelineSpec)
            // and has no handle left to release them by, so anything left here is leaked for
            // the process lifetime: an endpoint held "in use", and any session already begun
            // still streaming PCM with nothing able to stop it.
            if (!handedOver)
                Abandon(captures, considered, sessions);
        }
    }

    // Release everything a failed StartAll is still holding: the captures of specs it never
    // got to (the one it threw on, and every one after it), then the sessions that did begin,
    // then the enumerator all of them came out of.
    private static void Abandon(
        CaptureSet captures, int considered, Dictionary<string, TapSession> sessions)
    {
        IReadOnlyList<PipelineSpec> specs = captures.Specs;
        for (int i = considered; i < specs.Count; i++)
            specs[i].Capture.Dispose();

        // Bounded, and in practice synchronous: no audio has reached these sessions yet, so
        // none has a draining utterance to await — the cap is a backstop against a device
        // that hangs in teardown, not a wait this path expects to pay. DisposeAsync stops
        // and releases each capture, which is the whole point: a begun session owns its
        // capture, so disposing the capture directly here would leave the session live.
        //
        // The enumerator rides that same teardown rather than being released beside it, even
        // with no session to tear down, so the unwind releases it after the captures on the
        // one ordering the success path uses. A teardown that overruns the cap defers the
        // release rather than skipping it: the task still runs, and releasing the endpoints'
        // owner out from under a capture that is still closing is the failure worth avoiding.
        new CaptureOrchestrator(sessions, captures.Enumerator).DisposeAsync().AsTask().Wait(AbandonTeardownCap);
    }

    /// <summary>How many pipelines are currently running.</summary>
    public int PipelineCount => _sessions.Count;

    /// <summary>
    /// Re-tune running pipelines' <see cref="LevelGate"/>s from a per-identity map — the
    /// live-retune fan-out behind Settings → Save, so a per-device sensitivity change takes
    /// effect without a Stop/Start. Each entry is routed to the pipeline running under that
    /// identity (the same key <see cref="StartAll"/> bucketed sessions by); an identity with
    /// no running pipeline — a device that's unplugged or simply not in this meeting — is
    /// skipped without error, and a pipeline whose identity is absent from the map keeps its
    /// current tuning, so re-tuning one device never disturbs another's gate or open
    /// utterance. Each <see cref="TapSession.UpdateGate"/> publishes the change atomically
    /// relative to its capture thread, so a running meeting keeps streaming uninterrupted.
    /// The caller passes operator-validated options (the tray clamps the sliders); each
    /// pipeline re-validates via <see cref="LevelGate.UpdateTuning"/> and an out-of-range
    /// value throws.
    /// </summary>
    public void UpdateGates(IReadOnlyDictionary<string, GateOptions> gatesByIdentity)
    {
        ArgumentNullException.ThrowIfNull(gatesByIdentity);
        foreach ((string identity, GateOptions gate) in gatesByIdentity)
            if (_sessions.TryGetValue(identity, out TapSession? session))
                session.UpdateGate(gate);
    }

    /// <summary>
    /// End-of-meeting drain: for each running session, close its open utterance and
    /// await its drain to completion — bounded only by each stream's own
    /// <see cref="TapStreamOptions.DrainBudget"/>, not the 2 s <see cref="DisposeAsync"/>
    /// cap. The Recorder's strip / batch / summarize pipeline fires AFTER this returns,
    /// so the WAVs must be fully flushed (a truncated WAV that leaks into the pipeline
    /// is the bug this fixes).
    /// </summary>
    public async Task DrainAllAsync() =>
        await Task.WhenAll(_sessions.Values.Select(s => s.DrainAllAsync())).ConfigureAwait(false);

    /// <summary>
    /// Tear down every pipeline and then release the enumerator they came out of. Sessions
    /// are disposed <em>concurrently</em>: each <see cref="TapSession.DisposeAsync"/> is
    /// already bounded (drains within its own budget), so fanning them out keeps total
    /// teardown ~one budget instead of N×budget. That matters when the Recorder is
    /// unreachable: the shell's quit must not stall for N devices' worth of serialized drain
    /// give-ups. Idempotent, and throw-free by the seams it calls.
    /// </summary>
    public async ValueTask DisposeAsync()
    {
        try
        {
            await Task.WhenAll(_sessions.Values.Select(s => s.DisposeAsync().AsTask())).ConfigureAwait(false);
        }
        finally
        {
            // After the captures, never beside them, and exactly once however many teardowns
            // run. In a finally rather than after the await, because a session teardown that
            // faults must not strand the endpoints' owner for the process lifetime: the
            // capture that throw skipped is then still live, which is the lesser of the two
            // leaks and the only one the caller can still do something about.
            Interlocked.Exchange(ref _enumerator, null)?.Dispose();
        }
    }

    /// <summary>
    /// End-of-meeting teardown: <see cref="DrainAllAsync"/> every tap to completion
    /// (no 2 s Quit cap) and THEN <see cref="DisposeAsync"/> — stopping capture and
    /// releasing the devices and their enumerator. Exposed as ONE call so the End path can't
    /// drain without disposing: dropping the dispose leaks the capture devices and
    /// lets them keep streaming PCM into the session past the barrier, so the pipeline
    /// strips/transcribes audio captured after "End meeting". The pipeline trigger
    /// fires only after this returns. (Quit stays on the 2 s-bounded
    /// <see cref="DisposeAsync"/>.)
    /// </summary>
    public async Task EndMeetingAsync()
    {
        try
        {
            await DrainAllAsync().ConfigureAwait(false);
        }
        finally
        {
            // A drain that faults must not strand the endpoints for the process lifetime, so
            // the release is sequenced in a finally rather than after the await. The drain's
            // own failure still propagates: whether the taps flushed is the caller's business,
            // and it is not this method's to classify.
            await DisposeAsync().ConfigureAwait(false);
        }
    }
}
