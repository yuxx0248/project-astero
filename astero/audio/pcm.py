"""Decode Echo interleaved PCM and select one microphone channel.

The conversion functions are pure: they do not open devices or write files.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import struct
from typing import Literal


INPUT_SAMPLE_RATE = 16_000
INPUT_CHANNELS = 9
INPUT_FORMAT = "s24_3le"
MICROPHONE_CHANNELS = tuple(range(7))
REFERENCE_CHANNELS = (7, 8)
OutputFormat = Literal["pcm_s16le", "float32_le"]


@dataclass(frozen=True, slots=True)
class PCMFrame:
    """One interleaved capture frame, with timestamp in seconds."""

    timestamp: float
    sequence: int
    sample_rate: int
    channels: int
    sample_format: str
    data: bytes


@dataclass(frozen=True, slots=True)
class MonoFrame:
    """Single-channel output with source timing and sequence preserved."""

    timestamp: float
    sequence: int
    sample_rate: int
    sample_format: OutputFormat
    sample_count: int
    data: bytes


def decode_s24_3le(data: bytes | bytearray | memoryview) -> tuple[int, ...]:
    """Decode packed signed 24-bit little-endian samples into Python integers."""
    view = memoryview(data).cast("B")
    if len(view) % 3:
        raise ValueError("s24_3le data length must be a multiple of 3 bytes")
    values: list[int] = []
    for offset in range(0, len(view), 3):
        value = view[offset] | (view[offset + 1] << 8) | (view[offset + 2] << 16)
        if value & 0x800000:
            value -= 1 << 24
        values.append(value)
    return tuple(values)


def split_interleaved(samples: tuple[int, ...], channels: int) -> tuple[tuple[int, ...], ...]:
    """Split sample-major interleaved values into one tuple per channel."""
    if channels <= 0:
        raise ValueError("channels must be positive")
    if len(samples) % channels:
        raise ValueError("sample count is not divisible by channel count")
    return tuple(tuple(samples[i::channels]) for i in range(channels))


def _validate_frame(frame: PCMFrame, channel: int) -> None:
    if not math.isfinite(frame.timestamp):
        raise ValueError("timestamp must be finite")
    if frame.sequence < 0:
        raise ValueError("sequence must be non-negative")
    if frame.sample_rate != INPUT_SAMPLE_RATE:
        raise ValueError(f"sample_rate must be {INPUT_SAMPLE_RATE}")
    if frame.channels != INPUT_CHANNELS:
        raise ValueError(f"channels must be {INPUT_CHANNELS}")
    if frame.sample_format != INPUT_FORMAT:
        raise ValueError(f"sample_format must be {INPUT_FORMAT!r}")
    if not 0 <= channel < len(MICROPHONE_CHANNELS):
        raise ValueError("channel must select one of microphone channels 0 through 6")
    if len(frame.data) % (frame.channels * 3):
        raise ValueError("frame data must contain complete 9-channel s24_3le samples")


def convert_frame(
    frame: PCMFrame,
    *,
    channel: int = 0,
    output_format: OutputFormat = "pcm_s16le",
) -> MonoFrame:
    """Select one mic channel and convert it to PCM16LE or normalized float32LE.

    PCM16 conversion drops the least-significant 8 bits (arithmetic shift),
    exactly mapping the signed 24-bit range into signed 16-bit without wrap.
    Float output divides by 2**23, yielding [-1.0, 1.0) and never exceeding
    that range. The Echo reference channels 7 and 8 cannot be selected here.
    """
    _validate_frame(frame, channel)
    if output_format not in ("pcm_s16le", "float32_le"):
        raise ValueError("output_format must be 'pcm_s16le' or 'float32_le'")

    interleaved = decode_s24_3le(frame.data)
    mono = interleaved[channel::INPUT_CHANNELS]
    if output_format == "pcm_s16le":
        out = bytearray()
        for value in mono:
            # Arithmetic right shift is floor division by 256 and saturates
            # naturally at the endpoints of the 24-to-16-bit mapping.
            pcm16 = max(-32768, min(32767, value >> 8))
            out.extend(struct.pack("<h", pcm16))
    else:
        out = bytearray()
        for value in mono:
            normalized = max(-1.0, min(1.0, value / 8_388_608.0))
            out.extend(struct.pack("<f", normalized))

    return MonoFrame(
        timestamp=frame.timestamp,
        sequence=frame.sequence,
        sample_rate=frame.sample_rate,
        sample_format=output_format,
        sample_count=len(mono),
        data=bytes(out),
    )
