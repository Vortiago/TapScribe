namespace TapScribe.Bundle.Core;

/// <summary>How reading <c>.auth-password</c> went.</summary>
public enum PasswordStatus
{
    /// <summary>A password was read.</summary>
    Ok,

    /// <summary>No file yet — normally means the Recorder hasn't finished its first boot.</summary>
    Missing,

    /// <summary>The file is there but holds nothing usable.</summary>
    Empty,

    /// <summary>The file could not be read at all (locked, ACL, not a file).</summary>
    Unreadable,
}

/// <summary>
/// The outcome of a password read. <see cref="Password"/> is non-null exactly when
/// <see cref="Status"/> is <see cref="PasswordStatus.Ok"/>; <see cref="Message"/> is
/// always safe to show or log — it names the path but never the secret.
/// </summary>
public sealed record PasswordLookup(PasswordStatus Status, string? Password, string Message)
{
    public bool IsOk => Status == PasswordStatus.Ok;
}

/// <summary>
/// Reads the Recorder's dashboard password (<c>config.AUTH_PASSWORD_FILE</c> —
/// <c>&lt;data dir&gt;\.auth-password</c>) for the tray's <b>Copy password</b> item.
///
/// This is the Bundle's only door in. <c>start.sh</c> prints the generated password to
/// the terminal it was launched from; a Bundle is launched from the Start menu and has
/// no terminal, so on first run the tray menu is the operator's <i>only</i> way to learn
/// the password. Everything here therefore returns a <see cref="PasswordLookup"/> rather
/// than throwing: a menu-click handler that throws shows nothing at all, and "nothing at
/// all" is indistinguishable from "copied" — leaving the operator locked out of their
/// own dashboard with no clue why.
/// </summary>
public static class PasswordFile
{
    /// <summary>Read and interpret the file at <paramref name="path"/>. Never throws.</summary>
    public static PasswordLookup Read(string path)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(path);

        try
        {
            // Checked before File.Exists, which answers false for a directory and would
            // otherwise report a wrong-shaped path as the benign "not written yet".
            if (Directory.Exists(path))
                return new PasswordLookup(
                    PasswordStatus.Unreadable,
                    null,
                    $"The dashboard password path {path} is a folder, not a file. The Bundle's " +
                    "data directory layout is wrong — check TAPSCRIBE_BASE_DIR.");

            if (!File.Exists(path))
                return new PasswordLookup(
                    PasswordStatus.Missing,
                    null,
                    $"No dashboard password yet at {path}. The Recorder writes it on its first " +
                    "successful start — wait for the dashboard to come up, then try again.");

            return Parse(File.ReadAllText(path), path);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or NotSupportedException)
        {
            // Locked by another process, denied by an ACL, or the path isn't a file.
            // Swallowing is right here (see the type docs: a throwing menu handler is
            // silent), but the reason must reach the operator, so it goes in the message.
            return new PasswordLookup(
                PasswordStatus.Unreadable,
                null,
                $"Could not read the dashboard password at {path}: {error.Message}");
        }
    }

    /// <summary>
    /// Interpret file contents. Pure, so the trimming rules are testable without a disk.
    ///
    /// The Recorder writes one line with a trailing newline; we take the first non-blank
    /// line and trim it, which also survives a file an operator has hand-edited in
    /// Notepad (CRLF, a stray blank first line).
    /// </summary>
    public static PasswordLookup Parse(string? contents, string path)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(path);

        string? password = (contents ?? string.Empty)
            .Split('\n')
            .Select(line => line.Trim())
            .FirstOrDefault(line => line.Length > 0);

        if (password is null)
            return new PasswordLookup(
                PasswordStatus.Empty,
                null,
                $"The dashboard password file at {path} is empty. Stop TapScribe, delete that " +
                "file, and start again — the Recorder generates a fresh password when it is absent.");

        return new PasswordLookup(PasswordStatus.Ok, password, "Dashboard password copied to the clipboard.");
    }
}
