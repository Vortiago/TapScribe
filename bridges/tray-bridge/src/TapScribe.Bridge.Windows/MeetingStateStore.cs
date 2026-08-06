using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Windows;

/// <summary>
/// Persists the active <see cref="MeetingState"/> as JSON under %APPDATA%\TapScribe,
/// so a restarted tray app resumes the end-of-meeting pipeline it left running (#107).
/// A sibling of <see cref="BridgeSettingsStore"/>; unlike the settings file there is no
/// secret here (just a session id), so no DPAPI — the serialization itself lives in
/// Core (<see cref="MeetingState"/>) and is unit-tested there.
/// </summary>
public static class MeetingStateStore
{
    /// <summary>
    /// The on-disk restart-resume filename — an operator-facing contract like
    /// <see cref="BridgeSettingsStore.SettingsFileName"/>. Renaming it strands
    /// any in-flight meeting's resume state on upgrade; a change here needs a
    /// migration, not a rename.
    /// </summary>
    public const string StateFileName = "meeting-state.json";

    private static string DefaultPath => BridgeAppData.PathFor(StateFileName);

    /// <summary>Load the active meeting from the default %APPDATA% path, or null if none.</summary>
    public static MeetingState? Load() => Load(DefaultPath);

    /// <summary>Save the active meeting to the default %APPDATA% path.</summary>
    public static void Save(MeetingState state) => Save(state, DefaultPath);

    /// <summary>Forget the active meeting at the default %APPDATA% path.</summary>
    public static void Clear() => Clear(DefaultPath);

    /// <summary>
    /// Load from <paramref name="path"/>. A missing, corrupt, or unreadable file yields
    /// null (no active meeting) rather than throwing, so the tray always launches. (The
    /// path overload exists so the round-trip is testable without the real %APPDATA%.)
    /// </summary>
    public static MeetingState? Load(string path)
    {
        try
        {
            if (File.Exists(path))
                return MeetingState.FromJson(File.ReadAllText(path));
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Unreadable state file: treat as "no active meeting" rather than failing launch.
        }
        return null;
    }

    /// <summary>Save the active meeting to <paramref name="path"/>, creating parent dirs.</summary>
    public static void Save(MeetingState state, string path)
    {
        ArgumentNullException.ThrowIfNull(state);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, state.ToJson());
    }

    /// <summary>Delete the state file at <paramref name="path"/>. Best-effort: a missing
    /// file or a transient lock is swallowed — clearing is idempotent.</summary>
    public static void Clear(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or DirectoryNotFoundException)
        {
            // Already gone, parent dir absent, or briefly locked: nothing to surface — the
            // next Load returns null either way.
        }
    }
}
