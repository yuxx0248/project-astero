"""Loopback-only JSON API for a running :class:`AsteroService`."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from .app import AsteroService, ServiceError


MAX_REQUEST_BYTES = 64 * 1024


class LocalAsteroHTTPServer:
    """Serve the local API on loopback only; never bind a LAN interface."""

    def __init__(self, service: AsteroService, host: str = "127.0.0.1", port: int = 0,
                 *, request_logger: Callable[[str, str, int, float], None] | None = None) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Astero API must bind to a loopback address")
        self.service = service
        self.request_logger = request_logger
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AsteroLocal/1"
            sys_version = ""

            def do_GET(self):
                owner._handle(self, "GET")

            def do_POST(self):
                owner._handle(self, "POST")

            def do_DELETE(self):
                self._json(405, {"error": "method_not_allowed"})

            def log_message(self, format, *args):
                # Avoid default access logging of arbitrary request strings.
                return

            def _json(self, status: int, payload: object):
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                                  default=_json_default).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="astero-http", daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def _handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        path = urlsplit(handler.path).path
        request_started_ns = time.monotonic_ns()
        status = 500
        try:
            if method == "GET" and path == "/health":
                payload = self.service.health()
                status = 200 if payload.get("ready") is True else 503
                handler._json(status, payload)
            elif method == "POST" and path == "/v1/turns":
                body = self._body(handler)
                result = self.service.submit_turn(
                    session_id=body.get("session_id"), mode=body.get("mode", "voice"),
                    text=body.get("text"), turn_id=body.get("turn_id"),
                    trace_id=body.get("trace_id"),
                )
                status = 202
                handler._json(status, result)
            elif method == "GET" and path.startswith("/v1/turns/"):
                turn_id = path.removeprefix("/v1/turns/")
                if not turn_id or "/" in turn_id:
                    status = 400
                    handler._json(status, {"error": "invalid_turn_id"})
                else:
                    status = 200
                    handler._json(status, self.service.get_turn(turn_id))
            elif method == "GET" and path.startswith("/v1/sessions/") and path.endswith("/turns"):
                session_id = path[len("/v1/sessions/"):-len("/turns")].strip("/")
                query = parse_qs(urlsplit(handler.path).query)
                limit_raw = query.get("limit", ["100"])[0]
                limit = int(limit_raw)
                status = 200
                handler._json(status, {"session_id": session_id,
                                       "turns": self.service.list_turns(session_id, limit)})
            else:
                status = 404
                handler._json(status, {"error": "not_found"})
        except KeyError:
            status = 404
            handler._json(status, {"error": "not_found"})
        except (ValueError, TypeError, json.JSONDecodeError):
            status = 400
            handler._json(status, {"error": "invalid_request"})
        except ServiceError as exc:
            status = 503
            handler._json(status, {"error": "service_unavailable", "detail": str(exc)})
        except Exception:
            status = 500
            handler._json(status, {"error": "internal_error"})
        finally:
            if self.request_logger is not None:
                # Only method, route template, and status are exposed. Never
                # include body, query values, transcripts, or credentials.
                route = _route_template(path)
                try:
                    elapsed_ms = max(0.0, (time.monotonic_ns() - request_started_ns) / 1_000_000)
                    self.request_logger(method, route, status, elapsed_ms)
                except Exception:
                    pass

    @staticmethod
    def _body(handler: BaseHTTPRequestHandler) -> dict[str, object]:
        raw_length = handler.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required")
        length = int(raw_length)
        if not 0 <= length <= MAX_REQUEST_BYTES:
            raise ValueError("request body is too large")
        raw = handler.rfile.read(length)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request JSON must be an object")
        return value


def _route_template(path: str) -> str:
    if path.startswith("/v1/turns/"):
        return "/v1/turns/{turn_id}"
    if path.startswith("/v1/sessions/") and path.endswith("/turns"):
        return "/v1/sessions/{session_id}/turns"
    return path


def _json_default(value: object):
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    return str(value)
