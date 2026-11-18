import io
import itertools
import hashlib
from pathlib import Path
import tempfile
import unittest

from astero.audio.capture import (
    AdbCapture,
    AudioFrame,
    CaptureDisconnected,
    CaptureError,
    SyntheticCapture,
)


class _FakeProcess:
    def __init__(self, data: bytes, *, exit_code=0):
        self.stdout = io.BytesIO(data)
        self.stderr = io.BytesIO(b"")
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.exit_code = -15

    def wait(self, timeout=None):
        return self.exit_code


class CaptureFrameTests(unittest.TestCase):
    def test_synthetic_source_preserves_payload_and_metadata_order(self):
        frame_size = 4 * 9 * 3
        payloads = [bytes([0x11]) * frame_size, bytes([0x22]) * frame_size]
        frames = list(SyntheticCapture(iter(payloads), frame_samples=4).frames())

        self.assertEqual([f.sequence for f in frames], [1, 2])
        self.assertEqual([f.pcm for f in frames], payloads)
        for frame in frames:
            self.assertIsInstance(frame, AudioFrame)
            self.assertEqual(frame.sample_rate, 16000)
            self.assertEqual(frame.channels, 9)
            self.assertEqual(frame.sample_format, "s24_3le")
            self.assertEqual(frame.sample_count, 4)
            self.assertGreater(frame.timestamp_ns, 0)

    def test_synthetic_source_rejects_incomplete_frame(self):
        with self.assertRaisesRegex(CaptureError, "expected 108"):
            list(SyntheticCapture(iter([b"x" * 107]), frame_samples=4).frames())

    def test_adb_stream_splits_exact_frames_and_sequences(self):
        capture = AdbCapture("unused", "0" * 64, frame_samples=2)
        payload = b"a" * (2 * 9 * 3) + b"b" * (2 * 9 * 3)
        capture._proc = _FakeProcess(payload)
        frames = list(itertools.islice(capture.frames(), 2))
        self.assertEqual([frame.sequence for frame in frames], [1, 2])
        self.assertEqual([frame.pcm[0] for frame in frames], [ord("a"), ord("b")])

    def test_adb_stream_reports_mid_frame_disconnect(self):
        capture = AdbCapture("unused", "0" * 64, frame_samples=2)
        capture._proc = _FakeProcess(b"short")
        with self.assertRaisesRegex(CaptureError, "mid-frame"):
            list(capture.frames())

    def test_adb_stream_reports_clean_unexpected_disconnect(self):
        capture = AdbCapture("unused", "0" * 64, frame_samples=2)
        capture._proc = _FakeProcess(b"")
        with self.assertRaises(CaptureDisconnected):
            list(capture.frames())

    def test_context_restores_running_mixer_after_exception(self):
        class Runner:
            state = "running"
            removed = False

            def __call__(self, command, **kwargs):
                text = " ".join(command)
                output = ""
                if "id -u" in text:
                    output = "0\n"
                elif "getprop init.svc.mixer" in text:
                    output = self.state + "\n"
                elif "setprop ctl.stop mixer" in text:
                    self.state = "stopped"
                elif "setprop ctl.start mixer" in text:
                    self.state = "running"
                elif "rm -f" in text:
                    self.removed = True
                return type("Result", (), {"returncode": 0, "stdout": output, "stderr": ""})()

        runner = Runner()
        payload = b"helper source placeholder"
        proc = _FakeProcess(b"")
        launches = []
        with tempfile.TemporaryDirectory() as tempdir:
            helper = Path(tempdir) / "helper"
            helper.write_bytes(payload)
            capture = AdbCapture(
                helper, hashlib.sha256(payload).hexdigest(), runner=runner,
                popen=lambda *args, **kwargs: (launches.append(args[0]) or proc),
            )
            with self.assertRaisesRegex(RuntimeError, "simulated consumer failure"):
                with capture:
                    raise RuntimeError("simulated consumer failure")
        self.assertEqual(runner.state, "running")
        self.assertTrue(runner.removed)
        self.assertIn('2>/dev/null', launches[0][-1])

    def test_context_adds_mixer_restore_failure_to_original_exception(self):
        class Runner:
            state = "running"

            def __call__(self, command, **kwargs):
                text = " ".join(command)
                output = ""
                returncode = 0
                if "id -u" in text:
                    output = "0\n"
                elif "getprop init.svc.mixer" in text:
                    output = self.state + "\n"
                elif "setprop ctl.stop mixer" in text:
                    self.state = "stopped"
                elif "setprop ctl.start mixer" in text:
                    returncode = 1
                return type("Result", (), {
                    "returncode": returncode,
                    "stdout": output,
                    "stderr": "simulated init failure" if returncode else "",
                })()

        runner = Runner()
        payload = b"helper source placeholder"
        with tempfile.TemporaryDirectory() as tempdir:
            helper = Path(tempdir) / "helper"
            helper.write_bytes(payload)
            capture = AdbCapture(
                helper, hashlib.sha256(payload).hexdigest(), runner=runner,
                popen=lambda *args, **kwargs: _FakeProcess(b""),
            )
            with self.assertRaisesRegex(RuntimeError, "original consumer error") as raised:
                with capture:
                    raise RuntimeError("original consumer error")

        self.assertEqual(runner.state, "stopped")
        self.assertTrue(any(
            "mixer restoration failed" in note and "simulated init failure" in note
            for note in raised.exception.__notes__
        ))


if __name__ == "__main__":
    unittest.main()
