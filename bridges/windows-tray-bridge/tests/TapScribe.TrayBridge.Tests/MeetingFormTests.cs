using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// Windows E2E for the per-meeting window's render projection (#168): a REAL
/// <see cref="MeetingForm"/> constructed on an STA thread, <c>Render</c>ed with each
/// <see cref="PipelineView"/> phase, asserting the actual WinForms controls reflect the
/// (separately, cross-platform-tested) <c>MeetingFormView</c>. This pins the WinForms wiring —
/// view-model → TextBox/Label/Button — which only runs on the windows dotnet-build CI job.
/// </summary>
public class MeetingFormTests
{
    [Fact]
    public void Render_StartsInLoading_WithCopyDisabled() => Sta.Run(() =>
    {
        using var form = new MeetingForm(); // ctor seeds the Loading state

        Assert.Equal("Loading…", form.CurrentCaption());
        Assert.False(form.CopyEnabled());
    });

    [Fact]
    public void Render_Done_ShowsTheSummary_CaptionNamesTheModel_AndEnablesCopy() => Sta.Run(() =>
    {
        using var form = new MeetingForm();

        form.Render(PipelineView.Map(new PipelinePoll
        {
            State = "done",
            Summary = new PipelineSummary { Summary = "decided to ship", Model = "qwen3" },
        }));

        Assert.Equal("decided to ship", form.CurrentBodyText());
        Assert.Equal("Meeting summary · qwen3", form.CurrentCaption());
        Assert.True(form.CopyEnabled());
    });

    [Fact]
    public void Render_Running_ShowsTheProgressLine_WithCopyDisabled() => Sta.Run(() =>
    {
        using var form = new MeetingForm();

        form.Render(PipelineView.Map(
            new PipelinePoll { State = "running", Stage = "transcribe", Current = 1, Total = 3 }));

        Assert.Equal("Transcribing 1/3…", form.CurrentBodyText());
        Assert.False(form.CopyEnabled());
    });

    [Fact]
    public void Render_Unavailable_ShowsTheGoneMessage_WithCopyDisabled() => Sta.Run(() =>
    {
        using var form = new MeetingForm();

        form.Render(PipelineView.Unavailable("This meeting is no longer available on the recorder."));

        Assert.Contains("no longer available", form.CurrentBodyText(), StringComparison.OrdinalIgnoreCase);
        Assert.False(form.CopyEnabled());
    });
}
