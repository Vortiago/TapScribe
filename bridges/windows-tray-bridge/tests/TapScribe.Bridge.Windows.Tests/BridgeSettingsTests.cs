using System.Text.Json;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Windows.Tests;

public class BridgeSettingsTests
{
    [Fact]
    public void Token_IsNeverSerializedInCleartext()
    {
        // This is the test behind the README's security claim: a future change
        // that dropped [JsonIgnore] or serialised the plaintext would fail here.
        const string secret = "super-secret-tap-token-abc123";
        var settings = new BridgeSettings { Host = "rec.example", Token = secret };

        string json = JsonSerializer.Serialize(settings);

        Assert.DoesNotContain(secret, json);       // plaintext token never on disk
        Assert.DoesNotContain("\"Token\"", json);  // the plaintext property is [JsonIgnore]
        Assert.Contains("ProtectedToken", json);    // only the DPAPI blob is persisted
    }

    [Fact]
    public void Token_SurvivesProtectSerializeDeserializeUnprotect()
    {
        var original = new BridgeSettings { Token = "round-trip-token-xyz" };

        string json = JsonSerializer.Serialize(original);
        BridgeSettings restored = JsonSerializer.Deserialize<BridgeSettings>(json)!;

        Assert.Equal("round-trip-token-xyz", restored.Token);
    }

    [Fact]
    public void Token_ReadsAsEmptyWhenBlobIsCorrupt()
    {
        // A blob written by another user/machine, or a hand-edited file, must not
        // crash the app — it reads back as "no token" so the user re-enters it.
        var settings = new BridgeSettings { ProtectedToken = "@@ not valid base64 or DPAPI @@" };

        Assert.Equal("", settings.Token);
    }

    [Fact]
    public void EmptyToken_RoundTripsAsNoToken()
    {
        var settings = new BridgeSettings { Token = "" };

        string json = JsonSerializer.Serialize(settings);
        BridgeSettings restored = JsonSerializer.Deserialize<BridgeSettings>(json)!;

        Assert.Equal("", restored.Token); // empty => --no-auth (no subprotocol offered)
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
}
