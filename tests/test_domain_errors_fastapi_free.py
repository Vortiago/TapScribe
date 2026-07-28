"""RED contract for #228 — the session domain layer must be FastAPI-free.

The batch orchestrators establish the convention that the domain layer raises
plain domain errors and a single handler in `app.py` maps each to its HTTP
status (`DOMAIN_ERROR_STATUS`). But the path/validation seam every orchestrator
crosses still raises `fastapi.HTTPException` directly — in `session_paths`,
`sessions`, `session_maintenance`, and (as a catch-site) `session_merge`. So a
non-HTTP caller (a CLI / queue worker — the stated reason for the FastAPI-free
contract) gets HTTP exceptions from the domain layer anyway.

Behaviour OVER HTTP is UNCHANGED by this migration: the existing route tests
(`test_routes.py`, `test_sessions_path_safety.py`, `test_tap_endpoint.py`,
`test_session_paths_symlink_containment.py`) still pin the 404/400/409 statuses
and the /tap WS upgrade-refusal, and must stay green — the build UPDATES those
that assert `HTTPException` directly to assert the new domain type instead.

What THIS file pins is what those boundary tests structurally cannot see:

  1. COMPLETENESS — every domain module is FastAPI-free. An AST scan of each
     module's source: no `HTTPException` referenced in code (import / raise /
     except). This forces EVERY site to migrate, including the two easily-missed
     `except HTTPException` catch-sites (`sessions.known_names_for_session`,
     `session_merge.select_session_wavs`). Docstrings/comments are ast.Constant
     nodes, not Name/Attribute/alias, so prose that merely says the word passes.

  2. CORRECTNESS — the migration is real, not cosmetic. A direct call raises a
     DOMAIN error that is NOT `HTTPException` (nor a subclass of it: a
     `class SessionNotFound(HTTPException)` would defeat the whole point and is
     rejected by the `not isinstance` check), with the taxonomy name the issue
     names. The AST scan (1) cannot catch a subclass defined in another module.

  3. CATCH-SITE BEHAVIOUR — `known_names_for_session` degrades a vanished
     session to `[]` via its `except`; after the migration that `except` must
     catch the domain type, else it propagates. Pinned behaviourally.

Registration in `DOMAIN_ERROR_STATUS` (the third failure mode) is caught by the
existing route tests kept in this slice's scoped gate: an unregistered domain
error falls to the handler's 500, flipping their 404/400/409 to 500.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import HTTPException

from tapscribe import live_control, session_maintenance, session_merge, session_paths, sessions

# ---------------------------------------------------------------------------
# 1. Completeness — every domain module is FastAPI-free.
# ---------------------------------------------------------------------------

_DOMAIN_MODULES = [session_paths, sessions, session_maintenance, session_merge, live_control]


def _references_httpexception(module) -> bool:
    """True iff the module's SOURCE names `HTTPException` in CODE (an import
    alias, a raise, or an except) — docstrings/comments are `ast.Constant`
    nodes, never `Name`/`Attribute`/`alias`, so prose that says the word is
    ignored on purpose."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "HTTPException":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "HTTPException":
            return True
        if isinstance(node, ast.alias) and node.name == "HTTPException":
            return True
    return False


@pytest.mark.parametrize("module", _DOMAIN_MODULES, ids=lambda m: m.__name__)
def test_domain_module_is_fastapi_free(module):
    assert not _references_httpexception(module), (
        f"{module.__name__} still references fastapi.HTTPException in code — the "
        f"domain layer must be FastAPI-free (#228): raise a domain error and map it "
        f"in routes.errors.DOMAIN_ERROR_STATUS. This also forces the two `except HTTPException` "
        f"catch-sites (sessions.known_names_for_session, "
        f"session_merge.select_session_wavs) onto the domain type."
    )


# ---------------------------------------------------------------------------
# 2. Correctness — a direct call raises a real domain error, not HTTPException.
# ---------------------------------------------------------------------------


@pytest.fixture
def rec_root(recorder_under_test) -> Path:
    """`recorder_under_test` monkeypatches `config.RECORDINGS_DIR` to a tmpdir
    (and disables auth/live); expose the recordings root so direct-call tests
    can seed session folders the resolvers will find."""
    return Path(recorder_under_test.recordings_dir)


