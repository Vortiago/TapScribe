"""RED contract for #369 — absorb transcriber-adapter residue into `base`.

Five small patterns still repeat across the transcriber adapters with no shared
home. This slice gives them one, and fixes the off-convention `voxtral` backend
label. The consolidation must be BEHAVIOUR-PRESERVING at every site, so most of
this file is a per-site regression lock.

The two shared helpers this contract pins live in `tapscribe.transcribers.base`
(named here deliberately — a fully-qualified helper name in the contract
measurably prevents a reinvented sibling, #241):

  * ``tapscribe.transcribers.base.resolve_repo``   — the ``repo_for(name, key)
    or <fallback>`` tail (7 copies today).
  * ``tapscribe.transcribers.base.resolve_language`` — the
    ``source_lang or self.fixed_language or default_language_for(...)``
    precedence rule (3 copies today, which must stay in lockstep).

`base` MUST stay a leaf module — it may not import `catalog` at module scope
(`default_language_for`'s docstring states the no-`base`↔`catalog`-cycle rule,
and `test_base_stays_a_leaf_module` pins it). Today's adapters keep the cycle
open with a function-local `from .catalog import repo_for`; whatever the shared
helper does must preserve that.

WHY THE ROUTING TESTS MONKEYPATCH THE HELPER: a consolidation is invisible to a
value-only assertion — a hand-rolled MIRROR copy of the rule in each adapter
returns the same values and passes (#215). Patching the shared helper and
asserting every call site's behaviour changes is the only pin that proves the
sites actually route through ONE implementation, and it is not gameable by a
copy. Each site gets its own case: under-pinning a sweep ships a PARTIAL sweep
(#175), so all 7 repo sites and all 3 language sites are pinned individually.
"""

from __future__ import annotations

import importlib.machinery
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tapscribe.transcribers import (
    base,
    mlx_parakeet,
    mlx_voxtral,
    mlx_whisper,
    moonshine_mlx,
    moonshine_onnx,
    parakeet,
    voxtral,
)
from tapscribe.transcribers import (
    faster_whisper as fw,
)

# --------------------------------------------------------------------------
# The repo-fallback tail: `repo_for(name, key) or <fallback>` — 7 sites.
#
# Each row is (label, callable(model_name) -> repo, catalog-backend-key,
# off-registry model name, its construct-by-convention fallback). The name is
# chosen to be absent from the registry so `repo_for` misses and the fallback
# is what comes back.
# --------------------------------------------------------------------------

REPO_SITES = [
    (
        "voxtral",
        lambda n: voxtral._resolve_repo(n),
        "voxtral-hf",
        "Off-Registry-Voxtral",
        voxtral._VOXTRAL_REPO,
    ),
    (
        "mlx_voxtral",
        lambda n: mlx_voxtral._resolve_repo(n),
        "voxtral-mlx",
        "Off-Registry-Voxtral",
        mlx_voxtral._MLX_VOXTRAL_REPO,
    ),
    (
        "mlx_parakeet",
        lambda n: mlx_parakeet._resolve_repo(n),
        "parakeet-mlx",
        "off-registry-parakeet",
        "mlx-community/off-registry-parakeet",
    ),
    (
        "parakeet",
        lambda n: parakeet._resolve_repo(n),
        "parakeet-hf",
        "off-registry-parakeet",
        "nvidia/off-registry-parakeet",
    ),
    (
        "mlx_whisper",
        lambda n: mlx_whisper.mlx_whisper_repo(n),
        "mlx-whisper",
        "off-registry-size",
        "mlx-community/whisper-off-registry-size-mlx",
    ),
]


