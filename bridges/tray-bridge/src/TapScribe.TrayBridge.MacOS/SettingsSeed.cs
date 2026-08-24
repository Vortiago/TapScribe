using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// Seeding the Settings window's draft, which is the part of opening it that can lose an
/// operator's saved selection.
///
/// <see cref="SettingsDraft.ToSettings"/> collects the pin grid and the pins whose device is
/// currently absent, and BOTH are populated by
/// <see cref="SettingsDraft.SetAvailableDevices"/>. So a window that skips that call saves a
/// settings file with every pin gone, and it looks exactly like a Save that worked.
/// </summary>
internal static class SettingsSeed
{
    /// <summary>Seed a draft from the settings in force, against the devices present now.</summary>
    /// <param name="current">The settings the runtime is running on.</param>
    /// <param name="listDevices">Lists the endpoints present now.</param>
    internal static SettingsDraft From(BridgeSettings current, Func<IReadOnlyList<CaptureDevice>> listDevices)
    {
        ArgumentNullException.ThrowIfNull(current);
        ArgumentNullException.ThrowIfNull(listDevices);

        SettingsDraft draft = SettingsDraft.Seed(current);
        draft.SetAvailableDevices(Devices(listDevices));
        return draft;
    }

    private static IReadOnlyList<CaptureDevice> Devices(Func<IReadOnlyList<CaptureDevice>> listDevices)
    {
        try
        {
            return listDevices();
        }
        catch (ExternalException)
        {
            // CoreAudio could not be asked to walk the device tree, the enumerator seam's declared
            // failure. Swallowed because it must not stop the operator fixing a wrong host or a
            // rejected token, and because an empty list is SAFE here: every saved pin then counts
            // as absent, and the draft carries an absent pin forward verbatim. What is lost is the
            // pin grid for this one opening of the window, which reports the empty case.
            return [];
        }
    }
}
