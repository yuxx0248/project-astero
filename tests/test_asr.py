import struct
import time
import unittest

from astero.audio.pcm import MonoFrame
from astero.speech.asr import (
    ASRError,
    ASRTimeoutError,
    SherpaOnnxASR,
    ZipformerConfig,
    _SherpaZipformer,
)
from astero.speech.testing import ScriptedRecognizer


def frame(data, *, fmt="pcm_s16le", count=None, rate=16000):
    width = 2 if fmt == "pcm_s16le" else 4
    return MonoFrame(1.25, 3, rate, fmt, len(data) // width if count is None else count, data)


def config(timeout=1):
    return ZipformerConfig("enc", "dec", "join", "tokens", timeout_seconds=timeout)


class ASRTests(unittest.TestCase):
    def test_sherpa_online_result_supports_string_and_object_api(self):
        class Stream:
            def accept_waveform(self, *_): pass
            def input_finished(self): pass
        class Backend:
            def __init__(self, result): self.result = result
            def create_stream(self): return Stream()
            def is_ready(self, stream): return False
            def get_result(self, stream): return self.result
        adapter = _SherpaZipformer.__new__(_SherpaZipformer)
        for result in ('hello', type('Result', (), {'text': 'hello'})()):
            adapter._recognizer = Backend(result)
            self.assertEqual(adapter.recognize((0.1,), 16000), 'hello')

    def test_success_returns_text_duration_and_no_guessed_language(self):
        recognizer = ScriptedRecognizer(["你好，Astero"])
        with SherpaOnnxASR(config(), recognizer=recognizer) as asr:
            result = asr.recognize(frame(struct.pack("<hh", 16384, -8192)))
        self.assertEqual(result.text, "你好，Astero")
        self.assertIsNone(result.language)
        self.assertGreaterEqual(result.elapsed_seconds, 0)
        self.assertEqual(result.audio_duration_seconds, 2 / 16000)
        self.assertEqual(recognizer.calls[0][0], (0.5, -0.25))
        self.assertEqual(recognizer.calls[0][1], 16000)

    def test_empty_result_is_valid_and_exact_silence_skips_backend(self):
        recognizer = ScriptedRecognizer([""])
        with SherpaOnnxASR(config(), recognizer=recognizer) as asr:
            empty = asr.recognize(frame(struct.pack("<h", 20)))
            silence = asr.recognize(frame(bytes(2)))
        self.assertEqual(empty.text, "")
        self.assertEqual(silence.text, "")
        self.assertEqual(len(recognizer.calls), 1)

    def test_backend_error_propagates_as_asr_error(self):
        with SherpaOnnxASR(config(), recognizer=ScriptedRecognizer([RuntimeError("decode failed")])) as asr:
            with self.assertRaisesRegex(ASRError, "decode failed"):
                asr.recognize(frame(struct.pack("<h", 2)))

    def test_bad_rate_length_format_and_empty_audio_rejected(self):
        with SherpaOnnxASR(config(), recognizer=ScriptedRecognizer([])) as asr:
            with self.assertRaisesRegex(ValueError, "sample_rate"):
                asr.recognize(frame(struct.pack("<h", 2), rate=8000))
            with self.assertRaisesRegex(ValueError, "length"):
                asr.recognize(frame(struct.pack("<h", 2), count=2))
            with self.assertRaisesRegex(ValueError, "sample_format"):
                asr.recognize(frame(b"abcd", fmt="pcm_f32le"))
            with self.assertRaisesRegex(ValueError, "at least one sample"):
                asr.recognize(frame(b"", count=0))

    def test_timeout_is_explicit(self):
        class SlowRecognizer:
            def recognize(self, samples, sample_rate):
                time.sleep(0.08)
                return "late"

        asr = SherpaOnnxASR(config(), recognizer=SlowRecognizer())
        try:
            with self.assertRaises(ASRTimeoutError):
                asr.recognize(frame(struct.pack("<h", 2)), timeout_seconds=0.005)
            # The still-running native call cannot be cancelled safely.
            with self.assertRaises(ASRTimeoutError):
                asr.recognize(frame(struct.pack("<h", 2)))
        finally:
            asr.close()


if __name__ == "__main__":
    unittest.main()
