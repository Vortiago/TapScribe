using System.Text.Json;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The settings MODEL, with no file and no platform in sight: what the JSON shape is, how
/// an old file's single global gate tuning migrates into per-device gates (ADR-0007), what
/// the default device pair is, and what a tap's connection options come out as. Persistence
/// — including everything about the token at rest — belongs to
/// <see cref="BridgeSettingsStoreTests"/>, since the model itself no longer knows how a
/// token is protected.
/// </summary>
public class BridgeSettingsTests
{
    [Fact]
    public void Token_IsNeverSerialized()
    {
        // The plaintext token is in-memory only: the store decides what (if anything) goes
        // on disk in its place. A change that dropped [JsonIgnore] would fail here, and the
        // README's security claim with it.
        const string secret = "super-secret-tap-token-abc123";

        string json = JsonSerializer.Serialize(new BridgeSettings { Host = "rec.example", Token = secret });

        Assert.DoesNotContain(secret, json);
        Assert.DoesNotContain("\"Token\"", json);
    }

    [Fact]
    public void ToConnectionOptions_FillsSensibleFallbacks()
    {
        var settings = new BridgeSettings { Host = "", Port = 9000, Identity = "" };

        TapConnectionOptions options = settings.ToConnectionOptions();

        Assert.Equal("localhost", options.Host);                  // blank host -> localhost
        Assert.Equal(9000, options.Port);
        Assert.False(string.IsNullOrEmpty(options.Identity));     // blank identity -> username/windows-tray
        Assert.True(string.IsNullOrEmpty(options.UtteranceId));   // minted per-Utterance by TapStream, not here
    }

    [Fact]
    public void ToConnectionOptions_CarriesTokenThrough()
    {
        var settings = new BridgeSettings { Token = "tok-123" };

        Assert.Equal("tok-123", settings.ToConnectionOptions().Token);
    }

    [Fact]
    public void ToConnectionOptions_CarriesAllowSelfSignedCert()
    {
        var settings = new BridgeSettings { Tls = true, AllowSelfSignedCert = true };

        Assert.True(settings.ToConnectionOptions().AllowSelfSignedCert);
    }

    [Fact]
    public void AllowSelfSignedCert_DefaultsOff()
    {
        Assert.False(new BridgeSettings().AllowSelfSignedCert);
        Assert.False(new BridgeSettings().ToConnectionOptions().AllowSelfSignedCert);
    }

    [Fact]
    public void ToConnectionOptions_WithoutTls_CarriesAllowSelfSignedCertThroughButItIsInert()
    {
        // The data layer carries the opt-in faithfully even with TLS off — the security
        // boundary is the connection site, which only wires the accept-any validator when
        // Tls && AllowSelfSignedCert (so this combination is harmless: no cert is validated
        // on a ws:// connection). Pinned so the carry-through stays decoupled from the gate.
        var settings = new BridgeSettings { Tls = false, AllowSelfSignedCert = true };

        TapConnectionOptions options = settings.ToConnectionOptions();

        Assert.False(options.Tls);
        Assert.True(options.AllowSelfSignedCert); // present but inert without Tls
    }

    [Fact]
    public void AllowSelfSignedCert_RoundTripsThroughJson()
    {
        var original = new BridgeSettings { Tls = true, AllowSelfSignedCert = true };

        string json = JsonSerializer.Serialize(original);
        BridgeSettings restored = JsonSerializer.Deserialize<BridgeSettings>(json)!;

        Assert.True(restored.AllowSelfSignedCert);
    }

    [Fact]
    public void EffectiveDevices_WhenNoneSaved_DefaultsToFollowDefaultMicAndLoopback()
    {
        // A pre-#106 settings file has no devices key. The effective selection must be
        // the default pair so first run / upgrade still captures mic + system audio. Each
        // carries one label used as both identity and name (the dialog edits one Name per
        // device); the mic label prefers the operator's Name, then Identity.
        var settings = new BridgeSettings { Identity = "alice", Name = "Alice" };

        IReadOnlyList<DeviceSelection> effective = settings.EffectiveDevices;

        Assert.Equal(2, effective.Count);

        var mic = Assert.IsType<DeviceSelection.FollowDefault>(effective[0]);
        Assert.Equal(DeviceFlow.Capture, mic.Flow);
        Assert.Equal("Alice", mic.Identity);
        Assert.Equal("Alice", mic.Name);

        var system = Assert.IsType<DeviceSelection.FollowDefault>(effective[1]);
        Assert.Equal(DeviceFlow.Render, system.Flow);
        Assert.Equal("System audio", system.Identity);
        Assert.Equal("System audio", system.Name);
    }

    [Fact]
    public void EffectiveDevices_WhenSelectionsSaved_UsesThemVerbatim()
    {
        var settings = new BridgeSettings
        {
            Devices = [new DeviceSelection.Pinned("usb", "mic", "USB")],
        };

        DeviceSelection only = Assert.Single(settings.EffectiveDevices);
        Assert.IsType<DeviceSelection.Pinned>(only);
    }

    [Fact]
    public void EffectiveDevices_BrandNewFile_GivesEachDeviceItsPerFlowDefaultGate()
    {
        // No saved devices, no legacy global value: the default pair gets the sensible
        // per-flow defaults — the system loopback more sensitive than the mic (ADR-0007).
        var settings = new BridgeSettings { Identity = "alice", Name = "Alice" };

        IReadOnlyList<DeviceSelection> effective = settings.EffectiveDevices;

        Assert.Equal(GateSettings.DefaultForFlow(DeviceFlow.Capture), effective[0].Gate);
        Assert.Equal(GateSettings.DefaultForFlow(DeviceFlow.Render), effective[1].Gate);
        Assert.True(
            effective[1].Gate!.ToGateOptions().OpenThreshold < effective[0].Gate!.ToGateOptions().OpenThreshold,
            "the system loopback default should open on quieter sound than the mic");
    }

