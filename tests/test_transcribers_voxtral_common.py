"""Tests pinning the shared Voxtral skeleton's seams.

`VoxtralTranscriberBase` (`_voxtral_common.py`) owns the transcribe flow
both adapters (`VoxtralTranscriber`, `MlxVoxtralTranscriber`) share:
language resolution -> request -> generate -> decode -> sentence-split ->
result. The adapter-level tests (`test_transcribers_voxtral.py`,
`test_transcribers_mlx_voxtral.py`) already pin each adapter's observable
behavior through mocked processor/model objects; these tests pin the base
class's own contract directly, through a minimal fake subclass that
records which hooks were called, in what order, and with what arguments -
so a refactor that reorders the flow or drops a hook call fails here
first.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tapscribe.transcribers._voxtral_common import VoxtralTranscriberBase, inputs_kwargs


def _one_second_wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.zeros(16000, dtype=np.int16).tobytes())
    return path


class _FakeVoxtral(VoxtralTranscriberBase):
    """Minimal concrete subclass that records every hook invocation
    instead of talking to a real (or mocked) model library."""

    backend = "test-backend"
    device = "TEST"

    def __init__(
        self,
        *,
        model_name: str,
        repo_id: str = "repo/id",
        gen_kwargs: dict[str, Any] | None = None,
        decoded_text: str = "hi",
        calls: list[tuple] | None = None,
    ):
        self.model_name = model_name
        self._repo = repo_id
        self._gen_kwargs_value = gen_kwargs if gen_kwargs is not None else {"max_new_tokens": 5}
        self._decoded_text = decoded_text
        self.calls: list[tuple] = calls if calls is not None else []

    def _repo_id(self) -> str:
        self.calls.append(("_repo_id",))
        return self._repo

    def _apply_request(self, request_kwargs: dict[str, Any]) -> Any:
        self.calls.append(("_apply_request", dict(request_kwargs)))

        class _Inputs:
            class input_ids:
                shape = (1, 7)

        return _Inputs()

    def _gen_kwargs(self) -> dict[str, Any]:
        self.calls.append(("_gen_kwargs",))
        return dict(self._gen_kwargs_value)

    def _generate(self, inputs: Any, gen_kwargs: dict[str, Any]) -> Any:
        self.calls.append(("_generate", inputs, dict(gen_kwargs)))
        return "OUTPUTS"

    def _decode(self, outputs: Any, prompt_len: int) -> str:
        self.calls.append(("_decode", outputs, prompt_len))
        return self._decoded_text


class _NoInputIdsVoxtral(_FakeVoxtral):
    """Same as `_FakeVoxtral` but `_apply_request` returns an object with
    no `input_ids` attribute, to pin the `prompt_len == 0` fallback."""

    def _apply_request(self, request_kwargs: dict[str, Any]) -> Any:
        self.calls.append(("_apply_request", dict(request_kwargs)))
        return object()


def test_transcribe_calls_hooks_in_order_with_resolved_request_kwargs(tmp_path: Path):
    calls: list[tuple] = []
    t = _FakeVoxtral(model_name="nb-voxtral-mini", repo_id="repo/id", calls=calls)
    wav = _one_second_wav(tmp_path / "x.wav")

    result = t.transcribe(wav)

    # The five hooks fire exactly once, in the documented order.
    assert [c[0] for c in calls] == [
        "_repo_id",
        "_apply_request",
        "_gen_kwargs",
        "_generate",
        "_decode",
    ]

    # _apply_request's request_kwargs: audio path, repo id from _repo_id(),
    # and a language kwarg resolved via default_language_for("nb-*") -> "no"
    # because no explicit source_lang was passed.
    _, apply_kwargs = calls[1]
    assert apply_kwargs == {"audio": str(wav), "model_id": "repo/id", "language": "no"}

    # _generate receives the object _apply_request returned, plus exactly
    # what _gen_kwargs() produced.
    _, gen_inputs, gen_kwargs = calls[3]
    assert gen_kwargs == {"max_new_tokens": 5}

    # _decode receives _generate's return value and the prompt length
    # sliced from inputs.input_ids.shape[1].
    _, outputs, prompt_len = calls[4]
    assert outputs == "OUTPUTS"
    assert prompt_len == 7

    assert result.text == "hi"


def test_transcribe_prefers_source_lang_over_default_language_for(tmp_path: Path):
    calls: list[tuple] = []
    # "nb-voxtral-mini" would resolve to "no" via default_language_for, but
    # an explicit source_lang must win.
    t = _FakeVoxtral(model_name="nb-voxtral-mini", calls=calls)
    wav = _one_second_wav(tmp_path / "x.wav")

    t.transcribe(wav, source_lang="fr")

    _, apply_kwargs = calls[1]
    assert apply_kwargs["language"] == "fr"


def test_transcribe_omits_language_kwarg_when_no_hint_available(tmp_path: Path):
    calls: list[tuple] = []
    # Plain model name (no ".en"/"nb-" prefix) and no source_lang -> no hint.
    t = _FakeVoxtral(model_name="voxtral-mini", calls=calls)
    wav = _one_second_wav(tmp_path / "x.wav")

    t.transcribe(wav)

    _, apply_kwargs = calls[1]
    assert "language" not in apply_kwargs


def test_quality_settings_on_result_equals_gen_kwargs_output(tmp_path: Path):
    t = _FakeVoxtral(model_name="voxtral-mini", gen_kwargs={"temperature": 0.3, "foo": "bar"})
    wav = _one_second_wav(tmp_path / "x.wav")

    result = t.transcribe(wav)

    assert result.quality_settings == {"temperature": 0.3, "foo": "bar"}


def test_transcribe_prompt_len_is_zero_when_inputs_lack_input_ids(tmp_path: Path):
    calls: list[tuple] = []
    t = _NoInputIdsVoxtral(model_name="voxtral-mini", calls=calls)
    wav = _one_second_wav(tmp_path / "x.wav")

    t.transcribe(wav)

    _, _outputs, prompt_len = calls[-1]
    assert prompt_len == 0


def test_unimplemented_hooks_raise_not_implemented_error():
    # A subclass that implements none of the hooks — pins that the base
    # class is a template, not a usable adapter on its own.
    class _Bare(VoxtralTranscriberBase):
        model_name = "x"
        device = "TEST"

    b = _Bare()
    with pytest.raises(NotImplementedError):
        b._repo_id()
    with pytest.raises(NotImplementedError):
        b._apply_request({})
    with pytest.raises(NotImplementedError):
        b._gen_kwargs()
    with pytest.raises(NotImplementedError):
        b._generate(None, {})
    with pytest.raises(NotImplementedError):
        b._decode(None, 0)


def testinputs_kwargs_dict_unpacks_a_mapping():
    assert inputs_kwargs({"input_ids": [1, 2, 3]}) == {"input_ids": [1, 2, 3]}


def testinputs_kwargs_falls_back_to_input_ids_attribute_for_unmappable_object():
    class _Obj:
        input_ids = "IDS"

    assert inputs_kwargs(_Obj()) == {"input_ids": "IDS"}


def testinputs_kwargs_returns_empty_dict_when_neither_mapping_nor_input_ids():
    class _Obj:
        pass

    assert inputs_kwargs(_Obj()) == {}