@pytest.mark.parametrize("label,resolve,key,off_name,fallback", REPO_SITES, ids=[r[0] for r in REPO_SITES])
def test_repo_site_falls_back_to_its_own_convention_on_a_registry_miss(
    label: str, resolve: Any, key: str, off_name: str, fallback: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registry MISS → that site's construct-by-convention fallback.

    Each site's fallback is DIFFERENT (a fixed repo, `mlx-community/{name}`,
    `nvidia/{name}`, `mlx-community/whisper-{name}-mlx`), so a shared helper
    that hard-codes one shape breaks the others.
    """
    from tapscribe.transcribers import catalog

    monkeypatch.setattr(catalog, "repo_for", lambda name, backend: None)
    assert resolve(off_name) == fallback


@pytest.mark.parametrize("label,resolve,key,off_name,fallback", REPO_SITES, ids=[r[0] for r in REPO_SITES])
def test_repo_site_prefers_the_registry_hit_over_its_fallback(
    label: str, resolve: Any, key: str, off_name: str, fallback: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registry HIT wins (#206 single-source). Distinguishing: the sentinel is
    unequal to the fallback, so a resolver that ignored the registry and always
    constructed the convention repo fails here."""
    from tapscribe.transcribers import catalog

    seen: list[tuple[str, str]] = []

    def fake_repo_for(name: str, backend: str) -> str:
        seen.append((name, backend))
        return "sentinel/from-registry"

    monkeypatch.setattr(catalog, "repo_for", fake_repo_for)
    assert resolve(off_name) == "sentinel/from-registry"
    assert seen == [(off_name, key)], f"{label} must look up its own backend key"


