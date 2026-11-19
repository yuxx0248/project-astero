from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from astero.archive import (
    ArchiveReader,
    ArchiveRecordError,
    ArchiveSessionExistsError,
    ArchiveValidationError,
    ArchiveWriter,
)
from astero.audio.capture import AudioFrame
from astero.audio.pcm import MonoFrame


def raw_frame(sequence: int, pcm: bytes | None = None) -> AudioFrame:
    # One 9-channel s24_3le sample. This is deliberately small so exact byte
    # preservation is easy to inspect while retaining the real device format.
    return AudioFrame(
        timestamp_ns=1_000_000_000 + sequence * 10_000_000,
        sample_rate=16_000,
        channels=9,
        sample_format="s24_3le",
        sequence=sequence,
        pcm=pcm if pcm is not None else bytes([sequence & 255]) * 27,
    )


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "archives"
        self.writer = ArchiveWriter(self.root)
        self.reader = ArchiveReader(self.root)

    def tearDown(self):
        # Keep simulated interrupted manifests incomplete, but close test file
        # descriptors so resource warnings do not hide failures in the suite.
        for capture_id, session in list(self.writer._sessions.items()):
            if not session.closed:
                self.writer.close_session(capture_id, outcome="test_teardown")
        self.temp.cleanup()

    def test_raw_frames_round_trip_losslessly_and_in_order(self):
        capture_id = self.writer.begin_session({"device": "echo-dot-test"}, "capture_1")
        frames = [raw_frame(1, bytes(range(27))), raw_frame(2, bytes(reversed(range(27))))]
        for frame in frames:
            self.writer.append_frame(capture_id, frame)
        self.writer.close_session(capture_id)

        got = list(self.reader.iter_frames(capture_id))
        self.assertEqual(got, frames)
        manifest = self.reader.read_manifest(capture_id)
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["raw_audio"]["frame_count"], 2)
        self.assertEqual(manifest["raw_audio"]["bytes"], 54)
        self.assertEqual((self.root / capture_id / "raw.pcm").read_bytes(), b"".join(f.pcm for f in frames))

    def test_silence_and_pre_wake_frames_are_kept(self):
        capture_id = self.writer.begin_session(capture_id="silent_then_wake")
        pre_wake = raw_frame(1, bytes(27))
        wake = raw_frame(2, bytes([7]) * 27)
        self.writer.append_frame(capture_id, pre_wake)
        self.writer.append_frame(capture_id, wake)
        self.writer.close_session(capture_id)

        restored = list(self.reader.iter_frames(capture_id))
        self.assertEqual([frame.pcm for frame in restored], [bytes(27), bytes([7]) * 27])
        self.assertEqual(self.reader.read_manifest(capture_id)["raw_audio"]["frame_count"], 2)

    def test_unclosed_session_is_visibly_incomplete_but_written_frames_are_readable(self):
        capture_id = self.writer.begin_session(capture_id="interrupted")
        frame = raw_frame(1)
        self.writer.append_frame(capture_id, frame)
        # Simulate abrupt process termination: no close_session call.
        manifest = self.reader.read_manifest(capture_id)
        self.assertEqual(manifest["status"], "recording")
        self.assertFalse(manifest["complete"])
        self.assertEqual(list(self.reader.iter_frames(capture_id)), [frame])

    def test_exceptional_close_marks_incomplete_and_does_not_delete(self):
        capture_id = self.writer.begin_session(capture_id="failed_capture")
        self.writer.append_frame(capture_id, raw_frame(1))
        self.writer.close_session(capture_id, outcome="capture_disconnected")
        manifest = self.reader.read_manifest(capture_id)
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["status"], "incomplete")
        self.assertTrue((self.root / capture_id / "raw.pcm").exists())

    def test_derived_mono_round_trip_is_linked_to_capture_and_turn(self):
        capture_id = self.writer.begin_session(capture_id="mono_capture")
        self.writer.append_frame(capture_id, raw_frame(10))
        mono = MonoFrame(
            timestamp=1.1,
            sequence=10,
            sample_rate=16_000,
            sample_format="pcm_s16le",
            sample_count=3,
            data=b"\x01\x00\x02\x00\x03\x00",
        )
        self.writer.append_mono(capture_id, "turn_1", mono, channel=3, gain=2.0, source_sequence=10)
        self.writer.close_session(capture_id)

        self.assertEqual(list(self.reader.iter_mono(capture_id, "turn_1")), [mono])
        entry = self.reader.read_manifest(capture_id)["derived_audio"][0]
        self.assertEqual(entry["turn_id"], "turn_1")
        self.assertEqual(entry["channel"], 3)
        self.assertEqual(entry["gain"], 2.0)

    def test_events_and_debug_payloads_redact_credentials(self):
        capture_id = self.writer.begin_session(capture_id="debug_capture")
        self.writer.append_event(capture_id, {
            "trace_id": "trace-1",
            "stage": "llm_request",
            "event": "completed",
            "elapsed_ms": 9,
            "attributes": {"Authorization": "Bearer secret", "retry_count": 0},
        })
        self.writer.append_debug_payload(capture_id, "turn_1", "llm_request", {
            "messages": [{"role": "user", "content": "debug question"}],
            "api_key": "should-not-be-saved",
            "nested": {"refresh_token": "hidden"},
        })
        self.writer.append_debug_payload(capture_id, "turn_1", "transcript", "my spoken words")
        self.writer.close_session(capture_id)

        event = next(self.reader.iter_events(capture_id))
        payloads = list(self.reader.iter_debug_payloads(capture_id, "turn_1"))
        self.assertEqual(event["attributes"]["Authorization"], "[REDACTED]")
        self.assertEqual(payloads[0]["payload"]["messages"][0]["content"], "debug question")
        self.assertEqual(payloads[0]["payload"]["api_key"], "[REDACTED]")
        self.assertEqual(payloads[0]["payload"]["nested"]["refresh_token"], "[REDACTED]")
        self.assertEqual(payloads[1]["payload"], "my spoken words")
        directory_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (self.root / capture_id).glob("*.jsonl")
        )
        self.assertNotIn("should-not-be-saved", directory_text)
        self.assertNotIn("Bearer secret", directory_text)

    def test_archive_directories_and_files_have_private_permissions(self):
        capture_id = self.writer.begin_session(capture_id="private")
        self.writer.append_frame(capture_id, raw_frame(1))
        self.writer.close_session(capture_id)
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.root / capture_id).st_mode & 0o777, 0o700)
        for path in (self.root / capture_id).iterdir():
            if path.is_file():
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_duplicate_and_unsafe_capture_ids_are_rejected(self):
        self.writer.begin_session(capture_id="stable_id")
        with self.assertRaises(ArchiveSessionExistsError):
            self.writer.begin_session(capture_id="stable_id")
        with self.assertRaises(ArchiveValidationError):
            self.writer.begin_session(capture_id="../escape")
        self.assertFalse((Path(self.temp.name) / "escape").exists())

    def test_invalid_frame_is_rejected_before_any_bytes_are_written(self):
        capture_id = self.writer.begin_session(capture_id="invalid_frame")
        invalid = AudioFrame(1, 16_000, 9, "s24_3le", 1, b"short")
        with self.assertRaises(ArchiveValidationError):
            self.writer.append_frame(capture_id, invalid)
        self.writer.close_session(capture_id)
        self.assertEqual((self.root / capture_id / "raw.pcm").stat().st_size, 0)

    def test_write_error_is_visible_and_session_cannot_be_marked_complete(self):
        capture_id = self.writer.begin_session(capture_id="write_error")
        session = self.writer._sessions[capture_id]

        class BrokenWriter:
            def tell(self):
                return 0

            def write(self, data):
                raise OSError("disk full")

        original = session.raw
        session.raw = BrokenWriter()
        with self.assertRaises(ArchiveRecordError):
            self.writer.append_frame(capture_id, raw_frame(1))
        session.raw = original
        self.writer.close_session(capture_id)
        manifest = self.reader.read_manifest(capture_id)
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["write_error"], "raw_frame_write_failed")

    def test_partial_trailing_index_line_is_ignored_as_incomplete_tail(self):
        capture_id = self.writer.begin_session(capture_id="partial_tail")
        frame = raw_frame(1)
        self.writer.append_frame(capture_id, frame)
        session = self.writer._sessions[capture_id]
        session.frame_index.write(b'{"sequence":')
        self.assertEqual(list(self.reader.iter_frames(capture_id)), [frame])


if __name__ == "__main__":
    unittest.main()
