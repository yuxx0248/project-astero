import unittest

from astero.audio.pcm import MonoFrame
from astero.speech.endpoint import (
    EndpointCollector,
    EndpointConfig,
    EndpointKind,
    WakeEvent,
)


FRAME_SAMPLES = 1600  # 100 ms at 16 kHz


def frame(sequence, *, timestamp=None, marker=None):
    if timestamp is None:
        timestamp = sequence * 0.1
    value = sequence % 256 if marker is None else marker
    return MonoFrame(
        timestamp=timestamp,
        sequence=sequence,
        sample_rate=16000,
        sample_format="pcm_s16le",
        sample_count=FRAME_SAMPLES,
        data=bytes((value, 0)) * FRAME_SAMPLES,
    )


class ScriptedVAD:
    def __init__(self, speech_sequences=()):
        self.speech_sequences = set(speech_sequences)
        self.seen = []

    def is_speech(self, audio_frame):
        self.seen.append(audio_frame.sequence)
        return audio_frame.sequence in self.speech_sequences


class EndpointTests(unittest.TestCase):
    def test_wake_uses_bounded_preroll_and_emits_timed_segment(self):
        vad = ScriptedVAD({4, 5})
        collector = EndpointCollector(vad, EndpointConfig(
            pre_roll_seconds=0.3,
            pre_roll_buffer_seconds=0.4,
            end_silence_seconds=0.2,
            start_timeout_seconds=1.0,
            max_duration_seconds=2.0,
        ))
        for seq in range(6):
            collector.feed(frame(seq))
        events = collector.wake(WakeEvent(timestamp=0.5, score=0.91))
        self.assertEqual([event.kind for event in events],
                         [EndpointKind.WAKE, EndpointKind.SPEECH_STARTED])
        self.assertEqual(vad.seen, [2, 3, 4, 5])

        collector.feed(frame(6))
        terminal = collector.feed(frame(7))[0]
        self.assertEqual(terminal.kind, EndpointKind.ENDPOINT)
        segment = terminal.segment
        self.assertIsNotNone(segment)
        self.assertEqual((segment.sample_rate, segment.sample_format), (16000, "pcm_s16le"))
        self.assertEqual((segment.start_timestamp, segment.end_timestamp), (0.2, 0.8))
        self.assertEqual((segment.first_sequence, segment.last_sequence), (2, 7))
        self.assertEqual(len(segment.data), FRAME_SAMPLES * 2 * 6)
        self.assertFalse(collector.active)

    def test_no_speech_timeout_has_explicit_event_and_no_audio(self):
        collector = EndpointCollector(
            ScriptedVAD(),
            EndpointConfig(start_timeout_seconds=0.25, pre_roll_seconds=0,
                           pre_roll_buffer_seconds=0, max_duration_seconds=1),
        )
        collector.wake(WakeEvent(0.0))
        collector.feed(frame(0))
        collector.feed(frame(1))
        self.assertEqual(collector.feed(frame(2))[0].kind, EndpointKind.NO_SPEECH_TIMEOUT)
        self.assertFalse(collector.active)

    def test_maximum_duration_returns_partial_segment_after_speech(self):
        collector = EndpointCollector(
            ScriptedVAD({0}),
            EndpointConfig(start_timeout_seconds=1, end_silence_seconds=1,
                           pre_roll_seconds=0, pre_roll_buffer_seconds=0,
                           max_duration_seconds=0.25),
        )
        collector.wake(WakeEvent(0.0))
        self.assertEqual(collector.feed(frame(0))[0].kind, EndpointKind.SPEECH_STARTED)
        collector.feed(frame(1))
        result = collector.feed(frame(2))[0]
        self.assertEqual(result.kind, EndpointKind.MAX_DURATION)
        self.assertIsNotNone(result.segment)

    def test_cancel_and_disconnect_discard_incomplete_audio(self):
        collector = EndpointCollector(ScriptedVAD({0}))
        collector.feed(frame(0))
        collector.wake(WakeEvent(0.0))
        cancelled = collector.cancel()
        self.assertEqual(cancelled.kind, EndpointKind.CANCELLED)
        self.assertIsNone(cancelled.segment)

        collector.wake(WakeEvent(0.1))
        lost = collector.stream_lost(0.2)
        self.assertEqual(lost.kind, EndpointKind.STREAM_LOST)
        self.assertIsNone(lost.segment)
        self.assertFalse(collector.active)

    def test_preroll_memory_is_bounded_by_configured_window(self):
        collector = EndpointCollector(
            ScriptedVAD(),
            EndpointConfig(pre_roll_seconds=0.2, pre_roll_buffer_seconds=0.4),
        )
        for seq in range(10):
            collector.feed(frame(seq))
        collector.wake(WakeEvent(0.9))
        self.assertEqual([item.sequence for item in collector._active], [6, 7, 8, 9])

    def test_rejects_bad_format_and_reversed_timestamps(self):
        collector = EndpointCollector(ScriptedVAD())
        collector.feed(frame(1))
        with self.assertRaisesRegex(ValueError, "non-decreasing"):
            collector.feed(frame(0, timestamp=0.0))
        with self.assertRaisesRegex(ValueError, "sample_rate"):
            collector.feed(MonoFrame(0.2, 2, 8000, "pcm_s16le", FRAME_SAMPLES, bytes(FRAME_SAMPLES * 2)))

    def test_config_rejects_unbounded_or_inconsistent_preroll(self):
        with self.assertRaisesRegex(ValueError, "cover"):
            EndpointConfig(pre_roll_seconds=0.5, pre_roll_buffer_seconds=0.4)
        with self.assertRaisesRegex(ValueError, "finite"):
            EndpointConfig(max_duration_seconds=float("inf"))


if __name__ == "__main__":
    unittest.main()
