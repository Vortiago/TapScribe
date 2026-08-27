"""The speaker-embedding model is FETCHED at bring-up, not vendored.

30 MB of weights with a published sha256 is provenance a committed blob can't
match: the fetcher VERIFIES the digest where a checked-in binary is merely
trusted. The tests below are about that verification and about the probe
preflight plans off — a half-written file must read as absent, or the repair
never runs and the engine loads a truncated graph.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from tapscribe.diarizers import model as diarize_model

BODY = b"not really an onnx graph, but it hashes"
DIGEST = hashlib.sha256(BODY).hexdigest()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv(diarize_model.ENV_MODEL, raising=False)
    monkeypatch.setattr(diarize_model, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(diarize_model, "MODEL_SHA256", DIGEST)
    return tmp_path / "models"


def _opener(body: bytes):
    return lambda url: io.BytesIO(body)


def test_a_missing_model_is_absent(home: Path) -> None:
    assert diarize_model.model_present() is False


def test_a_truncated_download_is_absent(home: Path) -> None:
    """The failure this probe exists for: bytes on disk that aren't the model."""
    home.mkdir(parents=True)
    (home / diarize_model.MODEL_FILENAME).write_bytes(BODY[:10])

    assert diarize_model.model_present() is False


def test_a_matching_file_is_present(home: Path) -> None:
    home.mkdir(parents=True)
    (home / diarize_model.MODEL_FILENAME).write_bytes(BODY)

    assert diarize_model.model_present() is True


def test_the_operator_override_names_its_own_file(home: Path, tmp_path: Path, monkeypatch) -> None:
    """`TAPSCRIBE_DIARIZE_MODEL` is operator-controlled, like the summarizer's
    model env knobs — it points at a file they supplied, so the shipped digest
    does not apply to it."""
    elsewhere = tmp_path / "mine.onnx"
    elsewhere.write_bytes(b"some other export")
    monkeypatch.setenv(diarize_model.ENV_MODEL, str(elsewhere))

    assert diarize_model.model_path() == elsewhere
    assert diarize_model.model_present() is True


def test_fetch_writes_the_model(home: Path) -> None:
    path = diarize_model.fetch(open_url=_opener(BODY))

    assert path.read_bytes() == BODY
    assert diarize_model.model_present() is True


def test_fetch_refuses_a_body_with_the_wrong_digest(home: Path) -> None:
    """A CDN error page is 200 OK and a few KB of HTML. Without the check it
    lands as the model and onnxruntime raises something unrelated much later."""
    with pytest.raises(diarize_model.ModelFetchError, match="sha256"):
        diarize_model.fetch(open_url=_opener(b"<html>404</html>"))

    assert diarize_model.model_present() is False


def test_a_failed_fetch_leaves_nothing_behind(home: Path) -> None:
    """Including the part-file: preflight is non-fatal and re-runs every launch,
    so debris would accumulate one copy at a time."""
    with pytest.raises(diarize_model.ModelFetchError):
        diarize_model.fetch(open_url=_opener(b"<html>404</html>"))

    assert sorted(p.name for p in home.iterdir()) == []


def test_fetch_replaces_a_corrupt_file(home: Path) -> None:
    home.mkdir(parents=True)
    (home / diarize_model.MODEL_FILENAME).write_bytes(BODY[:5])

    diarize_model.fetch(open_url=_opener(BODY))

    assert diarize_model.model_present() is True


def test_main_reports_a_failed_fetch(home: Path, capsys: pytest.CaptureFixture) -> None:
    """preflight prints its own header; this exit code is what marks the step
    failed in the launch log."""
    rc = diarize_model.main(open_url=_opener(b"<html>404</html>"))

    assert rc != 0
    assert "sha256" in capsys.readouterr().err
