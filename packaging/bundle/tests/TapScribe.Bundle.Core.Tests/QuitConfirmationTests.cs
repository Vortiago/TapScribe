namespace TapScribe.Bundle.Core.Tests;

/// <summary>
/// What a Bundle's Quit says before it stops the Recorder this tray started, whose jobs die
/// with it and do not resume. Only a job in flight earns the question: a Quit held up by a
/// failed probe is a tray that will not go away. The flow around it (whose Recorder is asked,
/// the second click, a dialog that throws) is <c>HostControllerTests</c>'.
/// </summary>
public class QuitConfirmationTests
{
    [Fact]
    public void AJobInFlight_Warns()
    {
        string? warning = QuitConfirmation.WarningFor(activeJobs: 1);

        Assert.NotNull(warning);
        Assert.Contains("processing", warning);
        Assert.Contains("dashboard", warning); // says where the work can be run again
    }

    [Fact]
    public void NothingInFlight_QuitsStraightAway()
    {
        Assert.Null(QuitConfirmation.WarningFor(activeJobs: 0));
    }

    [Fact]
    public void ARecorderThatCouldNotBeAsked_IsNotTreatedAsBusy()
    {
        Assert.Null(QuitConfirmation.WarningFor(activeJobs: null));
    }
}
