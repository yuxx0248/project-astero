#!/usr/bin/env python3
"""Generate two offline VoiceDesign previews without touching resident services."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import time


MODEL_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
MODEL_REVISION = "5ecdb67327fd37bb2e042aab12ff7391903235d3"
TEXT = "Good evening. I am here to help. Tell me what you need, and we can begin."
PROFILES = (
    {
        "id": "british_full_female",
        "seed": 241118,
        "instruction": (
            "A mature British woman with a full-bodied, rounded low female voice and "
            "natural chest resonance. Contemporary standard Southern British English, "
            "non-rhotic pronunciation. Neutral, calm, grounded and understated, with "
            "clear articulation, moderate pace and short natural pauses. Even intonation "
            "and composed sentence endings. A solid, natural female vocal tone: no "
            "sweetness, coyness, whispering or breathiness. No theatrical announcer "
            "delivery, robotic or metallic effects."
        ),
    },
    {
        "id": "british_deep_contralto",
        "seed": 241119,
        "instruction": (
            "A mature British female contralto, with a distinctly deep, thick, warm and "
            "resonant female timbre. Rich chest resonance, rounded vowels and a solid "
            "low register, without forcing the throat or impersonating a man. Natural "
            "contemporary standard Southern British English with non-rhotic pronunciation. "
            "Calm, emotionally restrained and matter-of-fact, clear consonants, steady "
            "moderate pace and short pauses. Restrained pitch variation and settled "
            "sentence endings. No sugary, girlish, coy or breathy delivery, vocal fry, "
            "theatrical narration, robotic processing or metallic sound."
        ),
    },
)


def emit(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    args = parser.parse_args()
    if args.threads < 1 or args.max_new_tokens < 2:
        parser.error("threads must be positive and max-new-tokens must be at least 2")
    model_dir = args.model_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not (model_dir / "config.json").is_file():
        parser.error("model-dir must contain the downloaded VoiceDesign model")
    config = json.loads((model_dir / "config.json").read_text())
    if config.get("tts_model_type") != "voice_design":
        parser.error("model-dir must be a VoiceDesign model, not CustomVoice or Base")
    repo_dir = Path(__file__).resolve().parents[1]
    if output_dir == repo_dir or repo_dir in output_dir.parents:
        parser.error("output-dir must be outside the repository")
    if output_dir.exists():
        parser.error("output-dir already exists; choose a fresh directory")
    os.umask(0o077)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["OMP_NUM_THREADS"] = str(args.threads)
    # Keep optional inference dependencies out of --help and syntax-only checks.
    import numpy as np
    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel

    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "model_id": MODEL_ID,
        "expected_model_revision": MODEL_REVISION,
        "model_dir": str(model_dir),
        "device": "cpu", "dtype": "float32", "attention": "sdpa",
        "threads": args.threads, "max_new_tokens": args.max_new_tokens,
        "qwen_tts_version": importlib.metadata.version("qwen-tts"),
        "torch_version": torch.__version__,
        "text": TEXT, "language": "English", "profiles": PROFILES,
        "status": "loading", "results": [],
        "notes": "No audio effects. Accent, timbre and content need listening acceptance.",
    }

    def save() -> None:
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        )

    save()
    emit("model_load_started", model_dir=str(model_dir))
    started = time.monotonic()
    try:
        model = Qwen3TTSModel.from_pretrained(
            str(model_dir), device_map="cpu", dtype=torch.float32,
            attn_implementation="sdpa", local_files_only=True,
        )
        manifest["model_load_seconds"] = time.monotonic() - started
        manifest["status"] = "generating"
        save()
        emit("model_load_completed", seconds=manifest["model_load_seconds"])

        # qwen-tts 0.1.1 hides stopping metadata in its high-level API. Observe
        # the returned talker sequence, without changing its generation policy.
        original_generate = model.model.talker.generate
        stop_metadata: dict = {}

        def observed_generate(*positional, **kwargs):
            result = original_generate(*positional, **kwargs)
            sequence = result.sequences[0]
            eos = kwargs["eos_token_id"]
            eos_ids = set(eos if isinstance(eos, (list, tuple)) else [eos])
            stop_metadata.update(
                eos_observed=int(sequence[-1].item()) in eos_ids,
                sequence_tokens=int(sequence.numel()),
                generation_steps=len(result.hidden_states),
            )
            return result

        model.model.talker.generate = observed_generate
        for profile in PROFILES:
            stop_metadata.clear()
            torch.manual_seed(profile["seed"])
            np.random.seed(profile["seed"])
            emit("preview_started", profile=profile["id"], seed=profile["seed"])
            started = time.monotonic()
            wavs, sample_rate = model.generate_voice_design(
                text=TEXT, language="English", instruct=profile["instruction"],
                non_streaming_mode=True, max_new_tokens=args.max_new_tokens,
            )
            elapsed = time.monotonic() - started
            if len(wavs) != 1:
                raise ValueError("expected one waveform")
            audio = np.asarray(wavs[0])
            if (sample_rate != 24000 or audio.ndim != 1 or not audio.size
                    or not np.isfinite(audio).all() or not np.any(audio)):
                raise ValueError("invalid waveform: expected finite nonempty mono 24 kHz audio")
            complete = bool(stop_metadata.get("eos_observed"))
            name = profile["id"] + ("" if complete else "-INCOMPLETE") + ".wav"
            output = output_dir / name
            sf.write(output, audio, sample_rate, subtype="PCM_16")
            result = {
                "profile": profile["id"], "path": str(output),
                "generation_seconds": elapsed,
                "duration_seconds": int(audio.size) / sample_rate,
                "sample_rate": sample_rate, "samples": int(audio.size),
                "peak_before_pcm": float(np.max(np.abs(audio))),
                "samples_outside_pcm_range": int(np.sum(np.abs(audio) > 1)),
                "generation_complete": complete,
                "stop_reason": "eos" if complete else "no_eos_possible_token_limit",
                "listening_acceptance": "pending", **stop_metadata,
            }
            manifest["results"].append(result)
            save()
            emit("preview_completed", **result)
        manifest["status"] = (
            "generated" if all(r["generation_complete"] for r in manifest["results"])
            else "incomplete"
        )
        save()
        return 0 if manifest["status"] == "generated" else 3
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error_type"] = type(exc).__name__
        manifest["error"] = str(exc)
        save()
        raise


if __name__ == "__main__":
    raise SystemExit(main())
