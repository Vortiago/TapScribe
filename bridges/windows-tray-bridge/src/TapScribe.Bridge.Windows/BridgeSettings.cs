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

    /// <summary>
    /// Which devices to tap, as resolvable selections (follow-default or pinned). An
    /// empty list means "use the default pair" — see <see cref="EffectiveDevices"/>;
    /// this keeps a pre-#106 settings file (no devices key) behaving like today.
    /// </summary>
    public List<DeviceSelection> Devices { get; set; } = [];

    // Legacy GLOBAL level-gate knobs (pre per-device tuning, ADR-0007). Kept only to
    // migrate an upgrading operator's single tuning into per-device defaults on load —
    // see LegacyGlobalGate / EffectiveDevices. Nullable + omitted-when-null so a file
    // written by the per-device UI carries the tuning per device (on each DeviceSelection)
    // and never re-introduces a global value; an old file's value is read once here and
    // then absorbed into the devices on the next Save.
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? GateSensitivity { get; set; }

    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? GateHangoverMs { get; set; }

    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? GatePreRollMs { get; set; }

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
    /// The selections to actually resolve at Start: the saved <see cref="Devices"/> if
    /// any, else the default pair — follow-default mic (under the operator's
    /// identity/name) + follow-default system loopback. This is what makes an empty/old
    /// settings file behave like the pre-#106 hardcoded "mic + system" capture.
    ///
    /// Each selection is returned with a concrete per-device <see cref="DeviceSelection.Gate"/>
    /// (<see cref="NormalizeGates"/>): an explicit per-device value if present, else the
    /// migrated legacy global value, else the per-flow default. So everything downstream
    /// (resolution, Start, the per-identity live re-tune) sees a tuning per device with no
    /// nulls to special-case.
    /// </summary>
    [JsonIgnore]
    public IReadOnlyList<DeviceSelection> EffectiveDevices =>
        NormalizeGates(Devices.Count > 0 ? Devices : DefaultDevices());

    /// <summary>
    /// The default device pair when nothing is saved: follow-default mic + follow-default
    /// system loopback. Each carries a single label used as both identity and display name
    /// (the dialog edits one Name per device); the mic label is the operator's name /
    /// identity / OS username. The ONE definition of the default pair — both
    /// <see cref="EffectiveDevices"/> and the settings dialog's seed consume it, so the
    /// defaults can't drift apart.
    /// </summary>
    public IReadOnlyList<DeviceSelection> DefaultDevices()
    {
        string micLabel =
            !string.IsNullOrWhiteSpace(Name) ? Name.Trim()
            : !string.IsNullOrWhiteSpace(Identity) ? Identity.Trim()
            : FallbackIdentity();
        return
        [
            new DeviceSelection.FollowDefault(DeviceFlow.Capture, micLabel, micLabel),
            new DeviceSelection.FollowDefault(DeviceFlow.Render, "System audio", "System audio"),
        ];
    }

    /// <summary>
    /// The per-device level-gate options keyed by the identity each device streams under —
    /// the map the tray pushes to <see cref="CaptureOrchestrator.UpdateGates"/> on Settings →
    /// Save so each running pipeline re-tunes from its own device's tuning (#153). Keyed by
    /// the same effective identity <see cref="ResolveResult.ToTapOptions"/> stamps (a blank
    /// per-device identity falls back to the base identity), so the keys line up with the
    /// orchestrator's session keys. Entries for devices that aren't running (unplugged / not
    /// in this meeting) are harmless — the orchestrator skips them.
    /// </summary>
    public IReadOnlyDictionary<string, GateOptions> ToGateOptionsByIdentity()
    {
        string fallbackIdentity = EffectiveIdentity;
        var map = new Dictionary<string, GateOptions>(StringComparer.Ordinal);
        foreach (DeviceSelection device in EffectiveDevices)
        {
            string identity = string.IsNullOrWhiteSpace(device.Identity) ? fallbackIdentity : device.Identity;
            // EffectiveDevices ran NormalizeGates, so every gate is filled here.
            map[identity] = device.Gate!.ToGateOptions();
        }
        return map;
    }

    private static GateSettings FlowDefault(DeviceSelection device) =>
        GateSettings.DefaultForFlow(device is DeviceSelection.FollowDefault follow ? follow.Flow : DeviceFlow.Capture);

    // Fill in a concrete per-device gate for any selection that carries none: prefer a
    // migrated legacy GLOBAL value (so an upgrade doesn't reset an operator's tuning),
    // else the sensible per-flow default. A selection that already has its own gate is
    // left untouched.
    private IReadOnlyList<DeviceSelection> NormalizeGates(IReadOnlyList<DeviceSelection> devices)
    {
        GateSettings? legacy = LegacyGlobalGate();
        return devices
            .Select(device => device.Gate is not null ? device : device with { Gate = legacy ?? FlowDefault(device) })
            .ToList();
    }

    // The pre-per-device global tuning, reconstructed from the legacy fields when an old
    // file carried any of them; null on a brand-new file and on files written by the
    // per-device UI (which leaves these null), so a fresh install gets per-flow defaults
    // rather than a single global value.
    private GateSettings? LegacyGlobalGate()
    {
        if (GateSensitivity is null && GateHangoverMs is null && GatePreRollMs is null)
            return null;
        // Any field the old file omitted falls back to the mic default — which IS the
        // legacy global default expressed in operator units, so GateSettings stays the one
        // place that knows it.
        GateSettings d = GateSettings.DefaultForFlow(DeviceFlow.Capture);
        return new GateSettings(
            GateSensitivity ?? d.Sensitivity,
            GateHangoverMs ?? d.HangoverMs,
            GatePreRollMs ?? d.PreRollMs);
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
        Identity = EffectiveIdentity,
        Name = Name,
        Token = Token,
    };

    // The base identity a tap streams under when no per-device identity is set — never
    // blank. Shared by ToConnectionOptions and the gate-by-identity map so the live re-tune
    // keys line up with the tap identities, without decrypting the token to read it.
    private string EffectiveIdentity =>
        string.IsNullOrWhiteSpace(Identity) ? FallbackIdentity() : Identity.Trim();

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
