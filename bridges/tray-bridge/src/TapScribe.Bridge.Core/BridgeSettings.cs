using System.Text.Json.Serialization;

namespace TapScribe.Bridge.Core;

/// <summary>
/// User-editable connection settings for the tray Bridge, persisted by
/// <see cref="BridgeSettingsStore"/> so they survive restarts (PRD #99 stories 8 + 11).
/// The Settings dialog reads/writes these; environment variables only seed the first-run
/// defaults, so an operator never has to set env vars.
///
/// Portable: nothing here knows which OS it runs on. The tap token is a credential and
/// the platform decides how it is kept at rest — that decision lives behind
/// <see cref="ITapTokenStore"/>, applied by the store on Load/Save.
/// </summary>
public sealed class BridgeSettings
{
    public string Host { get; set; } = "localhost";
    public int Port { get; set; } = 8001;
    public bool Tls { get; set; }

    /// <summary>
    /// INSECURE, opt-in testing flag: accept any self-signed / invalid server cert over
    /// TLS (the <c>curl -k</c> equivalent, for a local Recorder serving a self-signed
    /// cert). Off by default; only meaningful with <see cref="Tls"/>. Seedable via
    /// <c>TAPSCRIBE_TLS_ALLOW_SELF_SIGNED=1</c>. See
    /// <see cref="TapConnectionOptions.AllowSelfSignedCert"/>.
    /// </summary>
    public bool AllowSelfSignedCert { get; set; }

    public string Identity { get; set; } = "";
    public string Name { get; set; } = "";

    /// <summary>
    /// When true (the default), <b>End meeting</b> fires the Recorder's end-of-meeting
    /// pipeline (strip → transcribe → summarize) and shows the summary. When false, End
    /// meeting only drains + closes the taps: the detached session and its recordings are
    /// left on the Recorder untouched, to be transcribed / summarized from the dashboard
    /// later. Defaults to true so a settings file written before this key existed keeps the
    /// original auto-process behaviour (System.Text.Json preserves the initializer when the
    /// key is absent).
    /// </summary>
    public bool ProcessOnEnd { get; set; } = true;

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

    /// <summary>
    /// The tap token at rest, as the platform's <see cref="ITapTokenStore"/> spells it —
    /// a base64 DPAPI blob on Windows, null when the secret lives out-of-band. Written and
    /// read only by <see cref="BridgeSettingsStore"/>.
    /// </summary>
    public string? ProtectedToken { get; set; }

    /// <summary>The tap token in plaintext (not serialised). Empty = offer no subprotocol.</summary>
    [JsonIgnore]
    public string Token { get; set; } = "";

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
        // Carry the opt-in faithfully; the security boundary is the connection site, which
        // only wires the accept-any validator when Tls && AllowSelfSignedCert.
        AllowSelfSignedCert = AllowSelfSignedCert,
        Identity = EffectiveIdentity,
        Name = Name,
        Token = Token,
    };

    // The base identity a tap streams under when no per-device identity is set — never
    // blank. Shared by ToConnectionOptions and the gate-by-identity map so the live re-tune
    // keys line up with the tap identities.
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
            AllowSelfSignedCert = Env("TAPSCRIBE_TLS_ALLOW_SELF_SIGNED") is "1" or "true",
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
        // "windows-tray" deliberately keeps its pre-rename spelling: the identity
        // is the WAV filename slug and the key the Recorder attributes recordings
        // under, so changing it re-attributes the tray as a brand-new speaker
        // (see TapConnectionOptions.Identity).
        return string.IsNullOrEmpty(user) ? "windows-tray" : user;
    }
}
