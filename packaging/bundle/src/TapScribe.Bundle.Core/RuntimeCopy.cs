namespace TapScribe.Bundle.Core;

/// <summary>What a <see cref="RuntimeCopy.Ensure"/> did. The tray renders its notice from
/// this rather than inferring one: only <see cref="Upgraded"/> and <see cref="Repaired"/>
/// lose the model backends <c>/setup</c> pip-installed, and a shell working that out from
/// paths would get it wrong the first time a fourth outcome existed.</summary>
public enum RuntimeCopyOutcome
{
    /// <summary>Nothing to do — this platform runs the payload in place (Windows).</summary>
    NotNeeded,

    /// <summary>The runtime for this version was already there and intact.</summary>
    Current,

    /// <summary>No runtime existed for any version: a first launch.</summary>
    Fresh,

    /// <summary>A runtime for an older version was replaced. The backends are gone.</summary>
    Upgraded,

    /// <summary>A runtime for THIS version was there but broken, so it was deleted and
    /// re-copied. The backends are gone. This is what makes "reinstall to repair" true on
    /// macOS, where reinstalling replaces the <c>.app</c> and never the copy.</summary>
    Repaired,
}

/// <summary>The result of ensuring the runtime, and whether the operator has to be told.</summary>
/// <param name="Outcome">What happened.</param>
/// <param name="PreviousVersion">The version whose runtime was replaced, when one was.</param>
public sealed record RuntimeCopyResult(RuntimeCopyOutcome Outcome, string? PreviousVersion = null)
{
    /// <summary>
    /// Whether <c>/setup</c>'s model backends are gone and the operator should be offered
    /// the install picker again (ADR-0024). True exactly when a copy was (re-)made over an
    /// interpreter that had been pip-installed into — never on a first launch, which had
    /// no backends to lose, and never on a hit.
    /// </summary>
    public bool BackendsLost => Outcome is RuntimeCopyOutcome.Upgraded or RuntimeCopyOutcome.Repaired;
}

/// <summary>
/// The macOS Bundle's first-launch copy (ADR-0024): the read-only payload inside the
/// <c>.app</c> becomes a writable, version-stamped runtime under the data root, because
/// <c>/setup</c> pip-installs at runtime and writing inside a signed <c>.app</c>
/// invalidates its signature.
///
/// In <c>Bundle.Core</c> and not in a <c>pkg</c> postinstall script, which is the whole
/// reason ADR-0024 rejects that option: a script runs as root with <c>$HOME</c> at
/// <c>/var/root</c>, has to resolve the console user by hand, and is untestable on the
/// Linux leg that covers the rest of this assembly. Every rule below is exercised there,
/// against real directories.
///
/// <para><b>The atomic rename IS the completion marker.</b> The copy lands in
/// <c>&lt;version&gt;.partial/</c> and becomes <c>&lt;version&gt;/</c> only once every
/// byte is written, so a crash partway through 300 MB leaves a partial nobody will ever
/// mistake for a runtime. There is deliberately no sentinel file to write, forget to
/// write, or write too early.</para>
/// </summary>
public static class RuntimeCopy
{
    /// <summary>Appended to the version while a copy is in flight. A directory that ends
    /// in this is never a runtime, however complete it looks.</summary>
    public const string PartialSuffix = ".partial";

