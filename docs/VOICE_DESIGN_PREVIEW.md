# Local VoiceDesign previews

`scripts/voice_design_preview.py` creates two new English voice candidates with the official `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` model: a mature, full-bodied British female voice and a deeper contralto variant. Both request neutral, restrained speech without coyness, breathiness or metallic processing. They are generated voices, not modifications of Serena or another CustomVoice speaker. Accent and timbre remain subject to listening acceptance.

The current speech.cpp deployment does not support VoiceDesign. Run this script in the isolated Python environment; it does not change the resident Astero/VoiceBridge services or download anything. The parent directory's model checkpoint must already be downloaded, including `speech_tokenizer/` files. Reference revision: `5ecdb67327fd37bb2e042aab12ff7391903235d3`.

```sh
/home/zuohang/.local/share/astero/voice-design-venv/bin/python \
  /home/zuohang/workspace/project-astero/scripts/voice_design_preview.py \
  --model-dir /home/zuohang/.local/share/astero/models/voice-design \
  --output-dir /home/zuohang/.local/share/astero/voice-previews/voice-design-take-01 \
  --threads 6
```

Use a new output directory on every run; the script refuses existing directories and any destination inside the repository. Dependencies are `qwen-tts==0.1.1` with CPU PyTorch and its dependencies. CPU float32/SDPA is a compatibility-first choice; generation speed is not a production latency benchmark.

Outputs include two 24 kHz mono WAV files and `manifest.json` recording prompts, seeds, package versions, timing, waveform checks and stopping metadata. The 256-token default bounds generation (roughly 20 seconds of codec frames). The script observes the talker EOS token: outputs without EOS are named `-INCOMPLETE.wav`, recorded as incomplete and produce exit code 3. Successful file generation does not certify the accent, spoken text or desired voice quality. No pitch shifting or electronic effects are applied.

Official workflow: [VoiceDesign](https://github.com/QwenLM/Qwen3-TTS#voice-design), followed by [VoiceDesign then Clone](https://github.com/QwenLM/Qwen3-TTS#voice-design-then-clone) if a selected voice should be reused consistently. This preview script does not install Base, create clone prompts, or switch the production voice.

## First local run — 2026-11-18

The official model and speech tokenizer files were downloaded at the pinned revision and their SHA-256 values checked against the upstream LFS metadata. The local `models/voice-design/astero-provenance.json` records this verification. The isolated environment uses `qwen-tts==0.1.1`, CPU PyTorch `2.15.0+cpu`, and Transformers `4.57.3`.

Outputs: `/home/zuohang/.local/share/astero/voice-previews/20261118/voice-design-british-v1/`.

| Candidate | Audio duration | Generation time | Completion |
| --- | ---: | ---: | --- |
| british_full_female | 5.12 s | 36.90 s | EOS observed |
| british_deep_contralto | 5.60 s | 35.39 s | EOS observed |

Model loading took 3.55 seconds. Both WAV files are mono 24 kHz PCM16, nonempty and finite, with no out-of-range samples before PCM conversion. Local SenseVoice transcribed both as the requested text, apart from punctuation; per-file `.asr.json` files preserve the results. This verifies basic content, not perceived accent or timbre, which still need user listening acceptance. No sound effects were applied. These CPU preview timings are much slower than the existing quantized CustomVoice service; this run is for voice selection and does not establish a production latency solution. Existing resident service configuration was not changed.

## Chinese preview using the selected B voice

`scripts/voice_clone_preview.py` uses the official `Qwen/Qwen3-TTS-12Hz-1.7B-Base` model and the actual `british_deep_contralto.wav` reference to speak three Chinese sentences. It does not create a new VoiceDesign candidate. Reference revision for Base: `fd4b254389122332181a7c3db7f27e918eec64e3`.

```sh
/home/zuohang/.local/share/astero/voice-design-venv/bin/python \
  /home/zuohang/workspace/project-astero/scripts/voice_clone_preview.py \
  --model-dir /home/zuohang/.local/share/astero/models/voice-base \
  --ref-audio /home/zuohang/.local/share/astero/voice-previews/20261118/voice-design-british-v1/british_deep_contralto.wav \
  --output-dir /home/zuohang/.local/share/astero/voice-previews/voice-clone-b-chinese-take-01 \
  --threads 6
```

The reference transcript is fixed to the B sample's original text: “Good evening. I am here to help. Tell me what you need, and we can begin.” Use that recording with this script. A single `create_voice_clone_prompt(..., x_vector_only_mode=False)` extraction is reused for all three sentences. Its audio codes and speaker embedding are also saved as pickle-free `reference_prompt.npz`, with transcript and settings in `manifest.json`. The manifest includes the reference SHA-256, reference duration, per-sentence seeds and generation times, and EOS stopping evidence. WAV validation, fresh-directory enforcement, offline loading, CPU settings and incomplete-output handling match the VoiceDesign preview script. No electronic or pitch-shifting effects are used. Cloning an English reference into Chinese does not guarantee identical perceived timbre or accent; listening acceptance is required.
