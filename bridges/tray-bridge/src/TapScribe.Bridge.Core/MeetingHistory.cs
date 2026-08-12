using System.Text.Json;
using System.Text.Json.Serialization;

namespace TapScribe.Bridge.Core;

/// <summary>
/// The tray's local Past-meetings history (#168): a bounded, most-recent-first list of
/// the meetings the tray itself ran, persisted to the tray's per-user data folder so the
/// user can re-open any past meeting's summary without the dashboard, even days later
/// (%APPDATA%\TapScribe on Windows). Keeping the history
/// LOCAL to the tray (only the sessions it created) preserves the tap token's low
/// privilege — no "list every session" endpoint is needed; browsing all sessions stays
/// the dashboard's job.
///
/// Immutable: <see cref="Append"/> returns a new history. The model, its
/// (de)serialization and the file IO (<see cref="MeetingHistoryStore"/>) all live in
/// Core — the sibling shape of <see cref="MeetingState"/>.
/// </summary>
public sealed record MeetingHistory
{
    /// <summary>The most a tray keeps — a sensible cap so the submenu stays usable and the
    /// list never accumulates entries whose summaries the Recorder has long since pruned.</summary>
    public const int MaxEntries = 20;

    /// <summary>Most-recent-first. Bounded to <see cref="MaxEntries"/>.</summary>
    [JsonPropertyName("meetings")]
    public IReadOnlyList<MeetingRecord> Meetings { get; init; } = [];

    /// <summary>An empty history — the cold-start value and the degraded value a missing or
    /// corrupt file loads to.</summary>
    public static MeetingHistory Empty { get; } = new();

    /// <summary>Return a new history with <paramref name="record"/> at the front. Any
    /// existing entry for the SAME session is dropped first (a re-append moves it to the
    /// top rather than duplicating), then the list is truncated to <see cref="MaxEntries"/>
    /// — newest kept, oldest discarded.</summary>
    public MeetingHistory Append(MeetingRecord record)
    {
        ArgumentNullException.ThrowIfNull(record);
        IEnumerable<MeetingRecord> withoutDuplicate = Meetings.Where(
            m => !string.Equals(m.SessionId, record.SessionId, StringComparison.Ordinal));
        return this with { Meetings = [record, .. withoutDuplicate.Take(MaxEntries - 1)] };
    }

    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);

    public string ToJson() => JsonSerializer.Serialize(this, Json);

    /// <summary>Parse a persisted history. Anything malformed — corrupt JSON, or a record
    /// missing its required session id — yields an EMPTY history rather than throwing: a
    /// bad file must degrade to "no past meetings", never crash the tray at boot (mirrors
    /// <see cref="MeetingState.FromJson"/>).</summary>
    public static MeetingHistory FromJson(string json)
    {
        try
        {
            return JsonSerializer.Deserialize<MeetingHistory>(json, Json) ?? Empty;
        }
        catch (JsonException)
        {
            // Corrupt or schema-mismatched history file: treat as no past meetings.
            return Empty;
        }
    }
}
