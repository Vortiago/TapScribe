namespace TapScribe.Bridge.Core;

/// <summary>
/// The tap token at rest. Two methods, platform-defined meaning: Windows protects the
/// token with DPAPI and keeps the opaque blob IN the settings file, macOS keeps the secret
/// in the Keychain and puts nothing in the file at all. <see cref="BridgeSettingsStore"/>
/// calls this on Load/Save so the portable half never sees a platform secret API.
/// </summary>
public interface ITapTokenStore
{
    /// <summary>Plaintext from the opaque value in the settings file (may be null).</summary>
    string Read(string? atRest);

    /// <summary>
    /// The opaque value to persist in the settings file, or null when the secret lives
    /// out-of-band (the macOS Keychain case).
    /// </summary>
    string? Write(string token);
}
