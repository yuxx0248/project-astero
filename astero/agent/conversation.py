"""Configurable conversation backends with isolated per-session history.

The remote adapter uses the OpenAI Chat Completions wire format but is not tied
to a provider. It makes no network request until ``respond`` is called.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import math
import os
import threading
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class ConversationError(RuntimeError):
    """Base class for explicit conversation backend failures."""


class BackendConfigurationError(ConversationError):
    """The backend is not configured safely or completely."""


class BackendRequestError(ConversationError):
    """The backend request failed or returned an invalid response."""


class BackendTimeoutError(BackendRequestError):
    """The backend did not respond within the configured timeout."""


class EmptyResponseError(BackendRequestError):
    """The backend completed the request without answer text."""


class ConversationBackend(Protocol):
    """Input a transcript and session ID; return answer text or raise."""

    def respond(self, text: str, session_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    """Explicit settings for a Chat Completions-compatible endpoint."""

    endpoint: str
    model: str
    api_key: str | None = None
    timeout_seconds: float = 30.0
    system_prompt: str | None = None
    max_history_turns: int = 8

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise BackendConfigurationError("endpoint must be an explicit HTTP(S) URL")
        if parsed.username or parsed.password:
            raise BackendConfigurationError("credentials must not be embedded in endpoint URL")
        if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise BackendConfigurationError("non-local LLM endpoints must use HTTPS")
        if not self.model.strip():
            raise BackendConfigurationError("model must not be empty")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise BackendConfigurationError("timeout_seconds must be finite and positive")
        if self.max_history_turns < 0:
            raise BackendConfigurationError("max_history_turns must be non-negative")

    @classmethod
    def from_env(cls) -> "OpenAICompatibleConfig":
        """Load explicit endpoint/model settings and an optional secret env var."""
        endpoint = os.environ.get("ASTERO_LLM_ENDPOINT", "").strip()
        model = os.environ.get("ASTERO_LLM_MODEL", "").strip()
        if not endpoint or not model:
            raise BackendConfigurationError(
                "set ASTERO_LLM_ENDPOINT and ASTERO_LLM_MODEL to an authorized backend"
            )
        raw_timeout = os.environ.get("ASTERO_LLM_TIMEOUT_SECONDS", "30")
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise BackendConfigurationError("ASTERO_LLM_TIMEOUT_SECONDS must be numeric") from exc
        return cls(
            endpoint=endpoint,
            model=model,
            api_key=os.environ.get("ASTERO_LLM_API_KEY") or None,
            timeout_seconds=timeout,
            system_prompt=os.environ.get("ASTERO_LLM_SYSTEM_PROMPT") or None,
        )


class OpenAICompatibleBackend:
    """OpenAI-compatible chat adapter with per-session conversation history."""

    def __init__(self, config: OpenAICompatibleConfig) -> None:
        self.config = config
        self._histories: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self._history_lock = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    def respond(self, text: str, session_id: str) -> str:
        text = _required_text(text, "text")
        session_id = _required_text(session_id, "session_id")
        # Serializing turns within one session keeps each request's context in
        # order. Distinct sessions remain concurrent and never share messages.
        with self._history_lock:
            session_lock = self._session_locks[session_id]
        with session_lock:
            history = list(self._histories[session_id])
            messages: list[dict[str, str]] = []
            if self.config.system_prompt:
                messages.append({"role": "system", "content": self.config.system_prompt})
            for user_text, assistant_text in history:
                messages.extend((
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": assistant_text},
                ))
            messages.append({"role": "user", "content": text})
            payload = json.dumps({
                "model": self.config.model,
                "messages": messages,
                "metadata": {"session_id": session_id},
            }).encode("utf-8")
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            request = Request(self.config.endpoint, data=payload, headers=headers, method="POST")
            try:
                with urlopen(request, timeout=self.config.timeout_seconds) as response:
                    body = response.read()
            except HTTPError as exc:
                # Avoid including response bodies or headers, which can contain
                # private prompts or provider diagnostics.
                exc.close()
                raise BackendRequestError(f"LLM backend returned HTTP {exc.code}") from None
            except (TimeoutError, URLError) as exc:
                if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
                    raise BackendTimeoutError("LLM backend request timed out") from None
                raise BackendRequestError("could not reach LLM backend") from None
            except OSError:
                raise BackendRequestError("could not read LLM backend response") from None

            try:
                decoded = json.loads(body.decode("utf-8"))
                answer = decoded["choices"][0]["message"]["content"]
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
                raise BackendRequestError("LLM backend returned an invalid response") from None
            if not isinstance(answer, str) or not answer.strip():
                raise EmptyResponseError("LLM backend returned empty answer text")
            answer = answer.strip()
            updated = history + [(text, answer)]
            if self.config.max_history_turns:
                updated = updated[-self.config.max_history_turns :]
            else:
                updated = []
            self._histories[session_id] = updated
            return answer


class MockConversationBackend:
    """Local deterministic substitute; its output is explicitly not an LLM."""

    def __init__(self, responder=None) -> None:
        self._responder = responder or (lambda text, session_id: f"[mock:{session_id}] {text}")

    def respond(self, text: str, session_id: str) -> str:
        text = _required_text(text, "text")
        session_id = _required_text(session_id, "session_id")
        answer = self._responder(text, session_id)
        if not isinstance(answer, str) or not answer.strip():
            raise EmptyResponseError("mock backend returned empty answer text")
        return answer.strip()


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()
