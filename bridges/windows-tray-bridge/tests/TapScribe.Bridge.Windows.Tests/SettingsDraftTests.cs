using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Windows.Tests;

/// <summary>
/// Tests for <see cref="SettingsDraft"/> — the pure editable state behind the tray
/// Settings dialog, seeded from a <see cref="BridgeSettings"/> and collected back into
/// one. This is the logic the WinForms <c>SettingsForm</c> used to hold inline (and so
/// couldn't be tested on the cross-platform runner): which selection to build per device,
/// how the per-device gate is sourced, migration-aware seeding, and the absent-pin
/// carry-forward. No WinForms here — the form is now a thin binding over these methods.
/// (Token is left empty throughout so nothing hits DPAPI, which is Windows-only.)
/// </summary>
public class SettingsDraftTests
{
    private static CaptureDevice Mic(string id, bool isDefault = false) =>
        new(id, $"Mic {id}", DeviceFlow.Capture, isDefault);

    private static CaptureDevice Speakers(string id, bool isDefault = false) =>
        new(id, $"Speakers {id}", DeviceFlow.Render, isDefault);

    [Fact]
    public void Seed_CopiesConnectionFields()
    {
        var current = new BridgeSettings { Host = "rec.example", Port = 9100, Tls = true };

        SettingsDraft draft = SettingsDraft.Seed(current);

        Assert.Equal("rec.example", draft.Host);
        Assert.Equal(9100, draft.Port);
        Assert.True(draft.Tls);
    }

    [Fact]
    public void Seed_TicksAndNamesTheSavedFollowDefaults()
    {
        var current = new BridgeSettings
        {
            Devices =
            [
                new DeviceSelection.FollowDefault(DeviceFlow.Capture, "Alice", "Alice", new GateSettings(40, 800, 300)),
                new DeviceSelection.FollowDefault(DeviceFlow.Render, "System", "System", new GateSettings(75, 800, 300)),
            ],
        };

        SettingsDraft draft = SettingsDraft.Seed(current);

        Assert.True(draft.MicEnabled);
        Assert.Equal("Alice", draft.MicName);
        Assert.Equal(40, draft.MicSensitivity);
        Assert.True(draft.SystemEnabled);
        Assert.Equal("System", draft.SystemName);
        Assert.Equal(75, draft.SystemSensitivity);
    }

    [Fact]
    public void Seed_BrandNewFile_UsesPerFlowDefaultSensitivities()
    {
        // No saved devices, no legacy value: the default pair seeds the sliders with the
        // per-flow defaults — system loopback more sensitive than the mic (ADR-0007).
        SettingsDraft draft = SettingsDraft.Seed(new BridgeSettings { Identity = "alice", Name = "Alice" });

        Assert.Equal(GateSettings.DefaultForFlow(DeviceFlow.Capture).Sensitivity, draft.MicSensitivity);
        Assert.Equal(GateSettings.DefaultForFlow(DeviceFlow.Render).Sensitivity, draft.SystemSensitivity);
        Assert.True(draft.SystemSensitivity > draft.MicSensitivity);
    }

    [Fact]
    public void Seed_OldGlobalTuning_SeedsBothSlidersFromTheMigratedValue()
    {
        var current = new BridgeSettings
        {
            GateSensitivity = 72,
            GateHangoverMs = 600,
            GatePreRollMs = 150,
            Devices =
            [
                new DeviceSelection.FollowDefault(DeviceFlow.Capture, "Alice", "Alice"),
                new DeviceSelection.FollowDefault(DeviceFlow.Render, "System", "System"),
            ],
        };

        SettingsDraft draft = SettingsDraft.Seed(current);

        Assert.Equal(72, draft.MicSensitivity);
        Assert.Equal(72, draft.SystemSensitivity);
        Assert.Equal(600, draft.HangoverMs);
        Assert.Equal(150, draft.PreRollMs);
    }

    [Fact]
    public void Seed_SharedHangoverPreRoll_ComeFromTheFirstEffectiveDevice()
    {
        var current = new BridgeSettings
        {
            Devices = [new DeviceSelection.FollowDefault(DeviceFlow.Capture, "mic", "Mic", new GateSettings(50, 720, 180))],
        };

        SettingsDraft draft = SettingsDraft.Seed(current);

        Assert.Equal(720, draft.HangoverMs);
        Assert.Equal(180, draft.PreRollMs);
    }

