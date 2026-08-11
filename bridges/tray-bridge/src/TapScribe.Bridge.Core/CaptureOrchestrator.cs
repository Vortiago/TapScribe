using System.Linq;
using System.Runtime.InteropServices;

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

    private CaptureOrchestrator(Dictionary<string, TapSession> sessions) => _sessions = sessions;

    /// <summary>
    /// Start one pipeline per spec. <paramref name="onConnected"/> /
    /// <paramref name="onFailed"/> are tagged with the firing pipeline's identity so
    /// the shell can show per-device state. Each pipeline's <see cref="LevelGate"/> is
    /// built from its spec's own <see cref="PipelineSpec.Gate"/>, falling back to the
    /// shared <paramref name="gate"/> when the spec carries none (the per-device tuning
    /// lives on the spec — ADR-0007). <paramref name="connectionFactory"/> defaults to a
    /// real <see cref="TapClient"/>; tests inject a fake.
    /// </summary>
    public static CaptureOrchestrator StartAll(
        IReadOnlyList<PipelineSpec> specs,
        Action<string> onConnected,
        Action<string, Exception> onFailed,
        GateOptions? gate = null,
        TapStreamOptions? stream = null,
        Func<TapConnectionOptions, ITapConnection>? connectionFactory = null)
    {
        ArgumentNullException.ThrowIfNull(specs);
        ArgumentNullException.ThrowIfNull(onConnected);
        ArgumentNullException.ThrowIfNull(onFailed);

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
                try
                {
                    sessions[identity] = TapSession.Begin(
                        spec.Capture, spec.Options,
                        onConnected: () => onConnected(identity),
                        onFailed: ex => onFailed(identity, ex),
                        spec.Gate ?? gate, stream, connectionFactory);
                }
                catch (Exception ex) when (ex is COMException or InvalidOperationException)
                {
                    // A device that failed to OPEN is skipped, not fatal: the remaining ones
                    // still start, so one dead device doesn't sink the whole meeting.
                    // TapSession.Begin rethrows WITHOUT disposing the capture — it only
                    // unsubscribes — so release it here and surface the failure tagged by
                    // identity. The filter is what capture.Start throws (WASAPI's
                    // COMException, or InvalidOperationException for an already-started or
                    // closed device); anything else is NOT a skippable device failure and
                    // goes to the unwind below. Dispose is contract-bound not to throw.
                    spec.Capture.Dispose();
                    onFailed(identity, ex);
                }
                considered++;
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
            return new CaptureOrchestrator(sessions);
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
                Abandon(specs, considered, sessions);
        }
    }

    // Release everything a failed StartAll is still holding: the captures of specs it never
    // got to (the one it threw on, and every one after it), then the sessions that did begin.
    private static void Abandon(
        IReadOnlyList<PipelineSpec> specs, int considered, Dictionary<string, TapSession> sessions)
    {
        for (int i = considered; i < specs.Count; i++)
            specs[i].Capture.Dispose();

        // Bounded, and in practice synchronous: no audio has reached these sessions yet, so
        // none has a draining utterance to await — the cap is a backstop against a device
        // that hangs in teardown, not a wait this path expects to pay. DisposeAsync stops
        // and releases each capture, which is the whole point: a begun session owns its
        // capture, so disposing the capture directly here would leave the session live.
        if (sessions.Count > 0)
            new CaptureOrchestrator(sessions).DisposeAsync().AsTask().Wait(AbandonTeardownCap);
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
    /// Tear down every pipeline. Sessions are disposed <em>concurrently</em> — each
    /// <see cref="TapSession.DisposeAsync"/> is already bounded (drains within its
    /// own budget), so fanning them out keeps total teardown ~one budget instead of
    /// N×budget. That matters when the Recorder is unreachable: the tray's Quit must
    /// not stall for N devices' worth of serialized drain give-ups.
    /// </summary>
    public async ValueTask DisposeAsync() =>
        await Task.WhenAll(_sessions.Values.Select(s => s.DisposeAsync().AsTask())).ConfigureAwait(false);

    /// <summary>
    /// End-of-meeting teardown: <see cref="DrainAllAsync"/> every tap to completion
    /// (no 2 s Quit cap) and THEN <see cref="DisposeAsync"/> — stopping capture and
    /// releasing the devices. Exposed as ONE call so the tray's End path can't drain
    /// without disposing: dropping the dispose leaks the WASAPI capture devices and
    /// lets them keep streaming PCM into the session past the barrier, so the pipeline
    /// strips/transcribes audio captured after "End meeting". The pipeline trigger
    /// fires only after this returns. (Quit stays on the 2 s-bounded
    /// <see cref="DisposeAsync"/>.)
    /// </summary>
    public async Task EndMeetingAsync()
    {
        await DrainAllAsync().ConfigureAwait(false);
        await DisposeAsync().ConfigureAwait(false);
    }
}
