namespace TapScribe.Bridge.Core;

/// <summary>
/// What the platform says about a permission the tray needs.
/// </summary>
public enum PermissionState
{
    /// <summary>The platform offers no way to ask. macOS 14.4 gained the process tap and no API
    /// for its consent, so System Audio Recording is permanently this.</summary>
    Unknown,

    /// <summary>Never asked. The one state a prompt can still appear for.</summary>
    NotDetermined,

    /// <summary>Refused, or restricted by policy. macOS will not prompt again.</summary>
    Denied,

    /// <summary>Granted to THIS build. An ad-hoc signature changes per build, so an update
    /// starts over at <see cref="NotDetermined"/>.</summary>
    Granted,
}

/// <summary>What a permission row's button does, if it has one.</summary>
public enum PermissionAction
{
    /// <summary>Nothing to offer: it is granted.</summary>
    None,

    /// <summary>Ask the platform now, which shows the operator a prompt.</summary>
    Request,

    /// <summary>Open the system's own privacy settings, the only place a refusal can be
    /// undone.</summary>
    OpenSettings,
}

/// <summary>
/// One permission, as the Settings dialog shows it.
/// </summary>
/// <param name="Title">The permission's name, as the system's own settings spell it, so the
/// operator can find the row this sends them to.</param>
/// <param name="Detail">What the state means for their next meeting, in those terms rather than
/// in the API's.</param>
/// <param name="ActionLabel">The button's text, or null when there is nothing to offer.</param>
/// <param name="Action">What that button does.</param>
public sealed record PermissionRow(string Title, string Detail, string? ActionLabel, PermissionAction Action)
{
    /// <summary>The row for one permission in one state.</summary>
    public static PermissionRow For(string title, PermissionState state)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(title);
        return state switch
        {
            PermissionState.Granted => new PermissionRow(
                title, "Granted.", null, PermissionAction.None),

            // The only state a prompt still appears for, and asking from a dialog the operator
            // opened is kinder than the first prompt landing in the middle of a Start.
            PermissionState.NotDetermined => new PermissionRow(
                title, "Not asked for yet.", "Ask now", PermissionAction.Request),

            // Asking again does nothing: the API answers denied without showing anything, so a
            // Request button would be a button that visibly does nothing.
            PermissionState.Denied => new PermissionRow(
                title,
                "Denied. macOS will not ask again, so this has to be changed in System Settings.",
                "Open System Settings",
                PermissionAction.OpenSettings),

            // Claiming granted would be the silent lie this dialog exists to end, and claiming
            // denied would send them to fix something that is probably fine.
            _ => new PermissionRow(
                title,
                "macOS cannot be asked about this one. It is requested at the first Start meeting.",
                "Open System Settings",
                PermissionAction.OpenSettings),
        };
    }
}
