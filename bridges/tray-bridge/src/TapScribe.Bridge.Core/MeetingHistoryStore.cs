using System.Text.Json;

namespace TapScribe.Bridge.Core;

/// <summary>
/// Persists the tray's Past-meetings history (#168) as JSON in
/// <paramref name="directory"/>, so the operator can re-open any past meeting's summary
/// across a tray restart. A sibling of <see cref="MeetingStateStore"/>: no secret here
/// (session ids + timestamps), so the only thing a platform contributes is the directory.
/// The model and its (de)serialization live on <see cref="MeetingHistory"/>. A missing or
/// corrupt file degrades to an empty history — never a crash.
/// </summary>
public sealed class MeetingHistoryStore(string directory)
{
    /// <summary>
    /// The on-disk history filename — an operator-facing contract. Renaming it silently
    /// discards every operator's Past-meetings list on upgrade; a change here needs a
    /// migration, not a rename.
    /// </summary>
    public const string HistoryFileName = "meeting-history.json";

    /// <summary>The history file this store reads and writes.</summary>
    public string FilePath { get; } = Path.Join(directory, HistoryFileName);

    /// <summary>
    /// Load the history. A missing, corrupt, or unreadable file yields an EMPTY history
    /// rather than throwing, so the tray always launches and the Past-meetings menu
    /// degrades to "(no past meetings)".
    /// </summary>
    public MeetingHistory Load()
    {
        try
        {
            if (File.Exists(FilePath))
                return MeetingHistory.FromJson(File.ReadAllText(FilePath));
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Unreadable history file: treat as "no past meetings" rather than failing
            // launch. What's lost is the local list; each summary stays re-fetchable from
            // the Recorder by session id.
        }
        return MeetingHistory.Empty;
    }

    /// <summary>Save the history, creating the directory if needed.</summary>
    public void Save(MeetingHistory history)
    {
        ArgumentNullException.ThrowIfNull(history);
        Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
        File.WriteAllText(FilePath, history.ToJson());
    }

    /// <summary>
    /// Append <paramref name="record"/> to the history and persist it. Best-effort: a
    /// failed read or write is swallowed — the history is a convenience and must NEVER
    /// break the End-meeting flow (the meeting still ran and its summary stays re-fetchable
    /// by session id; only this local list misses the entry). <see cref="Load"/> already
    /// degrades a corrupt file to empty, so a bad file is overwritten cleanly here.
    /// </summary>
    public void Append(MeetingRecord record)
    {
        ArgumentNullException.ThrowIfNull(record);
        try
        {
            Save(Load().Append(record));
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
