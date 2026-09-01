using System.Diagnostics;

namespace TapScribe.Bundle.Core;

/// <summary>
/// The backstop that takes the Recorder — and the grandchildren it spawns — down with the
/// tray, however the tray died.
///
/// <see cref="RecorderSupervisor"/> already kills its own child with
/// <c>entireProcessTree</c>, so this is not the ordinary path. It is the CRASH path: a tray
/// that is killed, faults, or is logged out from under runs no teardown at all, and what
/// survives is the Recorder plus its <c>whisperlivekit-server</c> grandchild, holding port
/// 8001 with nothing left to stop them.
///
/// A seam because the two platforms are genuinely not equivalent, which is the one thing
/// worth naming here rather than discovering later. Windows'
/// <c>JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE</c> reaps on process DEATH, crash included — the
/// whole reason the job object exists. A POSIX process group only reaps when the parent
/// exits cleanly enough to signal it, so the macOS implementation has to pair the group
/// with a parent-death watch in the CHILD's lifetime (ADR-0022, ADR-0024).
///
/// The supervisor treats a null reaper as a supported degraded mode — a Recorder that runs
/// with no backstop is better than a Bundle that will not start — so an implementation is
/// free to answer "could not" rather than to throw.
/// </summary>
public interface IProcessReaper : IDisposable
{
    /// <summary>
    /// Whether a process spawned by THIS process is already a member, with nothing to do
    /// per child. True is the good case on both platforms: membership by inheritance is
    /// what closes the window where a child forks a grandchild in its first instants and
    /// the grandchild escapes.
    ///
    /// When it is false the supervisor falls back to <see cref="Adopt"/>, which is
    /// narrower — it can only reach the child it is handed.
    /// </summary>
    bool CoversChildrenByInheritance { get; }

    /// <summary>
    /// Enrol one already-started child. Called only when
    /// <see cref="CoversChildrenByInheritance"/> is false. Returns false rather than
    /// throwing: the caller logs it and carries on, because the meeting the operator is
    /// about to record matters more than the leak they may get at quit.
    /// </summary>
    bool Adopt(Process child);
}
