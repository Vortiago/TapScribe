"""RED contract for #216 — memoise PeopleRegistry.load on people.json's stat.

`/api/state` rebuilds the People view every tick, and `PeopleRegistry.load()`
re-reads + re-parses + re-coerces people.json each call (attach_people on the
poll path, plus the rename/merge/detach routes). The fix memoises the load on
people.json's file-stat signature so an unchanged file is read from disk once.

The SUBTLE part — and the whole reason this needs a contract — is that `load()`
returns a registry callers MUTATE then `save()` (load → sync/rename/merge →
save), and #216 also moves the read/join onto a worker thread. So a naive
"cache the PeopleRegistry instance" is wrong: it would share one mutable object
across callers (and across the event loop + worker thread), leaking an un-saved
mutation from one caller into another and corrupting the cache. A correct
memoisation caches the parsed snapshot and hands each `load()` an INDEPENDENT
registry.

What this file pins (all at the load() boundary, so it holds for any correct
cache — snapshot-keyed-on-stat, copy-on-load, etc.):

  1. CACHE HIT (the perf win, RED at base): two loads of an unchanged file read
     people.json from disk ONCE.
  2. INDEPENDENCE (guardrail): a mutation on one load()'s result must not appear
     in another load()'s result — the anti-"cache the instance" pin. GREEN at
     base (every load re-reads), RED only if the cache shares a mutable instance.
  3. INVALIDATION on external change (guardrail): after people.json changes on
     disk, load() reflects it — a stale cache is forbidden.
  4. INVALIDATION on save (guardrail): after an in-process save(), the next
     load() sees it (all writers save(), which rewrites the file).

OUT OF THIS GATE (named in the plan-spec, verified by code-review): moving the
pure joins (session_occurrences / resolve_session_names / build_people_view) and
the jsonable_encoder + json.dumps + blake2b ETag hash off the event loop into
the existing state_view.build_state_blob worker thread. That is a loop-residency (perf)
change with no in-process behavioural assertion; the memoisation above is the
correctness-bearing half the gate can see.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tapscribe import config
from tapscribe.people import PEOPLE_JSON, PeopleRegistry


@pytest.fixture
def recordings_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """people.json lives at RECORDINGS_DIR/people.json; point it at a tmpdir
    (mirrors tests/test_people.py). Each test seeds the file it needs."""
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path)
    return tmp_path


def _write_people(root: Path, people: list[dict]) -> None:
    """Write people.json in the exact shape PeopleRegistry.save emits."""
    (root / PEOPLE_JSON).write_text(json.dumps({"people": people}), encoding="utf-8")


def _count_people_reads(monkeypatch: pytest.MonkeyPatch):
    """Count disk reads of people.json (any other file's reads are untouched)."""
    calls = {"n": 0}
    orig = Path.read_text

    def counting(self: Path, *args, **kwargs):
        if self.name == PEOPLE_JSON:
            calls["n"] += 1
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)
    return lambda: calls["n"]


def test_load_reads_disk_once_when_file_unchanged(
    recordings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_people(recordings_dir, [{"id": "p1", "name": "Alice", "identities": ["a"]}])
    reads = _count_people_reads(monkeypatch)
    PeopleRegistry.load()
    PeopleRegistry.load()
    assert reads() == 1, (
        "an unchanged people.json must be read from disk once, then served from the memoised snapshot"
    )


def test_load_returns_independent_registries(recordings_dir: Path) -> None:
    # The anti-"cache the mutable instance" pin: load → mutate (no save) → load
    # again; the second registry must NOT carry the first's un-saved mutation.
    _write_people(recordings_dir, [{"id": "p1", "name": "Alice", "identities": ["a"]}])
    r1 = PeopleRegistry.load()
    r1.sync(["ghost-identity"])  # mutates r1 in memory; deliberately NOT saved
    r2 = PeopleRegistry.load()
    assert r2.person_for_identity("ghost-identity") is None, (
        "a memoised load must hand out independent registries — one caller's un-saved "
        "mutation must never leak into another load()'s result (or the worker thread's)"
    )


def test_load_refreshes_when_file_changes_on_disk(recordings_dir: Path) -> None:
    # A stale cache is forbidden: an external rewrite (different content AND size)
    # must be reflected on the next load.
    _write_people(recordings_dir, [{"id": "p1", "name": "Alice", "identities": ["a"]}])
    assert PeopleRegistry.load().name_for_identity("a") == "Alice"
    _write_people(
        recordings_dir,
        [
            {"id": "p1", "name": "Bob", "identities": ["a"]},
            {"id": "p2", "name": "Carol", "identities": ["c"]},
        ],
    )
    r2 = PeopleRegistry.load()
    assert r2.name_for_identity("a") == "Bob", (
        "load() must reflect an external people.json change, not serve a stale cache"
    )
    assert r2.name_for_identity("c") == "Carol"


def test_save_then_load_sees_the_write(recordings_dir: Path) -> None:
    # All writers go through save(), which rewrites people.json — so the memoised
    # load must invalidate and see it.
    _write_people(recordings_dir, [{"id": "p1", "name": "Alice", "identities": ["a"]}])
    r = PeopleRegistry.load()
    r.rename("p1", "Renamed")
    r.sync(["new-identity"])
    r.save()
    reloaded = PeopleRegistry.load()
    assert reloaded.name_for_identity("a") == "Renamed", (
        "a load after an in-process save() must see the saved rename"
    )
    assert reloaded.person_for_identity("new-identity") is not None, (
        "a load after save() must see the newly-bound identity"
    )
