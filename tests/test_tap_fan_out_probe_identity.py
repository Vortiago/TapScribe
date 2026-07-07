"""RED contract for #220 — a reserved `__probe__` tap must leave no durable
Recorder state (no roster occurrence → no auto-bound Person).

Both bridges verify the tap secret by opening a real `/tap` WS as
`identity=__probe__&name=probe` (control-client.js:278,
ConnectionTester.cs:68-69) and immediately closing it with zero audio. But
`TapFanOut._open` rosters every FULL identity — `do_record` defaults True, so
the guard `if self._do_record or session_dir.exists()` fires and
`roster.record_occurrence(...)` writes a `__probe__` entry into
`session-roster.json`. `name_resolution.attach_people` (the /api/state read
path) then auto-binds every roster identity into people.json, so a permanent
Person named 'probe' materialises in the global People Registry, and every
probed session's roster carries a `__probe__` occurrence. The SpatialChat
popup probes on EVERY popup open, so this happens routinely.

The durable pollution is the roster (→ Person); the probe's WAV is already
unlinked-when-empty (zero audio). This contract pins the roster in BOTH
directions — a probe records nothing, a real speaker still records — plus the
probe tap still OPENS (the fix must make it side-effect-free, not reject it:
the whole point of the probe is to prove the tap secret works).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tapscribe import roster
from tapscribe.live import LiveConfig
from tapscribe.name_resolution import session_occurrences
from tapscribe.recorder import Recorder
from tapscribe.tap_fan_out import TapFanOut
from tapscribe.text import parse_wav_speaker_slug

# The reserved identity both bridges send to verify the tap secret.
PROBE_IDENTITY = "__probe__"

# A 20 ms frame of audible PCM at 16 kHz mono int16 (320 samples / 640 bytes),
# matching the bridges' wire frame — see test_tap_fan_out.py.
PCM_FRAME = b"\x10\x00" * 320


@pytest.fixture
def recorder(tmp_path: Path) -> Recorder:
    """A Recorder with the live channel stopped (live._proc=None) so the
    fan-out's relay path is never attempted — same shape as the fixture in
    test_tap_fan_out.py."""
    recordings = tmp_path / "recordings"
    config_dir = tmp_path / "config"
    recordings.mkdir()
    config_dir.mkdir()
    return Recorder(
        recordings_dir=recordings,
        config_dir=config_dir,
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=9999),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )


async def test_probe_identity_leaves_no_roster_occurrence(recorder: Recorder):
    """A `__probe__` tap (opened then closed with zero audio, exactly as the
    bridges probe) must NOT write a roster occurrence — otherwise
    attach_people auto-binds a permanent 'probe' Person into the registry."""
    async with await TapFanOut.open(
        recorder,
        identity=PROBE_IDENTITY,
        name="probe",
        utterance_id="utt-probe",
        do_record=True,
        do_live=False,
    ):
        pass  # a real probe opens then closes with no audio

    assert PROBE_IDENTITY not in roster.read_roster(recorder.session_dir)


async def test_probe_identity_tap_still_opens_and_closes(recorder: Recorder):
    """The probe's PURPOSE is to prove the tap secret by opening a real /tap
    WS, so the fix must make `__probe__` side-effect-free, NOT reject it:
    opening and closing the fan-out for the probe identity must not raise."""
    async with await TapFanOut.open(
        recorder,
        identity=PROBE_IDENTITY,
        name="probe",
        utterance_id="utt-probe-open",
        do_record=True,
        do_live=False,
    ) as fan_out:
        assert fan_out is not None
    # Reaching here without an exception == the probe's auth-verification open
    # path still works end to end.


async def test_probe_identity_leaves_no_wav(recorder: Recorder):
    """A probe writes zero audio, so its WAV is unlinked-when-empty today; pin
    that the probe path leaves no WAV on disk regardless of how the skip is
    implemented (skip-the-open, or open-then-unlink)."""
    async with await TapFanOut.open(
        recorder,
        identity=PROBE_IDENTITY,
        name="probe",
        utterance_id="utt-probe-wav",
        do_record=True,
        do_live=False,
    ):
        pass

    assert list(recorder.session_dir.glob("*.wav")) == []


async def test_normal_identity_still_rosters_via_fan_out(recorder: Recorder):
    """Positive control: a real speaker's recording tap MUST still write its
    roster occurrence through the fan-out — the probe-skip has to key off the
    reserved identity, not disable rostering for everyone."""
    async with await TapFanOut.open(
        recorder,
        identity="alice_ident01",
        name="Alice",
        utterance_id="utt-real",
        do_record=True,
        do_live=False,
    ) as fan_out:
        await fan_out.write_frame(PCM_FRAME)

    r = roster.read_roster(recorder.session_dir)
    assert "alice_ident01" in r
    assert r["alice_ident01"]["name"] == "Alice"


async def test_record_off_tap_in_existing_session_still_rosters(recorder: Recorder):
    """Positive control for the OR-branch the probe skip guards: a record-OFF
    live tap whose session folder ALREADY exists must still roster its (real)
    identity. Pins that the skip stays `not is_probe and (do_record or dir
    exists)` and doesn't collapse to `not is_probe and do_record` — which would
    pass the other cases while silently dropping record-off rostering."""
    recorder.session_dir.mkdir(parents=True, exist_ok=True)
    async with await TapFanOut.open(
        recorder,
        identity="bob_ident02",
        name="Bob",
        utterance_id="utt-recordoff",
        do_record=False,
        do_live=False,
    ):
        pass

    assert "bob_ident02" in roster.read_roster(recorder.session_dir)


async def test_probe_lookalike_identity_still_rosters(recorder: Recorder):
    """Exact-match control: an identity that merely RESEMBLES the reserved token
    ('__probe__x') is a real user and MUST still roster — the skip keys off an
    EXACT `== PROBE_IDENTITY`, not a `startswith('__probe__')` / `'probe' in`
    match that would swallow real users like '__probe__x' or 'probeman'."""
    async with await TapFanOut.open(
        recorder,
        identity="__probe__x",
        name="Probey",
        utterance_id="utt-lookalike",
        do_record=True,
        do_live=False,
    ) as fan_out:
        await fan_out.write_frame(PCM_FRAME)

    assert "__probe__x" in roster.read_roster(recorder.session_dir)


async def test_probe_with_audio_leaves_no_wav_or_backfilled_occurrence(recorder: Recorder):
    """The ULTIMATE harm, decoupled from the zero-audio co-invariant: a
    MISBEHAVING probe that sends audio must still leave no WAV, so the
    recorded-slug backfill (name_resolution F1) has no speaker slug to turn into
    a 'probe' Person. With an open-then-unlink fix a non-empty WAV would survive
    and re-materialise the probe occurrence despite the roster skip."""
    async with await TapFanOut.open(
        recorder,
        identity=PROBE_IDENTITY,
        name="probe",
        utterance_id="utt-probe-audio",
        do_record=True,
        do_live=False,
    ) as fan_out:
        await fan_out.write_frame(PCM_FRAME)  # a probe that (mis)sends audio

    assert list(recorder.session_dir.glob("*.wav")) == []

    speakers = [s for s in (parse_wav_speaker_slug(p.name) for p in recorder.session_dir.glob("*.wav")) if s]
    occ = session_occurrences({"roster": roster.read_roster(recorder.session_dir), "speakers": speakers})
    assert not any("probe" in key.lower() for key in occ)


async def test_probe_identity_registers_no_active_stream(recorder: Recorder):
    """The WAV-independent People path: /api/state builds live_identities from
    the ActiveStreams snapshot and attach_people auto-binds AND persists a blank
    Person for every live identity. A probe must therefore register no
    ActiveStream, or a durable __probe__ Person materialises on the next poll
    with zero audio sent."""
    async with await TapFanOut.open(
        recorder,
        identity=PROBE_IDENTITY,
        name="probe",
        utterance_id="utt-probe-stream",
        do_record=True,
        do_live=False,
    ):
        live = {s.identity for s in await recorder.streams.snapshot()}

    assert PROBE_IDENTITY not in live