def _raises_domain_error(fn, *args):
    """Call `fn(*args)`; assert it raised a DOMAIN error and return it.

    A domain error is any exception that is NOT `fastapi.HTTPException` (nor a
    subclass — the `except HTTPException` arm fires first, so a cosmetic
    `class SessionNotFound(HTTPException)` migration fails here). Not raising at
    all is also a failure."""
    label = getattr(fn, "__name__", repr(fn))
    try:
        fn(*args)
    except HTTPException as exc:
        pytest.fail(
            f"{label} still raises fastapi HTTPException (status {exc.status_code}) — "
            f"the domain layer must be FastAPI-free so a CLI/queue caller receives a "
            f"domain error, not an HTTP one (#228)."
        )
    except Exception as exc:
        return exc
    pytest.fail(f"{label} did not raise — expected a domain error")
    return None  # Unreachable: pytest.fail raises, kept explicit for static analysis.


def test_resolve_session_dir_raises_session_not_found(rec_root):  # noqa: ARG001 - fixture seeds config
    exc = _raises_domain_error(session_paths.resolve_session_dir, "no-such-session")
    assert type(exc).__name__ == "SessionNotFound", f"got {type(exc).__name__}"


def test_resolve_source_dir_unknown_source(rec_root):
    session_paths.create_session_dir("s-src")
    exc = _raises_domain_error(session_paths.resolve_source_dir, "s-src", "bogus")
    assert type(exc).__name__ == "UnknownSource", f"got {type(exc).__name__}"


def test_resolve_source_dir_stripped_missing(rec_root):
    session_paths.create_session_dir("s-strip")
    exc = _raises_domain_error(session_paths.resolve_source_dir, "s-strip", "stripped")
    assert type(exc).__name__ == "StrippedMissing", f"got {type(exc).__name__}"


def test_resolve_wav_missing(rec_root):
    session_paths.create_session_dir("s-wav")
    exc = _raises_domain_error(session_paths.resolve_wav, "s-wav", "nope.wav")
    assert type(exc).__name__ == "WavNotFound", f"got {type(exc).__name__}"


def test_write_session_meta_bad_language(rec_root):  # noqa: ARG001
    exc = _raises_domain_error(sessions.write_session_meta, "s-lang", {"languages": ["xx"]})
    assert type(exc).__name__ == "MetaValidationError", f"got {type(exc).__name__}"


def test_write_session_meta_bad_summary_source(rec_root):  # noqa: ARG001
    # A DISTINCT raise site inside write_session_meta than the bad-language one —
    # both must collapse to the same MetaValidationError type.
    exc = _raises_domain_error(sessions.write_session_meta, "s-summ", {"summary_source": "telepathy"})
    assert type(exc).__name__ == "MetaValidationError", f"got {type(exc).__name__}"


def test_absorb_collision_raises_absorb_collision(rec_root):
    for name in ("absorb-tgt", "absorb-src"):
        session_paths.create_session_dir(name)
        (rec_root / name / "clip.wav").write_bytes(b"RIFF0000WAVE")
    exc = _raises_domain_error(session_maintenance.absorb_session, "absorb-tgt", "absorb-src")
    assert type(exc).__name__ == "AbsorbCollision", f"got {type(exc).__name__}"


def test_absorb_same_target_and_source_is_domain_error(rec_root):  # noqa: ARG001
    # The `target == source` guard fires before any resolve; pin only that it is
    # a FastAPI-free domain error (name left to the plan — MetaValidationError or
    # its own), so a CLI caller of absorb_session sees a domain error here too.
    _raises_domain_error(session_maintenance.absorb_session, "same-id", "same-id")


def test_delete_session_audio_rmtree_failure_is_domain_error(rec_root, monkeypatch):
    session_paths.create_session_dir("del-me")
    (rec_root / "del-me" / "stripped").mkdir()

    def _boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr("shutil.rmtree", _boom)
    # Name left to the plan; pin that the rmtree-failure path is a domain error,
    # not an HTTPException, so the "FastAPI-free" claim holds on the error branch.
    _raises_domain_error(session_maintenance.delete_session_audio, "del-me")


# ---------------------------------------------------------------------------
# 3. Catch-site behaviour — a vanished session degrades to no names, not a raise.
# ---------------------------------------------------------------------------


def test_known_names_for_session_vanished_returns_empty(rec_root):  # noqa: ARG001
    # `known_names_for_session` catches the "session vanished" error to degrade
    # to `[]`. After the migration its `except` must catch the domain type; if it
    # still catches only HTTPException, the domain error propagates instead.
    assert sessions.known_names_for_session("vanished-session") == []
