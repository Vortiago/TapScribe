# Vendored Silero VAD model

`silero_vad.onnx` is copied **verbatim** from the [`silero-vad`][silero] PyPI
package (`silero_vad/data/silero_vad.onnx`, opset 16 — what upstream's
`load_silero_vad(onnx=True)` resolves to by default). 2.3 MB.

**Licence:** MIT, per silero-vad's own PyPI metadata
(`License :: OSI Approved :: MIT License`). Vendoring the weights is what lets
the package itself go — see the reasoning in `__init__.py` and issue #374.

## Why vendored rather than depended on

The `silero-vad` package declares `torch>=1.12` and `torchaudio>=0.12` as **hard**
requirements and imports both at module level even when the model is loaded with
`onnx=True`. That put ~773 MB of torch + torchaudio + silero into every
TapScribe install — measured on the dev box — including the Windows Bundle's
baseline payload (ADR-0015), where it was the single largest component. The
inference itself never needed torch: upstream uses it purely as an array
container around an `onnxruntime` session.

`tapscribe/vad/silero.py` is a faithful port of the three pieces TapScribe uses,
with numpy in place of those array calls.
`tests/test_vad_silero_port.py` runs the port and the real package over the same
audio and asserts **identical** output, so the port can't drift silently. Those
tests `importorskip` silero-vad, so they run wherever upstream is installed and
no-op otherwise.

## Re-copying / updating

Upstream ships the model inside the wheel, so:

```bash
pip install silero-vad
python - <<'EOF'
import pathlib, shutil, silero_vad
src = pathlib.Path(silero_vad.__file__).parent / "data" / "silero_vad.onnx"
shutil.copy(src, "tapscribe/vad/silero_vad.onnx")
EOF
pytest tests/test_vad_silero_port.py   # must stay green against the new weights
```

If a future upstream release changes the model's input/output signature (the
session is called with `input` / `state` / `sr` and returns `(out, state)`), the
port in `silero.py` needs the matching change — the differential test will fail
loudly rather than drift.

[silero]: https://github.com/snakers4/silero-vad
