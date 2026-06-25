namespace TapScribe.Bridge.Core;

/// <summary>
/// The view-model the tray's per-meeting window (<c>MeetingForm</c>) renders — a pure
/// projection of a <see cref="PipelineView"/> (or <c>null</c> while the first poll is
/// still in flight) onto the window's title, caption, body text, and whether Copy is
/// enabled. All the "what does the window show in state X" logic lives here so the
/// WinForms shell is a dumb projection; exhaustively unit-tested like
/// <see cref="PipelineView"/> and <see cref="StatusView"/>.
///
/// One window serves both #168's past-meeting re-opens (Loading → progress → summary)
/// and #107's End-meeting summary (opened straight at <see cref="PipelinePhase.Done"/>),
/// and is designed to render the live phases (<see cref="PipelinePhase.Recording"/>/
/// <see cref="PipelinePhase.Ending"/>) so an active-meeting window can reuse it later.
/// </summary>
public sealed record MeetingFormView(string Title, string Caption, string Body, bool CanCopy)
{
    private const string WindowTitle = "TapScribe — meeting summary";

    /// <summary>Project a poll-derived <paramref name="view"/> (or <c>null</c> for the
    /// pre-first-poll "loading" state) onto the window's display fields.</summary>
    public static MeetingFormView For(PipelineView? view)
    {
        if (view is null)
            return new(WindowTitle, "Loading…", "Fetching this meeting's summary…", CanCopy: false);

        return view.Phase switch
        {
            PipelinePhase.Done => DoneView(view),
            PipelinePhase.Running => new(WindowTitle, "Processing…", view.Progress ?? "Processing…", CanCopy: false),
            PipelinePhase.Ending => new(WindowTitle, "Ending…", "Closing the meeting…", CanCopy: false),
            PipelinePhase.Failed => new(WindowTitle, "Couldn't load this meeting",
                view.FailureReason ?? "The end-of-meeting pipeline failed.", CanCopy: false),
            // Idle / Recording: a session the Recorder holds no summary for (yet) — a
            // neutral empty state rather than a blank window.
            _ => new(WindowTitle, "No summary yet", "No summary is available for this meeting.", CanCopy: false),
        };
    }

    private static MeetingFormView DoneView(PipelineView view)
    {
        string body = view.SummaryText ?? "";
        return new(WindowTitle, CaptionForSummary(view.Summary), body, CanCopy: !string.IsNullOrEmpty(body));
    }

    // Where the summary came from, if the Recorder told us — a quiet caption above the text.
    private static string CaptionForSummary(PipelineSummary? summary)
    {
        if (!string.IsNullOrWhiteSpace(summary?.Model))
            return $"Meeting summary · {summary.Model}";
        if (!string.IsNullOrWhiteSpace(summary?.Source))
            return $"Meeting summary · {summary.Source}";
        return "Meeting summary";
    }
}
