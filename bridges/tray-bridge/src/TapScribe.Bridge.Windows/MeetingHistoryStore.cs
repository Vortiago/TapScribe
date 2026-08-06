using System.Text.Json;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Windows;

/// <summary>
/// Persists the tray's Past-meetings history (#168) as JSON under %APPDATA%\TapScribe,
/// so the user can re-open any past meeting's summary across a tray restart. A sibling of
/// <see cref="MeetingStateStore"/>: no secret here (session ids + timestamps), so no
/// DPAPI; the model + (de)serialization live in Core (<see cref="MeetingHistory"/>) and
/// are unit-tested there. A missing or corrupt file degrades to an empty history — never
/// a crash.
/// </summary>
public static class MeetingHistoryStore
{
    /// <summary>
    /// The on-disk history filename — an operator-facing contract like
    /// <see cref="BridgeSettingsStore.SettingsFileName"/>. Renaming it silently
    /// discards every operator's Past-meetings list on upgrade; a change here
    /// needs a migration, not a rename.
    /// </summary>
    public const string HistoryFileName = "meeting-history.json";

    private static string DefaultPath => BridgeAppData.PathFor(HistoryFileName);

    /// <summary>Load the history from the default %APPDATA% path (empty if none/corrupt).</summary>
    public static MeetingHistory Load() => Load(DefaultPath);

    /// <summary>Save the history to the default %APPDATA% path.</summary>
    public static void Save(MeetingHistory history) => Save(history, DefaultPath);

    /// <summary>Append a meeting to the default %APPDATA% history, best-effort.</summary>
    public static void Append(MeetingRecord record) => Append(record, DefaultPath);

    /// <summary>
    /// Load from <paramref name="path"/>. A missing, corrupt, or unreadable file yields an
    /// EMPTY history rather than throwing, so the tray always launches and the Past-meetings
    /// menu degrades to "(no past meetings)". (The path overload exists so the round-trip is
    /// testable without the real %APPDATA%.)
    /// </summary>
    public static MeetingHistory Load(string path)
    {
        try
        {
            if (File.Exists(path))
                return MeetingHistory.FromJson(File.ReadAllText(path));
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Unreadable history file: treat as "no past meetings" rather than failing launch.
        }
        return MeetingHistory.Empty;
    }

    /// <summary>Save the history to <paramref name="path"/>, creating parent dirs.</summary>
    public static void Save(MeetingHistory history, string path)
    {
        ArgumentNullException.ThrowIfNull(history);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, history.ToJson());
    }

    /// <summary>
    /// Append <paramref name="record"/> to the history at <paramref name="path"/> and persist
    /// it. Best-effort: a failed read or write is swallowed — the history is a convenience and
    /// must NEVER break the End-meeting flow (the meeting still ran and its summary stays
    /// re-fetchable by session id; only this local list misses the entry). <see cref="Load"/>
    /// already degrades a corrupt file to empty, so a bad file is overwritten cleanly here.
    /// </summary>
    public static void Append(MeetingRecord record, string path)
    {
        ArgumentNullException.ThrowIfNull(record);
        try
        {
            Save(Load(path).Append(record), path);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException)
        {
            // Couldn't persist the updated history (permissions, full disk, transient lock, or
            // a serialization failure): nothing to surface — the meeting ran and its summary is
            // re-fetchable by session id; the next successful End just re-appends from whatever
            // is on disk. JsonException is in the filter so the documented "never break the
            // End-meeting flow" contract holds even if ToJson ever throws.
        }
    }
}
