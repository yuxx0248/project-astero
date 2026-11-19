import threading
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from pathlib import Path

from astero.archive import ArchiveReader, ArchiveWriter
from astero.audio.capture import AudioFrame
from astero.observability import TraceContext, TraceRecorder
from astero.service.local_voice import LocalVoiceConfig, LocalVoiceTurnExecutor, _ArchivedCapturePump, _TraceASR
from astero.orchestrator import TurnResult, TurnStatus


def _s24(value):
    if value < 0:
        value += 1 << 24
    return bytes((value & 255, (value >> 8) & 255, (value >> 16) & 255))


class FakeCapture:
    def __init__(self, frames):
        self._frames = frames
        self.closed = False

    def frames(self):
        yield from self._frames

    def close(self):
        self.closed = True


class LocalVoiceRuntimeTests(unittest.TestCase):
    def test_asr_releases_frame_delivery_before_model_work(self):
        pump = Mock()
        asr = Mock()
        asr.recognize.side_effect = lambda frame: (
            self.assertTrue(pump.pause_processing.called) or SimpleNamespace(text="test"))
        traced = _TraceASR(asr, TraceRecorder(sink=lambda e: None),
                           TraceContext("trace", "turn", "s"), lambda *a: None,
                           capture_pump=pump)
        traced.recognize(object())
        pump.pause_processing.assert_called_once()

    def test_playback_success_does_not_mask_incomplete_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            voice = LocalVoiceTurnExecutor.__new__(LocalVoiceTurnExecutor)
            voice.config = LocalVoiceConfig(data_root=Path(temp))
            voice.helper_sha256 = "0" * 64
            voice.store = Mock()
            voice.asr = Mock()
            voice.wake = Mock()
            voice.bridge = Mock()
            voice._cancel = threading.Event()
            request = SimpleNamespace(turn_id="turn", trace_id="trace", session_id="s")
            pump = Mock(_close_error=None, _error=OSError("private error detail"))
            with patch('astero.service.local_voice.AdbCapture'), \
                 patch('astero.service.local_voice._ArchivedCapturePump', return_value=pump), \
                 patch('astero.service.local_voice.SileroVAD'), \
                 patch('astero.service.local_voice.AgentService'), \
                 patch('astero.service.local_voice.Orchestrator') as orchestrator:
                orchestrator.return_value.run_once.return_value = TurnResult(TurnStatus.COMPLETED)
                result = voice._run_locked(request)
            self.assertEqual(result.status, "failed")
            self.assertFalse(result.archive_complete)
            self.assertIsNotNone(result.error_code)
            self.assertNotIn("private", result.error_code)
            self.assertTrue(any(call.args[4:6] == ('capture_archive', 'failed')
                                for call in voice.store.append_stage_event.call_args_list))

    def test_close_drains_helper_output_before_waiting_for_exit(self):
        class HandshakeCapture:
            def __init__(self):
                self.quit_requested = threading.Event()
                self.drained = threading.Event()
                self.forced = False
            def frames(self):
                yield AudioFrame(1, 16000, 9, "s24_3le", 1, bytes(27 * 10))
                self.quit_requested.wait(2)
                # The helper cannot acknowledge q until outstanding stdout
                # writes drain; model that dependency without real hardware.
                for seq in range(2, 66):
                    yield AudioFrame(seq, 16000, 9, "s24_3le", seq, bytes(27 * 10))
                self.drained.set()
            def close(self):
                self.quit_requested.set()
                self.forced = not self.drained.wait(0.5)
        with tempfile.TemporaryDirectory() as temp:
            writer = ArchiveWriter(Path(temp))
            capture_id = writer.begin_session({"purpose": "test"})
            capture = HandshakeCapture()
            pump = _ArchivedCapturePump(
                capture, writer, capture_id, "turn1", LocalVoiceConfig(),
                TraceContext("trace1", "turn1", "s", capture_id), TraceRecorder(sink=lambda event: None),
            )
            iterator = iter(pump)
            next(iterator)
            started = time.monotonic()
            pump.close()
            self.assertFalse(capture.forced)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertIsNone(pump._error)
            self.assertIsNone(pump._close_error)
            self.assertEqual(list(iterator), [])
            writer.close_session(capture_id, outcome="clean")
            reader = ArchiveReader(Path(temp))
            self.assertTrue(reader.read_manifest(capture_id)["complete"])
            self.assertEqual(len(tuple(reader.iter_frames(capture_id))), 65)

    def test_requested_close_suppresses_source_io_but_not_archive_failure(self):
        for archive_failure in (False, True):
            with self.subTest(archive_failure=archive_failure), tempfile.TemporaryDirectory() as temp:
                class ClosingCapture:
                    def __init__(self):
                        self.closing = threading.Event()
                    def frames(self):
                        yield AudioFrame(1, 16000, 9, "s24_3le", 1, bytes(270))
                        self.closing.wait(2)
                        if archive_failure:
                            yield AudioFrame(2, 16000, 9, "s24_3le", 2, bytes(270))
                        raise OSError("pipe closed")
                    def close(self):
                        self.closing.set()
                writer = ArchiveWriter(Path(temp))
                capture_id = writer.begin_session({"purpose": "test"})
                original_append = writer.append_frame
                def append(cap, frame):
                    if archive_failure and frame.sequence == 2:
                        raise OSError("archive write failed")
                    return original_append(cap, frame)
                writer.append_frame = append
                pump = _ArchivedCapturePump(
                    ClosingCapture(), writer, capture_id, "turn1", LocalVoiceConfig(),
                    TraceContext("trace1", "turn1", "s", capture_id), TraceRecorder(sink=lambda event: None),
                )
                next(iter(pump))
                pump.close()
                if archive_failure:
                    self.assertIsInstance(pump._error, OSError)
                    self.assertEqual(str(pump._error), "archive write failed")
                else:
                    self.assertIsNone(pump._error)
                writer.close_session(capture_id, outcome="failed" if archive_failure else "clean")
                self.assertEqual(ArchiveReader(Path(temp)).read_manifest(capture_id)["complete"],
                                 not archive_failure)

    def test_stop_unblocks_capture_and_preserves_received_frames(self):
        class BlockingCapture:
            def __init__(self):
                self.closed = threading.Event()
            def frames(self):
                yield AudioFrame(1, 16000, 9, "s24_3le", 1, bytes(27 * 10))
                self.closed.wait(5)
            def close(self):
                self.closed.set()
        with tempfile.TemporaryDirectory() as temp:
            writer = ArchiveWriter(Path(temp))
            capture_id = writer.begin_session({"purpose": "test"})
            capture = BlockingCapture()
            pump = _ArchivedCapturePump(
                capture, writer, capture_id, "turn1", LocalVoiceConfig(),
                TraceContext("trace1", "turn1", "s", capture_id), TraceRecorder(sink=lambda event: None),
            )
            iterator = iter(pump)
            next(iterator)
            started = time.monotonic()
            pump.close()
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(list(iterator), [])
            writer.close_session(capture_id, outcome="clean")
            reader = ArchiveReader(Path(temp))
            self.assertTrue(reader.read_manifest(capture_id)["complete"])
            self.assertEqual(len(tuple(reader.iter_frames(capture_id))), 1)

    def test_capture_pump_archives_every_raw_frame_when_processing_pauses(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "archives"
            writer = ArchiveWriter(root)
            capture_id = writer.begin_session({"purpose": "test"})
            raws = []
            for sequence in range(1, 31):
                payload = b"".join(
                    _s24(sequence * 100 + channel)
                    for _sample in range(10) for channel in range(9)
                )
                raws.append(AudioFrame(sequence * 10_000_000, 16_000, 9, "s24_3le",
                                       sequence, payload))
            capture = FakeCapture(raws)
            events = []
            recorder = TraceRecorder(sink=events.append)
            pump = _ArchivedCapturePump(
                capture, writer, capture_id, "turn1",
                LocalVoiceConfig(data_root=Path(temp), channel=0, gain=1),
                TraceContext("trace1", "turn1", "session1", capture_id), recorder,
            )
            iterator = iter(pump)
            next(iterator)
            next(iterator)
            pump.pause_processing()
            deadline = time.monotonic() + 2
            while not pump._done.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            pump.close()
            writer.close_session(capture_id, outcome="clean")

            restored = tuple(ArchiveReader(root).iter_frames(capture_id))
            self.assertEqual(len(restored), len(raws))
            self.assertEqual([frame.sequence for frame in restored], list(range(1, 31)))
            self.assertEqual([frame.pcm for frame in restored], [frame.pcm for frame in raws])
            derived = tuple(ArchiveReader(root).iter_mono(capture_id, "turn1"))
            self.assertEqual(len(derived), len(raws))
            self.assertTrue(capture.closed)
            self.assertTrue(any(event.stage == "capture_frame_batch" for event in events))


if __name__ == "__main__":
    unittest.main()