    [Fact]
    public void SetAvailableDevices_PreTicksAndNamesSavedPins()
    {
        var current = new BridgeSettings
        {
            Devices = [new DeviceSelection.Pinned("usb", "USB Mic", "USB Mic", new GateSettings(60, 800, 300))],
        };
        SettingsDraft draft = SettingsDraft.Seed(current);

        draft.SetAvailableDevices([Mic("usb"), Mic("builtin", isDefault: true)]);

        PinnedDeviceRow usb = Assert.Single(draft.DeviceRows, r => r.DeviceId == "usb");
        Assert.True(usb.Pinned);
        Assert.Equal("USB Mic", usb.Name);
        Assert.Equal(DeviceFlow.Capture, usb.Flow);

        PinnedDeviceRow builtin = Assert.Single(draft.DeviceRows, r => r.DeviceId == "builtin");
        Assert.False(builtin.Pinned); // not pinned in the saved selection
    }

    [Fact]
    public void SetAvailableDevices_DisplayLabelShowsKindAndDefault()
    {
        SettingsDraft draft = SettingsDraft.Seed(new BridgeSettings());

        draft.SetAvailableDevices([Speakers("spk", isDefault: true)]);

        PinnedDeviceRow row = Assert.Single(draft.DeviceRows);
        Assert.Contains("loopback", row.DisplayLabel);
        Assert.Contains("default", row.DisplayLabel);
    }

    [Fact]
    public void ToSettings_BuildsFollowDefaultsWithPerDeviceGate()
    {
        SettingsDraft draft = SettingsDraft.Seed(new BridgeSettings());
        draft.MicEnabled = true;
        draft.MicName = "  Alice  "; // trimmed
        draft.MicSensitivity = 42;
        draft.SystemEnabled = true;
        draft.SystemName = "System";
        draft.SystemSensitivity = 77;
        draft.HangoverMs = 650;
        draft.PreRollMs = 200;

        BridgeSettings settings = draft.ToSettings();

        var mic = Assert.IsType<DeviceSelection.FollowDefault>(
            Assert.Single(settings.Devices, d => d is DeviceSelection.FollowDefault { Flow: DeviceFlow.Capture }));
        Assert.Equal("Alice", mic.Identity);
        Assert.Equal("Alice", mic.Name);
        Assert.Equal(new GateSettings(42, 650, 200), mic.Gate);

        var system = Assert.IsType<DeviceSelection.FollowDefault>(
            Assert.Single(settings.Devices, d => d is DeviceSelection.FollowDefault { Flow: DeviceFlow.Render }));
        Assert.Equal(new GateSettings(77, 650, 200), system.Gate);
    }

    [Fact]
    public void ToSettings_OmitsAnUntickedFollowDefault()
    {
        SettingsDraft draft = SettingsDraft.Seed(new BridgeSettings());
        draft.MicEnabled = true;
        draft.MicName = "Alice";
        draft.SystemEnabled = false;

        BridgeSettings settings = draft.ToSettings();

        Assert.DoesNotContain(settings.Devices, d => d is DeviceSelection.FollowDefault { Flow: DeviceFlow.Render });
    }

    [Fact]
    public void ToSettings_PinnedRowWithoutPriorGate_UsesItsFlowDefault()
    {
        SettingsDraft draft = SettingsDraft.Seed(new BridgeSettings());
        draft.SetAvailableDevices([Speakers("spk")]);
        PinnedDeviceRow row = Assert.Single(draft.DeviceRows);
        row.Pinned = true;
        row.Name = "Loopback";

        DeviceSelection.Pinned pinned = Assert.IsType<DeviceSelection.Pinned>(
            Assert.Single(draft.ToSettings().Devices, d => d is DeviceSelection.Pinned));
        // A freshly-pinned render device gets the sensitive loopback default.
        Assert.Equal(GateSettings.DefaultForFlow(DeviceFlow.Render).Sensitivity, pinned.Gate!.Sensitivity);
    }

