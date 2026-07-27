"""The WAV listing tells the dashboard which WAV a tap is writing right now.

Playback (#191) needs this: an **open WAV**'s RIFF/data-size header is only
patched when the tap closes (`TapFanOut._close`), so the bytes on disk declare
a length that isn't there yet and a browser decodes ~nothing. The dashboard
disables its play affordance for such a WAV, which it cannot do without being
told. Before this, `open_wavs` existed *only* to mask the size that feeds
`files_sig` (see `_files_signature`) and never reached the client.

See CONTEXT.md "Player · seek target · open WAV · playhead" and ADR-0017.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from wav_builders import seed_session  # type: ignore[import-not-found]

from tapscribe.text import build_recorder_wav_name

OPEN_NAME = build_recorder_wav_name(datetime(2026, 7, 26, 9, 30, 15, tzinfo=UTC), "alice", "aaaa1111")
CLOSED_NAME = build_recorder_wav_name(datetime(2026, 7, 26, 9, 31, 30, tzinfo=UTC), "bob", "bbbb2222")


def test_listing_marks_the_wav_a_tap_is_writing_as_open(tmp_path: Path) -> None:
    """`open` is per-WAV and true only for the names in `open_wavs`."""
    from tapscribe.sessions import build_session_files

    session_dir = seed_session(tmp_path, "session", [OPEN_NAME, CLOSED_NAME])

    wavs, _stripped = build_session_files(session_dir, open_wavs={OPEN_NAME})

    files = {f["name"]: f for f in wavs}
    assert files[OPEN_NAME]["open"] is True
    assert files[CLOSED_NAME]["open"] is False


def test_open_flag_defaults_false_and_does_not_stick_in_the_descriptor_cache(
    tmp_path: Path,
) -> None:
    """A tap closing must clear the flag on the very next walk.

    `_describe_wav` memoises descriptors on (mtime, size, sidecar sig) and an
    open WAV is the one file whose stats churn, so a descriptor stamped `open`
    and then cached would keep saying so after the tap closed — a play button
    disabled forever. The stamp therefore has to land on the per-walk copy,
    never on the cached dict.
    """
    from tapscribe.sessions import build_session_files

    session_dir = seed_session(tmp_path, "session", [OPEN_NAME, CLOSED_NAME])

    build_session_files(session_dir, open_wavs={OPEN_NAME})
    # Same on-disk state, no open taps: the previous walk must not have left
    # `open` behind on the cached descriptor.
    wavs, _stripped = build_session_files(session_dir)

    files = {f["name"]: f for f in wavs}
    assert files[OPEN_NAME]["open"] is False
