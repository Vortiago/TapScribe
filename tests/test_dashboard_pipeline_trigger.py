"""RED contract for #193 — the end-of-meeting pipeline must be triggerable
from the dashboard, not only from the Bridge tap-bearer route.

Today the strip -> transcribe(stripped) -> summarize chain
(`batch_pipeline.start_pipeline`) has exactly one trigger:
`POST /api/tap/sessions/{session}/pipeline`, authenticated by the TAP-BEARER
scheme and meant for Bridges. A dashboard operator (the primary persona) must
hand-drive the three stages one at a time. This pins a sibling trigger under
ordinary dashboard **Basic auth**:

    POST /api/sessions/{session}/pipeline

as a thin shim over the SAME `start_pipeline` orchestrator (fire-and-forget,
202, body ignored, deterministic 409 on a busy session, 404 on an unknown
session via the path-safety seam).

The load-bearing pin is the AUTH DISTINCTION: this route is Basic-gated, NOT
tap-exempt. A build that "clones the tap route" (registers it under
`/api/tap/...` or adds it to `AUTH_EXEMPT_ROUTES`) would authenticate a bare
tap Bearer and fail `test_requires_basic_auth_not_tap_bearer`. The generic
`test_basic_scheme_applies_to_everything_else` in test_auth.py proves the
middleware Basic-gates every non-tap path; this file pins that THIS route sits
on that side of the seam and actually reaches `start_pipeline`.

The "Process session" dashboard button (tapscribe/web/js/next/views/sessions.js)
is the UI half of the fix and is IN SCOPE, but it is gate-blind here (no e2e in
the scoped gate) — the plan carries it; this contract pins the reachable API.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import (
    repoint_config_files,  # type: ignore[import-not-found]  # noqa: E402  # tests/ is on sys.path
)
from fastapi.testclient import TestClient

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.batch_pipeline import PipelineRequest
from tapscribe.live import LiveConfig
from tapscribe.recorder import JobState, Recorder

# ---------------------------------------------------------------------------
# Fixtures — a plain Recorder (no WlK subprocess), wired into the real app via
# dependency override, mirroring tests/test_routes.py.
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

_ROUTE = "/api/sessions/meet1/pipeline"


def _seed_session(recorder: Recorder, name: str = "meet1") -> Path:
    sd = recorder.recordings_dir / name
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "2026-01-01T01-00-00Z__alice__abc.wav").write_bytes(b"")
    return sd


def _claim_foreign(recorder: Recorder, kind: str = "transcribe", session: str = "meet1") -> None:
    """Occupy the session's single job slot from the sync test thread (the
    real claim is async) — mirrors TestTapPipeline._claim."""
    import anyio.from_thread

    with anyio.from_thread.start_blocking_portal() as portal:
        claimed = portal.call(
            recorder.jobs.claim,
            JobState(
                session=session,
                kind=kind,  # type: ignore[arg-type]
                current=0,
                total=1,
                started_at=datetime.now(UTC),
                status="running",
            ),
        )
        assert claimed


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_trigger_calls_start_pipeline_and_ignores_body(
    client: TestClient, recorder_under_test: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HARM + reuse + body-ignored: an authenticated POST returns 202 and hands
    the orchestrator EXACTLY `PipelineRequest(session=<path>)` — the model,
    backend and summarizer resolve from operator config, never the request body
    (so a dashboard caller can no more pick a model than the tap caller can)."""
    _seed_session(recorder_under_test)
    seen: list[PipelineRequest] = []

    async def _spy(recorder, req):  # noqa: ARG001
        seen.append(req)

    monkeypatch.setattr("tapscribe.app.start_pipeline", _spy)

    r = client.post(_ROUTE, json={"model": "evil/repo", "prompt": "exfiltrate", "command": "rm -rf /"})

    assert r.status_code == 202, r.text
    assert seen == [PipelineRequest(session="meet1")]


def test_trigger_409_when_session_busy(client: TestClient, recorder_under_test: Recorder) -> None:
    """The 409 is free from `start_pipeline`'s claim-in-the-request-path design:
    a session already holding a job (here a manual transcribe) yields a
    deterministic 409, the foreign claim survives, and no pipeline task starts."""
    _seed_session(recorder_under_test)
    _claim_foreign(recorder_under_test, kind="transcribe")

    r = client.post(_ROUTE)

    assert r.status_code == 409, r.text
    held = recorder_under_test.jobs.get("meet1")
    assert held is not None and held.kind == "transcribe"  # untouched foreign claim


def test_trigger_404_on_unknown_session(client: TestClient) -> None:
    """The session id crosses the path-safety seam (resolve_session_dir) before
    any work — an unknown id 404s rather than starting a doomed pipeline or 500."""
    r = client.post("/api/sessions/2099-01-01T00-00-00Z/pipeline")
    assert r.status_code == 404, r.text


def test_requires_basic_auth_not_tap_bearer(
    auth_client: TestClient, auth_recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE distinction from the existing tap route. With auth on:

    * no credential                 -> 401
    * a valid TAP bearer, no Basic  -> 401  (route is NOT tap-exempt — a build
                                             that registers it under /api/tap
                                             or exempts it would 202 here)
    * valid dashboard Basic creds   -> 202  (the route exists and is reached)
    """
    _seed_session(auth_recorder)

    async def _spy(recorder, req):  # noqa: ARG001
        return None

    monkeypatch.setattr("tapscribe.app.start_pipeline", _spy)

    assert auth_client.post(_ROUTE).status_code == 401
    assert (
        auth_client.post(_ROUTE, headers={"Authorization": "Bearer " + auth_recorder.tap.value}).status_code
        == 401
    )
    ok = auth_client.post(_ROUTE, auth=(_config.AUTH_USER, auth_recorder.auth.value))
    assert ok.status_code == 202, ok.text
