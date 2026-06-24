namespace TapScribe.Bridge.Core;

/// <summary>The meeting card's lifecycle phase. <see cref="Running"/>/<see cref="Done"/>/
/// <see cref="Failed"/> are derived from the Recorder's poll state; <see cref="Ending"/>/
/// <see cref="Recording"/>/<see cref="Idle"/> are the tray's local pre- and post-pipeline
/// lifecycle the poll can't express. An enum (not a string) so the Core↔shell seam — the
/// <c>RenderPipeline</c> switch — can't silently mis-handle a typo'd phase.</summary>
public enum PipelinePhase
{
    Idle,
    Recording,
    Ending,
    Running,
    Done,
    Failed,
}

/// <summary>
/// The view-model the tray's meeting card renders, built purely from a
/// <see cref="PipelinePoll"/> by <see cref="Map"/> — the C# analogue of the
/// SpatialChat Bridge's <c>pipeline-view.js</c>, and the sibling of
/// <see cref="StatusView"/>: no HTTP, no clock, no WinForms, so it is
/// exhaustively unit-tested with plain inputs. Every field is always present so
/// the renderer never feature-detects; irrelevant fields are null.
///
/// The Recorder fixes the stage/status vocabulary and this consumes it verbatim
/// (CONTEXT.md "End-of-meeting pipeline" + batch_pipeline.py): running pairs
/// strip/stripping, transcribe/transcribing (with <c>current</c>/<c>total</c> WAV
/// counts), summarize/summarizing; done carries the summary; failed carries the
/// stage + <c>error_kind</c>.
/// </summary>
public sealed record PipelineView(
    PipelinePhase Phase,
    string? Progress,
    string? Stage,
    string? CurrentFile,
    PipelineSummary? Summary,
    string? SummaryText,
    string? FailureStage,
    string? FailureReason)
{
    /// <summary>True while the pipeline is still moving — the poll loop keeps going
    /// for <c>running</c> and the local pre-trigger <c>ending</c> phase, and stops on
    /// the terminal <c>done</c>/<c>failed</c> (and uninformative <c>idle</c>).</summary>
    public bool KeepPolling => Phase is PipelinePhase.Running or PipelinePhase.Ending;

    /// <summary>
    /// Map a raw poll body to the card view-model. <paramref name="meetingActive"/>
    /// and <paramref name="ending"/> are the tray's LOCAL meeting lifecycle, consulted
    /// ONLY when the poll itself is non-informative (state "idle" or absent — the
    /// Recorder holds no pipeline record yet), so the card can surface the two
    /// pre-pipeline phases the poll can't express: <c>ending</c> (taps draining toward
    /// the trigger) and <c>recording</c> (a meeting active but not yet ended). Every
    /// informative state (running / done / failed) is a pure function of the body.
    /// </summary>
    public static PipelineView Map(PipelinePoll? raw, bool meetingActive = false, bool ending = false)
    {
        switch (raw?.State)
        {
            case "running":
                return Of(PipelinePhase.Running,
                    progress: ProgressLabelFor(raw),
                    stage: NullIfEmpty(raw.Stage),
                    currentFile: NullIfEmpty(raw.CurrentFile));
            case "done":
                PipelineSummary? summary = raw.Summary;
                return Of(PipelinePhase.Done, summary: summary, summaryText: summary?.Summary ?? "");
            case "failed":
                return Of(PipelinePhase.Failed,
                    stage: NullIfEmpty(raw.Stage),
                    failureStage: NullIfEmpty(raw.Stage),
                    failureReason: FailureReasonFor(raw));
            default:
                // "idle" / missing / unrecognised: fold in the local lifecycle.
                if (ending)
                    return Of(PipelinePhase.Ending);
                return Of(meetingActive ? PipelinePhase.Recording : PipelinePhase.Idle);
        }
    }

    private static PipelineView Of(PipelinePhase phase, string? progress = null, string? stage = null,
        string? currentFile = null, PipelineSummary? summary = null, string? summaryText = null,
        string? failureStage = null, string? failureReason = null) =>
        new(phase, progress, stage, currentFile, summary, summaryText, failureStage, failureReason);

    private static string? NullIfEmpty(string? value) => string.IsNullOrEmpty(value) ? null : value;

    // stage → the live progress line. current/total only carry useful counts during
    // transcribe (one per WAV); strip and summarize are single-shot. A running poll
    // with no stage yet (the job snapshot hasn't attached in the instant after the
    // trigger) gets a generic line rather than a blank.
    private static string ProgressLabelFor(PipelinePoll raw) => raw.Stage switch
    {
        "strip" => "Stripping silence…",
        "transcribe" => raw.Total > 0 ? $"Transcribing {raw.Current}/{raw.Total}…" : "Transcribing…",
        "summarize" => "Summarizing…",
        _ => "Processing…",
    };

    // error_kind → a human-readable, operator-free explanation. A dictionary keyed on
    // the exact domain-error class names the pipeline raises (session_merge /
    // batch_summarize). An unrecognised kind falls back to the raw error text, then a
    // generic line, so a future Recorder error never renders as a blank failure.
    private static readonly IReadOnlyDictionary<string, string> FailureReasons =
        new Dictionary<string, string>(StringComparer.Ordinal)
        {
            ["NoUsableWavs"] = "No usable audio was captured — there was nothing to transcribe.",
            ["NoMergedTranscript"] = "Nothing was transcribed, so there was nothing to summarize.",
            ["SummarizerUnavailable"] = "The summarizer isn't configured on the recorder.",
            ["SummarizerFailed"] = "The summarizer failed while writing the notes.",
            ["InvalidRange"] = "The recorder rejected the session's audio range.",
        };

    private static string FailureReasonFor(PipelinePoll raw)
    {
        if (raw.ErrorKind is { } kind && FailureReasons.TryGetValue(kind, out string? known))
            return known;
        return string.IsNullOrEmpty(raw.Error) ? "The end-of-meeting pipeline failed." : raw.Error;
    }
}
