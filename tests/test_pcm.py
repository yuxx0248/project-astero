import math
import struct
import unittest

from astero.audio.pcm import (
    INPUT_CHANNELS,
    PCMFrame,
    convert_frame,
    decode_s24_3le,
    split_interleaved,
)


def s24(value: int) -> bytes:
    return (value & 0xFFFFFF).to_bytes(3, "little")


def frame(samples_by_time, *, timestamp=12.5, sequence=4, sample_rate=16000,
          channels=9, sample_format="s24_3le"):
    data = b"".join(s24(value) for row in samples_by_time for value in row)
    return PCMFrame(timestamp, sequence, sample_rate, channels, sample_format, data)


class PCMTests(unittest.TestCase):
    def test_signed_24_bit_extremes_and_sign_extension(self):
        self.assertEqual(
            decode_s24_3le(s24(0) + s24(1) + s24(-1) + s24(-8388608) + s24(8388607)),
            (0, 1, -1, -8388608, 8388607),
        )

    def test_split_interleaved_preserves_channels(self):
        self.assertEqual(split_interleaved((10, 20, 30, 11, 21, 31), 3),
                         ((10, 11), (20, 21), (30, 31)))

    def test_default_mic_channel_pcm16_and_metadata(self):
        rows = [tuple([123456] + [0] * 8), tuple([-8388608] + [8388607] * 8)]
        result = convert_frame(frame(rows), output_format="pcm_s16le")
        self.assertEqual(result.data, struct.pack("<hh", 482, -32768))
        self.assertEqual((result.timestamp, result.sequence, result.sample_rate), (12.5, 4, 16000))
        self.assertEqual((result.sample_format, result.sample_count), ("pcm_s16le", 2))
        self.assertEqual(len(result.data), result.sample_count * 2)

    def test_configurable_mic_channel_and_reference_exclusion(self):
        rows = [tuple([0] * channel + [8388607] + [0] * (8 - channel)) for channel in range(9)]
        # Channel 6 is selectable. The reference channels are never selectable.
        selected = convert_frame(frame(rows), channel=6, output_format="float32_le")
        self.assertEqual(struct.unpack("<9f", selected.data), (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0 - 1 / 8388608, 0.0, 0.0))
        with self.assertRaises(ValueError):
            convert_frame(frame(rows), channel=7)
        # Loud values isolated to echo reference channels cannot leak into default mic data.
        reference_only = frame([tuple([0] * 7 + [8388607, -8388608])])
        self.assertEqual(convert_frame(reference_only).data, b"\0\0")

    def test_float_scaling_and_saturation_endpoints(self):
        result = convert_frame(frame([(-8388608,) + (0,) * 8, (8388607,) + (0,) * 8]),
                               output_format="float32_le")
        negative, positive = struct.unpack("<ff", result.data)
        self.assertEqual(negative, -1.0)
        self.assertTrue(0.0 <= positive <= 1.0)
        self.assertTrue(math.isfinite(positive))

    def test_bad_lengths_and_metadata_fail(self):
        with self.assertRaisesRegex(ValueError, "multiple of 3"):
            decode_s24_3le(b"\0\0")
        with self.assertRaisesRegex(ValueError, "complete 9-channel"):
            convert_frame(PCMFrame(0, 0, 16000, 9, "s24_3le", bytes(28)))
        for kwargs, message in [
            ({"sample_rate": 8000}, "sample_rate"),
            ({"channels": 8}, "channels"),
            ({"sample_format": "s24_le"}, "sample_format"),
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, message):
                convert_frame(frame([(0,) * INPUT_CHANNELS], **kwargs))


if __name__ == "__main__":
    unittest.main()
