"""Tests for ParakeetTranscriber (Hugging Face `transformers` CUDA/CPU path).

The adapter drives the lower-level `AutoModelForTDT.generate(...,
return_dict_in_generate=True)` + `processor.decode(sequences,
durations=...)` path (NOT the ASR pipeline, whose `return_timestamps="word"`
is CTC-only and raises on a TDT transducer). We mock the model + processor
so the suite needs neither `transformers` nor the 600 MB Parakeet weights.

The end-to-end path (real weights, real audio) is exercised manually and
guarded structurally by `test_transformers_parakeet_upstream_contract`
below, which no-ops where `transformers` isn't importable.
"""

from __future__ import annotations

import types
import wave
from pathlib import Path

import numpy as np
import pytest

from tapscribe.transcribers.base import TranscriptionResult


def _wav(path: Path, *, seconds: float = 1.0) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.zeros(int(16000 * seconds), dtype=np.int16).tobytes())
    return path


class _FakeInputs(dict):
    """Stands in for the processor's BatchFeature: `**`-unpackable into
    `generate`, and `.to(device, dtype=)` returns itself."""

    def to(self, device, dtype=None):  # noqa: ARG002
        return self


class _FakeProcessor:
    """Returns a fixed per-window token list from `decode`. `feature_extractor.
    sampling_rate` defaults to the recorder rate so `_assert_*` passes."""

    def __init__(self, window_token_lists, *, sampling_rate: int = 16000):
        self._windows = window_token_lists
        self.feature_extractor = types.SimpleNamespace(sampling_rate=sampling_rate)
        self.call_sampling_rates: list = []
        self.decode_calls: list[dict] = []

    def __call__(self, arrays, sampling_rate=None):
        self.call_sampling_rates.append(sampling_rate)
        return _FakeInputs(input_features="X")

    def decode(self, sequences, durations=None, skip_special_tokens=True):  # noqa: ARG002
        idx = len(self.decode_calls)
        self.decode_calls.append({"durations": durations, "skip_special_tokens": skip_special_tokens})
        toks = self._windows[idx] if idx < len(self._windows) else []
        text = "".join(t["token"] for t in toks).strip()
        return ([text], [toks])


class _FakeModel:
    def __init__(self):
        self.device = "cpu"
        self.dtype = "float32"
        self.generate_calls: list[dict] = []

    def generate(self, return_dict_in_generate=False, **kwargs):
        self.generate_calls.append({"return_dict_in_generate": return_dict_in_generate, **kwargs})
        return types.SimpleNamespace(sequences=[[1, 2, 3]], durations=[0.1])


def _tok(token: str, start: float, end: float) -> dict:
    return {"token": token, "start": start, "end": end}


def _adapter(window_token_lists, *, device="CPU", **kw):
    from tapscribe.transcribers.parakeet import ParakeetTranscriber

    return ParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=_FakeModel(),
        processor=_FakeProcessor(window_token_lists),
        device=device,
        **kw,
    )


def test_metadata_properties():
    t = _adapter([[]], device="CUDA")
    assert t.name == "parakeet"
    assert t.backend == "parakeet-hf"
    assert t.device == "CUDA"
    assert t.model_name == "parakeet-tdt-0.6b-v3"


def test_transcribe_builds_words_and_segments_from_tokens(tmp_path: Path):
    tokens = [
        _tok("Hello", 0.10, 0.40),
        _tok(",", 0.40, 0.40),
        _tok(" there", 0.45, 0.80),
        _tok(".", 0.80, 0.80),
        _tok(" How", 0.90, 1.20),
        _tok(" are", 1.25, 1.40),
        _tok(" you", 1.40, 1.50),
        _tok("?", 1.50, 1.50),
    ]
    t = _adapter([tokens])
    r = t.transcribe(_wav(tmp_path / "x.wav"))
    assert isinstance(r, TranscriptionResult)
    assert [s.text for s in r.segments] == ["Hello, there.", "How are you?"]
    assert r.segments[0].start == 0.10
    assert r.segments[0].end == 0.80
    assert [w.word for w in (r.segments[0].words or ())] == ["Hello,", "there."]
    assert r.text == "Hello, there. How are you?"


def test_transcribe_uses_lower_level_generate_decode_path(tmp_path: Path):
    """Word/segment timestamps come from generate(return_dict_in_generate=True)
    + decode(durations=...) — assert the adapter wires both."""
    t = _adapter([[_tok("hi", 0.0, 0.1)]])
    t.transcribe(_wav(tmp_path / "x.wav"))
    assert t._model.generate_calls[0]["return_dict_in_generate"] is True
    # decode must be handed the durations the model emitted.
    assert t._processor.decode_calls[0]["durations"] == [0.1]
    # processor invoked at the recorder sample rate.
    assert t._processor.call_sampling_rates == [16000]