@pytest.mark.parametrize("label,resolve,key,off_name,fallback", REPO_SITES, ids=[r[0] for r in REPO_SITES])
def test_repo_site_routes_through_the_shared_base_helper(
    label: str, resolve: Any, key: str, off_name: str, fallback: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE CONSOLIDATION PIN. Patching `base.resolve_repo` must change every
    site — a mirrored per-adapter copy would not pick this up and fails."""
    monkeypatch.setattr(base, "resolve_repo", lambda *a, **k: "patched/shared-helper", raising=False)
    assert resolve(off_name) == "patched/shared-helper", f"{label} does not route through base.resolve_repo"


def test_moonshine_onnx_registry_hit_beats_the_module_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """moonshine_onnx resolves inside `load()`. A registry hit for a model id
    that is NOT in `_UPSTREAM_MODEL_NAMES` must resolve (so we get past the
    ValueError) rather than raising 'not a known Moonshine model'."""
    from tapscribe.transcribers import catalog

    monkeypatch.setattr(catalog, "repo_for", lambda name, backend: "sentinel-upstream")
    captured: dict[str, Any] = {}

    class _FakeModel:
        def __init__(self, model_name: str) -> None:
            captured["model_name"] = model_name

    fake_mod = type(sys)("moonshine_onnx")
    # `load()` calls importlib.util.find_spec("moonshine_onnx") before importing,
    # and find_spec raises on a sys.modules entry whose __spec__ is None — so the
    # stand-in needs a real ModuleSpec, not just the attributes.
    fake_mod.__spec__ = importlib.machinery.ModuleSpec("moonshine_onnx", loader=None)
    fake_mod.MoonshineOnnxModel = _FakeModel  # type: ignore[attr-defined]
    fake_mod.load_tokenizer = lambda: object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "moonshine_onnx", fake_mod)

    moonshine_onnx.OnnxMoonshineEngine.load("not-in-the-mapping")
    assert captured["model_name"] == "sentinel-upstream"


def test_moonshine_onnx_unknown_id_with_no_registry_hit_still_raises_valueerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry MISS + not in the mapping → the actionable ValueError survives
    the consolidation (the guardrail, not just the happy path)."""
    from tapscribe.transcribers import catalog

    monkeypatch.setattr(catalog, "repo_for", lambda name, backend: None)
    with pytest.raises(ValueError, match="not a known Moonshine model"):
        moonshine_onnx.OnnxMoonshineEngine.load("not-in-the-mapping")


def test_moonshine_mlx_registry_hit_beats_the_module_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """moonshine_mlx resolves inside `load()` too. A registry hit for an id
    absent from `_MODEL_REPOS` must get PAST resolution — it then fails on the
    missing optional dependency, which is the proof resolution succeeded."""
    from tapscribe.transcribers import catalog

    monkeypatch.setattr(catalog, "repo_for", lambda name, backend: "sentinel/repo")
    monkeypatch.setattr(moonshine_mlx.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(RuntimeError, match="mlx-audio is not installed"):
        moonshine_mlx.MlxMoonshineEngine.load("not-in-the-mapping")


def test_moonshine_mlx_unknown_id_with_no_registry_hit_still_raises_valueerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tapscribe.transcribers import catalog

    monkeypatch.setattr(catalog, "repo_for", lambda name, backend: None)
    with pytest.raises(ValueError, match="not a known Moonshine model"):
        moonshine_mlx.MlxMoonshineEngine.load("not-in-the-mapping")


# --------------------------------------------------------------------------
# The language-precedence rule — 3 sites that must stay in lockstep.
#
# BRANCH TABLE, not one happy value (#215): the three inputs are chosen to
# DISAGREE, so a resolver that consults them in the wrong order, or drops one
# rung, produces a different answer and fails. `nb-` makes
# `default_language_for` return "no", which is distinct from both overrides.
# --------------------------------------------------------------------------

LANGUAGE_LADDER = [
    # (source_lang, fixed_language, model_name, expected)
    ("fr", "en", "nb-whisper-small", "fr"),  # explicit pin wins over everything
    (None, "en", "nb-whisper-small", "en"),  # registry fixed language is next
    (None, None, "nb-whisper-small", "no"),  # name heuristic is the floor
    (None, None, "tiny.en", "en"),  # ...and reads the .en suffix too
    (None, None, "whisper-large", None),  # nothing to go on → auto-detect
    ("", "en", "nb-whisper-small", "en"),  # "" is NOT a pin (falsy) — falls through
    ("fr", None, "whisper-large", "fr"),  # a pin with no other rung present
]


@pytest.mark.parametrize("source_lang,fixed,model_name,expected", LANGUAGE_LADDER)
def test_resolve_language_precedence_ladder(
    source_lang: str | None, fixed: str | None, model_name: str, expected: str | None
) -> None:
    assert base.resolve_language(source_lang, fixed, model_name) == expected


def _fw_adapter(monkeypatch: pytest.MonkeyPatch, *, fixed: str | None, model_name: str) -> Any:
    """A faster-whisper adapter whose model records the language it was given."""

    class _Model:
        def __init__(self) -> None:
            self.seen: dict[str, Any] = {}

        def transcribe(self, path: str, **kwargs: Any) -> tuple[list[Any], Any]:
            self.seen.update(kwargs)
            return [], type("Info", (), {"language": "xx", "language_probability": 0.0, "duration": 0.0})()

    return fw.FasterWhisperTranscriber(
        model_name=model_name, model=_Model(), device="cpu", fixed_language=fixed
    )


@pytest.mark.parametrize("source_lang,fixed,model_name,expected", LANGUAGE_LADDER)
def test_faster_whisper_applies_the_same_ladder(
    source_lang: str | None,
    fixed: str | None,
    model_name: str,
    expected: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Site 1 of 3 — the ladder must reach the real decode call, not just the
    helper."""
    monkeypatch.setattr(fw, "wav_duration_s", lambda p: 0.0, raising=False)
    adapter = _fw_adapter(monkeypatch, fixed=fixed, model_name=model_name)
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    adapter.transcribe(wav, source_lang=source_lang)
    assert adapter._model.seen["language"] == expected


@pytest.mark.parametrize("source_lang,fixed,model_name,expected", LANGUAGE_LADDER)
def test_mlx_whisper_applies_the_same_ladder(
    source_lang: str | None,
    fixed: str | None,
    model_name: str,
    expected: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Site 2 of 3."""
    seen: dict[str, Any] = {}

    def fake_transcribe(audio: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"segments": [], "language": "xx"}

    monkeypatch.setattr(mlx_whisper, "load_recorder_wav_as_pcm", lambda p: b"", raising=False)
    monkeypatch.setattr(mlx_whisper, "wav_duration_s", lambda p: 0.0, raising=False)
    adapter = mlx_whisper.MlxWhisperTranscriber(
        model_name=model_name, hf_repo="r", fixed_language=fixed, transcribe_fn=fake_transcribe
    )
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    adapter.transcribe(wav, source_lang=source_lang)
    assert seen["language"] == expected


@pytest.mark.parametrize("source_lang,fixed,model_name,expected", LANGUAGE_LADDER)
def test_voxtral_common_applies_the_same_ladder(
    source_lang: str | None,
    fixed: str | None,
    model_name: str,
    expected: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Site 3 of 3 — the shared Voxtral base. A falsy language must be OMITTED
    from the request (today's `if language:`), so the expectation splits."""
    from tapscribe.transcribers import _voxtral_common as vc

    seen: dict[str, Any] = {}

    class _Adapter(vc.VoxtralTranscriberBase):
        name = "voxtral"
        backend = "voxtral-hf"

        def __init__(self) -> None:
            self.model_name = model_name
            self.device = "CPU"
            self.fixed_language = fixed

        def _repo_id(self) -> str:
            return "r"

        def _apply_request(self, kwargs: dict[str, Any]) -> Any:
            seen.update(kwargs)
            return type("Inputs", (), {})()  # no input_ids -> prompt_len 0 (hasattr guard)

        def _gen_kwargs(self) -> dict[str, Any]:
            return {}

        def _generate(self, inputs: Any, gen_kwargs: dict[str, Any]) -> Any:
            return []

        def _decode(self, outputs: Any, prompt_len: int) -> str:
            return ""

    monkeypatch.setattr(vc, "wav_duration_s", lambda p: 0.0, raising=False)
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    _Adapter().transcribe(wav, source_lang=source_lang)
    assert seen.get("language") == expected


def test_faster_whisper_routes_language_through_the_shared_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """THE LOCKSTEP PIN, site 1 — patching `base.resolve_language` must change
    what reaches the decode call. A mirrored copy of the expression would not
    pick this up and fails. (Behavioural, unlike a source-text scan, which a
    reformat would defeat and which proves nothing about what actually runs.)"""
    monkeypatch.setattr(base, "resolve_language", lambda *a, **k: "patched-lang", raising=False)
    monkeypatch.setattr(fw, "wav_duration_s", lambda p: 0.0, raising=False)
    adapter = _fw_adapter(monkeypatch, fixed="en", model_name="nb-whisper-small")
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    adapter.transcribe(wav, source_lang="fr")
    assert adapter._model.seen["language"] == "patched-lang"


def test_mlx_whisper_routes_language_through_the_shared_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """THE LOCKSTEP PIN, site 2."""
    seen: dict[str, Any] = {}

    def fake_transcribe(audio: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"segments": [], "language": "xx"}

    monkeypatch.setattr(base, "resolve_language", lambda *a, **k: "patched-lang", raising=False)
    monkeypatch.setattr(mlx_whisper, "load_recorder_wav_as_pcm", lambda p: b"", raising=False)
    monkeypatch.setattr(mlx_whisper, "wav_duration_s", lambda p: 0.0, raising=False)
    adapter = mlx_whisper.MlxWhisperTranscriber(
        model_name="nb-whisper-small", hf_repo="r", fixed_language="en", transcribe_fn=fake_transcribe
    )
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    adapter.transcribe(wav, source_lang="fr")
    assert seen["language"] == "patched-lang"


def test_voxtral_common_routes_language_through_the_shared_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """THE LOCKSTEP PIN, site 3."""
    from tapscribe.transcribers import _voxtral_common as vc

    seen: dict[str, Any] = {}

    class _Adapter(vc.VoxtralTranscriberBase):
        name = "voxtral"
        backend = "voxtral-hf"

        def __init__(self) -> None:
            self.model_name = "nb-whisper-small"
            self.device = "CPU"
            self.fixed_language = "en"

        def _repo_id(self) -> str:
            return "r"

        def _apply_request(self, kwargs: dict[str, Any]) -> Any:
            seen.update(kwargs)
            return type("Inputs", (), {})()  # no input_ids -> prompt_len 0 (hasattr guard)

        def _gen_kwargs(self) -> dict[str, Any]:
            return {}

        def _generate(self, inputs: Any, gen_kwargs: dict[str, Any]) -> Any:
            return []

        def _decode(self, outputs: Any, prompt_len: int) -> str:
            return ""

    monkeypatch.setattr(base, "resolve_language", lambda *a, **k: "patched-lang", raising=False)
    monkeypatch.setattr(vc, "wav_duration_s", lambda p: 0.0, raising=False)
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    _Adapter().transcribe(wav, source_lang="fr")
    assert seen.get("language") == "patched-lang"


# --------------------------------------------------------------------------
# The off-convention voxtral backend label.
#
# SUPERSEDED TESTS: tests/test_transcribers_voxtral.py currently pins
# "hf-transformers" in three places. Those assertions are legitimately updated
# by this slice (they are NOT protected); this file pins the NEW value.
# --------------------------------------------------------------------------


def test_voxtral_backend_label_follows_the_engine_framework_convention() -> None:
    """Every sibling is `<engine>-<framework>` (`parakeet-hf`, `mlx-voxtral`,
    `parakeet-mlx`). `hf-transformers` is the lone outlier — and it is already
    inconsistent with the catalog key this very module looks up
    (`repo_for(model_name, "voxtral-hf")`)."""
    assert voxtral.VoxtralTranscriber.backend == "voxtral-hf"


def test_voxtral_backend_label_matches_its_own_catalog_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The label and the registry lookup key must be the SAME string — that
    they diverged is the actual defect."""
    from tapscribe.transcribers import catalog

    seen: list[str] = []
    monkeypatch.setattr(catalog, "repo_for", lambda name, backend: seen.append(backend) or None)
    voxtral._resolve_repo("Off-Registry-Voxtral")
    assert seen == [voxtral.VoxtralTranscriber.backend]


def test_voxtral_backend_label_reaches_the_result_json() -> None:
    """The label lands in `TranscriptionResult.backend` → the result JSON → the
    dashboard, so pin it at the audit-field boundary, not only on the class."""

    class _Adapter:
        name = "voxtral"
        backend = voxtral.VoxtralTranscriber.backend
        device = "CPU"
        model_name = "Voxtral-Mini-3B-2507"

    result = base.build_transcription_result(_Adapter(), text="", segments=(), duration=0.0, language="en")
    assert result.backend == "voxtral-hf"


def test_no_source_or_doc_still_carries_the_old_label() -> None:
    """The stale string must not survive anywhere — including CONTEXT.md, which
    documents the backend vocabulary for future contributors.

    THIS file is excluded from the sweep: it names the old label four times on
    purpose (the prose above, and the grep's own argument list two lines down),
    so without the exclude the assertion could never go green no matter what the
    production edit does — an impossible pin, not a contract.
    """
    out = subprocess.run(
        [
            "git",
            "grep",
            "-l",
            "hf-transformers",
            "--",
            "*.py",
            "*.md",
            "*.js",
            "*.cs",
            f":(exclude)tests/{Path(__file__).name}",
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert out.stdout.strip() == "", f"stale 'hf-transformers' label remains in: {out.stdout.split()}"


# --------------------------------------------------------------------------
# Structural invariant the consolidation must not break.
# --------------------------------------------------------------------------


def test_base_stays_a_leaf_module() -> None:
    """`base` must not import `catalog` AT MODULE SCOPE. The repo-fallback
    helper needs `repo_for`, so the obvious consolidation is exactly the edit
    that would introduce a `base` ↔ `catalog` cycle (#230: pin the dependency
    DIRECTION, not just that the module imports).

    A function-local `from .catalog import repo_for` — today's convention in
    every adapter — is FINE and is what the helper should keep doing; only a
    top-level import breaks the leaf rule. So this reads base.py's AST rather
    than watching `sys.modules`: the `tapscribe.transcribers` package `__init__`
    imports `catalog` itself, so any import of `base` pulls it in regardless and
    a sys.modules check could never pass.
    """
    import ast

    tree = ast.parse(Path(base.__file__).read_text(encoding="utf-8"))
    offenders = [
        node
        for node in tree.body  # module scope ONLY — function bodies are not walked
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and "catalog" in (getattr(node, "module", None) or " ".join(a.name for a in node.names))
    ]
    assert not offenders, (
        f"base.py imports catalog at module scope (line {offenders[0].lineno}) — that is the "
        "base<->catalog cycle; use a function-local import like the adapters do"
    )
