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

    /// <summary>Granted to THIS build. An ad-hoc signature changes per build, so an update starts
    /// over at <see cref="NotDetermined"/>.</summary>
    Granted,
}

/// <summary>What a permission row's button does.</summary>
public enum PermissionAction
{
    /// <summary>Ask the platform now, which shows the operator a prompt.</summary>
    Request,

    /// <summary>Open the system's own privacy settings, the only place a refusal can be undone.
    /// </summary>
    OpenSettings,
}

/// <summary>The button a row offers, when it has one to offer.</summary>
public sealed record PermissionButton(string Label, PermissionAction Action);

/// <summary>
/// One permission, as the Settings dialog shows it.
/// </summary>
/// <param name="Title">The permission's name, as the system's own settings spell it, so the
/// operator can find the row this sends them to.</param>
/// <param name="Detail">What the state means for their next meeting, in those terms rather than in
/// the API's.</param>
/// <param name="Button">What can still be done about it, or null when it is granted. One field
/// rather than a label and an action that could disagree.</param>
public sealed record PermissionRow(string Title, string Detail, PermissionButton? Button)
{
    private static readonly PermissionButton ToSystemSettings =
        new("Open System Settings", PermissionAction.OpenSettings);

    /// <summary>The row for one permission in one state.</summary>
    public static PermissionRow For(string title, PermissionState state)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(title);
        (string detail, PermissionButton? button) = state switch
        {
            PermissionState.Granted => ("Granted.", null),

            // The only state a prompt still appears for, and asking from a dialog the operator
            // opened is kinder than the first prompt landing in the middle of a Start.
            PermissionState.NotDetermined => ("Not asked for yet.", new PermissionButton("Ask now", PermissionAction.Request)),

            // Asking again does nothing: the API answers denied without showing anything, so a
            // Request button here would be one that visibly does nothing.
            PermissionState.Denied => (
                "Denied. macOS will not ask again, so this has to be changed in System Settings.",
                ToSystemSettings),

            // Claiming granted would be the silent lie this dialog exists to end, and claiming
            // denied would send them to fix something that is probably fine.
            PermissionState.Unknown => (
                "macOS cannot be asked about this one. It is requested at the first Start meeting.",
                ToSystemSettings),

            // Named rather than defaulted, so a state added later cannot silently inherit copy
            // that makes a specific claim. Same rule as StatusSymbols.For.
            _ => throw new ArgumentOutOfRangeException(nameof(state), state, "no permission row for this state"),
        };

        return new PermissionRow(title, detail, button);
    }
}
