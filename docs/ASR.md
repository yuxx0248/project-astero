# Local Chinese and English ASR

## Selected implementation

AST-05 uses an injectable `Recognizer` interface and an optional local
`sherpa-onnx` adapter in [`astero/speech/asr.py`](../astero/speech/asr.py).
The recommended model is the sherpa-onnx streaming Zipformer bilingual
Chinese/English model dated 2023-02-20, using its int8 encoder, decoder and
joiner. Its Hugging Face model card declares Apache-2.0. The model supports
streaming inference; sherpa-onnx runs locally without sending audio to a
service. Its model card traces the weights to a separately hosted
Chinese-English Zipformer model, so users should review that upstream source
and intended use as well as the exported model card before redistribution.

The selected model does not provide language identification in this adapter;
`ASRResult.language` is `null` rather than a guessed language. No confidence is
reported. Empty sample input and malformed format/length are errors. Exact
digital silence returns an empty text result without invoking inference. Model
or runtime absence raises `ASRUnavailableError`; inference failures raise
`ASRError`; a deadline raises `ASRTimeoutError`. A native inference that is
already executing cannot be forcibly interrupted; after a timeout the adapter
rejects further calls until that inference finishes. Audio and result text are
not logged or saved.

## Runtime and model setup

The host checked for AST-05 is Linux `aarch64` (Fedora) with Python 3.15.2.
At inspection, `sherpa_onnx`, ONNX Runtime, Whisper, Vosk, and local ASR model
weights were absent. Python 3.15 support on Fedora is not established by the
upstream wheel table, so use the project's available Python version supported
by the current `sherpa-onnx` CPU wheel, or build from source if no matching
wheel is published. Upstream documents Linux aarch64 CPU wheels and source
build support. Install in the active virtual environment, then verify import:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install sherpa-onnx sherpa-onnx-bin
python -c 'import sherpa_onnx; print(sherpa_onnx.__version__)'
```

Download model files outside the repository, for example to
`~/.local/share/astero/models/`, from the upstream [model card and files](https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20).
Use its `*-int8.onnx` encoder, decoder and joiner, plus `tokens.txt`. Do not
commit model weights. Update paths in `configs/asr.example.json` or pass a
`ZipformerConfig` directly. The adapter expects mono 16 kHz `pcm_s16le` or
normalized `float32_le` `MonoFrame` input. PCM16 is scaled to `[-1, 1)`.

No model was downloaded or installed as part of AST-05, so the exact runtime
wheel and inference speed have not yet been verified on this machine.

## Interface

```python
from pathlib import Path
from astero.speech.asr import SherpaOnnxASR, ZipformerConfig

config = ZipformerConfig(
    encoder=Path("~/.local/share/astero/models/.../encoder-epoch-99-avg-1.int8.onnx").expanduser(),
    decoder=Path("~/.local/share/astero/models/.../decoder-epoch-99-avg-1.int8.onnx").expanduser(),
    joiner=Path("~/.local/share/astero/models/.../joiner-epoch-99-avg-1.int8.onnx").expanduser(),
    tokens=Path("~/.local/share/astero/models/.../tokens.txt").expanduser(),
)
with SherpaOnnxASR(config) as asr:
    result = asr.recognize(mono_frame)
    print(result.text, result.language, result.elapsed_seconds)
```

The `Recognizer` protocol can be substituted with `ScriptedRecognizer` from
`astero.speech.testing` for deterministic offline integration tests.

## Verification record

| Check | Result |
| --- | --- |
| Runtime / model inventory | No sherpa-onnx package or ASR model found; host is aarch64. |
| Offline substitute tests | Implemented; see `tests/test_asr.py`. |
| Real Chinese known sentence | Not tested: no user-authorized live sample was provided. |
| Real English known sentence | Not tested: no user-authorized live sample was provided. |
| Model, distance, output, latency | Not available until runtime/model and live samples are prepared. |

### Sources checked 2026-11-18

- [sherpa-onnx Python installation](https://k2-fsa.github.io/sherpa/onnx/python/install.html) documents CPU wheels for Linux `aarch64` and Python package setup.
- [sherpa-onnx Linux installation](https://k2-fsa.github.io/sherpa/onnx/install/linux.html) documents Linux arm64 CPU source builds.
- [sherpa-onnx model index](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/) lists Chinese-English streaming models.
- [Selected Zipformer model card](https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20) identifies the bilingual model and declares Apache-2.0.
