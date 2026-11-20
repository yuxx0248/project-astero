import io
import json
from pathlib import Path
from queue import Full
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import wave

from astero.speech.clone_service import (
    CloneHTTPServer, ClonePlaybackService, SynthesizedAudio,
)
from astero.speech.voice_bridge import VoiceBridgeClient, VoiceBridgeConfig


def fake_audio(complete=True):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\x01\x00" * 2400)
    return SynthesizedAudio(buf.getvalue(), {
        "generation_complete": complete, "samples_outside_pcm_range": 0,
    })


class FakeSynthesizer:
    def synthesize(self, text, seed):
        return fake_audio()


class CloneServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.profile = SimpleNamespace(id="cassandra", model_id="test-base",
            model_revision="revision", reference_sha256="b" * 64)
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.close(timeout=3)
        self.temp.cleanup()

    def service(self, factory=FakeSynthesizer, **kwargs):
        kwargs.setdefault("player", lambda _wav: None)
        result = ClonePlaybackService(self.profile, Path(self.temp.name), factory, **kwargs)
        self.services.append(result)
        deadline = time.monotonic() + 3
        while not result.ready and result.error is None and time.monotonic() < deadline:
            time.sleep(.005)
        return result

    def finish(self, service, job):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = service.status(job["id"])
            if status["status"] in {"completed", "failed"}:
                return status
            time.sleep(.005)
        self.fail("job did not finish")

    def test_selected_profile_archive_and_playback(self):
        calls = []
        service = self.service(player=lambda wav: calls.append(wav))
        result = self.finish(service, service.submit({"text": "你好", "voice": "cassandra"}))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["reference_sha256"], self.profile.reference_sha256)
        folder = Path(self.temp.name) / "tts" / result["id"]
        self.assertEqual((folder / "audio.wav").read_bytes(), calls[0])
        manifest = json.loads((folder / "result.json").read_text())
        self.assertEqual(manifest["text"], "你好")
        self.assertEqual(manifest["voice_profile"], "cassandra")
        self.assertEqual(set(result["timings_ms"]), {"queue", "synthesis", "playback", "total"})
        self.assertEqual((folder / "audio.wav").stat().st_mode & 0o777, 0o600)

    def test_unhandled_profile_or_style_is_rejected(self):
        service = self.service()
        for payload in [
            {"text": "hello", "voice": "serena"},
            {"text": "hello", "style": "British"},
            {"text": "hello", "voice": None},
            {"text": "hello", "style": None},
            {"text": "a" * 501}, {"text": " "},
        ]:
            with self.assertRaises(ValueError):
                service.submit(payload)

    def test_truncated_synthesis_is_saved_but_not_played(self):
        calls = []
        synth = SimpleNamespace(synthesize=lambda *_: fake_audio(False))
        service = self.service(factory=lambda: synth, player=lambda wav: calls.append(wav))
        status = self.finish(service, service.submit({"text": "hello"}))
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "synthesis_incomplete")
        self.assertFalse(calls)
        self.assertTrue((Path(self.temp.name) / "tts" / status["id"] / "audio.wav").is_file())

    def test_failed_player_never_reports_completed(self):
        def fail(_wav):
            raise OSError("device disconnected")
        service = self.service(player=fail)
        status = self.finish(service, service.submit({"text": "hello"}))
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "OSError")
        self.assertIn("playback", status["timings_ms"])

    def test_failed_synthesis_retains_duration_and_no_playback(self):
        def fail(*_args):
            raise RuntimeError("provider failure containing private prompt")
        service = self.service(factory=lambda: SimpleNamespace(synthesize=fail))
        status = self.finish(service, service.submit({"text": "hello"}))
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "RuntimeError")
        self.assertIn("synthesis", status["timings_ms"])
        self.assertNotIn("playback", status["timings_ms"])

    def test_queue_is_bounded(self):
        entered, release = threading.Event(), threading.Event()
        def synthesize(*_args):
            entered.set()
            release.wait(3)
            return fake_audio()
        service = self.service(factory=lambda: SimpleNamespace(synthesize=synthesize), queue_size=1)
        try:
            service.submit({"text": "one"})
            self.assertTrue(entered.wait(2))
            service.submit({"text": "two"})
            with self.assertRaises(Full):
                service.submit({"text": "three"})
        finally:
            release.set()

    def test_model_load_failure_visible_in_health(self):
        def fail():
            raise ValueError("wrong model")
        with self.assertLogs(level="ERROR"):
            service = self.service(factory=fail)
        self.assertFalse(service.health()["tts_ready"])
        self.assertEqual(service.health()["error_code"], "ValueError")

    def test_http_contract_matches_existing_client(self):
        service = self.service()
        server = CloneHTTPServer(("127.0.0.1", 0), service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            client = VoiceBridgeClient(VoiceBridgeConfig(base_url=url, voice="cassandra",
                poll_interval_seconds=.01))
            self.assertEqual(client.health()["voice_profile"], "cassandra")
            self.assertEqual(client.speak("hello").status, "completed")
            request = Request(url + "/announce", data=json.dumps({"text": "hi", "voice": "ryan"}).encode())
            with self.assertRaises(HTTPError) as exc:
                urlopen(request)
            self.assertEqual(exc.exception.code, 400)
            exc.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
