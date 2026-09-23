"""A merge-on-write never writes over a file it could not read.

Every site here reads a JSON file, merges a partial update into what it read,
and writes the whole file back. When the read swallows every `OSError`, "I
could not read this file" looks the same as "there is nothing here": the merge
starts from `{}` and the write replaces the file with the partial update. One
transient read failure (EACCES, EIO, EMFILE: anything short of the file being
absent) then silently discards the rest of the file.

The rule each site keeps:

  * A read that FAILS (any `OSError` except the file being absent) makes the
    write path RAISE, and the file on disk stays byte-identical.
  * A file that is absent, or torn (not valid JSON), still reads as empty, so
    the write proceeds. That is recovery, not loss.
  * The READ paths that feed the poll stay lenient: an unreadable file reads as
    empty there and never raises, so a bad file cannot crash a tick.

The fault is injected at `open` itself (`builtins.open` and `io.open`, which
`Path.read_text` uses), keyed on the one file's real path, so the rule holds
whatever read primitive a site uses. One rung also uses a real `chmod 000`.
"""

from __future__ import annotations

import builtins
import errno
import io
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from wav_builders import seed_session  # type: ignore[import-not-found]

from tapscribe import roster, session_maintenance, voices
from tapscribe.app import app, get_recorder
from tapscribe.session_paths import FILENAME_META_JSON, FILENAME_ROSTER_JSON
from tapscribe.sessions import read_session_meta, write_session_meta
from tapscribe.tap_mode import TAP_MODE_MULTI

# One of each read failure the lenient reads used to swallow. FileNotFoundError
# is deliberately absent: an absent file IS "nothing here".
READ_FAILURES = [
    pytest.param(PermissionError(errno.EACCES, "Permission denied"), id="EACCES"),
    pytest.param(OSError(errno.EIO, "Input/output error"), id="EIO"),
    pytest.param(OSError(errno.EMFILE, "Too many open files"), id="EMFILE"),
]

ALICE_WAV = "2026-01-01T00-00-00Z_Alice_Andersen_sc-alice-_aaaaaaaa.wav"
BOB_WAV = "2026-01-01T01-00-00Z_Bob_Bergman_sc-bob-us_bbbbbbbb.wav"
ALICE_IDENTITY = "sc-alice-9f8e7d6c5b4a3210"
BOB_IDENTITY = "sc-bob-user-0011223344556677"
T0 = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)


@contextmanager
def failing_reads(target: Path, exc: OSError, times: int | None = None) -> Iterator[None]:
    """Make every read-mode `open` of `target` raise `exc` (only the first
    `times` of them, when given). Writes, and every other file, pass through."""
    real_open = builtins.open
    target_real = os.path.realpath(target)
    left = [times]

    def fake_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        is_read = not any(flag in mode for flag in "wax+")
        is_target = isinstance(file, (str, bytes, os.PathLike)) and os.path.realpath(file) == target_real
        if is_read and is_target and (left[0] is None or left[0] > 0):
            if left[0] is not None:
                left[0] -= 1
            raise exc
        return real_open(file, mode, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(builtins, "open", fake_open)
        mp.setattr(io, "open", fake_open)
        yield


@pytest.fixture
def rec_root(recorder_under_test) -> Path:
    """`recorder_under_test` (tests/conftest.py) points `config.RECORDINGS_DIR`
    at a tmpdir, which the session resolvers read."""
    return Path(recorder_under_test.recordings_dir)


# ---------------------------------------------------------------------------
# roster.record_occurrence
# ---------------------------------------------------------------------------


def _seed_roster(rec_root: Path) -> Path:
    session_dir = seed_session(rec_root, "s", [ALICE_WAV])
    roster.record_occurrence(
        session_dir,
        identity=ALICE_IDENTITY,
        name="Alice Andersen",
        recorded=True,
        wav=ALICE_WAV,
        mode=TAP_MODE_MULTI,
    )
    return session_dir


@pytest.mark.parametrize("exc", READ_FAILURES)
def test_record_occurrence_refuses_to_write_over_an_unreadable_roster(rec_root: Path, exc: OSError) -> None:
    session_dir = _seed_roster(rec_root)
    path = session_dir / FILENAME_ROSTER_JSON
    before = path.read_bytes()

    with failing_reads(path, exc), pytest.raises(OSError):
        roster.record_occurrence(
            session_dir, identity=BOB_IDENTITY, name="Bob Bergman", recorded=True, wav=BOB_WAV
        )

    assert path.read_bytes() == before, "a failed read must not become an overwrite of every other identity"


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs a non-root POSIX user for chmod to bite"
)
def test_record_occurrence_refuses_a_roster_it_has_no_permission_to_read(rec_root: Path) -> None:
    session_dir = _seed_roster(rec_root)
    path = session_dir / FILENAME_ROSTER_JSON
    before = path.read_bytes()
    path.chmod(0o000)
    try:
        with pytest.raises(OSError):
            roster.record_occurrence(
                session_dir, identity=BOB_IDENTITY, name="Bob Bergman", recorded=True, wav=BOB_WAV
            )
    finally:
        path.chmod(0o644)
    assert path.read_bytes() == before


