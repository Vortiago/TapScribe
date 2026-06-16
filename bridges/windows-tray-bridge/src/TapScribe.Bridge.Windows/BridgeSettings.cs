using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Windows;

/// <summary>
/// User-editable connection settings for the tray Bridge, persisted to
/// %APPDATA%\TapScribe so they survive restarts (PRD #99 stories 8 + 11). The
/// Settings dialog reads/writes these; environment variables only seed the
/// first-run defaults, so an operator never has to set env vars.
///
/// The tap token is a credential, so it is NEVER written to disk in cleartext:
/// it is protected at rest with Windows DPAPI (CurrentUser scope) and only the
/// opaque blob is serialised (<see cref="ProtectedToken"/>). <see cref="Token"/>
/// is the in-memory plaintext, computed on demand and marked [JsonIgnore] so it
/// can never reach the file.
/// </summary>
public sealed class BridgeSettings
{
    public string Host { get; set; } = "localhost";
    public int Port { get; set; } = 8001;
    public bool Tls { get; set; }
    public string Identity { get; set; } = "";
    public string Name { get; set; } = "";

    /// <summary>The tap token at rest: a base64 DPAPI blob, or null for --no-auth.</summary>
    public string? ProtectedToken { get; set; }

    /// <summary>The tap token in plaintext (not serialised). Empty = offer no subprotocol.</summary>
    [JsonIgnore]
    public string Token
    {
        get => TokenProtection.Unprotect(ProtectedToken);
        set => ProtectedToken = TokenProtection.Protect(value);
    }

    /// <summary>
    /// Build the connection options for a tap. The per-Utterance <c>utterance_id</c>
    /// is minted by the <see cref="TapStream"/> at each speech segment, not here.
    /// </summary>
    public TapConnectionOptions ToConnectionOptions() => new()
    {
        Host = string.IsNullOrWhiteSpace(Host) ? "localhost" : Host.Trim(),
        Port = Port,
        Tls = Tls,
        Identity = string.IsNullOrWhiteSpace(Identity) ? FallbackIdentity() : Identity.Trim(),
        Name = Name,
        Token = Token,
    };

    /// <summary>
    /// Defaults for a first run with no saved file: seed from the legacy
    /// environment variables when present (so an existing env-based setup keeps
    /// working), otherwise sensible defaults.
    /// </summary>
    public static BridgeSettings SeedFromEnvironment()
    {
        return new BridgeSettings
        {
            Host = Env("TAPSCRIBE_HOST") ?? "localhost",
            Port = int.TryParse(Env("TAPSCRIBE_PORT"), out int port) ? port : 8001,
            Tls = Env("TAPSCRIBE_TLS") is "1" or "true",
            Identity = Env("TAPSCRIBE_IDENTITY") ?? FallbackIdentity(),
            Name = Env("TAPSCRIBE_NAME") ?? "",
            Token = Env("TAPSCRIBE_TAP_TOKEN") ?? "",
        };

        static string? Env(string key)
        {
            string? value = Environment.GetEnvironmentVariable(key);
            return string.IsNullOrEmpty(value) ? null : value;
        }
    }

    private static string FallbackIdentity()
    {
        string user = Environment.UserName;
        return string.IsNullOrEmpty(user) ? "windows-tray" : user;
    }
}

/// <summary>Loads/saves <see cref="BridgeSettings"/> as JSON under %APPDATA%.</summary>
public static class BridgeSettingsStore
{
    private static string DefaultPath => Path.Join(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "TapScribe", "windows-tray-bridge.json");

    /// <summary>Load from the default %APPDATA% path.</summary>
    public static BridgeSettings Load() => Load(DefaultPath);

    /// <summary>Save to the default %APPDATA% path.</summary>
    public static void Save(BridgeSettings settings) => Save(settings, DefaultPath);

    /// <summary>
    /// Load settings from <paramref name="path"/>. A missing, corrupt, or
    /// unreadable file falls back to environment-seeded defaults rather than
    /// throwing, so the app always launches. (The path overload exists so the
    /// round-trip is testable without touching the user's real %APPDATA%.)
    /// </summary>
    public static BridgeSettings Load(string path)
    {
        try
        {
            if (File.Exists(path))
            {
                using FileStream stream = File.OpenRead(path);
                BridgeSettings? loaded = JsonSerializer.Deserialize<BridgeSettings>(stream);
                if (loaded is not null)
                    return loaded;
            }
        }
        catch (Exception ex) when (ex is IOException or JsonException or UnauthorizedAccessException)
        {
            // Missing/corrupt/unreadable settings file: fall back to seeded
            // defaults rather than failing to launch. The user can re-save from
            // the dialog, which overwrites the bad file.
        }
        return BridgeSettings.SeedFromEnvironment();
    }

    /// <summary>Save settings to <paramref name="path"/>, creating parent dirs.</summary>
    public static void Save(BridgeSettings settings, string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        using FileStream stream = File.Create(path);
        JsonSerializer.Serialize(stream, settings, new JsonSerializerOptions { WriteIndented = true });
    }
}

/// <summary>
/// DPAPI (Windows Data Protection API) helpers for the tap token at rest. The
/// blob is tied to the current Windows user, so another user on the same box
/// can't read it, and it is never written in cleartext.
/// </summary>
internal static class TokenProtection
{
    public static string? Protect(string token)
    {
        if (string.IsNullOrEmpty(token))
            return null;
        byte[] cipher = ProtectedData.Protect(
            Encoding.UTF8.GetBytes(token), optionalEntropy: null, DataProtectionScope.CurrentUser);
        return Convert.ToBase64String(cipher);
    }

    public static string Unprotect(string? protectedToken)
    {
        if (string.IsNullOrEmpty(protectedToken))
            return "";
        try
        {
            byte[] plain = ProtectedData.Unprotect(
                Convert.FromBase64String(protectedToken), optionalEntropy: null, DataProtectionScope.CurrentUser);
            return Encoding.UTF8.GetString(plain);
        }
        catch (CryptographicException)
        {
            // Blob was written under a different user/machine or is corrupt:
            // treat as "no saved token" so the app still launches; the user
            // re-enters it in the dialog.
            return "";
        }
        catch (FormatException)
        {
            // ProtectedToken isn't valid base64 (hand-edited file): same handling.
            return "";
        }
    }
}
