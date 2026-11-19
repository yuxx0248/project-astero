import unittest
from unittest.mock import Mock, patch

from astero.audio.pcm import MonoFrame
from astero.observability import TraceContext, TraceRecorder
from astero.service.local_voice import LocalVoiceConfig, _TracePlayback
from astero.speech.endpoint import EndpointCollector, EndpointConfig, EndpointKind, WakeEvent


class VoiceLatencyTests(unittest.TestCase):
    def test_first_playing_is_observed_once_and_does_not_count_queue_as_audio(self):
        events = []
        bridge = Mock()
        bridge.announce.return_value = 'task'
        bridge.get_status.side_effect = [{'status': s} for s in
                                        ('queued', 'generating', 'playing', 'playing', 'completed')]
        traced = _TracePlayback(bridge, TraceRecorder(sink=events.append),
                                TraceContext('trace', 'turn', 'session'), lambda _: None, Mock())
        with patch('astero.service.local_voice.time.monotonic_ns', side_effect=[1_000_000_000, 2_000_000_000]):
            traced.announce('test')
            for _ in range(5):
                traced.get_status('task')
        first = [e for e in events if e.stage == 'first_playing_observed']
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].event, 'completed')
        self.assertEqual(first[0].elapsed_ms, 1000)
        self.assertEqual(first[0].status, 'playing')

    def test_fast_completion_does_not_fabricate_first_playing_time(self):
        events = []
        bridge = Mock()
        bridge.get_status.return_value = {'status': 'completed'}
        traced = _TracePlayback(bridge, TraceRecorder(sink=events.append),
                                TraceContext('trace', 'turn', 'session'), lambda _: None, Mock())
        traced.announce('test')
        traced.get_status('task')
        first = next(e for e in events if e.stage == 'first_playing_observed')
        self.assertEqual(first.event, 'skipped')
        self.assertIsNone(first.elapsed_ms)

    def test_short_pause_preserves_both_clauses_then_silence_ends_turn(self):
        class Vad:
            def is_speech(self, frame):
                return frame.data != b'\0' * len(frame.data)
        endpoint = EndpointCollector(Vad(), EndpointConfig(
            end_silence_seconds=LocalVoiceConfig().end_silence_seconds,
            pre_roll_seconds=0, pre_roll_buffer_seconds=0,
        ))
        endpoint.wake(WakeEvent(0))
        values = [True] * 5 + [False] * 7 + [True] * 5 + [False] * 10
        terminal = None
        for n, speech in enumerate(values):
            frame = MonoFrame(n / 10, n, 16000, 'pcm_s16le', 1600,
                              (b'\x01\0' if speech else b'\0\0') * 1600)
            for event in endpoint.feed(frame):
                if event.kind == EndpointKind.ENDPOINT:
                    terminal = event
            if terminal:
                break
        self.assertIsNotNone(terminal)
        self.assertGreaterEqual(terminal.timestamp, 2.5)
        # Both spoken clauses survived the internal 0.7 second pause.
        self.assertEqual(terminal.segment.data.count(b'\x01'), 16000)

    def test_invalid_endpoint_silence_rejected(self):
        for value in (0, -1, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                LocalVoiceConfig(end_silence_seconds=value)


if __name__ == '__main__':
    unittest.main()
