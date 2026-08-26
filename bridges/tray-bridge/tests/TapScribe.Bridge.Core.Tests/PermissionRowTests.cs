using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// What the Settings dialog says about a permission, and what its button does (#420). The
/// operator-visible half of the worst failure this product has: a dismissed prompt records a
/// silent microphone, and nothing anywhere said so.
///
/// The decision is here rather than in the shell for the usual reason, and one extra: the state
/// comes from a platform API the shell cannot be tested against, so if the MAPPING lived there
/// too, nothing about this feature would be tested at all.
/// </summary>
public class PermissionRowTests
{
    [Fact]
    public void For_APermissionMacOSHasNotAskedFor_OffersToAskNow()
    {
        // The only state macOS will still show a prompt for. Asking here, in a dialog the
        // operator opened deliberately, beats the first prompt landing mid-Start.
        PermissionRow row = PermissionRow.For("Microphone", PermissionState.NotDetermined);

        Assert.Equal(PermissionAction.Request, row.Action);
        Assert.False(string.IsNullOrWhiteSpace(row.ActionLabel));
    }

    [Fact]
    public void For_ADeniedPermission_SendsThemToSystemSettings()
    {
        // macOS never prompts twice: once denied, the API returns denied without showing
        // anything. A "Request" button here would do nothing at all, which is worse than no
        // button, so the only honest offer is the place that can still change it.
        PermissionRow row = PermissionRow.For("Microphone", PermissionState.Denied);

        Assert.Equal(PermissionAction.OpenSettings, row.Action);
        Assert.Contains("denied", row.Detail, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void For_AGrantedPermission_OffersNothing()
    {
        PermissionRow row = PermissionRow.For("Microphone", PermissionState.Granted);

        Assert.Equal(PermissionAction.None, row.Action);
        Assert.Null(row.ActionLabel);
    }

    [Fact]
    public void For_APermissionMacOSWillNotReportOn_SaysSo_AndStillOffersSystemSettings()
    {
        // System Audio Recording. macOS 14.4 gained the process tap and no API to ask about its
        // consent, so the tray cannot know. Saying "unknown" is the honest answer: claiming
        // granted would be the same silent lie the badge and this dialog exist to end, and
        // claiming denied would send operators to fix something that is probably fine.
        PermissionRow row = PermissionRow.For("System Audio Recording", PermissionState.Unknown);

        Assert.Equal(PermissionAction.OpenSettings, row.Action);
        Assert.Contains("cannot", row.Detail, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void For_EveryState_SaysWhichPermissionItIsAbout()
    {
        // Two rows sit side by side and one of them is the one that matters; a detail line that
        // does not name its permission is a row an operator cannot act on.
        foreach (PermissionState state in Enum.GetValues<PermissionState>())
            Assert.Equal("System Audio Recording", PermissionRow.For("System Audio Recording", state).Title);
    }
}
