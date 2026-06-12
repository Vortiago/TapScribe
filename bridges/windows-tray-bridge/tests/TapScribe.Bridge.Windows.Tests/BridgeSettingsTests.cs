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
        Assert.False(string.IsNullOrEmpty(options.UtteranceId));  // a fresh id per call
    }

    [Fact]
    public void ToConnectionOptions_CarriesTokenThrough()
    {
        var settings = new BridgeSettings { Token = "tok-123" };

        Assert.Equal("tok-123", settings.ToConnectionOptions().Token);
    }
}
