"""RED contract for #209 — the hallucination filter must be operator-editable
from the dashboard with WRITE-TIME validation, not a read-only rule list.

Today `hallucinations.txt` renders read-only in the config card while its
neighbours (prompt, hotwords) save through `PUT /api/config/{key}`. Adding a
rule means shelling into the recorder host. The infrastructure already exists:
`CONFIG_KEYS` (tapscribe/text.py) just lacks a `"hallucinations"` row, and the
`_ConfigSpec.check` hook is built for write-time validation.

This pins the BACKEND half — a new `"hallucinations"` config key whose write
check REUSES `hallucinations.py`'s existing ReDoS/compile validation and
REJECTS a bad rule at PUT time (400, naming the offending line) instead of
silently dropping it. The taxonomy is exhaustive on purpose:

  * valid rules (substr / exact: / safe re:)        -> 200, persisted
  * re: with a nested unbounded quantifier `(a+)+`  -> 400  (ReDoS SHAPE — it
                                                             COMPILES fine, so a
                                                             compile-only check
                                                             would wrongly accept
                                                             it; forces reuse of
                                                             `_regex_is_safe`)
  * re: over the 256-char length cap                -> 400  (length branch)
  * re: that does not compile                       -> 400  (re.error branch)
  * a reject leaves the on-disk file UNCHANGED (the check runs before the
    atomic write) and names the bad line in the error

and the load-bearing SEMANTIC distinction — write-time is STRICT, but the
runtime parser stays LENIENT: `parse_rules()` must still silently skip a bad
regex that somehow reaches the file (a legacy operator edit), never raise —
so the fix must not "unify" the two into a strict runtime that would wedge a
transcribe job on a pre-existing file.

The config-card editor swap (config-card.js read-only column -> buildEditor)
and exposing the raw file content to the card are the UI half — IN SCOPE, but
gate-blind here (the card render is playwright-only); the plan carries them.
This contract pins the reachable, sound backend surface.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import (
    repoint_config_files,  # type: ignore[import-not-found]  # noqa: E402  # tests/ is on sys.path
)
from fastapi.testclient import TestClient

from tapscribe import config as _config
from tapscribe import hallucinations
from tapscribe.app import app, get_recorder
from tapscribe.hallucinations import _MAX_REGEX_PATTERN_LEN
from tapscribe.live import LiveConfig
from tapscribe.recorder import Recorder

# ---------------------------------------------------------------------------
# Fixtures — a plain Recorder with config paths repointed to tmp, wired into
# the real app, mirroring tests/test_routes.py.
# ---------------------------------------------------------------------------


@pytest.fixture
def recorder_under_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
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
def client(recorder_under_test: Recorder) -> Iterator[TestClient]:
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _hal_file(recorder: Recorder) -> Path:
    return recorder.config_dir / "hallucinations.txt"


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_valid_rules_persist(client: TestClient, recorder_under_test: Recorder) -> None:
    """HARM: a valid ruleset — a plain substring, an `exact:` and a SAFE `re:` —
    saves through PUT /api/config/hallucinations and lands on disk verbatim, so
    an operator can add a rule from the dashboard instead of ssh-ing to the host."""
    content = "thank you for watching\nexact: please subscribe\nre: subtitles by .*\n"
    r = client.put("/api/config/hallucinations", json={"content": content})
    assert r.status_code == 200, r.text
    assert r.json()["key"] == "hallucinations"
    assert _hal_file(recorder_under_test).read_text(encoding="utf-8") == content


def test_rejects_redos_nested_quantifier_and_names_it(
    client: TestClient, recorder_under_test: Recorder
) -> None:
    """The distinguishing pin: `re: (a+)+` COMPILES fine but is the canonical
    catastrophic-backtracking shape `_regex_is_safe` rejects. A build that only
    does `try: re.compile` would wrongly accept it — so this forces reuse of the
    real ReDoS check. The PUT is 400, names the offending line, and (the atomic
    guarantee) leaves the previously-saved file UNCHANGED."""
    good = "thank you for watching\n"
    assert client.put("/api/config/hallucinations", json={"content": good}).status_code == 200

    r = client.put("/api/config/hallucinations", json={"content": good + "re: (a+)+\n"})
    assert r.status_code == 400, r.text
    assert "(a+)+" in r.json()["detail"]  # names the bad line, does not silently drop it
    assert _hal_file(recorder_under_test).read_text(encoding="utf-8") == good  # nothing landed


def test_rejects_overlong_regex(client: TestClient) -> None:
    """The length branch of `_regex_is_safe`: a `re:` pattern over the 256-char
    cap is rejected at write time (the whole file is still well under the config
    text cap, so this isolates the per-regex length rule)."""
    pattern = "a" * (_MAX_REGEX_PATTERN_LEN + 1)
    r = client.put("/api/config/hallucinations", json={"content": f"re: {pattern}\n"})
    assert r.status_code == 400, r.text


def test_rejects_uncompilable_regex_and_names_it(client: TestClient) -> None:
    """The compile-error branch: a `re:` pattern that fails `re.compile`
    (unbalanced `[`) is rejected — distinct from the ReDoS-shape branch — and the
    offending line is named rather than silently dropped."""
    r = client.put("/api/config/hallucinations", json={"content": "re: [unclosed\n"})
    assert r.status_code == 400, r.text
    assert "[unclosed" in r.json()["detail"]


def test_runtime_parse_stays_lenient(recorder_under_test: Recorder) -> None:
    """The load-bearing SEMANTIC distinction: write-time is strict, runtime is
    lenient. A bad regex that reaches the file by some other path (a legacy
    hand-edit) must still be SILENTLY SKIPPED by `parse_rules()` — never raise —
    so a transcribe job can't be wedged by a pre-existing file. The fix must not
    make the runtime parser strict."""
    _hal_file(recorder_under_test).write_text("good substring\nre: (a+)+\nexact: keep me\n", encoding="utf-8")
    hallucinations._RULES_CACHE.clear()

    rules = hallucinations.parse_rules()  # must not raise

    raws = {r["raw"] for r in rules}
    assert "good substring" in raws
    assert "exact: keep me" in raws
    assert "re: (a+)+" not in raws  # the risky rule is dropped at runtime, quietly
