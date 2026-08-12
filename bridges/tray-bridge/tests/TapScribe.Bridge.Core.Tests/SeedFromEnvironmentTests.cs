using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The classes that read or MUTATE the TAPSCRIBE_* environment variables, serialised
/// against each other. In the Windows assembly this was an assembly-wide
/// <c>DisableTestParallelization</c>, which is the wrong instrument here: it would
/// serialise the whole Core suite (including the slower async pipeline tests) to protect
/// two classes. xunit runs the classes WITHIN a collection sequentially while other
/// collections still run in parallel, so the fix is scoped to the classes that race.
/// </summary>
[CollectionDefinition(Name)]
public sealed class EnvironmentSeedCollection
{
    public const string Name = "tapscribe-environment-seed";
}

/// <summary>
/// First-run seeding: with no settings file yet, the connection fields come from the legacy
/// TAPSCRIBE_* environment variables when they are set, so an existing env-based setup
/// keeps working, and from sensible defaults when they are not.
/// </summary>
[Collection(EnvironmentSeedCollection.Name)]
public class SeedFromEnvironmentTests
{
    private static readonly string[] Keys =
    [
        "TAPSCRIBE_HOST", "TAPSCRIBE_PORT", "TAPSCRIBE_TLS", "TAPSCRIBE_TLS_ALLOW_SELF_SIGNED",
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
                ["TAPSCRIBE_TLS_ALLOW_SELF_SIGNED"] = "1",
                ["TAPSCRIBE_IDENTITY"] = "alice",
                ["TAPSCRIBE_NAME"] = "Alice B",
            },
            () =>
            {
                BridgeSettings seeded = BridgeSettings.SeedFromEnvironment();

                Assert.Equal("rec.example", seeded.Host);
                Assert.Equal(9200, seeded.Port);
                Assert.True(seeded.Tls);
                Assert.True(seeded.AllowSelfSignedCert);
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
                ["TAPSCRIBE_TLS_ALLOW_SELF_SIGNED"] = null,
                ["TAPSCRIBE_IDENTITY"] = null,
            },
            () =>
            {
                BridgeSettings seeded = BridgeSettings.SeedFromEnvironment();

                Assert.Equal("localhost", seeded.Host);
                Assert.Equal(8001, seeded.Port);
                Assert.False(seeded.Tls);
                Assert.False(seeded.AllowSelfSignedCert); // insecure opt-in is off by default
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
