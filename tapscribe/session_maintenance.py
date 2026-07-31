"""Session maintenance — operator-triggered lifecycle operations on a session
folder: prune empty sessions, absorb one session into another, and delete a
session's audio.

Split out of `sessions.py` (which keeps the poll-path listing / catalog): these
are destructive, infrequent, operator-driven filesystem operations, not part of
the once-per-second dashboard read path. They take a `session` id, resolve it
through `session_paths` (the path-safety seam), and read/write session meta via
`sessions`. Pure — no FastAPI dependency. The route handlers do the
current-session / in-flight-job pre-flight; these functions are purely the
filesystem op.

One documented exception to "purely the filesystem op": the in-flight tap mark
below. `prune_empty_sessions` is the enforcement point for the prune-vs-tap
invariant (#257), so the registry it consults lives beside it rather than on the
Recorder — the mark is a plain session-dirname refcount, not recorder state, so
this module stays recorder-free. `TapFanOut._open` (the recording hot path) is
what sets it; that mark is the one frequent, non-destructive thing here.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import tapscribe.strip_meta as strip_meta

from . import config
from .roster import read_roster
from .session_paths import (
    DIRNAME_STRIPPED,
    FILENAME_ROSTER_JSON,
    FILENAME_SUMMARY_JSON,
    FILENAME_TRANSCRIPT_JSON,
    FILENAME_TRANSCRIPT_TXT,
    resolve_session_dir,
    resolve_wav,
    stripped_dir,
)
from .sessions import read_session_meta, write_session_meta
from .text import atomic_write_text, parse_wav_start
from .wav_cache import sidecar_paths

# Sessions with a tap in flight, keyed by session dirname. Reference-counted so
# concurrent taps into one session are independent. Read it through
# `session_has_open_tap`; write it through the mark/release pair below.
_tap_open_sessions: dict[str, int] = {}


def mark_session_in_flight(session_name: str) -> None:
    """Mark a session as having a tap in flight, so `prune_empty_sessions`
    spares its directory through the mkdir→WAV-open window — the span where the
    folder exists and is still empty.

    Take the mark BEFORE the mkdir; one taken after it leaves that window open.
    Pair every mark with `release_session_mark` on EVERY exit path, partial-init
    failures included: a leaked mark makes the session permanently un-prunable,
    which is worse than the race it closes.

    Only the fresh-record open needs it: `try_resume` appends to an existing WAV
    (never empty) and a probe tap takes no mark at all. A record-off tap takes
    none either, though it can still materialise the folder via
    `roster.record_occurrence` — that path is safe only because prune stays
    synchronous on the event loop (see `prune_empty_sessions`).

    The registry is module-global rather than injected the way
    `reclaim_audio_older_than` takes `exclude_sessions` / `busy_check`; a neutral
    home for it is tracked in #405.
    """
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


# ---------------------------------------------------------------------------
# Domain errors — FastAPI-free maintenance exceptions.
# ---------------------------------------------------------------------------


class AbsorbCollision(Exception):
    """Filename collision between source and target during absorb."""


class InvalidAbsorbRequest(Exception):
    """target == source — absorbing a session into itself is a no-op."""


class SessionDeleteError(Exception):
    """IO failure while deleting a session's files (rmtree OSError).

    Covers BOTH scopes an operator can ask for: `delete_session_audio`'s
    `stripped/` teardown here, and `api_session_delete`'s teardown of the
    whole session folder. Sizing the blast radius of the 500 it maps to
    means reading the raise site, not this class."""


def session_is_empty(session_dir: Path) -> bool:
    """True when a session folder holds nothing worth keeping: no WAVs, no
    merged transcript, and no operator label.

    The single definition of "empty session", shared by `prune_empty_sessions`
    (which folders to delete) and the tap new-session idempotency guard
    (whether rotating would just churn an untouched session) — so "empty"
    means the same thing in both places.
    """
    if any(session_dir.glob("*.wav")):
        return False
    if (session_dir / FILENAME_TRANSCRIPT_JSON).exists():
        return False
    if read_session_meta(session_dir.name).get("label"):
        return False
    return True


def _iter_candidate_session_dirs(current_session: str) -> Iterator[Path]:
    """Yield each real session directory under ``RECORDINGS_DIR`` that is not
    ``current_session`` — the shared walk-safety guard for the destructive bulk
    ops (``prune_empty_sessions``, ``reclaim_audio_older_than``). Each caller
    layers its own tail filter (empty / age) on the yielded dirs.

    Skips symlinks BEFORE ``is_dir()`` (which follows them): a symlink planted
    in ``RECORDINGS_DIR`` must never let a delete escape to an out-of-tree
    target. (``shutil.rmtree`` also refuses symlinks, but don't rely on that
    internal.) This symlink-before-is_dir ordering is the security invariant the
    shared iterator keeps in one place.

    Pure filesystem walk over the ``RECORDINGS_DIR`` glob (a constant): no path
    is built from request input, so the path-injection guard doesn't apply here.
    """
    for sd in config.RECORDINGS_DIR.glob("*"):
        if sd.is_symlink():
            continue
        if not sd.is_dir():
            continue
        if sd.name == current_session:
            continue
        yield sd


def prune_empty_sessions(current_session: str) -> dict[str, Any]:
    """Delete every session folder under RECORDINGS_DIR that `session_is_empty`
    (no WAVs, no merged transcript, no operator label). Never deletes
    `current_session` or a session with a tap in flight
    (`session_has_open_tap`). Returns
    ``{"pruned": [...], "count": N, "failed": [...]}``.

    Shared by the manual `/api/sessions/prune-empty` endpoint and the
    rotate-then-prune flow behind every new-session trigger.

    Call it synchronously on the event loop — never via `asyncio.to_thread`,
    the house idiom for every other destructive walk in `routes/sessions.py`.
    The in-flight check, `session_is_empty` and the `rmtree` below are
    check-then-act; they are atomic against `TapFanOut._open`'s mark→mkdir only
    because nothing yields between them. Off the loop thread a tap can take its
    mark and mkdir after the check and still lose its audio to the rmtree — the
    half of the invariant the mark itself does NOT make structural.
    """
    pruned: list[str] = []
    failed: list[dict[str, str]] = []
    for sd in _iter_candidate_session_dirs(current_session):
        if session_has_open_tap(sd.name):
            continue
        if not session_is_empty(sd):
            continue
        try:
            shutil.rmtree(sd)
            pruned.append(sd.name)
        except OSError as e:
            # Log the raw OSError (it embeds the filesystem path + errno)
            # server-side only — this dict flows out of the tap-facing
            # /api/tap/new-session response, so surfacing it would leak
            # internals (CodeQL py/stack-trace-exposure). Return a generic
            # marker; the operator reads the real cause in the server log.
            print(f"[tapscribe] prune failed for {sd.name}: {e}", flush=True)
            failed.append({"session": sd.name, "error": "delete failed"})
    return {"pruned": pruned, "count": len(pruned), "failed": failed}


# ---------------------------------------------------------------------------
# Sidecar-aware move / delete helpers (shared by absorb + the delete endpoints)
# ---------------------------------------------------------------------------


def _move_sidecars_with_wav(src_wav: Path, dst_wav: Path) -> None:
    """Carry every sidecar the source WAV has to the WAV's new home.
    Any entry may be absent. The layout enumeration comes from
    `wav_cache.sidecar_paths` — the layout's one owner — so a new cache
    layout added there follows the WAV with no change here; the stable
    enumeration order lets the src/dst calls zip into move pairs."""
    for (kind, src), (_, dst) in zip(sidecar_paths(src_wav), sidecar_paths(dst_wav), strict=True):
        present = src.is_dir() if kind == "dir" else src.is_file()
        if present:
            shutil.move(str(src), str(dst))


def _safe_size(p: Path) -> int:
    """Size of `p` in bytes, or 0 if it isn't a file or can't be statted.
    The `bytes_freed` the delete endpoints report is advisory, so a stat
    race (a path vanishing mid-walk) must never abort the delete."""
    try:
        return p.stat().st_size if p.is_file() else 0
    except OSError:
        return 0


def _dir_size(d: Path) -> int:
    """Best-effort sum of file sizes under `d`, for the delete endpoints'
    advisory `bytes_freed`."""
    return sum(_safe_size(f) for f in d.rglob("*"))


def _delete_wav_with_sidecars(wav: Path, *, dry_run: bool = False) -> int:
    """Delete one WAV plus every sidecar `wav_cache.sidecar_paths`
    enumerates for it (any entry may be absent). Returns the bytes
    reclaimed (WAV + sidecars).

    With `dry_run=True` nothing is unlinked — it only SUMS the same bytes it
    would free. That makes this the sizing walk the bulk-reclaim PREVIEW
    reuses (via `delete_session_audio`), so a preview total can never drift
    from what an execute actually frees.

    The destructive analog of `_move_sidecars_with_wav` above: both iterate
    `wav_cache.sidecar_paths`, the layout's single enumeration, so a THIRD
    cache layout added in `wav_cache` is automatically moved on absorb and
    counted + removed here — nothing to wire by hand. The per-entry action
    keys off the entry's `kind` (file → stat + unlink, dir → walk + rmtree).

    `wav` is a Path discovered by globbing a `resolve_session_dir`-checked
    folder (or returned by `resolve_wav`), not a path BUILT from a parsed
    filename — so the `safe_name` round-trip rule (which guards path
    construction against `py/path-injection`) does not apply here; deleting
    an already-validated Path is safe."""
    freed = _safe_size(wav)
    for kind, sidecar in sidecar_paths(wav):
        if kind == "dir":
            if sidecar.is_dir():
                freed += _dir_size(sidecar)
                if not dry_run:
                    # Best-effort: a locked cache dir shouldn't block
                    # reclaiming the WAV; an orphaned cache dir is harmless
                    # and re-cleanable.
                    shutil.rmtree(sidecar, ignore_errors=True)
        elif sidecar.is_file():
            freed += _safe_size(sidecar)
            if not dry_run:
                sidecar.unlink(missing_ok=True)
    if not dry_run:
        wav.unlink(missing_ok=True)
    return freed


# ---------------------------------------------------------------------------
# Session absorb — fold one session's WAVs into another
# ---------------------------------------------------------------------------


def _prune_strip_meta_clip(session: str, clip_name: str) -> None:
    """Drop a deleted region clip's span from stripped/strip-meta.json so the
    committed-cut overlay stops drawing audio that no longer exists. An
    original whose spans all vanish loses its whole entry. Best-effort: a
    missing/legacy meta is left alone."""
    stripped = stripped_dir(session)
    strip_meta.prune_clip(stripped, clip_name)


def absorb_session(target: str, source: str) -> dict[str, Any]:
    """Move every WAV (and its `<name>.json` sidecar) from `source` into
    `target`, fold the source's `session-meta.json` aliases and
    `session-roster.json` occurrences into the target's (target wins on key
    conflict; roster WAV lists union), invalidate the target's merged
    transcript, and delete the source folder.

    Per-WAV filenames embed timestamps + UUIDs so cross-session collisions
    are essentially impossible, but we still refuse the merge rather than
    silently overwrite if any do collide.

    Pre-flight checks the route handler performs first (current-session
    refusal, in-flight-job refusal) live in the route; this function is
    purely the filesystem operation. Raises domain errors on validation
    failures so the route doesn't have to translate exceptions.
    """
    if target == source:
        raise InvalidAbsorbRequest("target and source must differ")

    target_dir = resolve_session_dir(target)
    source_dir = resolve_session_dir(source)

    src_wavs = sorted(source_dir.glob("*.wav"))
    src_stripped_dir = source_dir / DIRNAME_STRIPPED
    src_stripped_wavs = sorted(src_stripped_dir.glob("*.wav")) if src_stripped_dir.is_dir() else []

    # Refuse the merge rather than silently overwrite. Caller can rename
    # the colliding files manually if they really mean it.
    collisions: list[str] = []
    for w in src_wavs:
        if (target_dir / w.name).exists():
            collisions.append(w.name)
    tgt_stripped_dir = target_dir / DIRNAME_STRIPPED
    for w in src_stripped_wavs:
        if (tgt_stripped_dir / w.name).exists():
            collisions.append(f"stripped/{w.name}")
    if collisions:
        raise AbsorbCollision(f"filename collision(s) between sessions: {', '.join(collisions[:5])}")

    moved_wavs: list[str] = []
    moved_stripped: list[str] = []

    # Move originals + their sidecars. A WAV may carry either a legacy
    # `<wav>.json` (one transcript) or a `<wav>.transcripts/` directory
    # (one per cached backend+model); both layouts must follow the WAV.
    for w in src_wavs:
        shutil.move(str(w), str(target_dir / w.name))
        _move_sidecars_with_wav(w, target_dir / w.name)
        moved_wavs.append(w.name)

    # Move stripped/ siblings. If the target has no stripped/ yet, create
    # it; if both sides have stripped/, files merge in (the collision check
    # above already guarantees no overwrites).
    if src_stripped_wavs:
        tgt_stripped_dir.mkdir(parents=True, exist_ok=True)
        for w in src_stripped_wavs:
            shutil.move(str(w), str(tgt_stripped_dir / w.name))
            _move_sidecars_with_wav(w, tgt_stripped_dir / w.name)
            moved_stripped.append(w.name)
        # Carry the committed-cut sidecar with the clips it describes: merge
        # the source's per-original span entries into the target's (the
        # collision check above already guarantees original names can't
        # clash; target wins anyway). Knobs/stripped_at keep the TARGET's
        # values when both sides have a meta — they describe the target's
        # own last run; a target without a meta adopts the source's wholesale.
        src_strip_meta = strip_meta.read_strip_meta(src_stripped_dir)
        if src_strip_meta is not None:
            tgt_strip_meta = strip_meta.read_strip_meta(tgt_stripped_dir)
            if tgt_strip_meta is not None:
                tgt_strip_meta["files"] = {**src_strip_meta["files"], **tgt_strip_meta["files"]}
            strip_meta.write_strip_meta(tgt_stripped_dir, tgt_strip_meta or src_strip_meta)

    # Merge speaker aliases. Target wins on conflict; source fills in keys
    # the target doesn't already have. Target's label is preserved as-is.
    tgt_meta = read_session_meta(target)
    src_meta = read_session_meta(source)
    src_aliases = src_meta.get("aliases") or {}
    tgt_aliases = dict(tgt_meta.get("aliases") or {})
    aliases_added: list[str] = []
    for k, v in src_aliases.items():
        if k not in tgt_aliases:
            tgt_aliases[k] = v
            aliases_added.append(k)
    if aliases_added or tgt_meta:
        write_session_meta(
            target,
            {
                "label": tgt_meta.get("label", "") or "",
                "aliases": tgt_aliases,
            },
        )

    # Carry the source's Roster into the target's. The Roster is the ONLY
    # record of a recorded occurrence's FULL bridge Identity — the filename
    # keeps just `safe_name(identity)[:10]` (`parse_wav_speaker_ident`) — and
    # it is written by the tap path alone (`roster.record_occurrence`), never
    # rebuilt by transcribe/merge. The rmtree below would destroy it while the
    # WAVs it describes live on in the target, losing ADR-0009's cross-session
    # join key irreversibly (`attach_people` / `known_names_for_session` would
    # silently fall back to the truncated slug).
    #
    # Same conflict rule as the alias merge above — the target's entry wins on
    # the scalar fields — EXCEPT `wavs`, which unions: the source's WAVs now
    # physically live in the target, so the target's entry has to account for
    # them.
    src_roster = read_roster(source_dir)
    roster_merged = 0
    if src_roster:
        tgt_roster = read_roster(target_dir)
        for identity, src_entry in src_roster.items():
            tgt_entry = tgt_roster.get(identity)
            if tgt_entry is None:
                tgt_roster[identity] = src_entry
            else:
                tgt_entry["wavs"] = tgt_entry["wavs"] + [
                    w for w in src_entry["wavs"] if w not in tgt_entry["wavs"]
                ]
            roster_merged += 1
        atomic_write_text(
            target_dir / FILENAME_ROSTER_JSON,
            json.dumps(tgt_roster, indent=2, ensure_ascii=False),
        )

    # The target's merged transcript predates the just-moved WAVs, so it's
    # now stale. Drop it — both the JSON and the plain-text rendering
    # `batch_transcribe` writes beside it — so the operator's next "transcribe
    # whole session" rebuilds against the fuller WAV set. (Dropping only the
    # JSON reported `transcript_invalidated: True` while a stale .txt missing
    # every absorbed WAV survived on disk.)
    tgt_transcript = target_dir / FILENAME_TRANSCRIPT_JSON
    transcript_invalidated = tgt_transcript.exists()
    if transcript_invalidated:
        try:
            tgt_transcript.unlink()
            (target_dir / FILENAME_TRANSCRIPT_TXT).unlink(missing_ok=True)
        except OSError:
            # Best-effort: the WAVs are already moved at this point, so
            # raising would leave the merge half-applied (WAVs in target,
            # source still on disk, transcript still stale). The transcript
            # will be overwritten on the next ▶ transcribe whole session.
            pass

    # The target's persisted summary was built from that same now-dropped
    # transcript, so it is stale too. Drop it alongside, with the same
    # best-effort handling — the operator regenerates after the next transcribe.
    tgt_summary = target_dir / FILENAME_SUMMARY_JSON
    summary_invalidated = tgt_summary.exists()
    if summary_invalidated:
        try:
            tgt_summary.unlink()
        except OSError:
            # Best-effort, mirrors the transcript case above: the WAVs are
            # already moved, so raising would leave the merge half-applied.
            pass

    # Source folder is now expected to hold only metadata files (session-meta,
    # session-transcript) and an empty stripped/ at most. Wipe the whole tree.
    shutil.rmtree(source_dir)

    return {
        "target": target,
        "source": source,
        "wavs_moved": len(moved_wavs),
        "stripped_moved": len(moved_stripped),
        "aliases_added": aliases_added,
        "roster_merged": roster_merged,
        "transcript_invalidated": transcript_invalidated,
        "summary_invalidated": summary_invalidated,
    }


# ---------------------------------------------------------------------------
# Audio deletion (operator-triggered, used by the delete endpoints)
# ---------------------------------------------------------------------------


def delete_session_audio(session: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Delete ALL of a session's audio to reclaim disk: every original WAV
    in `<session>/`, the entire `<session>/stripped/` folder, and each
    WAV's per-WAV transcript-cache sidecars. KEEPS the merged
    `session-transcript.json` / `.txt` and `session-meta.json`, so the
    session survives `prune-empty` and the operator's result + label are
    preserved.

    With `dry_run=True` nothing is deleted — it returns the same
    `{session, wavs_deleted, bytes_freed}` it WOULD free. This is the one
    size-walk the bulk-reclaim preview reuses, so its reported total always
    matches what an execute frees (no duplicated sidecar-layout enumeration).

    Route-level guards (current-session refusal, in-flight-job refusal)
    live in the handler; this is purely the filesystem op. Returns
    `{session, wavs_deleted, bytes_freed}` — `wavs_deleted` counts the
    originals (matching the dashboard's "N wavs" header); `stripped/`
    bytes still fold into `bytes_freed`."""
    session_dir = resolve_session_dir(session)
    wavs_deleted = 0
    bytes_freed = 0
    for w in sorted(session_dir.glob("*.wav")):
        bytes_freed += _delete_wav_with_sidecars(w, dry_run=dry_run)
        wavs_deleted += 1
    stripped = session_dir / DIRNAME_STRIPPED
    if stripped.is_dir():
        bytes_freed += _dir_size(stripped)
        if not dry_run:
            try:
                shutil.rmtree(stripped)
            except OSError as e:
                raise SessionDeleteError(f"delete failed: {e}") from None
    return {"session": session, "wavs_deleted": wavs_deleted, "bytes_freed": bytes_freed}


