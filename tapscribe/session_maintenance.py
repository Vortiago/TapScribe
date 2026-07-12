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
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from . import config
from .session_paths import (
    DIRNAME_STRIPPED,
    FILENAME_STRIP_META_JSON,
    FILENAME_SUMMARY_JSON,
    FILENAME_TRANSCRIPT_JSON,
    resolve_session_dir,
    resolve_wav,
    stripped_dir,
)
from .sessions import read_session_meta, read_strip_meta, write_session_meta
from .text import atomic_write_text

# ---------------------------------------------------------------------------
# Domain errors — FastAPI-free maintenance exceptions.
# ---------------------------------------------------------------------------


class AbsorbCollision(Exception):
    """Filename collision between source and target during absorb."""


class InvalidAbsorbRequest(Exception):
    """target == source — absorbing a session into itself is a no-op."""


class SessionDeleteError(Exception):
    """IO failure during session audio deletion (rmtree OSError)."""


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


def prune_empty_sessions(current_session: str) -> dict[str, Any]:
    """Delete every session folder under RECORDINGS_DIR that `session_is_empty`
    (no WAVs, no merged transcript, no operator label). Never deletes
    `current_session`. Returns ``{"pruned": [...], "count": N, "failed": [...]}``.

    Pure filesystem walk over the ``RECORDINGS_DIR`` glob (a constant): no path
    is built from request input, so the path-injection guard doesn't apply here.
    Shared by the manual `/api/sessions/prune-empty` endpoint and the
    rotate-then-prune flow behind every new-session trigger.
    """
    pruned: list[str] = []
    failed: list[dict[str, str]] = []
    for sd in config.RECORDINGS_DIR.glob("*"):
        # Skip symlinks BEFORE is_dir() (which follows them): a symlink planted
        # in RECORDINGS_DIR must never let this delete an out-of-tree target.
        # (shutil.rmtree also refuses symlinks, but don't rely on that internal.)
        if sd.is_symlink():
            continue
        if not sd.is_dir():
            continue
        if sd.name == current_session:
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
    """Carry whichever cache layout the source uses to the WAV's new
    home — the legacy `<wav>.json` file, the new `<wav>.transcripts/`
    directory, or both. Either may be absent."""
    legacy = src_wav.with_suffix(".json")
    if legacy.is_file():
        shutil.move(str(legacy), str(dst_wav.with_suffix(".json")))
    transcripts = src_wav.with_suffix(".transcripts")
    if transcripts.is_dir():
        shutil.move(str(transcripts), str(dst_wav.with_suffix(".transcripts")))


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


def _delete_wav_with_sidecars(wav: Path) -> int:
    """Delete one WAV plus whichever transcript-cache layout it carries —
    the legacy `<wav>.json` file, the new `<wav>.transcripts/` directory,
    or both. Returns the bytes reclaimed (WAV + sidecars).

    The destructive analog of `_move_sidecars_with_wav` above: both
    enumerate the SAME two sidecar layouts, so if a third layout is ever
    added, update both functions.

    `wav` is a Path discovered by globbing a `resolve_session_dir`-checked
    folder (or returned by `resolve_wav`), not a path BUILT from a parsed
    filename — so the `safe_name` round-trip rule (which guards path
    construction against `py/path-injection`) does not apply here; deleting
    an already-validated Path is safe."""
    freed = _safe_size(wav)
    legacy = wav.with_suffix(".json")
    if legacy.is_file():
        freed += _safe_size(legacy)
        legacy.unlink(missing_ok=True)
    transcripts = wav.with_suffix(".transcripts")
    if transcripts.is_dir():
        freed += _dir_size(transcripts)
        # Best-effort: a locked cache dir shouldn't block reclaiming the
        # WAV; an orphaned cache dir is harmless and re-cleanable.
        shutil.rmtree(transcripts, ignore_errors=True)
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
    meta = read_strip_meta(stripped)
    if meta is None:
        return
    files = meta["files"]
    changed = False
    for orig, entry in list(files.items()):
        spans = entry.get("spans") if isinstance(entry, dict) else None
        if not isinstance(spans, list):
            continue
        kept = [sp for sp in spans if not (isinstance(sp, dict) and sp.get("name") == clip_name)]
        if len(kept) == len(spans):
            continue
        changed = True
        if kept:
            entry["spans"] = kept
        else:
            del files[orig]
    if changed:
        atomic_write_text(stripped / FILENAME_STRIP_META_JSON, json.dumps(meta, indent=2))


def absorb_session(target: str, source: str) -> dict[str, Any]:
    """Move every WAV (and its `<name>.json` sidecar) from `source` into
    `target`, fold the source's `session-meta.json` aliases into the
    target's (target wins on key conflict), invalidate the target's merged
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
        src_strip_meta = read_strip_meta(src_stripped_dir)
        if src_strip_meta is not None:
            tgt_strip_meta = read_strip_meta(tgt_stripped_dir)
            if tgt_strip_meta is not None:
                tgt_strip_meta["files"] = {**src_strip_meta["files"], **tgt_strip_meta["files"]}
            atomic_write_text(
                tgt_stripped_dir / FILENAME_STRIP_META_JSON,
                json.dumps(tgt_strip_meta or src_strip_meta, indent=2),
            )

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

    # The target's merged transcript predates the just-moved WAVs, so it's
    # now stale. Drop it so the operator's next "transcribe whole session"
    # rebuilds against the fuller WAV set.
    tgt_transcript = target_dir / FILENAME_TRANSCRIPT_JSON
    transcript_invalidated = tgt_transcript.exists()
    if transcript_invalidated:
        try:
            tgt_transcript.unlink()
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
        "transcript_invalidated": transcript_invalidated,
        "summary_invalidated": summary_invalidated,
    }


# ---------------------------------------------------------------------------
# Audio deletion (operator-triggered, used by the delete endpoints)
# ---------------------------------------------------------------------------


def delete_session_audio(session: str) -> dict[str, Any]:
    """Delete ALL of a session's audio to reclaim disk: every original WAV
    in `<session>/`, the entire `<session>/stripped/` folder, and each
    WAV's per-WAV transcript-cache sidecars. KEEPS the merged
    `session-transcript.json` / `.txt` and `session-meta.json`, so the
    session survives `prune-empty` and the operator's result + label are
    preserved.

    Route-level guards (current-session refusal, in-flight-job refusal)
    live in the handler; this is purely the filesystem op. Returns
    `{session, wavs_deleted, bytes_freed}` — `wavs_deleted` counts the
    originals (matching the dashboard's "N wavs" header); `stripped/`
    bytes still fold into `bytes_freed`."""
    session_dir = resolve_session_dir(session)
    wavs_deleted = 0
    bytes_freed = 0
    for w in sorted(session_dir.glob("*.wav")):
        bytes_freed += _delete_wav_with_sidecars(w)
        wavs_deleted += 1
    stripped = session_dir / DIRNAME_STRIPPED
    if stripped.is_dir():
        bytes_freed += _dir_size(stripped)
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
