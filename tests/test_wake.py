from __future__ import annotations

import unittest

from astero.audio.pcm import MonoFrame
from astero.wake.detector import (
    DetectorConfig,
    ModelUnavailableError,
    OpenWakeWordModel,
    WakeDetector,
    pcm16_frame,
    silence,
)
from astero.wake.testing import SequenceWakeModel


class WakeDetectorTests(unittest.TestCase):
    def test_frames_are_chunked_and_event_has_score_time_and_bounded_preroll(self) -> None:
        scores = SequenceWakeModel([0.1, 0.9, 0.95])
        detector = WakeDetector(
            scores,
            DetectorConfig(threshold=0.8, consecutive_frames=2, pre_roll_seconds=0.16),
        )
        first = detector.feed(pcm16_frame(silence(640), timestamp=2.0, sequence=10))
        self.assertEqual(first, ())
        events = detector.feed(pcm16_frame(silence(3200), timestamp=2.04, sequence=11))
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertAlmostEqual(event.timestamp, 2.24)
        self.assertEqual(event.sequence, 11)
        self.assertEqual(event.score, 0.95)
        self.assertEqual(event.sample_rate, 16000)
        self.assertEqual(event.pre_roll_samples, 2560)
        self.assertEqual(len(event.pre_roll_pcm_s16le), 5120)
        self.assertEqual([rate for _, rate in scores.chunks_seen], [16000, 16000, 16000])
        self.assertTrue(all(len(chunk) == 2560 for chunk, _ in scores.chunks_seen))

    def test_threshold_and_refractory_debounce(self) -> None:
        detector = WakeDetector(
            SequenceWakeModel([0.9, 0.2, 0.9, 0.9, 0.9, 0.1, 0.9, 0.9]),
            DetectorConfig(threshold=0.8, consecutive_frames=2, refractory_seconds=0.25, pre_roll_seconds=0),
        )
        events = []
        for index in range(8):
            events.extend(detector.feed(pcm16_frame(silence(1280), timestamp=index * 0.08, sequence=index)))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].sequence, 3)
        self.assertEqual(events[1].sequence, 7)

    def test_partial_input_is_buffered_until_complete_chunk(self) -> None:
        scores = SequenceWakeModel([0.95])
        detector = WakeDetector(scores, DetectorConfig(consecutive_frames=1, pre_roll_seconds=0))
        self.assertEqual(detector.feed(pcm16_frame(silence(300), timestamp=0.0, sequence=1)), ())
        events = detector.feed(pcm16_frame(silence(980), timestamp=0.02, sequence=2))
        self.assertEqual(len(scores.chunks_seen), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].sequence, 2)

    def test_invalid_input_and_missing_model_are_explicit(self) -> None:
        detector = WakeDetector(SequenceWakeModel([]))
        with self.assertRaisesRegex(ValueError, "pcm_s16le"):
            detector.feed(MonoFrame(0, 0, 16000, "float32_le", 1, b"\0" * 4))
        with self.assertRaisesRegex(ValueError, "sequence"):
            detector.feed(pcm16_frame(silence(1), timestamp=0.0, sequence=1))
            detector.feed(pcm16_frame(silence(1), timestamp=0.0, sequence=1))
        with self.assertRaises(ModelUnavailableError):
            OpenWakeWordModel("/definitely/missing/astero-model.tflite")


if __name__ == "__main__":
    unittest.main()
