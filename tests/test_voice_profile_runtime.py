import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from astero.service.local_voice import LocalVoiceConfig, LocalVoiceTurnExecutor
from astero.service.runtime import LocalServiceConfig, main
from astero.speech.voice_bridge import (
    VoiceBridgeClient, VoiceBridgeConfig, VoiceBridgeUnavailableError,
)


class Response(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode())
        self.status = status


class VoiceProfileRuntimeTests(unittest.TestCase):
    def test_default_profile_selects_clone_endpoint_and_legacy_is_explicit(self):
        for cls in (LocalServiceConfig, LocalVoiceConfig):
            config = cls()
            self.assertEqual(config.voice_profile, "cassandra")
            self.assertEqual(config.voice_bridge_url, "http://127.0.0.1:8767")
            legacy = cls(voice_profile="bridge-default")
            self.assertEqual(legacy.voice_bridge_url, "http://127.0.0.1:8765")
            override = cls(voice_bridge_url="http://127.0.0.1:9876")
            self.assertEqual(override.voice_bridge_url, "http://127.0.0.1:9876")
            with self.assertRaises(ValueError):
                cls(voice_profile="serena")

    def test_cli_default_and_explicit_legacy_profile(self):
        for args, expected in (([], "cassandra"),
                               (["--voice-profile", "bridge-default"], "bridge-default")):
            with patch("astero.service.runtime.LocalService") as service:
                self.assertEqual(main(args), 0)
                self.assertEqual(service.call_args.args[0].voice_profile, expected)
                service.return_value.serve_forever.assert_called_once()

    def test_runtime_configures_explicit_profile_and_readiness_fails_on_mismatch(self):
        with patch.object(LocalVoiceTurnExecutor, "_load_models"):
            voice = LocalVoiceTurnExecutor(Mock())
        self.assertEqual(voice.bridge.config.voice, "cassandra")
        self.assertEqual(voice.bridge.config.expected_voice_profile, "cassandra")
        self.assertEqual(voice.bridge.config.timeout_seconds, 300)
        voice.wake = voice.asr = object()
        voice.bridge = Mock()
        for profile, expected in ((None, False), ("serena", False), ("cassandra", True)):
            voice.bridge.health.return_value = {
                "status": "ok", "tts_ready": True, "voice_profile": profile,
            }
            result = voice.readiness()
            self.assertEqual(result["ready"], expected)
            self.assertEqual(result["voice_profile"], "cassandra")
            self.assertEqual(result["dependencies"]["tts"] == "ready", expected)

    def test_not_ready_backend_prevents_capture(self):
        with patch.object(LocalVoiceTurnExecutor, "_load_models"):
            voice = LocalVoiceTurnExecutor(Mock(), bridge=Mock())
        voice.wake = voice.asr = object()
        voice.bridge.health.return_value = {"status": "ok", "tts_ready": True}
        with patch.object(voice, "_run_locked") as capture:
            result = voice.run_turn(SimpleNamespace())
        self.assertEqual(result.error, "voice_runtime_not_ready")
        capture.assert_not_called()

    def test_profile_client_checks_backend_then_submits_explicit_voice_without_style(self):
        requests = []
        def opener(request, **kwargs):
            requests.append((request.full_url, request.data))
            if request.full_url.endswith("/health"):
                return Response(200, {"status": "ok", "tts_ready": True,
                                      "voice_profile": "cassandra"})
            return Response(202, {"id": "task1", "status": "queued"})
        client = VoiceBridgeClient(VoiceBridgeConfig(
            voice="cassandra", expected_voice_profile="cassandra"), opener=opener)
        self.assertEqual(client.announce("你好"), "task1")
        self.assertTrue(requests[0][0].endswith("/health"))
        self.assertEqual(json.loads(requests[1][1]), {"text": "你好", "voice": "cassandra"})
        with self.assertRaises(ValueError):
            client.announce("你好", voice="vivian")

    def test_misconfigured_endpoint_never_receives_announcement(self):
        requests = []
        def opener(request, **kwargs):
            requests.append(request.full_url)
            return Response(200, {"status": "ok", "tts_ready": True})
        client = VoiceBridgeClient(VoiceBridgeConfig(
            voice="cassandra", expected_voice_profile="cassandra"), opener=opener)
        with self.assertRaises(VoiceBridgeUnavailableError):
            client.announce("你好")
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0].endswith("/health"))


if __name__ == "__main__":
    unittest.main()
