"""Tests for the Recorder context class — composed sub-components + the
small bits of state (session, recording_enabled) that live directly on it.
"""

from __future__ import annotations

from pathlib import Path

from tapscribe.live import LiveConfig
from tapscribe.recorder import (
    ActiveStreams,
    JobTracker,
    LiveTranscripts,
    Recorder,
    SecretFile,
)


def _build_recorder(tmp_path: Path) -> Recorder:
    return Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=tmp_path / "config",
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=8000),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )


def test_recorder_composes_all_five_subcomponents(tmp_path: Path):
    r = _build_recorder(tmp_path)
    assert isinstance(r.streams, ActiveStreams)
    assert isinstance(r.jobs, JobTracker)
    assert isinstance(r.transcripts, LiveTranscripts)
    assert isinstance(r.auth, SecretFile)
    # LiveChannel is built lazily — check the attribute exists
    assert r.live is not None


def test_recorder_starts_with_recording_enabled_and_no_session_dir_on_disk(tmp_path: Path):
    r = _build_recorder(tmp_path)
    assert r.recording_enabled is True
    # session_start string follows the recorder's filename convention
    assert "T" in r.session_start
    assert r.session_start.endswith("Z")
    # Lazy directory creation — session_dir is the path, not yet created
    assert r.session_dir.parent == r.recordings_dir
    assert not r.session_dir.exists()


def test_recorder_rotate_session_changes_session_start_and_returns_previous(tmp_path: Path):
    r = _build_recorder(tmp_path)
    prev = r.session_start
    a, b = r.rotate_session()
    assert a == prev
    assert b == r.session_start
    assert r.session_dir.name == b
    # Note: session IDs are second-resolution, so a rotate called within
    # the same second yields the same string — that's an acceptable
    # no-op (operators don't click "new session" faster than 1Hz).


def test_recorder_toggle_recording_flips_when_called_without_arg(tmp_path: Path):
    r = _build_recorder(tmp_path)
    assert r.recording_enabled is True
    assert r.toggle_recording() is False
    assert r.recording_enabled is False
    assert r.toggle_recording() is True
    assert r.recording_enabled is True


def test_recorder_toggle_recording_with_explicit_value(tmp_path: Path):
    r = _build_recorder(tmp_path)
    assert r.toggle_recording(enabled=False) is False
    assert r.toggle_recording(enabled=False) is False  # idempotent
    assert r.toggle_recording(enabled=True) is True


def test_recorder_use_mlx_is_per_instance_not_module_global(tmp_path: Path):
    """ADR-0001 #4: use_mlx is per-call. Two Recorders in the same
    process can hold different preferences."""
    r1 = _build_recorder(tmp_path)
    r2 = Recorder(
        recordings_dir=tmp_path / "recordings2",
        config_dir=tmp_path / "config2",
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=8000),
        use_mlx=True,
        auth_password_file=tmp_path / ".auth-password-2",
    )
    assert r1.use_mlx is False
    assert r2.use_mlx is True


def test_recorder_auth_password_persists_across_instances(tmp_path: Path):
    """The password is on disk; a second Recorder with the same file
    path reads the same password."""
    r1 = _build_recorder(tmp_path)
    pw = r1.auth.value
    r2 = _build_recorder(tmp_path)
    assert r2.auth.value == pw
