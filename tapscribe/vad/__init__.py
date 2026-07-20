"""Silero VAD on raw onnxruntime — TapScribe's speech detector, without torch.

TapScribe used the `silero-vad` PyPI package, which declares `torch>=1.12` and
`torchaudio` as HARD requirements and imports both at module level even when the
model is loaded with `onnx=True`. That made torch a core dependency of every
install: ~500 MB on CPU, ~2.5 GB with CUDA, before a single ASR model is chosen.
It is also the largest single component of the Windows Bundle payload
(ADR-0015).

Reading the upstream code, none of that torch is *model execution* — the ONNX
session does the inference and torch is used purely as an array container
(`zeros`, `cat`, `from_numpy`, `.numpy()`). So this is a faithful port of the
three pieces TapScribe actually uses, with numpy in place of those calls:

  * `SileroVad`         ← upstream `OnnxWrapper`
  * `VadIterator`       ← upstream `VADIterator`   (the live SpeechGate)
  * `speech_timestamps` ← upstream `get_speech_timestamps`  (strip-silence)

**This is a port, not a reimplementation.** The thresholds, the min-duration and
padding arithmetic, and the max-speech cut logic are upstream's, line for line;
only the array calls changed. `tests/test_vad_silero_port.py` pins that claim by
running this and the real `silero-vad` over the same audio and asserting
identical output — it skips when silero-vad isn't installed, so the parity check
runs wherever upstream is available and never blocks an install that dropped it.

The model file (`silero_vad.onnx`, opset 16) is vendored from the silero-vad
package, which is MIT-licensed — see PROVENANCE.md. Vendoring is what lets the
package go, since the weights ship inside it.

Threading note, unchanged from before: silero's recurrent state lives ON the
model object, so every consumer needs its OWN instance — per-gate in
`speech_gate.make_silero_vad`, per-worker-thread in
`strip_silence._local_silero_model`. A shared instance would interleave
concurrent taps through one LSTM state.
"""

from __future__ import annotations

from .silero import SileroVad, VadIterator, load_model, speech_timestamps

__all__ = ["SileroVad", "VadIterator", "load_model", "speech_timestamps"]
