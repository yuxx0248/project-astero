from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import wave

from astero.speech import voice_profile
from voice_bridge import voice_profile as shared_voice_profile


class VoiceProfileTests(unittest.TestCase):
    @contextmanager
    def copied_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / voice_profile.DEFAULT_VOICE_PROFILE
            shutil.copytree(shared_voice_profile._PROFILE_ROOT / voice_profile.DEFAULT_VOICE_PROFILE, folder)
            with patch.object(shared_voice_profile, "_PROFILE_ROOT", root):
                yield folder

    def test_selected_reference_is_repository_asset_independent_of_cwd(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                profile = voice_profile.load_voice_profile()
            finally:
                os.chdir(previous)
        self.assertEqual(profile.id, "cassandra")
        self.assertTrue(profile.reference_audio.is_absolute())
        self.assertEqual(profile.reference_sha256, "50f35d5ac743544fe79ff3b08bc995f5157eb770f8a4af92f2fb0993147fab7a")
        self.assertEqual(profile.reference_text, "Good evening. I am here to help. Tell me what you need, and we can begin.")
        self.assertTrue(profile.provisional)

    def test_unknown_and_path_like_ids_cannot_select_external_assets(self):
        for value in ("serena", "../cassandra", "/tmp", "", None, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                voice_profile.load_voice_profile(value)

    def test_corrupt_reference_is_rejected_instead_of_changing_voice(self):
        with self.copied_profile() as folder:
            audio = folder / "reference.wav"
            audio.write_bytes(audio.read_bytes()[:-1] + b"\xff")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                voice_profile.load_voice_profile()

    def test_hash_matching_truncated_wav_is_still_rejected(self):
        with self.copied_profile() as folder:
            audio = folder / "reference.wav"
            audio.write_bytes(audio.read_bytes()[:-200])
            metadata = json.loads((folder / "profile.json").read_text())
            metadata["reference"]["sha256"] = hashlib.sha256(audio.read_bytes()).hexdigest()
            (folder / "profile.json").write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError, "truncated"):
                voice_profile.load_voice_profile()

    def test_hash_matching_wrong_audio_format_is_rejected(self):
        with self.copied_profile() as folder:
            audio = folder / "reference.wav"
            with wave.open(str(audio), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\x01\x00" * 16000)
            metadata = json.loads((folder / "profile.json").read_text())
            metadata["reference"]["sha256"] = hashlib.sha256(audio.read_bytes()).hexdigest()
            (folder / "profile.json").write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError, "24 kHz"):
                voice_profile.load_voice_profile()

    def test_invalid_metadata_is_rejected(self):
        for change in ({"type": "custom_voice"}, {"id": "elsewhere"}, {"accepted": False}, {"schema_version": True}):
            with self.subTest(change=change), self.copied_profile() as folder:
                metadata = json.loads((folder / "profile.json").read_text())
                metadata.update(change)
                (folder / "profile.json").write_text(json.dumps(metadata))
                with self.assertRaises(ValueError):
                    voice_profile.load_voice_profile()

    def test_profile_cannot_redirect_reference_file(self):
        with self.copied_profile() as folder:
            metadata = json.loads((folder / "profile.json").read_text())
            metadata["reference"]["file"] = "../../other.wav"
            (folder / "profile.json").write_text(json.dumps(metadata))
            with self.assertRaises(ValueError):
                voice_profile.load_voice_profile()


if __name__ == "__main__":
    unittest.main()
