#!/usr/bin/env python3
"""Preview three Chinese sentences using the selected B reference voice."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import time

from voice_design_preview import emit


MODEL_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
MODEL_REVISION = "fd4b254389122332181a7c3db7f27e918eec64e3"
REF_TEXT = "Good evening. I am here to help. Tell me what you need, and we can begin."
SENTENCES = (
    ("01_greeting", 241120, "你好，我在。有什么需要我帮忙的，直接告诉我就好。"),
    ("02_reassurance", 241121, "我们先把问题弄清楚，再一步一步处理，不用着急。"),
    ("03_voice_check", 241122, "声音已经准备好了。你可以听一听，这样的语气和厚度是否合适。"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--ref-audio", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    args = parser.parse_args()
    if args.threads < 1 or args.max_new_tokens < 2:
        parser.error("threads must be positive and max-new-tokens must be at least 2")
    model_dir = args.model_dir.expanduser().resolve()
    ref_audio = args.ref_audio.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not (model_dir / "config.json").is_file():
        parser.error("model-dir must contain the downloaded Base model")
    if json.loads((model_dir / "config.json").read_text()).get("tts_model_type") != "base":
        parser.error("model-dir must be a Base model, not CustomVoice or VoiceDesign")
    if not ref_audio.is_file():
        parser.error("ref-audio must be the local selected B reference WAV")
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
    import numpy as np
    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel

    reference, reference_rate = sf.read(ref_audio, dtype="float32")
    if (reference.ndim != 1 or not reference.size or not np.isfinite(reference).all()
            or not np.any(reference)):
        parser.error("reference must be nonempty finite mono audio")
    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "model_id": MODEL_ID, "expected_model_revision": MODEL_REVISION,
        "model_dir": str(model_dir),
        "voice_source": "clone_of_selected_B_british_deep_contralto_reference",
        "ref_audio": str(ref_audio),
        "ref_sha256": hashlib.sha256(ref_audio.read_bytes()).hexdigest(),
        "ref_text": REF_TEXT, "ref_sample_rate": reference_rate,
        "ref_duration_seconds": int(reference.size) / reference_rate,
        "x_vector_only_mode": False, "prompt_extractions": 0,
        "device": "cpu", "dtype": "float32", "attention": "sdpa",
        "threads": args.threads, "max_new_tokens": args.max_new_tokens,
        "qwen_tts_version": importlib.metadata.version("qwen-tts"),
        "torch_version": torch.__version__,
        "language": "Chinese", "non_streaming_mode": True,
        "sentences": [{"id": key, "seed": seed, "text": text}
                      for key, seed, text in SENTENCES],
        "postprocessing": None, "status": "loading", "results": [],
        "notes": "Reference B is cloned, not newly designed. Timbre, accent and content require listening acceptance.",
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
        manifest["status"] = "extracting_reference"
        save()
        emit("model_load_completed", seconds=manifest["model_load_seconds"])
        emit("reference_extraction_started", ref_audio=str(ref_audio))
        started = time.monotonic()
        prompt_items = model.create_voice_clone_prompt(
            ref_audio=str(ref_audio), ref_text=REF_TEXT, x_vector_only_mode=False,
        )
        if len(prompt_items) != 1:
            raise ValueError("expected exactly one cached reference prompt")
        manifest["reference_extraction_seconds"] = time.monotonic() - started
        manifest["prompt_extractions"] = 1
        # Reuse this same in-memory prompt in every call; also preserve its
        # tensor data in a pickle-free cache for later reproducibility.
        item = prompt_items[0]
        if item.ref_code is None or item.x_vector_only_mode or not item.icl_mode:
            raise ValueError("reference prompt must contain audio codes in ICL mode")
        np.savez_compressed(
            output_dir / "reference_prompt.npz",
            ref_code=item.ref_code.detach().cpu().numpy(),
            ref_spk_embedding=item.ref_spk_embedding.detach().cpu().numpy(),
        )
        manifest["reference_prompt_cache"] = "reference_prompt.npz"
        manifest["status"] = "generating"
        save()
        emit("reference_extraction_completed", seconds=manifest["reference_extraction_seconds"])

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
        for key, seed, text in SENTENCES:
            stop_metadata.clear()
            torch.manual_seed(seed)
            np.random.seed(seed)
            emit("preview_started", sentence=key, seed=seed)
            started = time.monotonic()
            wavs, sample_rate = model.generate_voice_clone(
                text=text, language="Chinese", voice_clone_prompt=prompt_items,
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
            output = output_dir / (key + ("" if complete else "-INCOMPLETE") + ".wav")
            sf.write(output, audio, sample_rate, subtype="PCM_16")
            result = {
                "sentence": key, "text": text, "seed": seed, "path": str(output),
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