    [Fact]
    public void ToSettings_PinnedDeviceWithMigratedGlobalGate_KeepsTheMigratedSensitivity()
    {
        // The regression this whole refactor makes testable: an upgrade with a global gate
        // + a pinned device must keep the migrated sensitivity on Save, not reset it.
        var current = new BridgeSettings
        {
            GateSensitivity = 33, // legacy global -> migrates onto the pinned device
            Devices = [new DeviceSelection.Pinned("usb", "USB", "USB")], // no per-device gate (old file)
        };
        SettingsDraft draft = SettingsDraft.Seed(current);
        draft.SetAvailableDevices([Mic("usb")]);
        // The user opens Settings and Saves without touching the pin (it stays ticked).

        DeviceSelection.Pinned pinned = Assert.IsType<DeviceSelection.Pinned>(
            Assert.Single(draft.ToSettings().Devices, d => d is DeviceSelection.Pinned));
        Assert.Equal(33, pinned.Gate!.Sensitivity); // migrated value preserved, not reset to the flow default
    }

    [Fact]
    public void ToSettings_CarriesForwardAPinWhoseDeviceIsAbsent()
    {
        var absent = new DeviceSelection.Pinned("unplugged", "Field Mic", "Field Mic", new GateSettings(55, 800, 300));
        var current = new BridgeSettings { Devices = [absent] };
        SettingsDraft draft = SettingsDraft.Seed(current);
        draft.SetAvailableDevices([Mic("builtin", isDefault: true)]); // the pinned device is gone

        DeviceSelection.Pinned carried = Assert.IsType<DeviceSelection.Pinned>(
            Assert.Single(draft.ToSettings().Devices, d => d is DeviceSelection.Pinned p && p.DeviceId == "unplugged"));
        Assert.Equal(absent.Gate, carried.Gate); // verbatim, not erased or re-defaulted
    }

    [Fact]
    public void ToSettings_LeavesLegacyGlobalGateNull_AndPassesBaseIdentityThrough()
    {
        var current = new BridgeSettings { Identity = "base-id", Name = "Base Name", GateSensitivity = 99 };
        SettingsDraft draft = SettingsDraft.Seed(current);
        draft.MicEnabled = true;
        draft.MicName = "Alice";

        BridgeSettings settings = draft.ToSettings();

        Assert.Null(settings.GateSensitivity);
        Assert.Null(settings.GateHangoverMs);
        Assert.Null(settings.GatePreRollMs);
        Assert.Equal("base-id", settings.Identity);
        Assert.Equal("Base Name", settings.Name);
    }

    [Fact]
    public void SeedThenToSettings_RoundTripsTheFollowDefaultPairsGate()
    {
        var current = new BridgeSettings
        {
            Devices =
            [
                new DeviceSelection.FollowDefault(DeviceFlow.Capture, "Alice", "Alice", new GateSettings(45, 800, 300)),
                new DeviceSelection.FollowDefault(DeviceFlow.Render, "System", "System", new GateSettings(70, 800, 300)),
            ],
        };

        // Seed -> (no edits) -> ToSettings should preserve each device's tuning.
        BridgeSettings round = SettingsDraft.Seed(current).ToSettings();

        var mic = Assert.IsType<DeviceSelection.FollowDefault>(
            Assert.Single(round.Devices, d => d is DeviceSelection.FollowDefault { Flow: DeviceFlow.Capture }));
        Assert.Equal(new GateSettings(45, 800, 300), mic.Gate);
        var system = Assert.IsType<DeviceSelection.FollowDefault>(
            Assert.Single(round.Devices, d => d is DeviceSelection.FollowDefault { Flow: DeviceFlow.Render }));
        Assert.Equal(new GateSettings(70, 800, 300), system.Gate);
    }

    [Fact]
    public void HasSavedPins_TrueOnlyWhenASavedSelectionPinnedADevice()
    {
        Assert.False(SettingsDraft.Seed(new BridgeSettings()).HasSavedPins);
        Assert.False(SettingsDraft.Seed(new BridgeSettings
        {
            Devices = [new DeviceSelection.FollowDefault(DeviceFlow.Capture, "mic", "Mic")],
        }).HasSavedPins);

        SettingsDraft withPin = SettingsDraft.Seed(new BridgeSettings
        {
            Devices = [new DeviceSelection.Pinned("usb", "USB", "USB")],
        });
        Assert.True(withPin.HasSavedPins);
    }

    [Fact]
    public void SensitivityLabel_ShowsTheSliderAndItsThreshold()
    {
        string label = SettingsDraft.SensitivityLabel(50);

        Assert.Contains("50 / 100", label);
        Assert.Contains("0.", label); // the RMS-threshold readout
    }
}
