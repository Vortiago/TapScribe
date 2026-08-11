using System.Net;

namespace TapScribe.Bridge.Core;

/// <summary>
/// Drives the End-meeting flow end to end: close the open taps (gate close +
/// Drain) BEFORE triggering the Recorder's end-of-meeting pipeline, then poll to a
/// terminal state, emitting a <see cref="PipelineView"/> per update for the tray to
/// render. This is where the feature's behaviour lives — the WinForms shell only
/// renders the emissions — so it is tested over real HTTP against a fake Recorder
/// (the C# analogue of SpatialChat's popup poll loop + presenter).
///
/// Single-use: one controller drives one meeting. A second <see cref="EndAsync"/>
/// call (a double-clicked menu item) is a no-op, so it can never fire a second
/// pipeline. The poll cadence is injected (<c>pollDelay</c>) so tests collapse it.
/// </summary>
public sealed class MeetingController
{
    /// <summary>
    /// How many CONSECUTIVE transient poll failures the loop rides out before it stops
    /// following the meeting. A blip (a network hiccup, a Recorder mid-restart) heals
    /// well inside this; a Recorder that keeps failing does not, and the caller needs
    /// the flow to END so its UI can be handed back to the operator. Counted in polls
    /// rather than seconds because the cadence is the caller's (<c>pollDelay</c>): at
    /// the tray's 1.5 s it is a little over half a minute of continuous failure.
    /// </summary>
    public const int MaxConsecutivePollFailures = 20;

    private readonly ControlClient _control;
    private readonly string _sessionId;
    private readonly Func<Task>? _drainAsync;
    private readonly Func<CancellationToken, Task> _pollDelay;
    private int _started;

    /// <summary>Raised for every card update — the tray marshals it to the UI thread
    /// and renders the header / summary window.</summary>
    public event Action<PipelineView>? Updated;

    /// <summary>A one-shot operator message (a balloon) for conditions the
    /// <see cref="PipelineView"/> can't express — currently a busy Recorder.</summary>
    public event Action<string>? OperatorNotice;

    public MeetingController(ControlClient control, string sessionId,
        Func<CancellationToken, Task> pollDelay, Func<Task>? drainAsync = null)
    {
        ArgumentNullException.ThrowIfNull(control);
        ArgumentException.ThrowIfNullOrEmpty(sessionId);
        ArgumentNullException.ThrowIfNull(pollDelay);
        _control = control;
        _sessionId = sessionId;
        _pollDelay = pollDelay;
        _drainAsync = drainAsync;
    }

    /// <summary>End the meeting: drain the open taps, trigger the pipeline, and poll to
    /// the finished summary (or a failure). A no-op on a second call.
    ///
    /// When <paramref name="triggerPipeline"/> is false (the operator's record-only mode,
    /// <c>BridgeSettings.ProcessOnEnd == false</c>), the taps still drain — so the recordings
    /// are fully written — but the pipeline is NOT triggered and NOT polled: the flow emits a
    /// terminal <see cref="PipelineView.Saved"/> and stops. The session and its recordings
    /// stay on the Recorder to be processed from the dashboard later.</summary>
    public async Task EndAsync(bool triggerPipeline = true, CancellationToken cancellationToken = default)
    {
        if (Interlocked.Exchange(ref _started, 1) != 0)
            return; // a double-clicked End meeting can't fire a second pipeline
        ArgumentNullException.ThrowIfNull(_drainAsync); // the end path needs a drain callback

        Updated?.Invoke(PipelineView.Map(null, ending: true));
        await _drainAsync().ConfigureAwait(false); // flush every utterance before strip runs

        if (!triggerPipeline)
        {
            // Record-only: taps drained, WAVs saved, no automatic processing. Terminal — no
            // trigger, no poll (so nothing to resume either; the tray persists no state).
            Updated?.Invoke(PipelineView.Saved());
            return;
        }

        PipelineTriggerOutcome outcome =
            await _control.TriggerPipelineAsync(_sessionId, cancellationToken).ConfigureAwait(false);
        if (outcome == PipelineTriggerOutcome.Busy)
            // Another job is already running on this session — we don't fire a second one;
            // its summary is what we want, so fall through to polling the in-flight run.
            OperatorNotice?.Invoke("Recorder busy — another job is already running on this session; showing its progress.");

        await PollToTerminalAsync(cancellationToken).ConfigureAwait(false);
    }

    /// <summary>Resume showing a pipeline that's already running on the Recorder — e.g.
    /// after a tray restart, off a persisted session id. No drain, no re-trigger: just
    /// poll the in-flight run to its progress / finished summary. A no-op on a second call.</summary>
    public async Task ResumeAsync(CancellationToken cancellationToken = default)
    {
        if (Interlocked.Exchange(ref _started, 1) != 0)
            return;
        await PollToTerminalAsync(cancellationToken).ConfigureAwait(false);
    }

    private async Task PollToTerminalAsync(CancellationToken cancellationToken)
    {
        int consecutiveFailures = 0;
        while (true)
        {
            PipelineView view;
            try
            {
                PipelinePoll poll = await _control.PollPipelineAsync(_sessionId, cancellationToken).ConfigureAwait(false);
                view = PipelineView.Map(poll);
                consecutiveFailures = 0; // the Recorder answered: whatever blip preceded it is over
            }
            catch (HttpRequestException ex)
            {
                if (ex.StatusCode == HttpStatusCode.NotFound)
                {
                    // The Recorder is UP and disowns this session (deleted dir / unknown id):
                    // a 404 is terminal, unlike a transient blip — surface it and STOP rather
                    // than self-healing forever. Reachable when re-opening a past meeting the
                    // Recorder has since pruned (#168); also stops a vanished End/Resume session.
                    Updated?.Invoke(PipelineView.Unavailable("This meeting is no longer available on the recorder."));
                    return;
                }
                // A transient poll failure (network blip / Recorder mid-restart): hold the
                // rendered state and self-heal at the poll cadence rather than aborting —
                // but only for a bounded RUN of them. Past that the Recorder isn't blipping,
                // it's failing, and a loop that never returns leaves the caller's UI pinned
                // on a processing state it can never leave (the tray greys both Start and
                // End for the whole flow). Emit a terminal view and stop; the run resets on
                // the first answered poll, so an intermittent Recorder still rides through.
                if (++consecutiveFailures >= MaxConsecutivePollFailures)
                {
                    Updated?.Invoke(PipelineView.Unavailable(
                        "Lost contact with the recorder while following this meeting. " +
                        "It may still be running — re-open it from Past meetings."));
                    return;
                }
                await _pollDelay(cancellationToken).ConfigureAwait(false);
                continue;
            }

            Updated?.Invoke(view);
            if (!view.KeepPolling)
                return;
            await _pollDelay(cancellationToken).ConfigureAwait(false);
        }
    }
}