    /// <summary>
    /// Make sure <see cref="BundleLayout.RuntimeDirectory"/> holds a usable interpreter,
    /// copying it from the payload when it does not.
    ///
    /// Answers <see cref="RuntimeCopyOutcome.NotNeeded"/> without touching the disk on a
    /// platform whose runtime IS the payload, so the caller is the same on both and the
    /// Windows tray does not have to know this type exists.
    /// </summary>
    /// <param name="layout">The resolved layout.</param>
    /// <param name="log">Where progress and the reason for a re-copy are said.</param>
    /// <exception cref="BundleLayoutException">The payload is not there to copy from —
    /// a packaging bug the operator must see, phrased the way every other one in this
    /// assembly is.</exception>
    public static RuntimeCopyResult Ensure(BundleLayout layout, Action<string> log)
    {
        ArgumentNullException.ThrowIfNull(layout);
        ArgumentNullException.ThrowIfNull(log);

        if (!layout.RuntimeIsACopy)
            return new RuntimeCopyResult(RuntimeCopyOutcome.NotNeeded);

        bool present = Directory.Exists(layout.RuntimeDirectory);
        if (present && IsIntact(layout))
            return new RuntimeCopyResult(RuntimeCopyOutcome.Current);

        // Runtimes for OTHER versions, kept until the new one is complete so a failed
        // upgrade leaves the operator with the Recorder they had. Read ONCE: the list is
        // reported as PreviousVersion and then deleted, and two enumerations that had to
        // agree could stop agreeing the first time the filter changed.
        List<string> superseded = OtherVersions(layout);

        RuntimeCopyOutcome outcome = present
            ? RuntimeCopyOutcome.Repaired
            : superseded.Count > 0 ? RuntimeCopyOutcome.Upgraded : RuntimeCopyOutcome.Fresh;

        if (present)
        {
            // Broken rather than absent: an interrupted copy of THIS version, or a
            // half-deleted one. Nothing here is worth keeping, and leaving it would make
            // the intact check below fail forever.
            log($"runtime: {layout.RuntimeDirectory} is incomplete — deleting and copying again.");
            Delete(layout.RuntimeDirectory);
        }

        string partial = layout.RuntimeDirectory + PartialSuffix;
        Delete(partial);

        log($"runtime: copying the interpreter to {layout.RuntimeDirectory} (this takes a moment).");
        // No cleanup handler around these two: a throw leaves the PARTIAL behind, which is
        // exactly right. It is the crash marker, it is named so nothing can mistake it for
        // a runtime, and the next launch deletes it above. Catching here to tidy up would
        // only race the failure that got us here — and a half-deleted partial is worse than
        // a whole one.
        CopyPayload(layout, partial);
        Directory.Move(partial, layout.RuntimeDirectory);

        // Only now — the new runtime is complete, so the old ones have stopped being the
        // fallback they were being kept as.
        foreach (string old in superseded)
        {
            log($"runtime: removing the superseded {old}.");
            try
            {
                Delete(Path.Join(layout.RuntimeRoot, old));
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException)
            {
                // Tidy-up, not the boot. The new runtime is complete and about to be used;
                // letting a locked or unreadable file in the OLD one propagate would turn a
                // successful upgrade into "TapScribe could not start", which is strictly
                // worse than one stale folder under the data root. The next launch tries
                // again, because a runtime for another version is still superseded.
                log($"runtime: could not remove the superseded {old} ({error.Message}) — carrying on.");
            }
        }

        return new RuntimeCopyResult(outcome, superseded.FirstOrDefault());
    }

    /// <summary>
    /// Whether an existing runtime can be run, which is asked as "is the interpreter
    /// there" and nothing more.
    ///
    /// Not a checksum: the payload is 300 MB and this runs on every launch, so a hash
    /// would cost seconds of a tray's startup to catch a corruption the OS does not
    /// otherwise produce. What it DOES catch is the case that actually happens — a copy
    /// that died partway, or a folder someone emptied — because the interpreter is
    /// written by <see cref="CopyPayload"/> like everything else and its absence means
    /// the tree is not whole.
    /// </summary>
    private static bool IsIntact(BundleLayout layout) =>
        File.Exists(layout.Python) && Directory.Exists(layout.WheelDirectory);

    /// <summary>Version folders under the runtime root that are not this version and not
    /// a partial — i.e. the previous release's runtime, which an upgrade supersedes.</summary>
    private static List<string> OtherVersions(BundleLayout layout)
    {
        if (!Directory.Exists(layout.RuntimeRoot))
            return [];

        string mine = Path.GetFileName(layout.RuntimeDirectory);
        return Directory.GetDirectories(layout.RuntimeRoot)
            .Select(Path.GetFileName)
            .OfType<string>()
            .Where(name => !string.Equals(name, mine, StringComparison.Ordinal))
            .Where(name => !name.EndsWith(PartialSuffix, StringComparison.Ordinal))
            .Order(StringComparer.Ordinal)
            .ToList();
    }

