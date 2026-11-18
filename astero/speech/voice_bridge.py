"""Client for the local Voice Bridge announcement API.

The Bridge accepts text asynchronously.  A successful ``announce`` call only
means that a job was queued; callers should use :meth:`speak` to wait until
the player reports ``completed`` or ``failed``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class VoiceBridgeError(RuntimeError):
    """Base class for Voice Bridge failures."""


class VoiceBridgeUnavailableError(VoiceBridgeError):
    """The local Bridge could not be reached or is not ready."""


class VoiceBridgeResponseError(VoiceBridgeError):
    """The Bridge returned an HTTP error or an invalid response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class VoiceBridgeTaskError(VoiceBridgeError):
    """The Bridge reports that a queued announcement failed."""

    def __init__(self, task_id: str, message: str) -> None:
        super().__init__(message)
        self.task_id = task_id


class VoiceBridgeTimeoutError(VoiceBridgeError, TimeoutError):
    """The task did not reach a terminal state before the deadline.

    The Bridge has no task cancellation endpoint.  The job may still be queued
    or playing when this exception is raised.
    """

    def __init__(self, task_id: str, timeout_seconds: float) -> None:
        super().__init__(
            f"announcement {task_id} did not finish within {timeout_seconds:g}s; "
            "it may still be queued or playing in Voice Bridge"
        )
        self.task_id = task_id


@dataclass(frozen=True, slots=True)
class VoiceBridgeConfig:
    base_url: str = "http://127.0.0.1:8765"
    timeout_seconds: float = 120.0
    poll_interval_seconds: float = 0.25
    request_timeout_seconds: float = 5.0
    voice: str | None = None
    style: str | None = None

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Voice Bridge must use a local loopback HTTP address")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain credentials, query, or fragment")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if not math.isfinite(self.poll_interval_seconds) or self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be finite and positive")
        if not math.isfinite(self.request_timeout_seconds) or self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be finite and positive")
        for name, value in (("voice", self.voice), ("style", self.style)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be text or None")


@dataclass(frozen=True, slots=True)
class AnnouncementResult:
    task_id: str
    status: str
    elapsed_seconds: float


class VoiceBridgeClient:
    """Submit text to Voice Bridge and poll for its final playback status.

    ``opener`` and ``sleep`` are injectable to support deterministic tests.
    No response bodies or announcement text are included in exception
    messages, keeping private replies out of ordinary logs.
    """

    _KNOWN = {"queued", "generating", "playing", "completed", "failed"}
    _TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")

    def __init__(
        self,
        config: VoiceBridgeConfig | None = None,
        *,
        opener: Callable = urlopen,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or VoiceBridgeConfig()
        self._opener = opener
        self._sleep = sleep
        self._monotonic = monotonic

    def health(self) -> dict[str, object]:
        """Return a validated health payload; raise if Bridge or TTS is not ready."""
        payload = self._request_json("GET", "/health")
        if payload.get("status") != "ok" or payload.get("tts_ready") is not True:
            raise VoiceBridgeUnavailableError("Voice Bridge or its TTS service is not ready")
        return payload

    def announce(self, text: str, *, voice: str | None = None,
                 style: str | None = None) -> str:
        """Submit a job and return its ID after validating the 202 response."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        payload: dict[str, str] = {"text": text.strip()}
        selected_voice = self.config.voice if voice is None else voice
        selected_style = self.config.style if style is None else style
        if selected_voice is not None:
            payload["voice"] = selected_voice
        if selected_style is not None:
            payload["style"] = selected_style
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.config.base_url.rstrip("/") + "/announce", data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        result = self._read_json_response(request, expected_status=202)
        task_id, status = result.get("id"), result.get("status")
        if not isinstance(task_id, str) or not self._TASK_ID.fullmatch(task_id) or status != "queued":
            raise VoiceBridgeResponseError("Voice Bridge returned an invalid queue response")
        return task_id

    def get_status(self, task_id: str) -> dict[str, object]:
        if not isinstance(task_id, str) or not self._TASK_ID.fullmatch(task_id):
            raise ValueError("task_id must contain only ASCII letters, digits, '_' or '-' (max 128)")
        result = self._request_json("GET", "/announcements/" + task_id)
        if result.get("id") != task_id:
            raise VoiceBridgeResponseError("Voice Bridge returned a mismatched task ID")
        status = result.get("status")
        if status not in self._KNOWN:
            raise VoiceBridgeResponseError("Voice Bridge returned an unknown task status")
        return result

    def speak(self, text: str, *, voice: str | None = None,
              style: str | None = None,
              timeout_seconds: float | None = None) -> AnnouncementResult:
        """Wait for playback to complete or fail; never treats queueing as success."""
        timeout = self.config.timeout_seconds if timeout_seconds is None else timeout_seconds
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        started = self._monotonic()
        task_id = self.announce(text, voice=voice, style=style)
        deadline = started + timeout
        while True:
            try:
                job = self.get_status(task_id)
            except (VoiceBridgeUnavailableError, VoiceBridgeResponseError) as exc:
                if isinstance(exc, VoiceBridgeResponseError) and (exc.status_code or 0) < 500:
                    raise
                raise VoiceBridgeUnavailableError(
                    f"Voice Bridge became unavailable while checking task {task_id}; "
                    "the task may still be queued or playing"
                ) from exc
            status = job["status"]
            if status == "completed":
                return AnnouncementResult(task_id, "completed", self._monotonic() - started)
            if status == "failed":
                detail = job.get("error")
                suffix = f": {detail}" if isinstance(detail, str) and detail else ""
                raise VoiceBridgeTaskError(task_id, f"Voice Bridge task failed{suffix}")
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise VoiceBridgeTimeoutError(task_id, timeout)
            self._sleep(min(self.config.poll_interval_seconds, remaining))

    def _request_json(self, method: str, path: str) -> dict[str, object]:
        request = Request(self.config.base_url.rstrip("/") + path,
                          headers={"Accept": "application/json"}, method=method)
        return self._read_json_response(request, expected_status=200)

    def _read_json_response(self, request: Request, *, expected_status: int) -> dict[str, object]:
        try:
            with self._opener(request, timeout=self.config.request_timeout_seconds) as response:
                status = response.status
                body = response.read(64 * 1024 + 1)
        except HTTPError as exc:
            exc.close()
            raise VoiceBridgeResponseError(f"Voice Bridge returned HTTP {exc.code}",
                                           status_code=exc.code) from None
        except (TimeoutError, URLError) as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
                raise VoiceBridgeUnavailableError("Voice Bridge request timed out") from None
            raise VoiceBridgeUnavailableError("Voice Bridge is unavailable") from None
        except OSError:
            raise VoiceBridgeUnavailableError("Voice Bridge is unavailable") from None
        if status != expected_status:
            raise VoiceBridgeResponseError(f"Voice Bridge returned HTTP {status}",
                                           status_code=status)
        if len(body) > 64 * 1024:
            raise VoiceBridgeResponseError("Voice Bridge response was too large")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise VoiceBridgeResponseError("Voice Bridge returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise VoiceBridgeResponseError("Voice Bridge returned an invalid JSON object")
        return payload
