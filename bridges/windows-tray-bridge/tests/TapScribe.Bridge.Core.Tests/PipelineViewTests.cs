using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Pins every branch of the pure <see cref="PipelineView.Map"/> mapper with plain
/// inputs (no HTTP, no mocks) — the C# mirror of the SpatialChat Bridge's
/// <c>pipeline-view.test.js</c>, and the sibling of <see cref="StatusViewTests"/>.
/// </summary>
public class PipelineViewTests
{
    [Fact]
    public void Running_Strip_ShowsTheStrippingSilenceLabel()
    {
        PipelineView view = PipelineView.Map(new PipelinePoll { State = "running", Stage = "strip", Status = "stripping" });

        Assert.Equal("running", view.Phase);
        Assert.Equal("Stripping silence…", view.Progress);
        Assert.Equal("strip", view.Stage);
        Assert.Null(view.SummaryText);
        Assert.Null(view.FailureReason);
    }

    [Fact]
    public void Running_Transcribe_CountsWavsFromCurrentAndTotal()
    {
        PipelineView view = PipelineView.Map(
            new PipelinePoll { State = "running", Stage = "transcribe", Status = "transcribing", Current = 3, Total = 12, CurrentFile = "a.wav" });

        Assert.Equal("Transcribing 3/12…", view.Progress);
        Assert.Equal("transcribe", view.Stage);
        Assert.Equal("a.wav", view.CurrentFile);
    }

    [Fact]
    public void Running_Transcribe_WithoutCounts_FallsBackToAPlainLabel()
    {
        PipelineView view = PipelineView.Map(new PipelinePoll { State = "running", Stage = "transcribe", Total = 0 });

        Assert.Equal("Transcribing…", view.Progress);
    }

    [Fact]
    public void Running_Summarize_ShowsTheSummarizingLabel()
    {
        PipelineView view = PipelineView.Map(new PipelinePoll { State = "running", Stage = "summarize", Status = "summarizing" });

        Assert.Equal("Summarizing…", view.Progress);
    }

    [Fact]
    public void Running_WithNoStageYet_ShowsAGenericProcessingLabel()
    {
        // The instant after the trigger the job snapshot may not have attached a stage.
        PipelineView view = PipelineView.Map(new PipelinePoll { State = "running" });

        Assert.Equal("Processing…", view.Progress);
        Assert.Null(view.Stage);
    }

    [Fact]
    public void Done_ExposesTheSummaryTextAndTheSummaryObject()
    {
        PipelineView view = PipelineView.Map(new PipelinePoll
        {
            State = "done",
            Summary = new PipelineSummary { Summary = "decided to ship", Source = "local", Model = "Llama" },
        });

        Assert.Equal("done", view.Phase);
        Assert.Equal("decided to ship", view.SummaryText);
        Assert.Equal("local", view.Summary?.Source);
        Assert.Equal("Llama", view.Summary?.Model);
        Assert.False(view.KeepPolling);
    }

    [Fact]
    public void Done_WithNoSummaryText_YieldsEmptyStringNotNull()
    {
        PipelineView view = PipelineView.Map(new PipelinePoll { State = "done", Summary = new PipelineSummary() });

        Assert.Equal("done", view.Phase);
        Assert.Equal("", view.SummaryText);
    }

    [Theory]
    [InlineData("NoUsableWavs", "No usable audio")]
    [InlineData("NoMergedTranscript", "Nothing was transcribed")]
    [InlineData("SummarizerUnavailable", "isn't configured")]
    [InlineData("SummarizerFailed", "failed while writing")]
    [InlineData("InvalidRange", "audio range")]
    public void Failed_MapsKnownErrorKindsToHumanReasons_AndCarriesTheFailingStage(string kind, string fragment)
    {
        PipelineView view = PipelineView.Map(
            new PipelinePoll { State = "failed", Stage = "transcribe", Error = "boom", ErrorKind = kind });

        Assert.Equal("failed", view.Phase);
        Assert.Equal("transcribe", view.FailureStage);
        Assert.Contains(fragment, view.FailureReason, StringComparison.Ordinal);
    }

    [Fact]
    public void Failed_UnknownErrorKind_FallsBackToTheRawErrorText()
    {
        PipelineView view = PipelineView.Map(
            new PipelinePoll { State = "failed", Stage = "summarize", Error = "kernel panic", ErrorKind = "Mystery" });

        Assert.Equal("kernel panic", view.FailureReason);
    }

    [Fact]
    public void Failed_WithNoErrorAtAll_FallsBackToAGenericReason()
    {
        PipelineView view = PipelineView.Map(new PipelinePoll { State = "failed", Stage = "strip" });

        Assert.Equal("The end-of-meeting pipeline failed.", view.FailureReason);
    }

    [Fact]
    public void IdlePoll_WhileEnding_SurfacesTheEndingPhase()
    {
        PipelineView view = PipelineView.Map(new PipelinePoll { State = "idle" }, ending: true);

        Assert.Equal("ending", view.Phase);
        Assert.True(view.KeepPolling);
    }

    [Fact]
    public void IdlePoll_WhileMeetingActive_SurfacesTheRecordingPhase()
    {
        PipelineView view = PipelineView.Map(new PipelinePoll { State = "idle" }, meetingActive: true);

        Assert.Equal("recording", view.Phase);
        Assert.False(view.KeepPolling);
    }

    [Fact]
    public void IdlePoll_WithNoLocalLifecycle_IsIdle()
    {
        Assert.Equal("idle", PipelineView.Map(new PipelinePoll { State = "idle" }).Phase);
        Assert.Equal("idle", PipelineView.Map(null).Phase);
    }

    [Fact]
    public void Running_KeepsPolling()
    {
        Assert.True(PipelineView.Map(new PipelinePoll { State = "running", Stage = "strip" }).KeepPolling);
    }
}
