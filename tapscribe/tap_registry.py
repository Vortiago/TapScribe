"""In-flight tap registry — reference-counted guard against pruning or
deleting a session while a tap is opening it.

The canonical home. Imported by the tap hot path (``tap_fan_out``), the
destructive-route preflight (``routes/guards``) and ``session_maintenance``'s
prune walk; ``session_maintenance`` also re-exports these names, which is how
#257's leak detector and the destructive-route contract reach the registry.
Deliberately a leaf: imports nothing in TapScribe, so neither the hot path nor
the preflight pulls operator-maintenance weight.

The registry carries TWO primitives:

* The **in-flight mark** (``mark_session_in_flight`` / ``release_session_mark`` /
  ``session_has_open_tap``) protects prune-empty from deleting an empty session
  directory that a tap is about to write into (the mkdir→WAV-open window).

* The **destruction guard** (``register_tap`` / ``unregister_tap`` /
  ``try_claim_destruct`` / ``release_destruct``) protects the destructive
  routes (``DELETE /api/sessions/{s}``, ``DELETE /api/sessions/{s}/audio``)
  from racing with an opening tap — ``try_claim_destruct`` atomically checks
  for open taps before the worker thread starts its walk, and a tap arriving
  mid-destruction is refused by the ``-1`` sentinel.
"""

from __future__ import annotations

import threading

# Sessions with a tap in flight, keyed by session dirname. Reference-counted
# so concurrent taps into one session are independent. Read through
# `session_has_open_tap`; write through the mark/release pair below.
_tap_open_sessions: dict[str, int] = {}

# Destruction guard: per-session reader counter (-1 = destruction in progress,
# positive = number of open taps, absent = no tap). Keys appear on the first
# register_tap and are removed when the counter drops to zero.
_tap_active_count: dict[str, int] = {}
# ONE lock for the whole counter, deliberately not one per session. A per-session
# lock has to be removed when its session goes away, and removing it is unsound:
# a thread that already holds a reference from setdefault/get can be left holding
# an ORPHANED lock while the next caller creates a fresh one, at which point two
# threads mutate the counter under different locks. That loses an update, and the
# update it loses can be the -1 sentinel — a live tap and an in-flight destruction
# at the same time, which is the very race this guard exists to close.
# Every critical section here is an integer read plus a write, so a single lock
# costs nothing measurable and has no lifetime problem.
_guard = threading.Lock()


def mark_session_in_flight(session_name: str) -> None:
    """Mark a session as having a tap in flight, so `prune_empty_sessions`
    spares its directory through the mkdir→WAV-open window — the span where
    the folder exists and is still empty.

    Take the mark BEFORE the mkdir; one taken after it leaves that window open.
    Pair every mark with `release_session_mark` on EVERY exit path, partial-init
    failures included: nothing but a process restart clears a leaked mark, and
    the destructive-route preflight reads this same registry (#405), so a leak
    leaves the session un-prunable AND makes its delete, audio-delete, absorb
    and WAV-delete routes answer 409 forever. Worse than the race it closes.

    Only the fresh-record open needs it: `try_resume` appends to an existing WAV
    (never empty) and a probe tap takes no mark at all. A record-off tap takes
    none either, though it can still materialise the folder via
    `roster.record_occurrence` — that path is safe only because prune stays
    synchronous on the event loop (see `prune_empty_sessions`)."""
    _tap_open_sessions[session_name] = _tap_open_sessions.get(session_name, 0) + 1


def release_session_mark(session_name: str) -> None:
    """Drop one in-flight mark; releasing an UNMARKED session is a no-op.

    That no-op is not double-release safety. With two concurrent taps the count
    is 2, so a second release from one of them drives it to 0 and pops the key —
    stranding the other tap's mark and making its directory prunable mid-window.
    Exactly one release per mark is the caller's obligation; `TapFanOut._close`
    discharges it by nulling `_prune_mark` after releasing."""
    count = _tap_open_sessions.get(session_name, 0) - 1
    if count > 0:
        _tap_open_sessions[session_name] = count
    else:
        _tap_open_sessions.pop(session_name, None)


def session_has_open_tap(session_name: str) -> bool:
    """True while at least one tap holds an in-flight mark on `session_name`."""
    return session_name in _tap_open_sessions


def register_tap(session_name: str) -> bool:
    """Increment the destruction-guard reader count for `session_name`, or
    abort if a destructive operation is in progress.

    Called at the end of ``TapFanOut._open``'s await-free segment, after the
    in-flight mark is taken and the session directory is created. A return of
    ``False`` means ``_open`` should raise (the tap cannot open into a session
    that is being destroyed).

    Returns ``True`` on success, ``False`` when ``session_name`` is currently
    marked for destruction (the ``-1`` sentinel)."""
    with _guard:
        count = _tap_active_count.get(session_name, 0)
        if count == -1:
            return False  # destruction in progress
        _tap_active_count[session_name] = count + 1
    return True


def unregister_tap(session_name: str) -> None:
    """Decrement the destruction-guard reader count for `session_name`.

    Called at the start of ``TapFanOut._close``'s sync-first cleanup, paired
    with ``register_tap``. Pops the key at zero."""
    with _guard:
        count = _tap_active_count.get(session_name, 0)
        if count <= 1:
            _tap_active_count.pop(session_name, None)
        else:
            _tap_active_count[session_name] = count - 1


def try_claim_destruct(session_name: str) -> bool:
    """Atomically check for open taps and claim destruction rights for
    `session_name`, or abort if a tap is live.

    Called by the destructive-route worker thread BEFORE its first destructive
    syscall. Returns ``True`` when the worker may proceed; ``False`` when at
    least one tap is open. On ``True``, ``release_destruct`` MUST be called
    after the worker finishes (success or failure)."""
    with _guard:
        if _tap_active_count.get(session_name, 0) > 0:
            return False
        _tap_active_count[session_name] = -1
        return True


def release_destruct(session_name: str) -> None:
    """Release the destruction guard set by ``try_claim_destruct``.

    Called by the destructive-route worker thread AFTER its destructive work
    completes (success or failure). Clears the ``-1`` sentinel so future
    workers can claim again."""
    with _guard:
        _tap_active_count.pop(session_name, None)
