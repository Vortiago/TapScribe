using System.Linq;
using System.Runtime.InteropServices;

namespace TapScribe.Bridge.Core;

/// <summary>
/// One device to tap: a capture source paired with the <see cref="TapConnectionOptions"/>
/// its frames stream under. Each spec carries its own <c>Identity</c>/<c>Name</c>
/// (the per-speaker split) while sharing the connection coordinates
/// (<c>Host</c>/<c>Port</c>/<c>Token</c>/<c>Session</c>). The orchestrator takes
/// ownership of <see cref="Capture"/> — see <see cref="CaptureOrchestrator.StartAll"/>.
/// </summary>
public sealed record PipelineSpec(IAudioCapture Capture, TapConnectionOptions Options);

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
    // Running sessions keyed by their pipeline identity — the per-identity channel a
    // runtime re-tune fans out over (and that the later per-device-tuning slice keys
    // its updates by). Identities are unique here: StartAll rejects collisions up front.
    private readonly Dictionary<string, TapSession> _sessions;

    private CaptureOrchestrator(Dictionary<string, TapSession> sessions) => _sessions = sessions;

    /// <summary>
    /// Start one pipeline per spec. <paramref name="onConnected"/> /
    /// <paramref name="onFailed"/> are tagged with the firing pipeline's identity so
    /// the shell can show per-device state. <paramref name="connectionFactory"/>
    /// defaults to a real <see cref="TapClient"/>; tests inject a fake.
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

        var sessions = new Dictionary<string, TapSession>(specs.Count, StringComparer.Ordinal);
        foreach (PipelineSpec spec in specs)
        {
            string identity = spec.Options.Identity;
            try
            {
                sessions[identity] = TapSession.Begin(
                    spec.Capture, spec.Options,
                    onConnected: () => onConnected(identity),
                    onFailed: ex => onFailed(identity, ex),
                    gate, stream, connectionFactory);
            }
            catch (Exception ex) when (ex is COMException or InvalidOperationException)
            {
                // TapSession.Begin opens the device in its ctor (capture.Start) and
                // rethrows WITHOUT disposing the capture — it only unsubscribes. Dispose
                // it here so a device that fails to open can't leak, then surface the
                // failure tagged by identity. Best-effort: the remaining devices still
                // start, so one dead device doesn't sink the whole meeting. The filter is
                // what capture.Start throws — WASAPI's COMException, or
                // InvalidOperationException for an already-started/closed device — so an
                // unexpected exception still propagates rather than being swallowed.
                // Dispose is contract-bound not to throw (the WASAPI backend swallows COM
                // teardown errors internally), so it needs no guard of its own.
                spec.Capture.Dispose();
                onFailed(identity, ex);
            }
        }
        return new CaptureOrchestrator(sessions);
    }

    /// <summary>How many pipelines are currently running.</summary>
    public int PipelineCount => _sessions.Count;

    /// <summary>
    /// Re-tune every running pipeline's <see cref="LevelGate"/> with one new
    /// <see cref="GateOptions"/> — the live-retune fan-out behind Settings → Save, so a
    /// sensitivity change takes effect without a Stop/Start. The gate is global for this
    /// slice (every pipeline gets the same tuning); the per-identity keying is what a
    /// later per-device-tuning slice routes its updates through. Each
    /// <see cref="TapSession.UpdateGate"/> publishes the change atomically relative to
    /// its capture thread, so a running meeting keeps streaming uninterrupted. The
    /// caller passes operator-validated options (the tray clamps the sliders); each
    /// pipeline re-validates via <see cref="LevelGate.UpdateTuning"/> and an
    /// out-of-range value throws.
    /// </summary>
    public void UpdateGates(GateOptions gate)
    {
        ArgumentNullException.ThrowIfNull(gate);
        foreach (TapSession session in _sessions.Values)
            session.UpdateGate(gate);
    }

    /// <summary>
    /// Tear down every pipeline. Sessions are disposed <em>concurrently</em> — each
    /// <see cref="TapSession.DisposeAsync"/> is already bounded (drains within its
    /// own budget), so fanning them out keeps total teardown ~one budget instead of
    /// N×budget. That matters when the Recorder is unreachable: the tray's Quit must
    /// not stall for N devices' worth of serialized drain give-ups.
    /// </summary>
    public async ValueTask DisposeAsync() =>
        await Task.WhenAll(_sessions.Values.Select(s => s.DisposeAsync().AsTask())).ConfigureAwait(false);
}
