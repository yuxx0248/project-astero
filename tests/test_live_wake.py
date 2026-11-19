import unittest
from astero.speech.live_wake import _OverlappingKeywordStreams


class Stream:
    def __init__(self):
        self.tokens = []
        self.pending = False
        self.result = ''

    def accept_waveform(self, rate, samples):
        self.tokens.extend(samples)
        self.pending = True


class Spotter:
    def __init__(self):
        self.created = []

    def create_stream(self):
        stream = Stream()
        self.created.append(stream)
        return stream

    def is_ready(self, stream):
        return stream.pending

    def decode_stream(self, stream):
        stream.pending = False
        # Model a keyword requiring its complete prefix and bounded context.
        stream.result = 'ASTERO' if stream.tokens[-3:] == [1, 2, 3] and len(stream.tokens) <= 8 else ''

    def get_result(self, stream):
        return stream.result


class RollingKeywordTests(unittest.TestCase):
    def detector(self):
        return _OverlappingKeywordStreams(Spotter(), sample_rate=1, window_seconds=8, hop_seconds=4)

    def test_long_listen_keeps_keyword_history_bounded(self):
        detector = self.detector()
        for _ in range(100):
            self.assertFalse(detector.feed([0]))
            self.assertLessEqual(len(detector.streams), 2)
        self.assertFalse(detector.feed([1]))
        self.assertFalse(detector.feed([2]))
        self.assertTrue(detector.feed([3]))

    def test_word_crossing_window_boundary_survives_and_is_not_duplicated(self):
        detector = self.detector()
        for _ in range(6):
            detector.feed([0])
        self.assertFalse(detector.feed([1]))
        self.assertFalse(detector.feed([2]))
        self.assertTrue(detector.feed([3]))
        for _ in range(12):
            self.assertFalse(detector.feed([0]))

    def test_near_word_and_reset_do_not_trigger(self):
        detector = self.detector()
        for token in [1, 2, 0, 1, 0, 3]:
            self.assertFalse(detector.feed([token]))
        detector.feed([1])
        detector.reset()
        self.assertFalse(detector.feed([2]))
        self.assertFalse(detector.feed([3]))


class WakeFrameTests(unittest.TestCase):
    def test_alternate_requires_identical_capture_position(self):
        from astero.audio.pcm import MonoFrame
        from astero.speech.live_wake import with_alternate
        primary = MonoFrame(1.0, 3, 16000, 'pcm_s16le', 1, b'\x00\x00')
        alternate = MonoFrame(1.0, 3, 16000, 'pcm_s16le', 1, b'\x01\x00')
        frame = with_alternate(primary, alternate)
        self.assertEqual(frame.data, primary.data)
        self.assertEqual(frame.alternate_data, alternate.data)
        with self.assertRaises(ValueError):
            with_alternate(primary, MonoFrame(1.1, 4, 16000, 'pcm_s16le', 1, b'\x00\x00'))


class DualKeywordTests(unittest.TestCase):
    def make_detector(self, primary_hit, secondary_hit):
        try:
            import numpy
        except ImportError:
            self.skipTest('numpy required by live inference adapter')
        from unittest.mock import Mock
        from astero.speech.live_wake import SherpaKeyword
        d = SherpaKeyword.__new__(SherpaKeyword)
        d.keyword_streams = Mock()
        d.keyword_streams.feed.return_value = primary_hit
        d.secondary_keyword_streams = Mock()
        d.secondary_keyword_streams.feed.return_value = secondary_hit
        d._alternate_active = False
        d.recognizer = Mock()
        d.recognizer.is_ready.return_value = False
        d.asr_stream = Mock()
        d.stream_samples = 0
        return d

    def test_secondary_can_wake_without_changing_asr_audio(self):
        from astero.speech.live_wake import WakeFrame
        d = self.make_detector(False, True)
        primary_stream = d.asr_stream
        frame = WakeFrame(1.0, 3, 16000, 'pcm_s16le', 1, b'\x00\x00', b'\xff\x7f')
        self.assertEqual(len(d.feed(frame)), 1)
        self.assertEqual(primary_stream.accept_waveform.call_args.args[1].tolist(), [0.0])
        self.assertEqual(d.recognizer.create_stream.call_count, 1)
        d.keyword_streams.reset.assert_called_once()
        d.secondary_keyword_streams.reset.assert_called_once()

    def test_both_channels_report_one_event_and_plain_mono_remains_supported(self):
        from astero.speech.live_wake import WakeFrame
        from astero.audio.pcm import MonoFrame
        d = self.make_detector(True, True)
        self.assertEqual(len(d.feed(WakeFrame(1.0, 3, 16000, 'pcm_s16le', 1,
                                             b'\x00\x00', b'\x00\x00'))), 1)
        d = self.make_detector(False, True)
        self.assertEqual(d.feed(MonoFrame(1.0, 3, 16000, 'pcm_s16le', 1, b'\x00\x00')), [])
        d.secondary_keyword_streams.feed.assert_not_called()
