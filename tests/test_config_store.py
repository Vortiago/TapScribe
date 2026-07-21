"""Config-store write-time guards + cache invalidation.

Two behaviours that have no home in the older suites:

- the "batch-model" WRITE guard must reject a catalog id that exists but is
  LIVE-ONLY (`contexts={"live"}`), because the end-of-meeting pipeline
  resolves that value with no operator in the loop and a live-only id dies
  at the transcribe stage with a raw `NotImplementedError`;
- every writer backed by `_read_config_text_cached` must invalidate that
  cache structurally — not by relying on the filesystem's stat signature
  moving, which is exactly the case the invalidation exists to cover.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tapscribe import config as _config
from tapscribe import config_store
from tapscribe.app import app, get_recorder
from tapscribe.batch_transcribe import resolve_batch_model
from tapscribe.config_store import (
    read_config,
    read_languages,
    read_summarizer_config,
    write_config,
    write_languages,
    write_summarizer_config,
)
from tapscribe.live import LiveConfig
from tapscribe.recorder import Recorder
from tapscribe.transcribers.catalog import DEFAULT_BATCH_MODEL, REGISTRY

# A catalog id that is registered but LIVE-ONLY — the exact shape the old
# `REGISTRY.get(...) is not None` guard let through.
LIVE_ONLY_ID = "moonshine-tiny"


@pytest.fixture(autouse=True)
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    monkeypatch.setattr(_config, "CONFIG_DIR", d)
    monkeypatch.setattr(_config, "BATCH_MODEL_FILE", d / "batch-model.txt")
    monkeypatch.setattr(_config, "PROMPT_FILE", d / "prompt.txt")
    monkeypatch.setattr(_config, "SUMMARIZER_CONFIG_FILE", d / "summarizer.json")
    monkeypatch.setattr(_config, "LANGUAGES_FILE", d / "languages.txt")
    config_store._CONFIG_TEXT_CACHE.clear()
    yield d
    config_store._CONFIG_TEXT_CACHE.clear()


def test_the_live_only_id_is_really_in_the_catalog_and_really_live_only():
    """Guards the premise of the two tests below: if the catalog ever gives
    Moonshine a batch adapter, they'd pass for the wrong reason."""
    entry = REGISTRY.get(LIVE_ONLY_ID)
    assert entry is not None
    assert entry.supports_context("live")
    assert not entry.supports_context("batch")


# ---------------------------------------------------------------------------
# batch-model: the WRITE guard
# ---------------------------------------------------------------------------


def test_write_config_rejects_a_live_only_batch_model():
    with pytest.raises(ValueError, match="batch"):
        write_config("batch-model", LIVE_ONLY_ID)
    # Nothing landed on disk — the guard runs before the write.
    assert read_config("batch-model") == ""


def test_write_config_accepts_a_batch_capable_model():
    write_config("batch-model", DEFAULT_BATCH_MODEL)
    assert read_config("batch-model") == DEFAULT_BATCH_MODEL


def test_resolve_batch_model_ignores_an_out_of_band_live_only_id(config_dir: Path):
    """Read-time mirror: a batch-model.txt hand-edited (or written by an older
    build) to a live-only id must not be handed to the pipeline's loader."""
    (config_dir / "batch-model.txt").write_text(LIVE_ONLY_ID + "\n", encoding="utf-8")
    assert resolve_batch_model(warn=False) == DEFAULT_BATCH_MODEL


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path):
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    (tmp_path / "recordings").mkdir()
    recorder = Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=config_dir,
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=8000),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )
    app.dependency_overrides[get_recorder] = lambda: recorder
    app.state.recorder = recorder
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_put_batch_model_with_a_live_only_id_is_a_400(client):
    r = client.put("/api/config/batch-model", json={"content": LIVE_ONLY_ID})
    assert r.status_code == 400
    assert LIVE_ONLY_ID in r.json()["detail"]
    assert read_config("batch-model") == ""


# ---------------------------------------------------------------------------
# Structural cache invalidation — all three writers
# ---------------------------------------------------------------------------


@pytest.fixture
def frozen_stat_sig(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin `file_stat_sig` to a constant so the cache can NEVER notice a change
    on its own. This is the coarse-mtime filesystem the invalidation exists
    for: without the structural pop, a write-then-read serves the stale value.
    """
    monkeypatch.setattr(config_store, "file_stat_sig", lambda path, **kw: ("frozen",))


def test_write_config_is_never_served_stale(frozen_stat_sig):
    (_config.PROMPT_FILE).write_text("first", encoding="utf-8")
    assert read_config("prompt") == "first"  # populates the cache
    write_config("prompt", "second")
    assert read_config("prompt") == "second"


def test_write_summarizer_config_is_never_served_stale(frozen_stat_sig):
    _config.SUMMARIZER_CONFIG_FILE.write_text(
        json.dumps({"source": "command", "command": "first"}), encoding="utf-8"
    )
    assert read_summarizer_config()["command"] == "first"  # populates the cache
    write_summarizer_config({"source": "command", "command": "second"})
    assert read_summarizer_config()["command"] == "second"


def test_write_languages_is_never_served_stale(frozen_stat_sig):
    _config.LANGUAGES_FILE.write_text("da,no", encoding="utf-8")
    assert read_languages() == ("da", "no")  # populates the cache
    write_languages("en")
    assert read_languages() == ("en",)
