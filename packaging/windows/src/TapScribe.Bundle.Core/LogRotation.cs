using System.Globalization;

namespace TapScribe.Bundle.Core;

/// <summary>One file in the log directory, as the rotation policy sees it.</summary>
public sealed record LogFileInfo(string Name, long Size);

/// <summary>
/// The size + count bound on the Launcher's log directory. Both limits are required:
/// size alone lets archives accumulate forever, count alone lets one archive grow without
/// bound.
/// </summary>
public sealed record LogRotationPolicy
{
    public LogRotationPolicy(long MaxBytes, int KeepArchives)
    {
        ArgumentOutOfRangeException.ThrowIfNegativeOrZero(MaxBytes);
        // Zero archives would mean rolling and immediately deleting what we just rolled —
        // i.e. losing the log at the exact moment it got interesting.
        ArgumentOutOfRangeException.ThrowIfNegativeOrZero(KeepArchives);
        this.MaxBytes = MaxBytes;
        this.KeepArchives = KeepArchives;
    }

    /// <summary>Roll once the active log reaches this size.</summary>
    public long MaxBytes { get; }

    /// <summary>How many rolled files to keep, newest first.</summary>
    public int KeepArchives { get; }

    /// <summary>8 MiB × (3 archives + 1 active) — a bounded ~32 MiB worst case.</summary>
    public static readonly LogRotationPolicy Default = new(MaxBytes: 8L * 1024 * 1024, KeepArchives: 3);
}

/// <summary>
/// What the writer should do next: optionally rename the active log to
/// <see cref="ArchiveName"/>, and delete <see cref="Delete"/> (oldest first).
/// </summary>
public sealed record LogRotationPlan(
    bool ShouldRoll,
    string? ArchiveName,
    IReadOnlyList<string> Delete);

/// <summary>
/// The Launcher's log rotation, as a <b>pure decision</b> over a list of
/// <c>(name, size)</c>. No filesystem: the Windows shell lists the directory, calls
/// <see cref="Plan"/>, and performs the rename/deletes. That split is the point — the
/// policy (when to roll, what to delete, and above all what NOT to delete) is the part
/// that can be got wrong, and it is fully unit-tested on Linux, while what is left in
/// the shell is a rename and a loop of deletes.
///
/// Archives are named <c>recorder-yyyyMMdd-HHmmss.log</c> from a UTC stamp, so the names
/// sort chronologically and "delete the oldest" is decidable from names alone — no file
/// timestamps, no clock reads inside the decision.
/// </summary>
public static class LogRotation
{
    private const string StampFormat = "yyyyMMdd-HHmmss";
    private const int StampLength = 15; // 8 + 1 + 6

    /// <summary>
    /// Decide what to do with <paramref name="activeName"/> given everything currently in
    /// the log directory.
    ///
    /// Deletions are computed whether or not we roll: a tightened policy, or a previous
    /// run that died mid-prune, must not have to wait for the next roll to come back
    /// inside its bound. Files that are not our own archives are never touched — the log
    /// directory belongs to the operator, and deleting a stray file for sitting next to
    /// ours would be unforgivable.
    /// </summary>
    public static LogRotationPlan Plan(
        string activeName,
        IReadOnlyList<LogFileInfo> files,
        DateTimeOffset now,
        LogRotationPolicy policy)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(activeName);
        ArgumentNullException.ThrowIfNull(files);
        ArgumentNullException.ThrowIfNull(policy);

        LogFileInfo? active = files.FirstOrDefault(
            f => string.Equals(f.Name, activeName, StringComparison.OrdinalIgnoreCase));
        bool shouldRoll = active is not null && active.Size >= policy.MaxBytes;

        // Oldest first — the order deletions are performed in, and the order the budget
        // is applied from.
        List<string> archives = files
            .Select(f => f.Name)
            .Where(name => TryParseArchive(activeName, name, out _, out _))
            .OrderBy(name => Key(activeName, name))
            .ToList();

        string? archiveName = null;
        if (shouldRoll)
        {
            archiveName = UniqueArchiveName(activeName, now, files);
            archives.Add(archiveName); // the newest — it sorts last by construction
        }

        int surplus = archives.Count - policy.KeepArchives;
        IReadOnlyList<string> delete = surplus > 0
            ? archives.Take(surplus).ToArray()
            : Array.Empty<string>();

        return new LogRotationPlan(shouldRoll, archiveName, delete);
    }

    /// <summary>
    /// The archive name the active log rolls to at <paramref name="now"/>, made unique
    /// against what already exists — two rolls inside one second (a burst of output on a
    /// small <c>MaxBytes</c>) must not clobber the first archive.
    /// </summary>
    public static string UniqueArchiveName(string activeName, DateTimeOffset now, IReadOnlyList<LogFileInfo> files)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(activeName);
        ArgumentNullException.ThrowIfNull(files);

        string stem = Path.GetFileNameWithoutExtension(activeName);
        string extension = Path.GetExtension(activeName);
        string stamp = now.UtcDateTime.ToString(StampFormat, CultureInfo.InvariantCulture);

        string candidate = $"{stem}-{stamp}{extension}";
        for (int counter = 1; Exists(files, candidate); counter++)
            candidate = $"{stem}-{stamp}-{counter.ToString(CultureInfo.InvariantCulture)}{extension}";

        return candidate;
    }

    private static bool Exists(IReadOnlyList<LogFileInfo> files, string name) =>
        files.Any(f => string.Equals(f.Name, name, StringComparison.OrdinalIgnoreCase));

    /// <summary>
    /// Is <paramref name="candidate"/> an archive this policy created from
    /// <paramref name="activeName"/>? Case-insensitive, because the filesystem this runs
    /// on is. Anything that does not parse is somebody else's file.
    /// </summary>
    private static bool TryParseArchive(string activeName, string candidate, out string stamp, out int counter)
    {
        stamp = string.Empty;
        counter = 0;

        string stem = Path.GetFileNameWithoutExtension(activeName);
        string extension = Path.GetExtension(activeName);
        string prefix = stem + "-";

        if (!candidate.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            return false;
        if (!candidate.EndsWith(extension, StringComparison.OrdinalIgnoreCase))
            return false;

        int middleLength = candidate.Length - prefix.Length - extension.Length;
        if (middleLength < StampLength)
            return false;

        ReadOnlySpan<char> middle = candidate.AsSpan(prefix.Length, middleLength);
        if (!DateTime.TryParseExact(
                middle[..StampLength],
                StampFormat,
                CultureInfo.InvariantCulture,
                DateTimeStyles.None,
                out _))
            return false;

        ReadOnlySpan<char> rest = middle[StampLength..];
        if (rest.IsEmpty)
        {
            stamp = middle[..StampLength].ToString();
            return true;
        }

        if (rest[0] != '-' || !int.TryParse(rest[1..], NumberStyles.None, CultureInfo.InvariantCulture, out counter))
            return false;

        stamp = middle[..StampLength].ToString();
        return true;
    }

    /// <summary>
    /// Chronological sort key. Sorting the raw names would be subtly wrong: '-' sorts
    /// before '.', so <c>…121314-1.log</c> (the LATER, de-duplicated roll) would sort
    /// ahead of <c>…121314.log</c> and get deleted first.
    /// </summary>
    private static (string Stamp, int Counter) Key(string activeName, string candidate)
    {
        TryParseArchive(activeName, candidate, out string stamp, out int counter);
        return (stamp, counter);
    }
}
