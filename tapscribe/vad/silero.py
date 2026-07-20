"""The port itself. See `tapscribe/vad/__init__.py` for why it exists.

Every function here mirrors an upstream `silero_vad.utils_vad` counterpart. Where
a line differs from upstream it is only because a torch array call became a
numpy one; the control flow, the thresholds and the arithmetic are unchanged, so
that `tests/test_vad_silero_port.py`'s differential test against the real
package is a meaningful check rather than a tautology.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

#: Vendored alongside this module (MIT — see PROVENANCE.md). Opset 16, which is
#: what upstream's `load_silero_vad(onnx=True)` resolves to by default.
MODEL_PATH = Path(__file__).resolve().parent / "silero_vad.onnx"

#: 16 kHz mono is TapScribe's only audio shape (`audio.SAMPLE_RATE`), and the
#: 8 kHz branch upstream carries would be dead code here. Supporting one rate
#: keeps the window/context constants below literal instead of conditional.
SUPPORTED_RATE = 16_000
WINDOW_SAMPLES = 512
CONTEXT_SAMPLES = 64


class SileroVad:
    """One Silero VAD session plus its streaming state — upstream `OnnxWrapper`.

    NOT thread-safe and NOT shareable: `_state` and `_context` are this
    instance's rolling RNN state. Every consumer constructs its own (see the
    module docstring).
    """

    def __init__(self, model_path: Path | str = MODEL_PATH) -> None:
        import onnxruntime  # noqa: PLC0415 — heavy; only production wiring loads a model.

        options = onnxruntime.SessionOptions()
        # Upstream pins both to 1. The gate runs one 512-sample window at a
        # time on a per-tap thread, so intra-op parallelism buys nothing and
        # a thread pool per gate would be actively harmful with many taps open.
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"], sess_options=options
        )
        self.reset_states()

    def reset_states(self, batch_size: int = 1) -> None:
        self._state = np.zeros((2, batch_size, 128), dtype=np.float32)
        self._context = np.zeros((0,), dtype=np.float32)
        self._last_batch_size = 0

    def __call__(self, x: np.ndarray, sr: int) -> np.ndarray:
        """Speech probability for ONE window. Returns an array so callers can
        use `.item()` exactly as they did against upstream."""
        if sr != SUPPORTED_RATE:
            raise ValueError(f"unsupported sampling rate {sr} (TapScribe is {SUPPORTED_RATE} Hz mono)")

        x = np.asarray(x, dtype=np.float32)
        if x.ndim == 1:
            x = x[np.newaxis, :]
        if x.ndim > 2:
            raise ValueError(f"too many dimensions for input audio chunk: {x.ndim}")
        if x.shape[-1] != WINDOW_SAMPLES:
            raise ValueError(f"expected {WINDOW_SAMPLES} samples per window, got {x.shape[-1]}")

        batch_size = x.shape[0]
        if not self._last_batch_size or self._last_batch_size != batch_size:
            self.reset_states(batch_size)
        if not len(self._context):
            self._context = np.zeros((batch_size, CONTEXT_SAMPLES), dtype=np.float32)

        x = np.concatenate([self._context, x], axis=1)
        out, state = self.session.run(
            None, {"input": x, "state": self._state, "sr": np.array(sr, dtype="int64")}
        )
        self._state = state
        self._context = x[..., -CONTEXT_SAMPLES:]
        self._last_batch_size = batch_size
        return out


def load_model(model_path: Path | str = MODEL_PATH) -> SileroVad:
    """A FRESH model instance. Deliberately uncached — see the module docstring
    on why sharing one across gates is a correctness bug, not an optimisation."""
    return SileroVad(model_path)


class VadIterator:
    """Streaming speech start/end detection — upstream `VADIterator`.

    Returns `{"start": sample}` / `{"end": sample}` / `None` per window, which
    is the shape `speech_gate.SpeechGate` already consumes.
    """

    def __init__(
        self,
        model: SileroVad,
        *,
        threshold: float = 0.5,
        sampling_rate: int = SUPPORTED_RATE,
        min_silence_duration_ms: int = 100,
        speech_pad_ms: int = 30,
    ) -> None:
        if sampling_rate != SUPPORTED_RATE:
            raise ValueError(f"VadIterator supports {SUPPORTED_RATE} Hz only, got {sampling_rate}")
        self.model = model
        self.threshold = threshold
        self.sampling_rate = sampling_rate
        self.min_silence_samples = sampling_rate * min_silence_duration_ms / 1000
        self.speech_pad_samples = sampling_rate * speech_pad_ms / 1000
        self.reset_states()

    def reset_states(self) -> None:
        self.model.reset_states()
        self.triggered = False
        self.temp_end = 0
        self.current_sample = 0

    def __call__(self, x: np.ndarray) -> dict | None:
        x = np.asarray(x, dtype=np.float32)
        window_size_samples = x.shape[-1]
        self.current_sample += window_size_samples

        speech_prob = self.model(x, self.sampling_rate).item()

        if (speech_prob >= self.threshold) and self.temp_end:
            self.temp_end = 0

        if (speech_prob >= self.threshold) and not self.triggered:
            self.triggered = True
            speech_start = max(0, self.current_sample - self.speech_pad_samples - window_size_samples)
            return {"start": int(speech_start)}

        # The 0.15 hysteresis below the trigger threshold is upstream's: it stops
        # a probability hovering at the boundary from chattering open/closed.
        if (speech_prob < self.threshold - 0.15) and self.triggered:
            if not self.temp_end:
                self.temp_end = self.current_sample
            if self.current_sample - self.temp_end < self.min_silence_samples:
                return None
            speech_end = self.temp_end + self.speech_pad_samples - window_size_samples
            self.temp_end = 0
            self.triggered = False
            return {"end": int(speech_end)}

        return None


def speech_timestamps(
    audio: np.ndarray,
    model: SileroVad,
    *,
    threshold: float = 0.5,
    sampling_rate: int = SUPPORTED_RATE,
    min_speech_duration_ms: int = 250,
    max_speech_duration_s: float = float("inf"),
    min_silence_duration_ms: int = 100,
    speech_pad_ms: int = 30,
    neg_threshold: float | None = None,
    min_silence_at_max_speech: int = 98,
    use_max_poss_sil_at_max_speech: bool = True,
) -> list[dict]:
    """Speech regions over a whole clip — upstream `get_speech_timestamps`.

    Returns `[{"start": sample, "end": sample}, ...]`. Upstream's
    presentation-only arguments (`return_seconds`, `time_resolution`,
    `visualize_probs`, `progress_tracking_callback`) are omitted: TapScribe
    passes none of them, and dropping them keeps the ported surface to the parts
    that actually decide where a region begins and ends.
    """
    if sampling_rate != SUPPORTED_RATE:
        raise ValueError(f"unsupported sampling rate {sampling_rate} (TapScribe is {SUPPORTED_RATE} Hz mono)")

    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:
        audio = np.squeeze(audio)
        if audio.ndim > 1:
            raise ValueError("more than one dimension in audio — is this a 2-channel file?")

    window_size_samples = WINDOW_SAMPLES
    model.reset_states()

    min_speech_samples = sampling_rate * min_speech_duration_ms / 1000
    speech_pad_samples = sampling_rate * speech_pad_ms / 1000
    max_speech_samples = sampling_rate * max_speech_duration_s - window_size_samples - 2 * speech_pad_samples
    min_silence_samples = sampling_rate * min_silence_duration_ms / 1000
    min_silence_samples_at_max_speech = sampling_rate * min_silence_at_max_speech / 1000
    audio_length_samples = len(audio)

    speech_probs: list[float] = []
    for current_start_sample in range(0, audio_length_samples, window_size_samples):
        chunk = audio[current_start_sample : current_start_sample + window_size_samples]
        if len(chunk) < window_size_samples:
            chunk = np.pad(chunk, (0, int(window_size_samples - len(chunk))))
        speech_probs.append(model(chunk, sampling_rate).item())

    triggered = False
    speeches: list[dict] = []
    current_speech: dict = {}
    if neg_threshold is None:
        neg_threshold = max(threshold - 0.15, 0.01)
    temp_end = 0
    prev_end = next_start = 0
    possible_ends: list[tuple[int, int]] = []

    for i, speech_prob in enumerate(speech_probs):
        cur_sample = window_size_samples * i

        if (speech_prob >= threshold) and temp_end:
            sil_dur = cur_sample - temp_end
            if sil_dur > min_silence_samples_at_max_speech:
                possible_ends.append((temp_end, sil_dur))
            temp_end = 0
            if next_start < prev_end:
                next_start = cur_sample

        if (speech_prob >= threshold) and not triggered:
            triggered = True
            current_speech["start"] = cur_sample
            continue

        if triggered and (cur_sample - current_speech["start"] > max_speech_samples):
            if use_max_poss_sil_at_max_speech and possible_ends:
                # Cut at the LONGEST silence seen inside this over-long run,
                # rather than wherever the limit happened to land.
                prev_end, dur = max(possible_ends, key=lambda x: x[1])
                current_speech["end"] = prev_end
                speeches.append(current_speech)
                current_speech = {}
                next_start = prev_end + dur
                if next_start < prev_end + cur_sample:
                    current_speech["start"] = next_start
                else:
                    triggered = False
                prev_end = next_start = temp_end = 0
                possible_ends = []
            elif prev_end:
                current_speech["end"] = prev_end
                speeches.append(current_speech)
                current_speech = {}
                if next_start < prev_end:
                    triggered = False
                else:
                    current_speech["start"] = next_start
                prev_end = next_start = temp_end = 0
                possible_ends = []
            else:
                current_speech["end"] = cur_sample
                speeches.append(current_speech)
                current_speech = {}
                prev_end = next_start = temp_end = 0
                triggered = False
                possible_ends = []
                continue

        if (speech_prob < neg_threshold) and triggered:
            if not temp_end:
                temp_end = cur_sample
            sil_dur_now = cur_sample - temp_end
            if not use_max_poss_sil_at_max_speech and sil_dur_now > min_silence_samples_at_max_speech:
                prev_end = temp_end
            if sil_dur_now < min_silence_samples:
                continue
            current_speech["end"] = temp_end
            if (current_speech["end"] - current_speech["start"]) > min_speech_samples:
                speeches.append(current_speech)
            current_speech = {}
            prev_end = next_start = temp_end = 0
            triggered = False
            possible_ends = []
            continue

    if current_speech and (audio_length_samples - current_speech["start"]) > min_speech_samples:
        current_speech["end"] = audio_length_samples
        speeches.append(current_speech)

    # Pad each region outwards, splitting the gap when two regions are closer
    # than 2*pad so padding can never make them overlap.
    for i, speech in enumerate(speeches):
        if i == 0:
            speech["start"] = int(max(0, speech["start"] - speech_pad_samples))
        if i != len(speeches) - 1:
            silence_duration = speeches[i + 1]["start"] - speech["end"]
            if silence_duration < 2 * speech_pad_samples:
                speech["end"] += int(silence_duration // 2)
                speeches[i + 1]["start"] = int(max(0, speeches[i + 1]["start"] - silence_duration // 2))
            else:
                speech["end"] = int(min(audio_length_samples, speech["end"] + speech_pad_samples))
                speeches[i + 1]["start"] = int(max(0, speeches[i + 1]["start"] - speech_pad_samples))
        else:
            speech["end"] = int(min(audio_length_samples, speech["end"] + speech_pad_samples))

    return speeches
