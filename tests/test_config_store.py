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
from collections.abc import Iterator
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
from tapscribe.recorder import Recorder
from tapscribe.transcribers.catalog import DEFAULT_BATCH_MODEL, REGISTRY

# A catalog id that is registered but LIVE-ONLY — the exact shape the old
# `REGISTRY.get(...) is not None` guard let through.
LIVE_ONLY_ID = "moonshine-tiny"


@pytest.fixture(autouse=True)
def config_dir(recorder_under_test: Recorder) -> Iterator[Path]:
    """The tmpdir config dir every test here writes into.

    Sourced from the shared `recorder_under_test` fixture (conftest), whose
    `repoint_config_files` re-binds CONFIG_DIR **and every `*_FILE` under it**
    by introspecting `tapscribe.config` — so a fifth config file is
    self-registering and can never silently start writing into the repo's real
    `config/`. A hand-written list here covered four of the nine and left the
    rest aimed at the working tree.

    The module-level text cache is cleared either side so no value leaks
    between tests.
    """
    config_store._CONFIG_TEXT_CACHE.clear()
    yield _config.CONFIG_DIR
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
def client(recorder_under_test: Recorder):
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
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


#: Every writer whose reader is backed by `_read_config_text_cached`. A new one
#: is added HERE and inherits the invariant below — that is the point of the
#: list. Each case is (seed the file, read → `before`, write, read → `after`);
#: the callables are evaluated inside the test so the fixtures' repointing of
#: `_config` has already landed.
_CACHED_WRITERS = [
    pytest.param(
        lambda: _config.PROMPT_FILE.write_text("first", encoding="utf-8"),
        lambda: read_config("prompt"),
        lambda: write_config("prompt", "second"),
        "first",
        "second",
        id="write_config",
    ),
    pytest.param(
        lambda: _config.SUMMARIZER_CONFIG_FILE.write_text(
            json.dumps({"source": "command", "command": "first"}), encoding="utf-8"
        ),
        lambda: read_summarizer_config()["command"],
        lambda: write_summarizer_config({"source": "command", "command": "second"}),
        "first",
        "second",
        id="write_summarizer_config",
    ),
    pytest.param(
        lambda: _config.LANGUAGES_FILE.write_text("da,no", encoding="utf-8"),
        lambda: read_languages(),
        lambda: write_languages("en"),
        ("da", "no"),
        ("en",),
        id="write_languages",
    ),
]


@pytest.mark.parametrize(("seed", "read", "write", "before", "after"), _CACHED_WRITERS)
def test_a_write_is_never_served_stale(frozen_stat_sig, seed, read, write, before, after):
    """The INVARIANT, deliberately not the mechanism: whatever performs the
    invalidation — each writer popping the key today, `atomic_write_text` owning
    it tomorrow — a value written must never be served from the cache that the
    read before it populated."""
    seed()
    assert read() == before  # populates the cache
    write()
    assert read() == after
