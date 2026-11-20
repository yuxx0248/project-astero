#!/usr/bin/env python3
"""Fetch and verify the fixed Base checkpoint required by a repository voice profile."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from astero.speech.voice_profile import DEFAULT_VOICE_PROFILE, load_voice_profile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=DEFAULT_VOICE_PROFILE)
    parser.add_argument("--model-dir", type=Path,
                        default=Path.home() / ".local/share/astero/models/voice-base")
    args = parser.parse_args()
    try:
        profile = load_voice_profile(args.profile)
    except ValueError as exc:
        parser.error(str(exc))
    target = args.model_dir.expanduser().resolve()
    if target == REPO_ROOT or REPO_ROOT in target.parents:
        parser.error("model-dir must be outside the repository")
    # The only non-stdlib dependency is imported after parsing, keeping --help
    # usable before installation and never loading the inference model.
    from huggingface_hub import HfApi, snapshot_download

    os.umask(0o077)
    info = HfApi().model_info(
        profile.model_id, revision=profile.model_revision, files_metadata=True,
    )
    if info.sha != profile.model_revision:
        raise RuntimeError("upstream revision does not match the selected profile")
    patterns = ("*.json", "*.txt", "*.safetensors")
    selected = [f for f in info.siblings
                if any(fnmatch.fnmatch(f.rfilename, pattern) for pattern in patterns)]
    for file in selected:
        path = PurePosixPath(file.rfilename)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError("invalid upstream file path")
    weights = sorted((f for f in selected if f.rfilename.endswith(".safetensors")),
                     key=lambda f: f.rfilename)
    if not weights:
        raise RuntimeError("checkpoint contains no safetensors weights")
    # Require checksums before downloading, rather than saving an unverified
    # checkpoint as successful if upstream metadata is incomplete.
    for file in weights:
        if file.lfs is None or not file.lfs.sha256 or file.size is None:
            raise RuntimeError(f"missing LFS checksum or size: {file.rfilename}")
    target.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=profile.model_id, revision=profile.model_revision,
        local_dir=str(target), allow_patterns=list(patterns), max_workers=3,
    )
    verified = []
    for file in weights:
        path = target / file.rfilename
        if path.stat().st_size != file.size:
            raise RuntimeError(f"checkpoint size mismatch: {file.rfilename}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != file.lfs.sha256:
            raise RuntimeError(f"checkpoint SHA-256 mismatch: {file.rfilename}")
        verified.append({"file": file.rfilename, "size": file.size, "sha256": digest})
        print(f"Verified {file.rfilename}", flush=True)
    provenance = {"repo": profile.model_id, "revision": profile.model_revision,
                  "files": verified}
    # Write the completed verification record atomically. Existing unrelated
    # files and downloaded models are not removed; repeated runs reverify them.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target,
                                     prefix=".provenance-", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(provenance, stream, indent=2)
        stream.write("\n")
    try:
        temporary.replace(target / "astero-provenance.json")
    finally:
        temporary.unlink(missing_ok=True)
    print(str(target), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