def test_transcribe_caps_generation_length(tmp_path: Path):
    """`generate` must be given an explicit `max_new_tokens` sized to the
    window. Without it transformers falls back to a model-agnostic default
    (~1510) that both warns and can truncate a long window's transcript.
    The bound scales with audio length, so a longer clip gets a larger cap."""
    short = _adapter([[_tok("hi", 0.0, 0.1)]])
    short.transcribe(_wav(tmp_path / "short.wav", seconds=1.0))
    short_cap = short._model.generate_calls[0]["max_new_tokens"]
    assert isinstance(short_cap, int) and short_cap > 0

    long = _adapter([[_tok("hi", 0.0, 0.1)]])
    long.transcribe(_wav(tmp_path / "long.wav", seconds=4.0))
    long_cap = long._model.generate_calls[0]["max_new_tokens"]
    assert long_cap > short_cap, "max_new_tokens must scale with the window's audio length"


def test_transcribe_records_source_language(tmp_path: Path):
    t = _adapter([[_tok("bonjour", 0.0, 0.5)]])
    r = t.transcribe(_wav(tmp_path / "x.wav"), source_lang="fr")
    assert r.language == "fr"
    assert r.source_language == "fr"


def test_transcribe_defaults_language_to_auto(tmp_path: Path):
    t = _adapter([[_tok("ok", 0.0, 0.2)]])
    r = t.transcribe(_wav(tmp_path / "x.wav"))
    assert r.language == "auto"


def test_transcribe_echoes_prompt_and_hotwords_audit_only(tmp_path: Path):
    """Parakeet has no prompt/hotwords slot: accepted, dropped at the model
    call, echoed on the result for audit parity."""
    t = _adapter([[_tok("ok", 0.0, 0.2)]])
    r = t.transcribe(_wav(tmp_path / "x.wav"), initial_prompt="standup", hotwords="Acme")
    gen_kwargs = t._model.generate_calls[0]
    assert "initial_prompt" not in gen_kwargs
    assert "hotwords" not in gen_kwargs
    assert r.initial_prompt_used == "standup"
    assert r.hotwords_used == "Acme"


def test_transcribe_chunks_long_audio_and_offsets_timestamps(tmp_path: Path):
    """Two windows (0.5 s chunks, no overlap over a 1 s WAV): the second
    window's token timestamps must be shifted by its 0.5 s offset."""
    win0 = [_tok("one", 0.10, 0.30), _tok(".", 0.30, 0.30)]
    win1 = [_tok("two", 0.10, 0.30), _tok(".", 0.30, 0.30)]
    t = _adapter([win0, win1], chunk_duration_s=0.5, overlap_duration_s=0.0)
    r = t.transcribe(_wav(tmp_path / "x.wav", seconds=1.0))
    assert len(t._model.generate_calls) == 2
    assert [s.text for s in r.segments] == ["one.", "two."]
    # window 1's "two." is shifted by the 0.5 s window offset.
    assert r.segments[1].start == 0.60


def test_assert_feature_extractor_sample_rate_mismatch_raises():
    from tapscribe.transcribers.parakeet import ParakeetTranscriber

    t = ParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=_FakeModel(),
        processor=_FakeProcessor([[]], sampling_rate=8000),
        device="CPU",
    )
    with pytest.raises(RuntimeError, match="sampling_rate"):
        t._assert_feature_extractor_sample_rate()


def _hide_modules(monkeypatch, *names: str, present: tuple[str, ...] = ()) -> None:
    """Make `find_spec` report `names` as missing and `present` as installed.

    `present` is not optional sugar: a test that only HIDES a package asserts
    nothing about the ones it left alone, and the CI matrix installs neither
    `transformers` nor `librosa`. So hiding librosa alone produced
    "missing: transformers, librosa" there — a test meaning "only librosa is
    missing" has to pin the other side of that too, or it passes locally and
    fails on all nine legs.
    """
    import importlib.util as importlib_util

    real_find_spec = importlib_util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name in names:
            return None
        if name in present:
            return object()  # a truthy spec — load() only checks for None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib_util, "find_spec", fake_find_spec)