@pytest.mark.parametrize("exc", READ_FAILURES)
def test_read_roster_stays_lenient_on_an_unreadable_roster(rec_root: Path, exc: OSError) -> None:
    session_dir = _seed_roster(rec_root)

    with failing_reads(session_dir / FILENAME_ROSTER_JSON, exc):
        assert roster.read_roster(session_dir) == {}


def test_record_occurrence_over_a_torn_roster_still_records(rec_root: Path) -> None:
    session_dir = seed_session(rec_root, "s", [BOB_WAV])
    (session_dir / FILENAME_ROSTER_JSON).write_text("{not json", encoding="utf-8")

    roster.record_occurrence(
        session_dir, identity=BOB_IDENTITY, name="Bob Bergman", recorded=True, wav=BOB_WAV
    )

    assert set(roster.read_roster(session_dir)) == {BOB_IDENTITY}


# ---------------------------------------------------------------------------
# sessions.write_session_meta
# ---------------------------------------------------------------------------


def _seed_meta(rec_root: Path) -> Path:
    session_dir = seed_session(rec_root, "s", [ALICE_WAV])
    write_session_meta(
        "s", {"label": "Weekly sync", "aliases": {"Alice_Andersen": "Alice"}, "hotwords": "TapScribe"}
    )
    return session_dir / FILENAME_META_JSON


@pytest.mark.parametrize("exc", READ_FAILURES)
def test_write_session_meta_refuses_to_write_over_an_unreadable_meta(rec_root: Path, exc: OSError) -> None:
    path = _seed_meta(rec_root)
    before = path.read_bytes()

    with failing_reads(path, exc), pytest.raises(OSError):
        write_session_meta("s", {"prompt": "Discuss the roadmap"})

    assert path.read_bytes() == before, "a failed read must not become an overwrite of the label and aliases"


@pytest.mark.parametrize("exc", READ_FAILURES)
def test_read_session_meta_stays_lenient_on_an_unreadable_meta(rec_root: Path, exc: OSError) -> None:
    path = _seed_meta(rec_root)

    with failing_reads(path, exc):
        assert read_session_meta("s") == {}


def test_write_session_meta_over_a_torn_meta_still_writes(rec_root: Path) -> None:
    session_dir = seed_session(rec_root, "s", [])
    (session_dir / FILENAME_META_JSON).write_text("{not json", encoding="utf-8")

    write_session_meta("s", {"label": "Recovered"})

    assert read_session_meta("s")["label"] == "Recovered"


# ---------------------------------------------------------------------------
# PUT /api/sessions/{session}/voices: its OWN read of the voices map
# ---------------------------------------------------------------------------