    /// <summary>Copy the interpreter and the wheel into <paramref name="destination"/>,
    /// which is the partial. Both, because the runtime is what
    /// <see cref="BundleLayout.ResolveWheel"/> reads and preflight installs from — a copy
    /// with only the interpreter would fail at the first pip run rather than here.</summary>
    private static void CopyPayload(BundleLayout layout, string destination)
    {
        if (!Directory.Exists(layout.PayloadPythonDirectory))
            throw new BundleLayoutException(
                $"the Bundle's interpreter is missing: {layout.PayloadPythonDirectory}. " +
                $"This build of TapScribe is incomplete — {layout.ReinstallAdvice}");

        Directory.CreateDirectory(destination);
        CopyTree(layout.PayloadPythonDirectory, Path.Join(destination, BundleLayout.PythonFolder));
        if (Directory.Exists(layout.PayloadWheelDirectory))
            CopyTree(layout.PayloadWheelDirectory, Path.Join(destination, BundleLayout.WheelFolder));
    }

    /// <summary>
    /// A plain recursive file copy that RE-CREATES symlinks rather than following them.
    ///
    /// Deliberately not <c>ditto</c> or <c>cp -R</c> through a subprocess, which would put
    /// this back on the shell-out path CLAUDE.md constrains and make it untestable off
    /// macOS. The executable bit is what matters for the regular files, and
    /// <see cref="File.Copy(string, string)"/> preserves the unix mode on .NET.
    ///
    /// <para>Links are the part a naive copy gets wrong, and the packaging script says so
    /// out loud — <c>build-bundle-pkg.sh</c> uses <c>ditto</c> "for the symlinks and exec
    /// bits a .app and a python-build-standalone tree are both made of". Following them
    /// instead would (a) throw <see cref="FileNotFoundException"/> on a dangling one and
    /// fail the whole launch, (b) recurse forever into a directory link that points at an
    /// ancestor, which is a StackOverflow no catch tier can absorb, and (c) silently
    /// duplicate every linked binary. Recreating the link is all three at once.</para>
    /// </summary>
    private static void CopyTree(string source, string destination)
    {
        Directory.CreateDirectory(destination);

        foreach (string file in Directory.EnumerateFiles(source))
        {
            string target = Path.Join(destination, Path.GetFileName(file));
            if (Relink(new FileInfo(file), target, directory: false))
                continue;
            File.Copy(file, target, overwrite: true);
        }

        // ONE walk, carrying the destination down with it. The two-sweep shape this
        // replaced read the whole tree twice — once for directories, once for files — and
        // materialised both into arrays: a python-build-standalone tree with core deps
        // installed is tens of thousands of entries, so that was megabytes of strings live
        // at once, plus a Path.GetRelativePath per entry, in a menu-bar app that otherwise
        // idles small.
        foreach (string dir in Directory.EnumerateDirectories(source))
        {
            string target = Path.Join(destination, Path.GetFileName(dir));
            // BEFORE the recursion, never inside it: a directory link is exactly the entry
            // that must not be walked.
            if (Relink(new DirectoryInfo(dir), target, directory: true))
                continue;
            CopyTree(dir, target);
        }
    }

    /// <summary>Reproduce <paramref name="entry"/> at <paramref name="target"/> when it is a
    /// symlink, answering whether it was one. The RAW target is copied, relative links
    /// included, so a link into the tree keeps pointing inside the copy.</summary>
    private static bool Relink(FileSystemInfo entry, string target, bool directory)
    {
        if (entry.LinkTarget is not { } linkTarget)
            return false;

        // Overwrite rather than fail. The copy normally lands in a freshly deleted partial,
        // so there is nothing here; this is what keeps a re-copy over a surviving entry from
        // throwing on the name. A DANGLING link answers false to both Exists probes while
        // still occupying the name, which is why the unlink is unconditional and its own
        // failure is the caller's to see.
        if (Directory.Exists(target) && new DirectoryInfo(target).LinkTarget is null)
            Directory.Delete(target, recursive: true);
        else
            File.Delete(target); // no-op when absent; unlinks a link without following it

        if (directory)
            Directory.CreateSymbolicLink(target, linkTarget);
        else
            File.CreateSymbolicLink(target, linkTarget);
        return true;
    }

    /// <summary>Remove a tree if it is there. Absent is the desired state, not an error.</summary>
    private static void Delete(string directory)
    {
        if (Directory.Exists(directory))
            Directory.Delete(directory, recursive: true);
    }
}
