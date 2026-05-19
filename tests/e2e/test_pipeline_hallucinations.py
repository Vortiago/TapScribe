"""E2E tests for hallucination filtering and many-speaker session merge.

Two concerns the existing E2E suite doesn't cover:

  - Hallucination rules in `config/hallucinations.txt` actually apply
    end-to-end (FakeTranscriber emits a hallucination line, the merged
    session result lists it under `suppressed` with `matched_rule` set,
    and the line is NOT in `plain_text`). This pins the
    `TranscriptionResult.suppressed_hallucinations` contract from
    CONTEXT.md across the whole pipeline rather than just at the
    decoder boundary.

  - Many-speaker sessions merge chronologically without silently
    dropping anyone. The two-speaker happy path can't catch a bug that
    only surfaces with 5+ identities interleaved.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tapscribe import config as _config
from tapscribe import transcribers as _transcribers

from .conftest import RunningRecorder
from .fake_transcriber import FakeTranscriber
from .harness import (
    stream_wav_via_tap,
    streams_drained,
    synth_speech_like_wav,
    wait_until,
)

# ---------------------------------------------------------------------------
# Test 6: hallucination filter end-to-end
# ---------------------------------------------------------------------------


HALLUCINATION_LINE = "Thanks for watching!"


@pytest.fixture
def hallucination_rule_installed(
    running_recorder: RunningRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """Write a single `exact:` rule into the recorder's config_dir and
    repoint `_config.HALLUCINATIONS_FILE` at it.

    The fixture's monkeypatch of CONFIG_DIR in `running_recorder` runs at
    Recorder construction time only; the module-level
    `HALLUCINATIONS_FILE` constant was bound at import. We need to
    rebind it for the test's lifetime so `hallucinations.parse_rules()`
    reads our file rather than the repo's committed defaults.
    """
    rec = running_recorder.recorder
    rule = f"exact:{HALLUCINATION_LINE}"
    halluc_path = rec.config_dir / "hallucinations.txt"
    halluc_path.write_text(rule + "\n", encoding="utf-8")
    monkeypatch.setattr(_config, "HALLUCINATIONS_FILE", halluc_path)
    return rule


@pytest.fixture
def hallucinating_fake_transcriber(monkeypatch: pytest.MonkeyPatch) -> FakeTranscriber:
    """Like the headline E2E test's fake transcriber, but the scripted
    text for Alice includes the hallucination phrase. The filter must
    move it into `suppressed`."""
    fake = FakeTranscriber(
        text_by_speaker={
            "Alice": HALLUCINATION_LINE,
            "Bob": "real transcription content from Bob",
        }
    )

    def _factory(model_name: str, *, use_mlx: bool) -> FakeTranscriber:  # noqa: ARG001
        return fake

    monkeypatch.setattr(_transcribers, "load_transcriber", _factory)
    import tapscribe.app as _app

    monkeypatch.setattr(_app, "load_transcriber", _factory)
    _transcribers.clear_cache()
    return fake


async def test_hallucination_filter_moves_match_to_suppressed(
    running_recorder: RunningRecorder,
    hallucination_rule_installed: str,
    hallucinating_fake_transcriber: FakeTranscriber,  # noqa: ARG001
    tmp_path: Path,
):
    """End-to-end: write a rules file, have the transcriber return a
    matching line, run /api/transcribe-session, and verify the line
    appears in the merged transcript's `suppressed` list with
    `matched_rule` set per CONTEXT.md TranscriptionResult section.

    The line MUST NOT appear in `plain_text`, and the un-suppressed
    speaker's text must still come through normally — the filter is a
    surgical drop, not a session-level kill switch.
    """
    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url

    alice_wav = synth_speech_like_wav(tmp_path / "alice.wav", seconds=0.5, freq_hz=220.0)
    bob_wav = synth_speech_like_wav(tmp_path / "bob.wav", seconds=0.5, freq_hz=440.0)

    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="alice",
        name="Alice",
        wav_path=alice_wav,
        utterance_id="utt-alice-halluc",
    )
    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="bob",
        name="Bob",
        wav_path=bob_wav,
        utterance_id="utt-bob-real",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=3.0)

    async with httpx.AsyncClient(base_url=base, timeout=15.0) as client:
        resp = await client.post(
            "/api/transcribe-session",
            json={"session": rec.session_start, "model": "fake-small.en"},
        )
        assert resp.status_code == 200, resp.text
        merged = resp.json()

    # The merged transcript surfaces hallucination-filtered segments
    # under `suppressed` (renamed from the per-WAV
    # `suppressed_hallucinations` key — see session_merge.SessionTranscript).
    suppressed = merged.get("suppressed", [])
    assert any(s.get("text", "").strip() == HALLUCINATION_LINE for s in suppressed), (
        f"hallucination line not in suppressed list: {suppressed}"
    )
    # `matched_rule` must be annotated per CONTEXT.md TranscriptionResult
    # section: "After hallucinations.apply runs, suppressed_hallucinations
    # holds the dropped segments with their matched_rule annotated."
    halluc_entry = next(s for s in suppressed if s.get("text", "").strip() == HALLUCINATION_LINE)
    assert halluc_entry.get("matched_rule") == hallucination_rule_installed, (
        f"matched_rule mismatch — got {halluc_entry.get('matched_rule')!r}, "
        f"expected {hallucination_rule_installed!r}"
    )
    assert halluc_entry.get("speaker") == "Alice"
    assert merged.get("suppressed_count", 0) >= 1

    # The line must NOT appear in plain_text.
    plain_text = merged["plain_text"]
    assert HALLUCINATION_LINE not in plain_text, f"hallucination line leaked into plain_text:\n{plain_text}"

    # Bob's real content must still come through — the filter is
    # surgical, not a session-level kill switch.
    assert "real transcription content from Bob" in plain_text


# ---------------------------------------------------------------------------
# Test 7: large session merge — 5+ speakers, no silent drops
# ---------------------------------------------------------------------------


SPEAKERS = [
    ("alice", "Alice", "Alice says her piece."),
    ("bob", "Bob", "Bob chimes in with context."),
    ("carol", "Carol", "Carol asks a clarifying question."),
    ("dave", "Dave", "Dave answers with a concrete example."),
    ("eve", "Eve", "Eve summarises the decision."),
]


@pytest.fixture
def many_speaker_transcriber(monkeypatch: pytest.MonkeyPatch) -> FakeTranscriber:
    fake = FakeTranscriber(text_by_speaker={name: text for _, name, text in SPEAKERS})

    def _factory(model_name: str, *, use_mlx: bool) -> FakeTranscriber:  # noqa: ARG001
        return fake

    monkeypatch.setattr(_transcribers, "load_transcriber", _factory)
    import tapscribe.app as _app

    monkeypatch.setattr(_app, "load_transcriber", _factory)
    _transcribers.clear_cache()
    return fake


async def test_large_session_merge_preserves_all_speakers_in_order(
    running_recorder: RunningRecorder,
    many_speaker_transcriber: FakeTranscriber,  # noqa: ARG001
    tmp_path: Path,
):
    """5 speakers, each with 1-2 short utterances. The merged transcript
    must include contributions from every speaker, the speaker order
    in plain_text must be chronological (matching wav_start times), and
    no speaker is silently dropped (e.g. by a sort key colliding or a
    speaker_name extraction edge case)."""
    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url

    # Build a per-speaker WAV up front. Use a distinct frequency per
    # speaker so the synthesised audio differs cosmetically.
    wavs = {}
    for idx, (identity, name, _) in enumerate(SPEAKERS):
        wav_path = synth_speech_like_wav(
            tmp_path / f"{identity}.wav",
            seconds=0.5,
            freq_hz=200.0 + idx * 40.0,
        )
        wavs[identity] = (name, wav_path)

    # Stream order: round 1 in declaration order, then round 2 for the
    # first three speakers (so Alice / Bob / Carol have two WAVs each
    # while Dave / Eve have one). Stream sequentially — the WAV
    # filename's timestamp is what drives merge order, and the per-file
    # timestamp resolution is one second, so we want predictable
    # ordering without flake from concurrent opens landing in the same
    # second.
    streamed_order: list[str] = []  # display names in the order they were streamed
    for identity, (name, wav_path) in wavs.items():
        await stream_wav_via_tap(
            ws_base_url=ws_base,
            identity=identity,
            name=name,
            wav_path=wav_path,
            utterance_id=f"utt-{identity}-1",
        )
        streamed_order.append(name)
        # Wait for finalisation between streams so each WAV gets a
        # distinct start timestamp; without this, multiple WAVs can
        # share a second and merge order becomes filename-tiebreaker-
        # dependent.
        assert await wait_until(lambda: streams_drained(rec), timeout=3.0)

    # Round 2 for the first three. Different utterance_id → fresh WAV
    # (one utterance = one WAV invariant).
    for identity, name, _ in SPEAKERS[:3]:
        wav_path = wavs[identity][1]
        await stream_wav_via_tap(
            ws_base_url=ws_base,
            identity=identity,
            name=name,
            wav_path=wav_path,
            utterance_id=f"utt-{identity}-2",
        )
        streamed_order.append(name)
        assert await wait_until(lambda: streams_drained(rec), timeout=3.0)

    # Eight WAVs total (5 + 3).
    on_disk = sorted(rec.session_dir.glob("*.wav"))
    assert len(on_disk) == 8, f"expected 8 WAVs from 5 speakers + 3 back-to-back, got {len(on_disk)}"

    async with httpx.AsyncClient(base_url=base, timeout=20.0) as client:
        resp = await client.post(
            "/api/transcribe-session",
            json={"session": rec.session_start, "model": "fake-small.en"},
        )
        assert resp.status_code == 200, resp.text
        merged = resp.json()

    # All 5 speakers must surface — no silent drops.
    expected_speakers = {name for _, name, _ in SPEAKERS}
    assert set(merged["speakers"]) == expected_speakers, (
        f"speaker set mismatch: got {merged['speakers']}, expected {sorted(expected_speakers)}"
    )

    # Every speaker has at least one segment.
    segments_by_speaker: dict[str, list[dict]] = {name: [] for _, name, _ in SPEAKERS}
    for seg in merged["segments"]:
        if seg["speaker"] in segments_by_speaker:
            segments_by_speaker[seg["speaker"]].append(seg)
    for name in segments_by_speaker:
        assert segments_by_speaker[name], f"speaker {name!r} has zero segments in merged transcript"

    # Round-1 speakers each have 2 segments; round-2-only speakers have 1.
    round1_only = {name for _, name, _ in SPEAKERS[3:]}
    round2_speakers = {name for _, name, _ in SPEAKERS[:3]}
    for name in round1_only:
        assert len(segments_by_speaker[name]) == 1
    for name in round2_speakers:
        assert len(segments_by_speaker[name]) == 2

    # Chronological ordering: segments are sorted by abs_start (rendered
    # as the HH:MM:SS prefix on each plain_text line). Per CONTEXT.md
    # this is the merge's sort key, so a regression that re-orders by
    # speaker or filename would surface here. We can't pin an exact
    # streamed order — the filename timestamp resolution is one second
    # so back-to-back WAVs share a wav_start and the same-second tie-
    # break is filename-alphabetical, not stream-order. The load-bearing
    # property is non-decreasing abs_start.
    timestamps = [
        line.split("]", 1)[0].lstrip("[")
        for line in merged["plain_text"].splitlines()
        if line.startswith("[")
    ]
    assert timestamps == sorted(timestamps), (
        f"plain_text not chronologically ordered by abs_start:\n{merged['plain_text']}"
    )

    # streamed_order is referenced to lock the test's intent — eight
    # WAVs, five distinct speakers, in this particular interleave.
    assert len(streamed_order) == 8
    assert set(streamed_order) == {name for _, name, _ in SPEAKERS}
