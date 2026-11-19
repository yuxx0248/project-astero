"""Default local service composition and command-line entry point."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import logging
from pathlib import Path
import signal
import sys
import threading
from typing import Mapping

from astero.agent import AgentService
from astero.archive import ArchiveWriter
from astero.live import CodexConversation
from astero.observability import TraceRecorder
from astero.persistence import AsteroStore
from astero.service.app import AsteroService, TurnRequest
from astero.service.http import LocalAsteroHTTPServer
from astero.service.local_voice import LocalVoiceConfig, LocalVoiceTurnExecutor
from astero.service.listener import ResidentListener


@dataclass(frozen=True, slots=True)
class LocalServiceConfig:
    data_root: Path = Path.home() / ".local" / "share" / "astero"
    host: str = "127.0.0.1"
    port: int = 8766
    channel: int = 3
    wake_secondary_channel: int = 5
    listen: bool = False
    session_id: str = "local-voice"
    gain: float = 16.0
    end_silence_seconds: float = 0.8
    voice_bridge_url: str = "http://127.0.0.1:8765"

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session_id must be nonempty")
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Astero service must bind to loopback")
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be between 0 and 65535")


class _StoreEventSink:
    def __init__(self, store: AsteroStore):
        self.store = store

    def __call__(self, event) -> None:
        self.store.append_stage_event(
            event.event_id, event.trace_id, event.turn_id, event.session_id,
            event.stage, event.event, event.timestamp_utc, event.elapsed_ms,
            event.status, event.attempt, event.attributes,
            monotonic_ns=event.monotonic_ns,
        )


class LocalService:
    def __init__(self, config: LocalServiceConfig | None = None):
        self.config = config or LocalServiceConfig()
        self.config.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.store = AsteroStore(self.config.data_root / "astero.sqlite3")
        for interrupted in self.store.list_unfinished_turns():
            self.store.update_turn(
                interrupted.turn_id, "interrupted", error_code="service_process_restarted",
                result={"trace_id": interrupted.trace_id,
                        "session_id": interrupted.session_id},
            )
        self.voice = LocalVoiceTurnExecutor(
            self.store,
            LocalVoiceConfig(data_root=self.config.data_root,
                             channel=self.config.channel, gain=self.config.gain,
                             wake_secondary_channel=self.config.wake_secondary_channel,
                             end_silence_seconds=self.config.end_silence_seconds,
                             voice_bridge_url=self.config.voice_bridge_url),
        )
        event_recorder = TraceRecorder(sink=_StoreEventSink(self.store))
        self.agent = AgentService(
            self.store, CodexConversation(), trace_recorder=event_recorder,
            manage_turn_state=False,
        )
        self.app = AsteroService(
            self.store,
            text_executor=self._run_text,
            voice_executor=self.voice,
            readiness=self._readiness,
            trace_recorder=event_recorder,
            workers=1,
        )
        self.listener = ResidentListener(self.app, self.voice,
                                         session_id=self.config.session_id,
                                         enabled=self.config.listen)
        self.http = LocalAsteroHTTPServer(
            self.app, host=self.config.host, port=self.config.port,
            request_logger=self._log_http,
        )
        self._stop = threading.Event()

    def _readiness(self):
        return {**self.voice.readiness(), "listener": self.listener.status()}

    def _run_text(self, request: TurnRequest):
        # This explicitly diagnostic path has no audio and cannot count as
        # acoustic acceptance. It still exercises persistent agent state.
        return self.agent.submit_turn(
            request.text or "", request.session_id,
            trace_id=request.trace_id, turn_id=request.turn_id,
        )

    @staticmethod
    def _log_http(method: str, route: str, status: int, elapsed_ms: float) -> None:
        logging.getLogger("astero.http").info(
            "method=%s route=%s status=%d elapsed_ms=%.3f",
            method, route, status, elapsed_ms,
        )

    def serve_forever(self) -> None:
        self.http.start()
        self.listener.start()
        host, port = self.http.address
        logging.getLogger("astero.service").info(
            "Astero service listening on http://%s:%d", host, port,
        )
        previous_term = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.close()
            signal.signal(signal.SIGTERM, previous_term)

    def close(self) -> None:
        self._stop.set()
        self.http.close()
        self.listener.stop()
        self.app.close(wait=True)
        self.voice.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Project Astero local service")
    parser.add_argument("--data-root", type=Path,
                        default=Path.home() / ".local" / "share" / "astero")
    parser.add_argument("--host", default="127.0.0.1",
                        help="must be a loopback address")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--channel", type=int, default=3, choices=range(7))
    parser.add_argument("--wake-secondary-channel", type=int, default=5, choices=range(7),
                        help="second microphone for keyword detection only; same as channel disables it")
    parser.add_argument("--listen", action="store_true", help="automatically rearm wake listening after each turn")
    parser.add_argument("--session-id", default="local-voice")
    parser.add_argument("--gain", type=float, default=16.0)
    parser.add_argument("--end-silence", type=float, default=0.8,
                        help="endpoint silence in seconds after VAD releases speech")
    parser.add_argument("--voice-bridge-url", default="http://127.0.0.1:8765")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        service = LocalService(LocalServiceConfig(
            data_root=args.data_root, host=args.host, port=args.port,
            channel=args.channel, gain=args.gain,
            wake_secondary_channel=args.wake_secondary_channel,
            end_silence_seconds=args.end_silence,
            listen=args.listen, session_id=args.session_id,
            voice_bridge_url=args.voice_bridge_url,
        ))
    except (OSError, ValueError, RuntimeError) as exc:
        logging.getLogger("astero.service").error("service initialization failed: %s", type(exc).__name__)
        return 1
    service.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
