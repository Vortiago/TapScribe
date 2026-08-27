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

        Assert.Equal(PermissionAction.Request, row.Button?.Action);
        Assert.False(string.IsNullOrWhiteSpace(row.Button?.Label));
    }

    [Fact]
    public void For_ADeniedPermission_SendsThemToSystemSettings()
    {
        // macOS never prompts twice: once denied, the API returns denied without showing
        // anything. A "Request" button here would do nothing at all, which is worse than no
        // button, so the only honest offer is the place that can still change it.
        PermissionRow row = PermissionRow.For("Microphone", PermissionState.Denied);

        Assert.Equal(PermissionAction.OpenSettings, row.Button?.Action);
        Assert.Contains("denied", row.Detail, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void For_AGrantedPermission_OffersNothing()
    {
        PermissionRow row = PermissionRow.For("Microphone", PermissionState.Granted);

        Assert.Null(row.Button);
    }

    [Fact]
    public void For_APermissionMacOSWillNotReportOn_SaysSo_AndStillOffersSystemSettings()
    {
        // System Audio Recording, permanently: see PermissionState.Unknown for why macOS cannot
        // be asked, and PermissionRow.For for why unknown is reported rather than guessed.
        PermissionRow row = PermissionRow.For("System Audio Recording", PermissionState.Unknown);

        Assert.Equal(PermissionAction.OpenSettings, row.Button?.Action);
        Assert.Contains("cannot", row.Detail, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void For_AStateItDoesNotKnow_Refuses()
    {
        // A state added in Core must not silently inherit another one's copy, which makes a
        // specific claim about a specific permission.
        Assert.Throws<ArgumentOutOfRangeException>(
            () => PermissionRow.For("Microphone", (PermissionState)999));
    }
}