def test_load_fails_fast_without_transformers(monkeypatch):
    """No `transformers` installed → an actionable RuntimeError naming the
    package, not a deep ImportError chain."""
    _hide_modules(monkeypatch, "transformers", present=("librosa",))

    from tapscribe.transcribers.parakeet import ParakeetTranscriber

    # Anchored on the trailing PERIOD: a bare "missing: transformers" is also a
    # prefix of "missing: transformers, librosa", so it would pass in a venv
    # missing BOTH and stop distinguishing the two cases — which is exactly how
    # the librosa-only test came to be red on every CI leg.
    with pytest.raises(RuntimeError, match=r"missing: transformers\."):
        ParakeetTranscriber.load("parakeet-tdt-0.6b-v3", kind="cpu")


def test_load_fails_fast_when_only_librosa_is_missing(monkeypatch):
    """The Voxtral-only venv: `transformers` IS installed (voxtral-cpu
    declares it) but `librosa` — declared by `parakeet-cpu` alone — is not.

    Guarding on transformers alone let this venv sail past `load()` and die
    inside `AutoProcessor.from_pretrained`, deep in the feature extractor,
    with no mention of the package the operator actually needs.
    """
    _hide_modules(monkeypatch, "librosa", present=("transformers",))

    from tapscribe.transcribers.parakeet import ParakeetTranscriber

    with pytest.raises(RuntimeError, match=r"missing: librosa\b"):
        ParakeetTranscriber.load("parakeet-tdt-0.6b-v3", kind="cpu")


# ── Upstream-contract smoke test ─────────────────────────────────────
# Pins the `transformers` symbols + signatures the adapter imports so an
# upstream rename fails CI on the bump PR instead of in production. No-ops
# where transformers isn't importable (the Linux CI unit matrix); runs in
# full where it is.


def test_transformers_parakeet_upstream_contract():
    import inspect

    transformers = pytest.importorskip("transformers")

    for name in ("AutoModelForTDT", "AutoProcessor", "ParakeetForTDT"):
        assert hasattr(transformers, name), f"transformers.{name} missing — upstream API drift"

    # `processor.decode` must accept the `durations` kwarg the adapter passes.
    proc_decode = transformers.ParakeetProcessor.decode
    params = inspect.signature(proc_decode).parameters
    assert "durations" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()), (
        "ParakeetProcessor.decode no longer accepts durations="
    )


# ── chunk/overlap joint constraint ──────────────────────────────────


def test_incompatible_chunk_overlap_env_pair_clamps_at_construction(tmp_path, monkeypatch, capsys):
    """`env_float` validates the two knobs INDEPENDENTLY, so a legal-but-
    incompatible PAIR used to sail through construction and then kill every
    transcribe: `chunk_windows` raises `ValueError: overlap_s (15.0) must be
    <= chunk_s * 0.9 (9.0)`, which isn't in `_DOMAIN_ERROR_STATUS` (a bare
    500) and aborts the whole job in `transcribe_session`.

    A chunk of 10 s is inside `_CHUNK_S_BOUNDS` and the default 15 s overlap
    is inside `_OVERLAP_S_BOUNDS` — neither knob is individually wrong.
    """
    monkeypatch.setenv("TAPSCRIBE_PARAKEET_CHUNK_S", "10")
    monkeypatch.delenv("TAPSCRIBE_PARAKEET_OVERLAP_S", raising=False)

    t = _adapter([[_tok(" hi", 0.0, 0.2)]])

    assert t.chunk_duration_s == 10.0
    assert t.overlap_duration_s == 9.0  # clamped to chunk * 0.9
    # Degraded LOUDLY, matching env_float's own "ignoring …; using …" notice.
    out = capsys.readouterr().out
    assert "ignoring overlap" in out and "using 9.0" in out

    # And the adapter actually transcribes instead of dying per-WAV.
    result = t.transcribe(_wav(tmp_path / "x.wav", seconds=1.0))
    assert result.text == "hi"
    assert result.quality_settings["overlap_duration_s"] == 9.0


def test_compatible_chunk_overlap_pair_is_left_alone(monkeypatch, capsys):
    """The clamp must not touch a pair that `chunk_windows` already accepts —
    otherwise it would quietly re-tune every default install."""
    monkeypatch.delenv("TAPSCRIBE_PARAKEET_CHUNK_S", raising=False)
    monkeypatch.delenv("TAPSCRIBE_PARAKEET_OVERLAP_S", raising=False)

    t = _adapter([[]])

    assert (t.chunk_duration_s, t.overlap_duration_s) == (120.0, 15.0)
    assert "ignoring overlap" not in capsys.readouterr().out
