// These tests mutate process environment variables, and the BridgeSettingsStore
// fallback tests also read them (via SeedFromEnvironment), so run this small
// assembly's tests sequentially to avoid cross-test env races.
[assembly: Xunit.CollectionBehavior(DisableTestParallelization = true)]

namespace TapScribe.Bridge.Windows.Tests;

public class SeedFromEnvironmentTests
{
    private static readonly string[] Keys =
    [
        "TAPSCRIBE_HOST", "TAPSCRIBE_PORT", "TAPSCRIBE_TLS",
        "TAPSCRIBE_IDENTITY", "TAPSCRIBE_NAME", "TAPSCRIBE_TAP_TOKEN",
    ];

    [Fact]
    public void SeedFromEnvironment_ReadsTapscribeVariables_WhenSet()
    {
        WithEnv(
            new()
            {
                ["TAPSCRIBE_HOST"] = "rec.example",
                ["TAPSCRIBE_PORT"] = "9200",
                ["TAPSCRIBE_TLS"] = "1",
                ["TAPSCRIBE_IDENTITY"] = "alice",
                ["TAPSCRIBE_NAME"] = "Alice B",
            },
            () =>
            {
                BridgeSettings seeded = BridgeSettings.SeedFromEnvironment();

                Assert.Equal("rec.example", seeded.Host);
                Assert.Equal(9200, seeded.Port);
                Assert.True(seeded.Tls);
                Assert.Equal("alice", seeded.Identity);
                Assert.Equal("Alice B", seeded.Name);
            });
    }

    [Fact]
    public void SeedFromEnvironment_UsesSensibleDefaults_WhenUnset()
    {
        WithEnv(
            new()
            {
                ["TAPSCRIBE_HOST"] = null,
                ["TAPSCRIBE_PORT"] = null,
                ["TAPSCRIBE_TLS"] = null,
                ["TAPSCRIBE_IDENTITY"] = null,
            },
            () =>
            {
                BridgeSettings seeded = BridgeSettings.SeedFromEnvironment();

                Assert.Equal("localhost", seeded.Host);
                Assert.Equal(8001, seeded.Port);
                Assert.False(seeded.Tls);
                Assert.False(string.IsNullOrEmpty(seeded.Identity)); // username / "windows-tray"
            });
    }

    /// <summary>Set the given env vars, run the body, then restore every key to its prior value.</summary>
    private static void WithEnv(Dictionary<string, string?> values, Action body)
    {
        var originals = Keys.ToDictionary(k => k, Environment.GetEnvironmentVariable);
        try
        {
            foreach ((string key, string? value) in values)
                Environment.SetEnvironmentVariable(key, value);
            body();
        }
        finally
        {
            foreach ((string key, string? original) in originals)
                Environment.SetEnvironmentVariable(key, original);
        }
    }
}
