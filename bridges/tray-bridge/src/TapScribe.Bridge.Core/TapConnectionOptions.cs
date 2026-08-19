using System.Text;

namespace TapScribe.Bridge.Core;

/// <summary>
/// Everything needed to open one `/tap` WebSocket, plus the pure builders that
/// turn it into a URL and a subprotocol. Kept pure (no socket) so URL/auth
/// construction is unit-tested without a live connection — mirrors the
/// local-test-bridge's build_tap_url / build_subprotocols helpers.
/// </summary>
public sealed record TapConnectionOptions
{
    public string Host { get; init; } = "localhost";
    public int Port { get; init; } = 8001;

    /// <summary>Use wss:// (the Recorder was started with --tls).</summary>
    public bool Tls { get; init; }

    /// <summary>
    /// INSECURE, opt-in testing flag: accept <b>any</b> server certificate on the TLS
    /// connection (expired, wrong-host, untrusted/self-signed root) — the <c>curl -k</c>
    /// equivalent, for reaching a Recorder serving a local self-signed cert. Off by
    /// default. Only honoured together with <see cref="Tls"/>: every connection site gates
    /// the accept-any validator on <c>Tls &amp;&amp; AllowSelfSignedCert</c>, so it is never
    /// wired up on a normal connection. Removes MITM protection; never use against an
    /// untrusted network. See <see cref="InsecureTls"/>.
    /// </summary>
    public bool AllowSelfSignedCert { get; init; }

    /// <summary>
    /// The tray's frozen per-speaker slug, deliberately keeping its pre-rename spelling (the
    /// bridges/tray-bridge/ directory rename left it alone). Changing it re-attributes the tray
    /// as a brand-new speaker, so it needs a migration, not a rename: the same contract class as
    /// <c>TrayStores.SettingsFileName</c>. Named rather than repeated, because it is also what
    /// <see cref="BridgeSettings"/> seeds when the OS offers no username, and the two must
    /// move together or they are a migration bug rather than two defaults.
    /// </summary>
    public const string TrayIdentity = "windows-tray";

    /// <summary>
    /// Stable per-speaker identifier; the WAV filename slug and the key the
    /// Recorder attributes recordings under.
    /// </summary>
    public string Identity { get; init; } = TrayIdentity;

    /// <summary>Human-readable display name shown on the dashboard.</summary>
    public string Name { get; init; } = "";

    /// <summary>Per-utterance id, kept stable across reconnects within an utterance.</summary>
    public string? UtteranceId { get; init; }

    /// <summary>Detached-session id to route this tap into; null = global current session.</summary>
    public string? Session { get; init; }

    /// <summary>Tap token. Empty = no subprotocol offered (Recorder under --no-auth).</summary>
    public string Token { get; init; } = "";

    // Reserved `tap_mode` values — does this tap carry one human or several?
    // Only a multi-person tap is diarized. Stamped from the Recorder by
    // tools/stamp_tap_wire.py; never hand-edit.
    public const string TapModeSingle = "single";
    public const string TapModeMulti = "multi";

    /// <summary>
    /// Single- vs multi-person. A mic is the operator; a Render (loopback)
    /// device is the far end of the meeting. The operator can override it, so
    /// this is only ever a default.
    /// </summary>
    public string Mode { get; init; } = TapModeSingle;

    /// <summary>
    /// The single/multi default for a device flow: a Capture device is the
    /// operator's mic, a Render device is loopback carrying the far end of the
    /// meeting. Mirrors GateSettings.DefaultForFlow.
    /// </summary>
    public static string TapModeForFlow(DeviceFlow flow) =>
        flow == DeviceFlow.Render ? TapModeMulti : TapModeSingle;

    /// <summary>
    /// Build the `/tap` WebSocket URI with query params. utterance_id and session
    /// are only sent when set (the Recorder 404s an unknown session id).
    /// </summary>
    public Uri BuildTapUri()
    {
        var query = new StringBuilder();
        query.Append("identity=").Append(Uri.EscapeDataString(Identity));
        query.Append("&name=").Append(Uri.EscapeDataString(Name));
        query.Append("&tap_mode=").Append(Uri.EscapeDataString(Mode));
        if (!string.IsNullOrEmpty(UtteranceId))
            query.Append("&utterance_id=").Append(Uri.EscapeDataString(UtteranceId));
        if (!string.IsNullOrEmpty(Session))
            query.Append("&session=").Append(Uri.EscapeDataString(Session));

        // UriBuilder (not string interpolation) so a host the user pasted with a
        // scheme/port/path/whitespace can't produce a malformed URI or land on the
        // wrong port. NormalizeHost reduces it to a bare hostname; the Port/Tls
        // fields stay authoritative.
        return new UriBuilder
        {
            Scheme = Tls ? "wss" : "ws",
            Host = NormalizeHost(Host),
            Port = Port,
            Path = "/tap",
            Query = query.ToString(),
        }.Uri;
    }

    /// <summary>
    /// Reduce a user-entered host to a bare hostname. Accepts a plain hostname or
    /// one pasted with a scheme ("wss://host"), a port ("host:9000"), a path
    /// ("host/path"), or surrounding whitespace. The Port and TLS settings remain
    /// authoritative, so an embedded scheme/port here is ignored — this just stops
    /// a stray paste from producing a malformed connection URI.
    /// </summary>
    public static string NormalizeHost(string host)
    {
        string trimmed = (host ?? string.Empty).Trim();
        if (trimmed.Length == 0)
            return "localhost";
        string withScheme = trimmed.Contains("://", StringComparison.Ordinal) ? trimmed : "ws://" + trimmed;
        return Uri.TryCreate(withScheme, UriKind.Absolute, out Uri? parsed) && !string.IsNullOrEmpty(parsed.Host)
            ? parsed.Host
            : trimmed;
    }

    /// <summary>
    /// The `Sec-WebSocket-Protocol` value to offer, or null under --no-auth
    /// (empty token). The token is produced by the Recorder via
    /// secrets.token_urlsafe, i.e. the base64url charset, which is a valid RFC
    /// token, so the joined string passes ClientWebSocket subprotocol validation.
    /// </summary>
    public string? BuildSubprotocol() =>
        string.IsNullOrEmpty(Token) ? null : TapWire.SubprotocolPrefix + Token;
}
