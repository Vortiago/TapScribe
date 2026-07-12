"""RED contract for #192 — cross-session transcript-content search.

The Sessions view can filter only by label / session id / date (client-side).
The highest-leverage missing read feature is answering "which meeting did we
discuss X in?" against the persisted merged transcripts on disk. This pins the
backend half: a Basic-auth `GET /api/search?q=<term>` that scans each session's
merged transcript and returns one hit per matching session —
`{session, label, snippet, count}`.

Pinned at the REAL endpoint (via TestClient), because the harm is end-to-end
(a helper that scans text is useless if it isn't wired into a reachable,
correctly-authenticated route). What this file pins:

  - CONTENT search, not the existing label/id filter: a session whose LABEL
    matches but whose TRANSCRIPT does not is NOT returned.
  - Round-trips the ordinary route tests would miss: case-insensitivity, the
    per-session occurrence `count`, and real snippet EXTRACTION (a snippet, not
    the whole transcript).
  - Every field the Sessions UI consumes — `session, label, snippet, count` —
    with its type, so a silent rename/removal fails this Python gate (the UI
    half is playwright-only and rides on the plan).
  - The degenerate empty/blank query returns no hits (never a full-corpus dump,
    never a 500).
  - The load-bearing AUTH DISTINCTION: `/api/search` is ordinary dashboard
    Basic auth, NOT a tap-bearer / auth-exempt path.

OUT OF THIS GATE (named in the plan-spec, verified by inspection / code-review):
the off-event-loop scan (`asyncio.to_thread`) and the stat-signature read cache,
and the Sessions-view search-box wiring (JS, no e2e in the scoped gate).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import (  # type: ignore[import-not-found]  # tests/ is on sys.path
    repoint_config_files,
    seed_merged_transcript,
)
from fastapi.testclient import TestClient

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.live import LiveConfig
from tapscribe.recorder import Recorder

# ---------------------------------------------------------------------------
# Fixtures — a plain Recorder wired into the real app (mirrors test_routes.py /
# the #193 contract): a no-auth `client` and an AUTH_ENABLED `auth_client`.
# ---------------------------------------------------------------------------


def _build_recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    cfg = tmp_path / "config"
    cfg.mkdir()
    repoint_config_files(monkeypatch, cfg)
    (tmp_path / "recordings").mkdir()
    return Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=tmp_path / "config",
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=8000),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )


@pytest.fixture
def recorder_under_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    return _build_recorder(tmp_path, monkeypatch)


@pytest.fixture
def client(recorder_under_test: Recorder) -> Iterator[TestClient]:
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def auth_recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    monkeypatch.setattr(_config, "AUTH_ENABLED", True)
    return _build_recorder(tmp_path, monkeypatch)


@pytest.fixture
def auth_client(auth_recorder: Recorder) -> Iterator[TestClient]:
    """The real app with AUTH_ENABLED, so the Basic-auth middleware runs."""
    app.dependency_overrides[get_recorder] = lambda: auth_recorder
    app.state.recorder = auth_recorder
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed(rec: Recorder, session: str, text: str, *, label: str | None = None) -> None:
    """Seed `session` with a merged transcript whose plain_text is `text`, and
    optionally a session-meta label."""
    seed_merged_transcript(rec.recordings_dir, session, plain_text=text)
    if label is not None:
        (rec.recordings_dir / session / "session-meta.json").write_text(
            json.dumps({"label": label}), encoding="utf-8"
        )


def _results(resp) -> list:
    """The hit list, tolerant of a bare-list or `{results: [...]}` envelope."""
    data = resp.json()
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    assert isinstance(data, list), f"expected a list of hits, got {type(data).__name__}"
    return data


def _sessions(resp) -> set[str]:
    return {hit["session"] for hit in _results(resp)}


# ---------------------------------------------------------------------------
# Behaviour — driven through the real GET /api/search route.
# ---------------------------------------------------------------------------


def test_search_finds_only_matching_sessions(client, recorder_under_test):
    _seed(recorder_under_test, "s-a", "we discussed the retention policy at length")
    _seed(recorder_under_test, "s-b", "retention came up again in this meeting")
    _seed(recorder_under_test, "s-c", "nothing relevant was said here")
    r = client.get("/api/search", params={"q": "retention"})
    assert r.status_code == 200, r.text
    assert _sessions(r) == {"s-a", "s-b"}


def test_search_is_case_insensitive(client, recorder_under_test):
    _seed(recorder_under_test, "s-ci", "The Retention Policy was finalized")
    r = client.get("/api/search", params={"q": "retention"})
    assert r.status_code == 200, r.text
    assert "s-ci" in _sessions(r)


def test_search_snippet_is_an_extract_containing_the_term(client, recorder_under_test):
    filler = "we talked about many unrelated things for a while. " * 60
    full = filler + "then the SECRETWORD came up and we moved on. " + filler
    _seed(recorder_under_test, "s-snip", full)
    r = client.get("/api/search", params={"q": "secretword"})
    assert r.status_code == 200, r.text
    hits = _results(r)
    assert len(hits) == 1
    snippet = hits[0]["snippet"]
    assert "secretword" in snippet.lower(), f"snippet must contain the match: {snippet!r}"
    assert len(snippet) < len(full), "snippet must be an EXTRACT, not the whole transcript"


def test_search_count_reflects_occurrences(client, recorder_under_test):
    # `count` = the number of times the query term appears in the transcript.
    _seed(recorder_under_test, "s-cnt", "retention. retention. retention. done.")
    r = client.get("/api/search", params={"q": "retention"})
    assert r.status_code == 200, r.text
    hits = [h for h in _results(r) if h["session"] == "s-cnt"]
    assert len(hits) == 1
    assert hits[0]["count"] == 3, f"expected 3 occurrences, got {hits[0]['count']}"


def test_search_surfaces_the_session_label(client, recorder_under_test):
    _seed(recorder_under_test, "s-lbl", "the retention topic was discussed", label="Q3 Planning")
    r = client.get("/api/search", params={"q": "retention"})
    assert r.status_code == 200, r.text
    hits = [h for h in _results(r) if h["session"] == "s-lbl"]
    assert len(hits) == 1
    assert hits[0]["label"] == "Q3 Planning"


def test_search_is_content_not_label(client, recorder_under_test):
    # The existing (client-side) filter already covers label/id/date. This
    # endpoint searches TRANSCRIPT CONTENT: a label-only match must NOT appear.
    _seed(recorder_under_test, "s-lblonly", "hello world, nothing else", label="Retention Sync")
    r = client.get("/api/search", params={"q": "retention"})
    assert r.status_code == 200, r.text
    assert "s-lblonly" not in _sessions(r)


def test_search_no_match_returns_empty(client, recorder_under_test):
    _seed(recorder_under_test, "s-x", "some ordinary meeting transcript")
    r = client.get("/api/search", params={"q": "zzznowaythisappears"})
    assert r.status_code == 200, r.text
    assert _results(r) == []


@pytest.mark.parametrize("blank", ["", "   "])
def test_search_blank_query_returns_no_hits(client, recorder_under_test, blank):
    # A degenerate query must not dump the whole corpus (or 500) — it matches
    # nothing, like the label/id/date filter on an empty box.
    _seed(recorder_under_test, "s-y", "retention retention retention everywhere")
    r = client.get("/api/search", params={"q": blank})
    assert r.status_code == 200, r.text
    assert _results(r) == []


def test_search_result_shape(client, recorder_under_test):
    _seed(recorder_under_test, "s-shape", "a retention discussion", label="Weekly Sync")
    r = client.get("/api/search", params={"q": "retention"})
    assert r.status_code == 200, r.text
    hits = _results(r)
    assert len(hits) == 1
    hit = hits[0]
    assert set(hit) >= {"session", "label", "snippet", "count"}, f"missing fields: {hit}"
    assert isinstance(hit["session"], str)
    assert isinstance(hit["label"], str)
    assert isinstance(hit["snippet"], str)
    assert isinstance(hit["count"], int)


def test_search_requires_basic_auth_not_tap_bearer(auth_client, auth_recorder):
    # Load-bearing: /api/search sits on the Basic-auth side of the middleware
    # seam, NOT tap-exempt. A build that clones the tap route (auth-exempt or
    # under /api/tap/...) would authenticate a bare tap Bearer here.
    _seed(auth_recorder, "s-auth", "a retention conversation")
    params = {"q": "retention"}
    assert auth_client.get("/api/search", params=params).status_code == 401
    bearer = {"Authorization": f"Bearer {_config.TAP_PREFIX}"}
    assert auth_client.get("/api/search", params=params, headers=bearer).status_code == 401
    ok = auth_client.get("/api/search", params=params, auth=(_config.AUTH_USER, auth_recorder.auth.value))
    assert ok.status_code == 200, ok.text
    assert "s-auth" in {h["session"] for h in _results(ok)}
