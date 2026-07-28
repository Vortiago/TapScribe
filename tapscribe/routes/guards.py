"""The destructive-route preflight.

Every route that deletes or moves a session's bytes crosses this: session
delete, session-audio delete, absorb, WAV delete. It stays in the HTTP layer
rather than moving into `session_maintenance` because it needs the live
Recorder (jobs + streams) and that module is deliberately recorder-free (its
bulk reclaim takes a `busy_check` callback instead).
"""

from __future__ import annotations

from fastapi import HTTPException

from ..recorder import Recorder, SessionBusy


async def refuse_current_or_busy(
    recorder: Recorder,
    *sessions: str,
    action: str,
    current: str,
    hint: str = "",
) -> None:
    """The three-guard pre-flight the destructive session/WAV routes need:
    refuse the CURRENT session — before the caller's `resolve_session_dir`,
    since the live session's directory may not be materialised on disk yet
    (rotate_session creates it lazily) — then refuse if any of `sessions`
    has a transcribe/strip job in flight — then refuse if `current` has a
    live tap writing to it.

    `current` names which of `sessions` must not be the live session —
    required, not derived, so a multi-session caller (absorb) can't
    silently skip it: absorb's target MAY be the live session, only its
    source may not. The active-tap guard reuses that SAME `current` scope
    (not all of `sessions`): absorb only moves source's files into target
    and never rewrites target's own files, so a tap on a live TARGET is
    never unsafe — only a tap on the session actually being emptied/deleted
    (`current`) is. `action` fills the current-session message's verb
    phrase ("delete", "absorb", …); `hint` appends extra guidance (absorb's
    rotate-then-absorb tip). The busy-job and active-tap branches both raise
    `SessionBusy` — the same domain error `JobTracker.run` raises, mapped to
    409 by `DOMAIN_ERROR_STATUS` — so "session busy" has one canonical
    exception app-wide; only the current-session branch raises
    `HTTPException` directly (no domain error exists for session-identity)."""
    if current == recorder.session_start:
        msg = f"cannot {action} the current session — rotate to a new one first"
        raise HTTPException(409, f"{msg}, {hint}" if hint else msg)
    if any(recorder.jobs.get(s) is not None for s in sessions):
        noun = "this session" if len(sessions) == 1 else "one of these sessions"
        raise SessionBusy(f"a transcribe or strip job is in flight on {noun}")
    if any(s.session == current for s in await recorder.streams.snapshot()):
        raise SessionBusy("a live tap is writing to this session")
