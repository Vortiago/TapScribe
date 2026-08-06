using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The projection the tray's per-meeting window renders (#168): a pure mapping from a
/// <see cref="PipelineView"/> (or none, while the first poll is in flight) to the
/// window's title / caption / body / copy-enablement. Keeping it here makes the WinForms
/// <c>MeetingForm</c> a dumb projection and pins every state cross-platform — the sibling
/// of <see cref="PipelineView"/>/<c>StatusView</c>.
/// </summary>
public class MeetingFormViewTests
{
    [Fact]
    public void For_Null_IsLoading_AndNotCopyable()
    {
        MeetingFormView v = MeetingFormView.For(null);

        Assert.Equal("Loading…", v.Caption);
        Assert.False(v.CanCopy);
    }

    [Fact]
    public void For_Running_ShowsTheProgressLine_AndNotCopyable()
    {
        PipelineView running = PipelineView.Map(
            new PipelinePoll { State = "running", Stage = "transcribe", Current = 1, Total = 3 });

        MeetingFormView v = MeetingFormView.For(running);

        Assert.Equal("Transcribing 1/3…", v.Body);
        Assert.False(v.CanCopy);
    }

    [Fact]
    public void For_Done_ShowsTheSummary_AndIsCopyable()
    {
        PipelineView done = PipelineView.Map(
            new PipelinePoll { State = "done", Summary = new PipelineSummary { Summary = "decided to ship" } });

        MeetingFormView v = MeetingFormView.For(done);

        Assert.Equal("decided to ship", v.Body);
        Assert.Equal("Meeting summary", v.Caption);
        Assert.True(v.CanCopy);
    }

    [Fact]
    public void For_Done_MarksBodyAsMarkdown()
    {
        PipelineView done = PipelineView.Map(
            new PipelinePoll { State = "done", Summary = new PipelineSummary { Summary = "## Notes\n- a" } });

        Assert.True(MeetingFormView.For(done).BodyIsMarkdown); // the summary is the only rich-markdown body
    }

    [Theory]
    [InlineData(null)] // loading
    [InlineData("running")]
    [InlineData("failed")]
    [InlineData("idle")]
    public void For_NonDone_BodyIsPlainNotMarkdown(string? state)
    {
        PipelineView? view = state is null ? null : PipelineView.Map(new PipelinePoll { State = state });

        Assert.False(MeetingFormView.For(view).BodyIsMarkdown); // status/failure lines render verbatim
    }

    [Fact]
    public void For_Done_WithModel_CaptionNamesTheModel()
    {
        PipelineView done = PipelineView.Map(new PipelinePoll
        {
            State = "done",
            Summary = new PipelineSummary { Summary = "notes", Model = "qwen3" },
        });

        Assert.Equal("Meeting summary · qwen3", MeetingFormView.For(done).Caption);
    }

    [Fact]
    public void For_Done_WithEmptySummary_IsNotCopyable()
    {
        PipelineView done = PipelineView.Map(
            new PipelinePoll { State = "done", Summary = new PipelineSummary { Summary = "" } });

        Assert.False(MeetingFormView.For(done).CanCopy); // nothing to copy
    }

    [Fact]
    public void For_Failed_ShowsTheReason_AndNotCopyable()
    {
        PipelineView failed = PipelineView.Map(
            new PipelinePoll { State = "failed", Stage = "transcribe", ErrorKind = "NoUsableWavs" });

        MeetingFormView v = MeetingFormView.For(failed);

        Assert.Contains("No usable audio", v.Body, StringComparison.Ordinal);
        Assert.False(v.CanCopy);
    }

    [Fact]
    public void For_Unavailable_ShowsTheGoneReason()
    {
        MeetingFormView v = MeetingFormView.For(PipelineView.Unavailable("This meeting is no longer available on the recorder."));

        Assert.Contains("no longer available", v.Body, StringComparison.OrdinalIgnoreCase);
        Assert.False(v.CanCopy);
    }

    [Fact]
    public void For_Idle_ShowsANeutralNoSummaryState()
    {
        PipelineView idle = PipelineView.Map(new PipelinePoll { State = "idle" });

        MeetingFormView v = MeetingFormView.For(idle);

        Assert.Contains("No summary", v.Body, StringComparison.Ordinal);
        Assert.False(v.CanCopy);
    }
}