@pytest.fixture
def client(recorder_under_test):
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def diarized(recorder_under_test) -> Path:
    """One multi-person tap with two Voices, as a finished diarization leaves it."""
    session_dir = seed_session(
        recorder_under_test.recordings_dir,
        "s",
        [f"{T0.strftime('%Y-%m-%dT%H-%M-%SZ')}_them_sysaudio_0000abcd.wav"],
    )
    (session_dir / FILENAME_ROSTER_JSON).write_text(
        json.dumps(
            {
                "sysaudio": {
                    "name": "Them",
                    "source": "recorded",
                    "slug": "them",
                    "wavs": [],
                    "mode": TAP_MODE_MULTI,
                }
            }
        ),
        encoding="utf-8",
    )
    voices.record_voices(
        session_dir,
        identity="sysaudio",
        run_id="run-1",
        spans={
            "A": [(T0, T0 + timedelta(seconds=30))],
            "B": [(T0 + timedelta(seconds=30), T0 + timedelta(seconds=45))],
        },
    )
    return session_dir


@pytest.mark.parametrize("exc", READ_FAILURES)
def test_mapping_a_voice_over_a_transiently_unreadable_meta_keeps_the_other_mappings(
    client: TestClient, diarized: Path, exc: OSError
) -> None:
    """The route reads the voices map, edits one key, and writes the map back.
    A failure on ITS read alone (the meta is readable again a moment later)
    must not write back a map holding only the new key."""
    assert client.put("/api/sessions/s/voices", json={"key": "sysaudio#A", "name": "Dana"}).status_code == 200
    path = diarized / FILENAME_META_JSON
    before = path.read_bytes()

    with failing_reads(path, exc, times=1):
        client.put("/api/sessions/s/voices", json={"key": "sysaudio#B", "name": "Robin"})

    assert "sysaudio#A" in (read_session_meta("s").get("voices") or {}), (
        "Voice A's mapping to Dana was discarded"
    )
    after = path.read_bytes()
    assert after == before or "sysaudio#B" in json.loads(after).get("voices", {}), (
        "the meta must either stay as it was or carry BOTH mappings"
    )


# ---------------------------------------------------------------------------
# session_maintenance.absorb_session: the target's and the source's reads
# ---------------------------------------------------------------------------


def _seed_absorb(rec_root: Path) -> tuple[Path, Path]:
    target = seed_session(rec_root, "tgt", [ALICE_WAV])
    source = seed_session(rec_root, "src", [BOB_WAV])
    roster.record_occurrence(
        target, identity=ALICE_IDENTITY, name="Alice Andersen", recorded=True, wav=ALICE_WAV
    )
    roster.record_occurrence(source, identity=BOB_IDENTITY, name="Bob Bergman", recorded=True, wav=BOB_WAV)
    write_session_meta("tgt", {"label": "Weekly sync", "aliases": {"Alice_Andersen": "Alice"}})
    write_session_meta("src", {"aliases": {"Bob_Bergman": "Bob"}})
    return target, source


@pytest.mark.parametrize("exc", READ_FAILURES)
@pytest.mark.parametrize("side", ["tgt", "src"])
@pytest.mark.parametrize("filename", [FILENAME_ROSTER_JSON, FILENAME_META_JSON])
def test_absorb_refuses_to_fold_a_file_it_could_not_read(
    rec_root: Path, exc: OSError, side: str, filename: str
) -> None:
    """Absorb folds both sessions' roster and meta into the target and then
    deletes the source folder. A read failure on either side must stop it
    before the unread file is overwritten or the source is deleted."""
    target, source = _seed_absorb(rec_root)
    path = (target if side == "tgt" else source) / filename
    before = path.read_bytes()

    with failing_reads(path, exc), pytest.raises(OSError):
        session_maintenance.absorb_session("tgt", "src")

    assert source.is_dir(), "the source folder was deleted over a file absorb could not read"
    assert path.read_bytes() == before, f"the {side} {filename} was overwritten after a failed read"
