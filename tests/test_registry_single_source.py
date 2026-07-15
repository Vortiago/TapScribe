"""RED contract for #206 — the registry is the single source of a model's
language and HF-repo, replacing the per-adapter repo tables and the name-prefix
language heuristic.

ADR-0003 created the registry to centralise model metadata, but two kinds of
model knowledge still live scattered outside it:

  1. LANGUAGE — `default_language_for(model_name)` (transcribers/base.py) guesses
     a fixed language from the *name*: `.en` suffix -> "en", `nb-` prefix ->
     "no", else None. The catalog ALSO declares each model's languages
     declaratively (`languages=("en",)` / `("no",)` / `("auto",)`), so a
     model's fixed language has two sources of truth. They agree only by luck of
     naming: a fixed-language model whose id happens NOT to match the prefix
     heuristic silently loses its language hint (and, via
     faster_whisper.detect_constrained_language, its detect-pass shortcut).

  2. REPO — four per-adapter dicts (MLX_REPO_TABLE, two _MODEL_REPO_TABLEs,
     NB_WHISPER_REPO_TABLE) map model_id -> HuggingFace repo, duplicating
     knowledge the registry row should own.

The fix makes the registry the single source. LANGUAGE lives at the catalog
level — `ModelEntry.fixed_language()` (a pure function of the frozen row) and
`TranscriberRegistry.fixed_language_for(model_name)` — and the loader thunks
thread it onto each adapter at construction as `adapter.fixed_language`, the
exact shape #206 used for HF repos (`ModelEntry.repos` + `catalog.repo_for`).
`base.default_language_for` stays the catalog-free NAME-HEURISTIC fallback for
names with no registry entry, so `base` never imports the catalog (the CodeQL
cyclic-import finding that desynced this contract from `main`). REPO: the
resolvers read a registry-carried repo and the four table dicts are deleted.
The public resolver FUNCTIONS (`mlx_whisper_repo`, the adapters'
`_resolve_repo`) keep their signatures and stay green — this file plus the
incumbent tests (tests/test_transcribers_mlx_whisper.py,
tests/test_transcribers_base.py, in the gate) pin that the repos still resolve
correctly after the move.

What this file pins:

  * DISCRIMINATOR: `moonshine-tiny` / `moonshine-base` are declared
    languages=("en",) but their ids match NEITHER `.en` nor `nb-`, so the name
    heuristic returns None while the registry says "en". This is the one real
    input where name-derived != registry — the pin that proves the language
    source is the registry row. (Moonshine is an available=False placeholder,
    so the resolution MUST consult unfiltered entries, not the installed-only
    view.)
  * GUARDRAILS (green -> green): every existing language branch keeps its answer
    (name-matching fixed-language models still resolve; multilingual/auto models
    still return None, i.e. the registry must not hand a multi-language model a
    spurious fixed language).
  * DELETION (RED at base): the four per-adapter repo-table dicts no longer exist
    as module attributes — the structural proof that the repo source moved. One
    pin per site, so a partial migration that leaves any table behind fails.
  * REPO GUARDRAILS (green -> green): the surviving Parakeet resolvers still
    return the correct repo (now registry-sourced) — catches a re-homing that
    drops or corrupts a repo string. (mlx_whisper's resolver + the large-v3-turbo
    no-suffix quirk + the fallback pattern are pinned by the incumbent
    tests/test_transcribers_mlx_whisper.py, kept in the gate.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tapscribe import nb_whisper
from tapscribe.transcribers import base, faster_whisper, mlx_parakeet, mlx_whisper, parakeet

# Bound BY VALUE at import time, before any test runs: even if another test
# module swaps the `catalog.REGISTRY` attribute (test_transcribers_factory
# rebuilds it around stub loaders), this name keeps pointing at the immutable
# production registry. The language assertions below are therefore immune to
# collection order — the fragility that broke the previous, attribute-reading
# incarnation of this contract on CI.
from tapscribe.transcribers.catalog import REGISTRY

# ─────────────────────────────────────────────────────────────────────────────
# 1. LANGUAGE — the registry's declared `languages`, not the name, is the source
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("model_id", ["moonshine-tiny", "moonshine-base"])
def test_offpattern_fixed_language_model_resolves_from_registry(model_id: str) -> None:
    # THE discriminator. moonshine-* is declared languages=("en",) in the
    # catalog, but its id ends in neither ".en" nor starts with "nb-", so the
    # name heuristic can never know its language — only the registry row can.
    assert REGISTRY.fixed_language_for(model_id) == "en", (
        f"{model_id!r} is declared languages=('en',) in the catalog; its fixed language "
        "must come from that registry row, not from a name-prefix heuristic its id doesn't match"
    )
    # And the catalog-free name heuristic alone indeed cannot answer this —
    # proof the discriminator discriminates (registry != name-derived here).
    assert base.default_language_for(model_id) is None


def test_name_matching_fixed_language_models_unchanged() -> None:
    # Guardrail: models whose name matched the old heuristic keep their answer
    # under the registry-sourced resolution.
    assert REGISTRY.fixed_language_for("tiny.en") == "en"
    assert REGISTRY.fixed_language_for("medium.en") == "en"
    assert REGISTRY.fixed_language_for("nb-whisper-medium") == "no"
    assert REGISTRY.fixed_language_for("nb-whisper-large") == "no"


def test_multilingual_models_get_no_fixed_language() -> None:
    # Guardrail: an auto/multi-language model must NOT be handed a spurious fixed
    # language (catches a naive "return languages[0]" registry lookup). Only a
    # single concrete language code -> that code; anything else -> None.
    assert REGISTRY.fixed_language_for("large-v3") is None  # ("auto",)
    assert REGISTRY.fixed_language_for("small") is None  # multilingual whisper -> ("auto",)
    assert REGISTRY.fixed_language_for("voxtral-mini") is None  # 8 languages
    assert REGISTRY.fixed_language_for("parakeet-tdt-0.6b-v3") is None  # 25 languages


def test_unknown_and_empty_names_resolve_to_none() -> None:
    # Guardrail: a name with no registry entry (or empty) stays None — the
    # lookup miss must degrade to the name heuristic / auto-detect, never raise.
    assert REGISTRY.fixed_language_for("") is None
    assert REGISTRY.fixed_language_for("not-a-registered-model-xyz") is None
    # ... and a heuristic-matching name with no registry entry keeps its hint
    # (ad-hoc checkpoints outside the catalog still resolve).
    assert REGISTRY.fixed_language_for("nb-someones-finetune") == "no"


def test_adapter_carries_registry_language_from_construction() -> None:
    # The loader thunks resolve `catalog.fixed_language_for(model_name)` at
    # construction and the adapter answers from that stored value — no mutable
    # module state read at transcribe time. With the fixed language threaded
    # in, the detect shortcut resolves without touching a model or a file
    # (off-pattern name, so the answer can only have come from the threaded
    # registry value).
    t = faster_whisper.FasterWhisperTranscriber(
        model_name="moonshine-tiny", model=None, device="CPU", fixed_language="en"
    )
    assert t.detect_constrained_language(Path("unused.wav"), ("en", "no")) == "en"
    # A fixed-language model can't produce a candidate outside its language.
    assert t.detect_constrained_language(Path("unused.wav"), ("no", "da")) is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. REPO — the four per-adapter table dicts are deleted (single source)
# ─────────────────────────────────────────────────────────────────────────────


def test_per_adapter_repo_tables_are_deleted() -> None:
    # DELETION pins — one per site. RED at base (all four dicts exist); the repo
    # data must move onto the registry row and these module-level tables go away.
    # The resolver FUNCTIONS survive (pinned below + by the incumbent tests); it
    # is only the duplicated source-of-truth dicts that must not exist.
    assert not hasattr(mlx_whisper, "MLX_REPO_TABLE"), (
        "mlx_whisper.MLX_REPO_TABLE must be deleted — the mlx-whisper repo belongs on the registry row"
    )
    assert not hasattr(mlx_parakeet, "_MODEL_REPO_TABLE"), (
        "mlx_parakeet._MODEL_REPO_TABLE must be deleted — the repo belongs on the registry row"
    )
    assert not hasattr(parakeet, "_MODEL_REPO_TABLE"), (
        "parakeet._MODEL_REPO_TABLE must be deleted — the repo belongs on the registry row"
    )
    assert not hasattr(nb_whisper, "NB_WHISPER_REPO_TABLE"), (
        "nb_whisper.NB_WHISPER_REPO_TABLE must be deleted — the repo belongs on the registry row"
    )


def test_parakeet_repos_still_resolve_after_move() -> None:
    # REPO guardrail (green -> green): the surviving resolvers still hand back the
    # right HF repo once it is registry-sourced. Catches a re-homing that drops
    # or corrupts a Parakeet repo string. (mlx-whisper's resolver, its
    # large-v3-turbo no-suffix quirk, and the unknown-model fallback are covered
    # by the incumbent tests/test_transcribers_mlx_whisper.py, kept in the gate.)
    assert mlx_parakeet._resolve_repo("parakeet-tdt-0.6b-v3") == "mlx-community/parakeet-tdt-0.6b-v3"
    assert parakeet._resolve_repo("parakeet-tdt-0.6b-v3") == "nvidia/parakeet-tdt-0.6b-v3"


def test_nb_whisper_repo_still_resolves_after_move() -> None:
    # REPO guardrail (green -> green) for the fourth swept site: nb_whisper's
    # resolver is one of the four re-homed onto the registry, but nothing else in
    # the gate pins it (test_nb_whisper.py only covers ensure_nb_whisper_lang_ids).
    # Catches a re-homing that drops or corrupts the NbAiLab repo string.
    assert nb_whisper._resolve_nb_whisper_repo("nb-whisper-large") == "NbAiLab/nb-whisper-large"
    # A miss (no registry entry) RAISES — it must never degrade to the raw
    # name, because the resolved value flows straight into an HF Hub
    # snapshot_download(repo_id=...) and the name can arrive from a request
    # body (PRD #120 story 23: the catalog is the allowlist; same rule as
    # the summarizer SUMMARY_MODELS gate).
    with pytest.raises(RuntimeError, match="not-a-registered-model-xyz"):
        nb_whisper._resolve_nb_whisper_repo("not-a-registered-model-xyz")
