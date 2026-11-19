import io
import itertools
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

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

    def test_close_waits_for_other_reader_to_drain_buffered_tail(self):
        exited = threading.Event()
        first_read = threading.Event()
        class DelayedTailStream(io.BytesIO):
            def read(self, count=-1):
                if self.tell() >= 160 * 27:
                    if not exited.wait(1):
                        raise TimeoutError("process never exited")
                    time.sleep(0.02)
                return super().read(count)
        class Process(_FakeProcess):
            def __init__(self):
                super().__init__(b"", exit_code=None)
                self.stdout = DelayedTailStream(b'a' * (160 * 27) + b'b' * (96 * 27))
                self.stdin = io.BytesIO()
            def wait(self, timeout=None):
                self.exit_code = 0
                exited.set()
                return 0
        capture = AdbCapture("unused", "0" * 64)
        proc = Process()
        capture._proc = proc
        frames, errors = [], []
        def consume():
            try:
                for frame in capture.frames():
                    frames.append(frame)
                    first_read.set()
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=consume)
        thread.start()
        self.assertTrue(first_read.wait(1))
        start = time.monotonic()
        capture.close()
        thread.join(timeout=1)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([f.sample_count for f in frames], [160, 96])
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(capture._reader_done.is_set())

    def test_same_thread_or_unused_generator_does_not_wait_for_itself(self):
        for started in (True, False):
            with self.subTest(started=started):
                capture = AdbCapture("unused", "0" * 64, frame_samples=2)
                capture._proc = _FakeProcess(b'a' * 54)
                frames = capture.frames()
                if started:
                    next(frames)
                start = time.monotonic()
                capture.close()
                self.assertLess(time.monotonic() - start, 0.5)
                frames.close()
                self.assertTrue(capture._reader_done.is_set())

    def test_stalled_other_reader_is_reported_after_bounded_wait(self):
        capture = AdbCapture("unused", "0" * 64)
        capture._proc = _FakeProcess(b"")
        capture._reader_thread_id = -1
        capture._reader_done = Mock()
        capture._reader_done.wait.return_value = False
        with self.assertRaisesRegex(CaptureError, "reader did not finish"):
            capture.close()
        capture._reader_done.wait.assert_called_once_with(timeout=2)
        self.assertTrue(capture._proc.stdout.closed)

    def test_requested_close_retains_short_sample_aligned_tail(self):
        capture = AdbCapture("unused", "0" * 64, frame_samples=160)
        capture._proc = _FakeProcess(b'a' * (160 * 27) + b'b' * (96 * 27))
        frames = capture.frames()
        first = next(frames)
        capture._closed = True  # requested while reader is already active
        tail = list(frames)
        self.assertEqual(first.sample_count, 160)
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0].sample_count, 96)
        self.assertEqual(tail[0].sequence, 2)
        self.assertEqual(tail[0].pcm, b'b' * (96 * 27))

    def test_requested_close_does_not_hide_misaligned_tail(self):
        capture = AdbCapture("unused", "0" * 64, frame_samples=2)
        capture._proc = _FakeProcess(b'a' * 54 + b'b' * 26)
        frames = capture.frames()
        next(frames)
        capture._closed = True
        with self.assertRaisesRegex(CaptureError, "mid-frame"):
            list(frames)

    def test_unexpected_eof_with_aligned_tail_still_fails(self):
        capture = AdbCapture("unused", "0" * 64, frame_samples=2)
        capture._proc = _FakeProcess(b'a' * 27)
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
        self.assertEqual(launches[0][1:3], ['shell', '-T'])

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
