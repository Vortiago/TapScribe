using System.Text.Json;
using System.Text.Json.Serialization;

namespace TapScribe.Bridge.Core;

/// <summary>
/// The persisted handle to an in-flight (or just-finished) meeting, so a restarted
/// tray app can resume showing the end-of-meeting pipeline's progress / summary —
/// the Recorder keeps polling working across its own restart, and this is the tray's
/// matching memory. Just the session id: the live state is re-read from the poll
/// endpoint, not cached here. The model, its (de)serialization and the file IO
/// (<see cref="MeetingStateStore"/>) all live in Core; a platform contributes only the
/// directory the store writes into.
/// </summary>
public sealed record MeetingState
{
    [JsonPropertyName("session")] public required string SessionId { get; init; }

    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);

    public string ToJson() => JsonSerializer.Serialize(this, Json);

    /// <summary>Parse a persisted state, returning null on anything malformed (corrupt
    /// file, missing session id) — a bad file must not crash the tray at boot.</summary>
    public static MeetingState? FromJson(string json)
    {
        try
        {
            return JsonSerializer.Deserialize<MeetingState>(json, Json);
        }
        catch (JsonException)
        {
            // Corrupt or schema-mismatched state file (e.g. a missing required session id):
            // treat as "no active meeting" rather than failing the tray's startup.
            return null;
        }
    }
}