def delete_session_wav(session: str, name: str, source: str = "original") -> dict[str, Any]:
    """Delete a single WAV (validated via `resolve_wav`) plus its sidecars.

    No region cascade: deleting an original does NOT sweep its derived
    `stripped/` regions, because regions bucket on `(speaker_slug, ident)`
    which is not unique per original (multiple WAVs from one tap identity
    share it), so a cascade could over-delete a sibling original's regions.
    Use the bulk `delete_session_audio` to free everything.

    Returns `{session, name, source, bytes_freed}`."""
    wav = resolve_wav(session, name, source)
    bytes_freed = _delete_wav_with_sidecars(wav)
    if source == "stripped":
        _prune_strip_meta_clip(session, name)
    return {"session": session, "name": name, "source": source, "bytes_freed": bytes_freed}


# ---------------------------------------------------------------------------
# Bulk audio reclaim (operator-triggered, #207)
# ---------------------------------------------------------------------------


def reclaim_audio_older_than(
    current_session: str,
    older_than_days: int,
    *,
    execute: bool = False,
    exclude_sessions: frozenset[str] = frozenset(),
    busy_check: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Walk the recordings archive and reclaim audio from sessions older than
    ``older_than_days`` days that also have a merged transcript.

    Eligibility: a session is eligible iff ALL of:

      * NOT the ``current_session`` (live session is NEVER touched)
      * NOT in ``exclude_sessions`` — the caller passes the sessions that have
        a transcribe/strip job in flight (``recorder.jobs``) or a live tap
        writing to them (``recorder.streams``); this recorder-free function
        can't see that state itself, so without the set a bulk reclaim could
        delete a session's WAVs out from under a running job/tap
      * NOT busy at delete time (see the ``busy_check`` note below)
      * Has at least one ``*.wav`` file
      * Has a ``session-transcript.json`` (audio backed by a transcript)
      * Its latest WAV start timestamp is older than the cutoff

    Age derivation: the session's ``latest_iso`` is the maximum WAV start
    timestamp across all WAVs (mirroring ``_describe_session`` in ``sessions.py``)
    — a session mixing old and recent WAVs reads as recent (kept) so we never
    reclaim one still being appended to. A session whose WAVs can't be parsed
    is skipped — no timestamp means no way to determine "older than".

    ``execute=False`` (preview): reports eligible sessions and their
    reclaimable byte counts without deleting anything.

    ``execute=True``: calls ``delete_session_audio`` for each eligible
    session, preserving the merged transcript + meta. A session whose delete
    fails (a locked ``stripped/`` dir, etc.) is collected into ``failed`` and
    the walk continues, so one bad session never aborts the whole bulk op or
    strands the operator in an unknown partial state (mirrors
    ``prune_empty_sessions``).

    ``busy_check`` — optional callable(session_name) -> JobState | None
    consulted at delete time (only when ``execute=True``). When it returns
    truthy (a session became busy *after* the ``exclude_sessions`` snapshot),
    the session is silently skipped, closing the TOCTOU gap. When absent,
    only the static ``exclude_sessions`` set is used.

    Returns ``{"sessions": [{"session": str, "bytes_freed": int}],
    "total_bytes": int, "failed": [{"session": str, "error": str}]}``.
    """
    # Defense in depth: the sole route caller already rejects <= 0, but a
    # non-positive cutoff reaches into the future and would reclaim even
    # near-current sessions — never let a stray/future caller trigger a
    # delete-everything.
    if older_than_days <= 0:
        return {"sessions": [], "total_bytes": 0, "failed": []}

    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    sessions: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    total_bytes = 0

    for sd in _iter_candidate_session_dirs(current_session):
        # Busy (job in flight) or live-tap sessions the caller flagged.
        if sd.name in exclude_sessions:
            continue
        # Must have a merged transcript (audio backed).
        if not (sd / FILENAME_TRANSCRIPT_JSON).exists():
            continue
        # Materialize WAV list once to avoid triple glob per session.
        wavs = list(sd.glob("*.wav"))
        if not wavs:
            continue
        # Derive age from the latest WAV timestamp. No parseable WAV → no age
        # → skip (nothing to reclaim by age anyway).
        wav_starts = [ts for w in wavs if (ts := parse_wav_start(w.name)) is not None]
        if not wav_starts:
            continue
        latest_iso = max(wav_starts)
        if latest_iso >= cutoff:
            # Inside the cutoff window → too young.
            continue

        # Eligible. Both branches route through delete_session_audio so the
        # preview's byte total is the SAME walk an execute frees.
        if execute:
            if busy_check is not None and busy_check(sd.name):
                continue
            try:
                bytes_freed = delete_session_audio(sd.name)["bytes_freed"]
            except SessionDeleteError as e:
                # Log the raw cause server-side, surface a generic marker, and
                # keep going — a locked stripped/ dir must not abort the op.
                print(f"[tapscribe] bulk reclaim failed for {sd.name}: {e}", flush=True)
                failed.append({"session": sd.name, "error": "delete failed"})
                continue
        else:
            bytes_freed = delete_session_audio(sd.name, dry_run=True)["bytes_freed"]
        sessions.append({"session": sd.name, "bytes_freed": bytes_freed})
        total_bytes += bytes_freed

    return {"sessions": sessions, "total_bytes": total_bytes, "failed": failed}
