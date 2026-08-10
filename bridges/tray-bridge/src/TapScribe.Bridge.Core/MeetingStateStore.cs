namespace TapScribe.Bridge.Core;

/// <summary>
/// Persists the active <see cref="MeetingState"/> as JSON in <paramref name="directory"/>,
/// so a restarted tray app resumes the end-of-meeting pipeline it left running (#107). A
/// sibling of <see cref="BridgeSettingsStore"/>; unlike the settings file there is no
/// secret here (just a session id), so no token seam — the only thing a platform
/// contributes is the directory. The serialization lives on <see cref="MeetingState"/>.
/// </summary>
public sealed class MeetingStateStore(string directory)
{
    /// <summary>
    /// The on-disk restart-resume filename — an operator-facing contract. Renaming it
    /// strands any in-flight meeting's resume state on upgrade; a change here needs a
    /// migration, not a rename.
    /// </summary>
    public const string StateFileName = "meeting-state.json";

    /// <summary>The state file this store reads and writes.</summary>
    public string FilePath { get; } = Path.Join(directory, StateFileName);

    /// <summary>
    /// Load the active meeting, or null if there is none. A missing, corrupt, or unreadable
    /// file yields null rather than throwing, so the tray always launches.
    /// </summary>
    public MeetingState? Load()
    {
        try
        {
            if (File.Exists(FilePath))
                return MeetingState.FromJson(File.ReadAllText(FilePath));
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Unreadable state file: treat as "no active meeting" rather than failing
            // launch. What's lost is the resume; the meeting itself already ran and its
            // session is still on the Recorder.
        }
        return null;
    }

    /// <summary>Save the active meeting, creating the directory if needed.</summary>
    public void Save(MeetingState state)
    {
        ArgumentNullException.ThrowIfNull(state);
        Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
        File.WriteAllText(FilePath, state.ToJson());
    }

    /// <summary>Forget the active meeting. Best-effort: a missing file or a transient lock
    /// is swallowed — clearing is idempotent.</summary>
    public void Clear()
    {
        try
        {
            File.Delete(FilePath);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or DirectoryNotFoundException)
        {
            // Already gone, parent dir absent, or briefly locked: nothing to surface — the
            // next Load returns null either way.
        }
    }
}
