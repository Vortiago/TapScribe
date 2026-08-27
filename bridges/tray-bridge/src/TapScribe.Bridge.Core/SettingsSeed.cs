using System.Runtime.InteropServices;

namespace TapScribe.Bridge.Core;

/// <summary>The endpoints a Settings dialog could list, and why it could not. Empty
/// <paramref name="Devices"/> means "none" when <paramref name="Error"/> is null and "unknown"
/// when it is not: the distinction <see cref="IAudioDeviceEnumerator.List"/> draws in its own
/// contract, and a dialog that cannot see it says "no devices" to an operator who has some.
/// </summary>
public readonly record struct DeviceListing(IReadOnlyList<CaptureDevice> Devices, string? Error);

/// <summary>
/// Seeding a Settings dialog's draft: the part of opening one that can lose a saved selection.
/// <see cref="SettingsDraft.ToSettings"/> collects the pin grid and the absent pins from
/// <see cref="SettingsDraft.SetAvailableDevices"/>, so a dialog that skips it saves a file with
/// every pin gone, and that looks exactly like a Save that worked.
/// </summary>
public static class SettingsSeed
{
    /// <param name="current">The settings the runtime is running on.</param>
    /// <param name="devices">What <see cref="Listing"/> found. Empty is valid and safe.</param>
    public static SettingsDraft From(BridgeSettings current, IReadOnlyList<CaptureDevice> devices)
    {
        ArgumentNullException.ThrowIfNull(current);
        ArgumentNullException.ThrowIfNull(devices);

        SettingsDraft draft = SettingsDraft.Seed(current);
        draft.SetAvailableDevices(devices);
        return draft;
    }

    /// <summary>List the endpoints present now, reporting a failed walk rather than throwing:
    /// the dialog must stay open so a wrong host or a rejected token is still fixable, and an
    /// empty list is safe for the draft, which carries an absent pin forward verbatim.</summary>
    public static DeviceListing Listing(Func<IReadOnlyList<CaptureDevice>> listDevices)
    {
        ArgumentNullException.ThrowIfNull(listDevices);

        try
        {
            return new DeviceListing(listDevices(), null);
        }
        catch (ExternalException ex)
        {
            // List's whole declared set, so it names the type rather than calling CaptureSeam.
            // Reported, not swallowed: what must survive is that the grid is empty because the
            // question failed, not because the operator has no devices.
            return new DeviceListing([], ex.Message);
        }
    }
}
