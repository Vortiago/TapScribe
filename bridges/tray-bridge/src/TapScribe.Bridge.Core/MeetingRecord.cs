using System.Globalization;
using System.Text.Json.Serialization;

namespace TapScribe.Bridge.Core;

/// <summary>
/// One entry in the tray's local Past-meetings history (#168): a meeting the tray
/// itself ran, kept so its summary can be re-opened later — even days on, without the
/// dashboard. Just the session id (the durable handle the summary is re-derived from,
/// like <see cref="MeetingState"/>), the wall-clock start time for a readable list
/// line, and an optional human label (reserved — no naming UI yet). No cached summary:
/// it is re-fetched from the session id via the tap-token poll endpoint on each open.
/// The model + the menu-line formatting live in Core (Linux-tested); the %APPDATA%
/// file IO is the Windows store's job (<c>MeetingHistoryStore</c>).
/// </summary>
public sealed record MeetingRecord
{
    [JsonPropertyName("session")] public required string SessionId { get; init; }
    [JsonPropertyName("startedAt")] public required DateTimeOffset StartedAt { get; init; }
    [JsonPropertyName("label")] public string? Label { get; init; }

    /// <summary>The Past-meetings submenu line: the start time in a fixed, readable
    /// pattern (invariant culture, so it's deterministic and unit-testable), with the
    /// optional label appended when present. Formats the stored offset's own wall clock
    /// — the value was captured at Start with the local offset baked in.</summary>
    public string MenuLabel()
    {
        string time = StartedAt.ToString("ddd d MMM HH:mm", CultureInfo.InvariantCulture);
        return string.IsNullOrWhiteSpace(Label) ? time : $"{time} · {Label}";
    }
}