    [Fact]
    public void EffectiveDevices_OldGlobalTuning_MigratesIntoEachDevicesGate()
    {
        // The migration AC: an old file's single global tuning loads as the per-device
        // default — no reset on upgrade. Both devices inherit the operator's saved value.
        var legacy = new GateSettings(Sensitivity: 72, HangoverMs: 600, PreRollMs: 150);
        var settings = new BridgeSettings
        {
            GateSensitivity = 72,
            GateHangoverMs = 600,
            GatePreRollMs = 150,
            Devices =
            [
                new DeviceSelection.FollowDefault(DeviceFlow.Capture, "alice", "Alice"),
                new DeviceSelection.FollowDefault(DeviceFlow.Render, "System", "System"),
            ],
        };

        IReadOnlyList<DeviceSelection> effective = settings.EffectiveDevices;

        Assert.Equal(legacy, effective[0].Gate);
        Assert.Equal(legacy, effective[1].Gate);
    }

    [Fact]
    public void EffectiveDevices_ExplicitPerDeviceGate_WinsOverLegacyAndDefault()
    {
        // A device that carries its own tuning is never overwritten by migration or the
        // per-flow default — the per-device value is authoritative.
        var own = new GateSettings(Sensitivity: 88, HangoverMs: 400, PreRollMs: 100);
        var settings = new BridgeSettings
        {
            GateSensitivity = 10, // a legacy value that must NOT win
            Devices = [new DeviceSelection.FollowDefault(DeviceFlow.Render, "system", "System", own)],
        };

        Assert.Equal(own, Assert.Single(settings.EffectiveDevices).Gate);
    }

    [Fact]
    public void ToGateOptionsByIdentity_MapsEachDevicesGate_KeyedByItsIdentity()
    {
        var micGate = new GateSettings(Sensitivity: 40, HangoverMs: 800, PreRollMs: 300);
        var systemGate = new GateSettings(Sensitivity: 75, HangoverMs: 800, PreRollMs: 300);
        var settings = new BridgeSettings
        {
            Devices =
            [
                new DeviceSelection.FollowDefault(DeviceFlow.Capture, "mic", "Mic", micGate),
                new DeviceSelection.FollowDefault(DeviceFlow.Render, "system", "System", systemGate),
            ],
        };

        IReadOnlyDictionary<string, GateOptions> map = settings.ToGateOptionsByIdentity();

        Assert.Equal(2, map.Count);
        Assert.Equal(micGate.ToGateOptions(), map["mic"]);
        Assert.Equal(systemGate.ToGateOptions(), map["system"]);
    }

    [Fact]
    public void ToGateOptionsByIdentity_BlankPerDeviceIdentity_KeyedByTheBaseIdentity()
    {
        // ToTapOptions stamps a blank per-device identity with the base identity, so the
        // gate map must key the same way or the live re-tune would miss that pipeline.
        var settings = new BridgeSettings
        {
            Identity = "alice",
            Devices = [new DeviceSelection.FollowDefault(DeviceFlow.Capture, "", "")],
        };

        IReadOnlyDictionary<string, GateOptions> map = settings.ToGateOptionsByIdentity();

        Assert.True(map.ContainsKey("alice"));
    }

    [Fact]
    public void PerDeviceGate_RoundTripsThroughJson()
    {
        var gate = new GateSettings(Sensitivity: 66, HangoverMs: 720, PreRollMs: 180);
        var original = new BridgeSettings
        {
            Devices = [new DeviceSelection.FollowDefault(DeviceFlow.Render, "system", "System", gate)],
        };

        string json = JsonSerializer.Serialize(original);
        BridgeSettings restored = JsonSerializer.Deserialize<BridgeSettings>(json)!;

        Assert.Equal(gate, Assert.Single(restored.Devices).Gate);
    }

    [Fact]
    public void OldGlobalTuning_SurvivesJson_AndMigratesAfterLoad()
    {
        // The realistic upgrade path: serialise a pre-per-device file (global knobs set,
        // devices with no gate), then deserialise and confirm the global value migrated
        // into each device's gate.
        var old = new BridgeSettings
        {
            GateSensitivity = 33,
            GateHangoverMs = 900,
            GatePreRollMs = 250,
            Devices =
            [
                new DeviceSelection.FollowDefault(DeviceFlow.Capture, "alice", "Alice"),
                new DeviceSelection.FollowDefault(DeviceFlow.Render, "System", "System"),
            ],
        };

        string json = JsonSerializer.Serialize(old);
        BridgeSettings restored = JsonSerializer.Deserialize<BridgeSettings>(json)!;

        var expected = new GateSettings(33, 900, 250);
        Assert.All(restored.EffectiveDevices, d => Assert.Equal(expected, d.Gate));
    }

    [Fact]
    public void NewFile_OmitsTheLegacyGlobalGateKeys()
    {
        // A file written by the per-device UI leaves the legacy fields null; they must not
        // be serialised, so a re-save can't re-introduce a global value to re-migrate.
        var settings = new BridgeSettings
        {
            Devices = [new DeviceSelection.FollowDefault(DeviceFlow.Capture, "mic", "Mic", new GateSettings(50, 800, 300))],
        };

        string json = JsonSerializer.Serialize(settings);

        Assert.DoesNotContain("GateSensitivity", json);
        Assert.DoesNotContain("GateHangoverMs", json);
        Assert.DoesNotContain("GatePreRollMs", json);
    }
}
