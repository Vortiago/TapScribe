namespace TapScribe.Bundle.Core.Tests;

/// <summary>
/// When a Bundle's Quit asks first. It stops the Recorder this tray started, and that
/// Recorder's jobs die with it and do not resume, so the operator is asked while one is in
/// flight — and only then: a Quit held up by a failed probe, or by a Recorder somebody else
/// owns, is a tray that will not go away.
/// </summary>
public class QuitConfirmationTests
{
    [Fact]
    public void AJobInFlightOnOurRecorder_Warns()
    {
        string? warning = QuitConfirmation.WarningFor(stopsRecorder: true, activeJobs: 1);

        Assert.NotNull(warning);
        Assert.Contains("processing", warning);
        Assert.Contains("dashboard", warning); // says where the work can be run again
    }

    [Fact]
    public void NothingInFlight_QuitsStraightAway()
    {
        Assert.Null(QuitConfirmation.WarningFor(stopsRecorder: true, activeJobs: 0));
    }

    [Fact]
    public void ARecorderThatCouldNotBeAsked_IsNotTreatedAsBusy()
    {
        Assert.Null(QuitConfirmation.WarningFor(stopsRecorder: true, activeJobs: null));
    }

    [Fact]
    public void ARecorderThisTrayDoesNotStop_IsNotItsToWarnAbout()
    {
        // Somebody else's Recorder (a start.sh, another account's install) outlives the Quit,
        // and so does whatever it is working on.
        Assert.Null(QuitConfirmation.WarningFor(stopsRecorder: false, activeJobs: 3));
    }
}
