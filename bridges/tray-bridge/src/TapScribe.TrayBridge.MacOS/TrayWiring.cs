using TapScribe.Bridge.Core;
using TapScribe.Bridge.MacOS;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// Everything outside the shell that a meeting needs, wired for a Mac: CoreAudio behind the
/// portable enumerator seam, the detached-session mint, and the three stores. The sibling of
/// the Windows shell's <c>TrayDependencies</c>, minus the two members that were WinForms
/// facts: the menu-bar presence is the shell's own (there is no <c>Shell_NotifyIcon</c> to
/// keep at arm's length), and the loop-start hook is AppKit's own
/// <c>DidFinishLaunching</c>.
/// </summary>
internal static class TrayWiring
{
    /// <summary>Build the meeting runtime's outside world over one set of stores.</summary>
    /// <param name="stores">Where settings, restart-resume state and Past-meetings history
    /// live, and how the tap token is kept at rest.</param>
    internal static BridgeDependencies For(TrayStores stores)
    {
        ArgumentNullException.ThrowIfNull(stores);
        return new BridgeDependencies(
            // Opened per meeting and released with it, so the process holds no CoreAudio
            // handle while nothing is recording. The enumerator owns the HAL it is given.
            static () => new MacOSAudioDeviceEnumerator(new CoreAudioHal()),
            MintDetachedSessionAsync,
            stores.Settings,
            stores.MeetingState,
            stores.MeetingHistory);
    }

    /// <summary>What the app runs: the operator's own files and their login Keychain.</summary>
    internal static BridgeDependencies Production => For(TrayStores.Production);

    private static async Task<string> MintDetachedSessionAsync(
        BridgeSettings settings, CancellationToken cancellationToken)
    {
        using var control = new ControlClient(
            settings.Host, settings.Port, settings.Tls, settings.Token,
            allowSelfSignedCert: settings.AllowSelfSignedCert);
        return await control.CreateDetachedSessionAsync(cancellationToken).ConfigureAwait(false);
    }
}
