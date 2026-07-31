"""In-flight tap registry — reference-counted guard against pruning or
deleting a session while a tap is opening it.

The canonical home. Imported by the tap hot path (``tap_fan_out``), the
destructive-route preflight (``routes/guards``) and ``session_maintenance``'s
prune walk; ``session_maintenance`` also re-exports these names, which is how
#257's leak detector and the destructive-route contract reach the registry.
Deliberately a leaf: imports nothing in TapScribe, so neither the hot path nor
the preflight pulls operator-maintenance weight."""

from __future__ import annotations

# Sessions with a tap in flight, keyed by session dirname. Reference-counted
# so concurrent taps into one session are independent. Read through
# `session_has_open_tap`; write through the mark/release pair below.
_tap_open_sessions: dict[str, int] = {}


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
