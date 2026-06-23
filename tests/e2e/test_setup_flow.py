"""End-to-end install flow against the real server.

Drives ``POST /api/setup/install`` on a live uvicorn server (via the
``running_recorder`` fixture), reads the Server-Sent progress stream, and
verifies the installed model becomes available — both for first-run setup and
for adding a model afterwards.

The ONLY faked seam is the pip subprocess (``setup_install._create_subprocess``):
running real pip in CI is slow + networked and is covered separately by the
``install (...)`` job matrix. The fake streams a couple of log lines and, on
completion, extends the catalog's installed-modules test override so the
freshly-"installed" backend becomes importable — exactly the observable effect a
real install has. Everything else is real: the endpoint, ``run_install``, the
SSE wire, the post-install probe refresh, ``/api/setup/state``, ``/api/models``,
and the ``/`` first-run gate.
"""

from __future__ import annotations

import json

import httpx

from tapscribe.transcribers.catalog import REGISTRY, set_installed_modules_for_testing


def _probe_modules_for(family: str, kind: str) -> set[str]:
    """The probe modules a real install of (family, kind) would make importable."""
    return {
        b.probe_module
        for e in REGISTRY.entries()
        if e.family == family
        for b in e.backends
        if kind in b.kinds and b.probe_module
    }


def _installing_spawn(installed_before: set[str], family: str, kind: str):
    """Fake ``_create_subprocess``: stream two log lines, then (on wait) extend
    the installed-modules override with what installing (family, kind) would
    add — simulating pip making that backend importable."""
    installed_after = frozenset(installed_before | _probe_modules_for(family, kind))

    async def _stdout():
        for line in (b"resolving wheels\n", f"installed {family}\n".encode()):
            yield line

    class _Proc:
        def __init__(self) -> None:
            self.stdout = _stdout()
            self.returncode: int | None = None

        async def wait(self) -> int:
            set_installed_modules_for_testing(installed_after)
            self.returncode = 0
            return 0

    async def spawn(_argv):
        return _Proc()

    return spawn


async def _run_install(client: httpx.AsyncClient, families: dict[str, str]) -> list[dict]:
    """POST the install and collect the parsed SSE events to completion."""
    events: list[dict] = []
    async with client.stream("POST", "/api/setup/install", json={"families": families}) as resp:
        assert resp.status_code == 200, resp.status_code
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


def _family(state: dict, key: str) -> dict:
    return next(f for f in state["families"] if f["family"] == key)


async def test_first_run_install_makes_a_model_available(running_recorder, monkeypatch):
    """First run (nothing installed) → ``/`` redirects to ``/setup`` → install
    Whisper → it becomes installed + offered for batch jobs and ``/`` stops
    redirecting."""
    # Start from a clean first-run machine; never write the real state file.
    set_installed_modules_for_testing(frozenset())
    monkeypatch.setattr("tapscribe.setup_install.write_picker_state", lambda *a, **k: None)
    monkeypatch.setattr(
        "tapscribe.setup_install._create_subprocess",
        _installing_spawn(set(), "whisper", "cpu"),
    )

    async with httpx.AsyncClient(base_url=running_recorder.base_url, timeout=10.0) as client:
        # First-run: state says so and GET / redirects to /setup.
        st = (await client.get("/api/setup/state")).json()
        assert st["first_run"] is True
        assert _family(st, "whisper")["installed"] is False
        r = await client.get("/", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/setup"

        # Install Whisper; the stream begins with `start`, ends with `done`.
        events = await _run_install(client, {"whisper": "cpu"})
        assert events[0] == {"phase": "start"}
        assert events[-1] == {"phase": "done", "ok": True, "returncode": 0}

        # Now installed, no longer first-run, offered for batch, / serves the dashboard.
        st2 = (await client.get("/api/setup/state")).json()
        assert st2["first_run"] is False
        assert _family(st2, "whisper")["installed"] is True
        batch = (await client.get("/api/models", params={"context": "batch"})).json()
        assert any(m["family"] == "whisper" for m in batch["models"])
        assert (await client.get("/", follow_redirects=False)).status_code == 200


async def test_adding_a_model_after_first_run_makes_it_available(running_recorder, monkeypatch):
    """Already set up with Whisper → add Parakeet from the manage-models surface
    → Parakeet becomes available for batch jobs without disturbing Whisper."""
    whisper_mods = _probe_modules_for("whisper", "cpu")
    set_installed_modules_for_testing(frozenset(whisper_mods))
    monkeypatch.setattr("tapscribe.setup_install.write_picker_state", lambda *a, **k: None)
    monkeypatch.setattr(
        "tapscribe.setup_install._create_subprocess",
        _installing_spawn(set(whisper_mods), "parakeet", "cpu"),
    )

    async with httpx.AsyncClient(base_url=running_recorder.base_url, timeout=10.0) as client:
        # Not first-run; Parakeet not yet installed; / serves the dashboard.
        st = (await client.get("/api/setup/state")).json()
        assert st["first_run"] is False
        assert _family(st, "whisper")["installed"] is True
        assert _family(st, "parakeet")["installed"] is False
        assert (await client.get("/", follow_redirects=False)).status_code == 200

        # Add Parakeet.
        events = await _run_install(client, {"parakeet": "cpu"})
        assert events[-1] == {"phase": "done", "ok": True, "returncode": 0}

        # Parakeet now installed + offered for batch; Whisper still installed.
        st2 = (await client.get("/api/setup/state")).json()
        assert _family(st2, "parakeet")["installed"] is True
        assert _family(st2, "whisper")["installed"] is True
        batch = {
            m["family"]
            for m in (await client.get("/api/models", params={"context": "batch"})).json()["models"]
        }
        assert {"whisper", "parakeet"} <= batch
