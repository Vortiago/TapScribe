using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS.Tests;

/// <summary>
/// How the Mac Settings window seeds its draft (#419). The window is AppKit and cannot be
/// tested; the seeding is not, and it is the half that can silently destroy an operator's
/// saved selection.
///
/// <see cref="SettingsDraft.ToSettings"/> collects the pin grid and the pins whose device is
/// absent, and both of those are populated by <see cref="SettingsDraft.SetAvailableDevices"/>.
/// A window that never calls it therefore saves a settings file with every pin gone, which
/// looks exactly like a working Save. The Mac window has no pin grid yet (slice 9 owns
/// device parity), so the call it makes for correctness rather than for display is precisely
/// the thing worth pinning.
/// </summary>
public class SettingsSeedTests
{
    private static readonly GateSettings SavedGate = new(42, 800, 300);

    [Fact]
    public void From_CarriesTheConnectionSettingsIntoTheDraft()
    {
        var current = new BridgeSettings { Host = "recorder.local", Port = 9443, Tls = true, Token = "tap-token" };

        SettingsDraft draft = SettingsSeed.From(current, static () => []);

        Assert.Equal("recorder.local", draft.Host);
        Assert.Equal(9443, draft.Port);
        Assert.True(draft.Tls);
        Assert.Equal("tap-token", draft.Token);
    }

    [Fact]
    public void From_KeepsAPinWhoseDeviceIsPresent()
    {
        var current = new BridgeSettings
        {
            Devices = [new DeviceSelection.Pinned("uid-1", "Desk mic", "Desk mic", SavedGate)],
        };

        SettingsDraft draft = SettingsSeed.From(
            current, static () => [new CaptureDevice("uid-1", "Desk mic", DeviceFlow.Capture, IsDefault: true)]);

        DeviceSelection.Pinned saved = Assert.Single(draft.ToSettings().Devices.OfType<DeviceSelection.Pinned>());
        Assert.Equal("uid-1", saved.DeviceId);
    }

    [Fact]
    public void From_KeepsAPinWhoseDeviceIsGone()
    {
        // An unplugged interface must not quietly erase the operator's pin on the next Save.
        var current = new BridgeSettings
        {
            Devices = [new DeviceSelection.Pinned("uid-gone", "Desk mic", "Desk mic", SavedGate)],
        };

        SettingsDraft draft = SettingsSeed.From(current, static () => []);

        DeviceSelection.Pinned saved = Assert.Single(draft.ToSettings().Devices.OfType<DeviceSelection.Pinned>());
        Assert.Equal("uid-gone", saved.DeviceId);
    }

    [Fact]
    public void From_WhenTheDevicesCannotBeListed_StillOpensAndKeepsEveryPin()
    {
        // CoreAudio refusing to walk the device tree is the seam's declared failure, and it
        // must not stop the operator fixing a wrong host or a rejected token. Seeding with no
        // devices is what makes that safe: every saved pin is then an ABSENT one, which the
        // draft carries forward verbatim rather than dropping.
        var current = new BridgeSettings
        {
            Devices = [new DeviceSelection.Pinned("uid-1", "Desk mic", "Desk mic", SavedGate)],
        };

        SettingsDraft draft = SettingsSeed.From(
            current, static IReadOnlyList<CaptureDevice> () => throw new ExternalException("no audio service"));

        DeviceSelection.Pinned saved = Assert.Single(draft.ToSettings().Devices.OfType<DeviceSelection.Pinned>());
        Assert.Equal("uid-1", saved.DeviceId);
    }

    [Fact]
    public void From_KeepsThePinnedDevicesGateThroughASave()
    {
        // A pinned device has no sensitivity control, so its tuning survives only by being
        // recovered from the saved selection. Losing it on every Save would silently retune a
        // device the operator deliberately tuned.
        var current = new BridgeSettings
        {
            Devices = [new DeviceSelection.Pinned("uid-1", "Desk mic", "Desk mic", SavedGate)],
        };

        SettingsDraft draft = SettingsSeed.From(
            current, static () => [new CaptureDevice("uid-1", "Desk mic", DeviceFlow.Capture, IsDefault: false)]);

        DeviceSelection.Pinned saved = Assert.Single(draft.ToSettings().Devices.OfType<DeviceSelection.Pinned>());
        Assert.Equal(SavedGate.Sensitivity, saved.Gate!.Sensitivity);
    }
}
