"""Streaming, local-only capture archives for Astero debugging.

Archives are created outside the repository by default. Raw PCM frames are
stored byte-for-byte and indexed as they arrive, so interrupted sessions stay
readable but are never mistaken for complete sessions.
"""

from .store import (
    ArchiveError,
    ArchiveIncompleteError,
    ArchiveRecordError,
    ArchiveSessionExistsError,
    ArchiveSessionNotFoundError,
    ArchiveValidationError,
    ArchiveWriter,
    ArchiveReader,
)

__all__ = [
    "ArchiveError",
    "ArchiveIncompleteError",
    "ArchiveRecordError",
    "ArchiveSessionExistsError",
    "ArchiveSessionNotFoundError",
    "ArchiveValidationError",
    "ArchiveWriter",
    "ArchiveReader",
]
