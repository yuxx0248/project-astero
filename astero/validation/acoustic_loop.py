"""Local TTS-to-Echo acoustic loop acceptance driver."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
import math
from pathlib import Path
import time
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from astero.speech.voice_bridge import VoiceBridgeClient, VoiceBridgeConfig


DEFAULT_WAKE_STYLE = "Speak English clearly and slowly."


class ValidationError(RuntimeError):
    """The acoustic acceptance run could not complete."""


class ServiceClientProtocol(Protocol):
    def health(self) -> Mapping[str, object]: ...
    def submit_voice_turn(self, session_id: str) -> Mapping[str, object]: ...
    def get_turn(self, turn_id: str) -> Mapping[str, object]: ...


class PlaybackProtocol(Protocol):
    def health(self) -> object: ...
    def speak(self, text: str, *, style: str | None = None): ...


class LocalServiceClient:
    """Small, loopback-only HTTP client for Astero's local API."""

    def __init__(self, base_url: str = "http://127.0.0.1:8766", *, timeout: float = 5.0):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in
                {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password):
            raise ValueError("Astero validation client must use loopback HTTP without credentials")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health(self) -> Mapping[str, object]:
        return self._request("GET", "/health")

    def submit_voice_turn(self, session_id: str) -> Mapping[str, object]:
        return self._request("POST", "/v1/turns", {
            "session_id": session_id, "mode": "voice",
        })

    def get_turn(self, turn_id: str) -> Mapping[str, object]:
        return self._request("GET", "/v1/turns/" + turn_id)

    def _request(self, method: str, path: str, payload: Mapping[str, object] | None = None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(self.base_url + path, data=body,
                          headers={"Accept": "application/json",
                                   "Content-Type": "application/json"}, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = response.read(64 * 1024 + 1)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ValidationError(f"Astero service request failed ({type(exc).__name__})") from None
        if len(data) > 64 * 1024:
            raise ValidationError("Astero service response was too large")
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValidationError("Astero service returned invalid JSON") from None
        if not isinstance(value, dict):
            raise ValidationError("Astero service returned an invalid response")
        return value


class AcousticLoopRunner:
    """Coordinate synthetic speech through actual playback and microphone."""

    def __init__(self, service: ServiceClientProtocol, playback: PlaybackProtocol, *,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic,
                 poll_interval_seconds: float = 0.2):
        self.service = service
        self.playback = playback
        self.sleep = sleep
        self.monotonic = monotonic
        self.poll_interval_seconds = poll_interval_seconds

    def run(self, text: str, *, session_id: str = "astero-acoustic-validation",
            wake_phrase: str = "Astero", wake_style: str = DEFAULT_WAKE_STYLE,
            timeout_seconds: float = 150.0,
            wake_gap_seconds: float = 0.5) -> dict[str, object]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be non-empty")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be non-empty")
        if not isinstance(wake_phrase, str) or not wake_phrase.strip():
            raise ValueError("wake_phrase must be non-empty")
        if not isinstance(wake_style, str):
            raise ValueError("wake_style must be text; empty text disables style guidance")
        health = self.service.health()
        if health.get("ready") is not True:
            raise ValidationError("Astero service is not ready")
        playback_health = self.playback.health()
        turn = self.service.submit_voice_turn(session_id.strip())
        turn_id = _required(turn, "turn_id")
        trace_id = _required(turn, "trace_id")
        started = self.monotonic()
        deadline = started + timeout_seconds
        ready_turn = self._wait_for_capture(turn_id, deadline)

        wake_result = self.playback.speak(wake_phrase, style=wake_style)
        self.sleep(wake_gap_seconds)
        command_result = self.playback.speak(text.strip())
        final_turn = self._wait_for_terminal(turn_id, deadline)
        elapsed_ms = max(0.0, (self.monotonic() - started) * 1000)
        result_data = final_turn.get("result")
        if not isinstance(result_data, Mapping):
            result_data = {}
        status = final_turn.get("status")
        archive_complete = result_data.get("archive_complete") is True
        return {
            "input_kind": "synthetic_tts",
            "synthetic_voice_note": "This verifies the acoustic service loop, not human wake-word accuracy.",
            "session_id": session_id.strip(),
            "turn_id": turn_id,
            "trace_id": trace_id,
            "capture_id": result_data.get("capture_id"),
            "status": status,
            "diagnostic_only": False,
            "prompt_text": text.strip(),
            "wake_phrase": wake_phrase,
            "wake_style": wake_style,
            "transcript": result_data.get("transcript"),
            "reply": result_data.get("reply"),
            "error_code": final_turn.get("error_code") or result_data.get("error_code"),
            "archive_complete": archive_complete,
            "frame_count": result_data.get("frame_count"),
            "turn_elapsed_ms": result_data.get("total_elapsed_ms"),
            "validation_elapsed_ms": elapsed_ms,
            "service_stages": _stage_summary(final_turn.get("events")),
            "trace_summary": final_turn.get("trace_summary"),
            "wake_tts": _as_mapping(wake_result),
            "command_tts": _as_mapping(command_result),
            "service_health": dict(health),
            "tts_health": _as_mapping(playback_health),
            "capture_ready_seen": bool(ready_turn),
            "completed": status == "completed" and archive_complete,
        }

    def _wait_for_capture(self, turn_id: str, deadline: float) -> Mapping[str, object]:
        while self.monotonic() < deadline:
            turn = self.service.get_turn(turn_id)
            events = turn.get("events", ())
            if any(_event_field(event, "stage") == "capture_ready" and
                   _event_field(event, "event") == "completed" for event in events):
                return turn
            if turn.get("status") in _TERMINAL:
                raise ValidationError("voice turn ended before capture became ready")
            self.sleep(self.poll_interval_seconds)
        raise TimeoutError("timed out waiting for Echo capture readiness")

    def _wait_for_terminal(self, turn_id: str, deadline: float) -> Mapping[str, object]:
        while self.monotonic() < deadline:
            turn = self.service.get_turn(turn_id)
            if turn.get("status") in _TERMINAL:
                return turn
            self.sleep(self.poll_interval_seconds)
        raise TimeoutError("timed out waiting for Astero voice turn")


_TERMINAL = {"completed", "failed", "cancelled", "no_wake", "no_speech",
             "empty_input", "interrupted"}


def run_acoustic_loop(service_url: str, bridge_url: str, text: str, *,
                      session_id: str = "astero-acoustic-validation",
                      timeout_seconds: float = 150.0,
                      wake_style: str = DEFAULT_WAKE_STYLE) -> dict[str, object]:
    service = LocalServiceClient(service_url)
    playback = VoiceBridgeClient(VoiceBridgeConfig(base_url=bridge_url,
                                                   timeout_seconds=timeout_seconds))
    return AcousticLoopRunner(service, playback).run(text, session_id=session_id,
                                                     timeout_seconds=timeout_seconds,
                                                     wake_style=wake_style)


def _required(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"service did not return {key}")
    return value


def _event_field(event: object, name: str):
    if isinstance(event, Mapping):
        return event.get(name)
    return getattr(event, name, None)


def _stage_summary(events: object) -> list[dict[str, object]]:
    if not isinstance(events, (list, tuple)):
        return []
    result = []
    for event in events:
        row = _as_mapping(event)
        result.append({key: row.get(key) for key in
                       ("stage", "event", "elapsed_ms", "status", "error_code", "attempt")})
    return result


def _as_mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    if isinstance(value, str):
        return {"status": value}
    return {"value": value}
