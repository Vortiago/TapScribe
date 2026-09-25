using System.Globalization;

namespace TapScribe.Bundle.Core;

/// <summary>
/// The tray re-invoked as its own watchdog (ADR-0024): "watch process
/// <see cref="TrayPid"/>, and when it goes, kill process group
/// <see cref="GroupId"/>".
///
/// <para><b>Why a second process exists at all.</b> Windows gets this from the kernel —
/// <c>JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE</c> fires on process DEATH, so a tray that
/// crashes still takes the Recorder and its <c>whisperlivekit-server</c> grandchild with
/// it. macOS has no equivalent: a process group is only reaped by someone who is still
/// alive to signal it, and there is no <c>PDEATHSIG</c>. So the watch has to live somewhere
/// that OUTLIVES the tray, which is what ADR-0024 means by a parent-death watch in the
/// child's lifetime.</para>
///
/// <para>The watchdog is the tray's own executable rather than a second shipped binary:
/// one thing to sign, one thing to install, and nothing that can go missing on its own.</para>
///
/// Parsing is here, in the assembly the Linux leg tests, because it is the half that can be
/// got wrong silently — a malformed argv that parses to a plausible pid would have the
/// watchdog kill a process group nobody asked it to.
/// </summary>
/// <param name="TrayPid">The process to watch.</param>
/// <param name="GroupId">The process group to kill once it exits.</param>
public sealed record ReapRequest(int TrayPid, int GroupId)
{
    /// <summary>The argv[1] that means "you are the watchdog, not the tray".</summary>
    public const string Flag = "--reap-group";

    /// <summary>
    /// Read a watchdog invocation out of argv, or answer null for the ordinary launch.
    ///
    /// Everything is rejected rather than guessed: the wrong count, a non-integer, a
    /// non-positive id. This process's whole job is to send a signal to a process group, so
    /// an argument it had to interpret loosely is one it should refuse instead — and 0 and
    /// -1 are exactly the <c>killpg</c> arguments that mean "my own group" and "everything I
    /// am allowed to signal".
    /// </summary>
    public static ReapRequest? Parse(IReadOnlyList<string> args)
    {
        ArgumentNullException.ThrowIfNull(args);

        if (args.Count != 3 || !string.Equals(args[0], Flag, StringComparison.Ordinal))
            return null;

        return TryId(args[1], out int tray) && TryId(args[2], out int group)
            ? new ReapRequest(tray, group)
            : null;
    }

    private static bool TryId(string text, out int id) =>
        int.TryParse(text, NumberStyles.None, CultureInfo.InvariantCulture, out id) && id > 0;
}
