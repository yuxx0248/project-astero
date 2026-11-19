"""Serial, resident wake listening supervisor with bounded retry backoff."""
from __future__ import annotations

import logging
import threading


class ResidentListener:
    def __init__(self, app, voice, *, session_id="local-voice", enabled=False,
                 poll_seconds=0.1, retry_seconds=2.0):
        self.app = app
        self.voice = voice
        self.session_id = session_id
        self.enabled = enabled
        self.poll_seconds = poll_seconds
        self.retry_seconds = retry_seconds
        self._stop = threading.Event()
        self._thread = None
        self._state = "idle" if enabled else "disabled"
        self._active_turn_id = None

    def status(self):
        activity = self.voice.activity()
        return {"enabled": self.enabled, "session_id": self.session_id,
                "state": activity["state"] if activity["active_turn_id"] else self._state,
                "listening": activity["listening"],
                "active_turn_id": activity["active_turn_id"] or self._active_turn_id}

    def start(self):
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="astero-listener", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.voice.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._state = "stopped"

    def _run(self):
        failures = 0
        while not self._stop.is_set():
            try:
                self._state = "starting"
                result = self.app.submit_turn(session_id=self.session_id, mode="voice")
                self._active_turn_id = result["turn_id"]
                while not self._stop.wait(self.poll_seconds):
                    # Read the durable status only: full trace reconstruction on
                    # every poll would grow expensive over long idle captures.
                    row = self.app.store.get_turn(self._active_turn_id)
                    if row is not None and row.status not in {"queued", "running"}:
                        break
                if self._stop.is_set():
                    return
                failed = row.status in {"failed", "interrupted"}
            except Exception as exc:
                logging.getLogger("astero.listener").error("listener retry: %s", type(exc).__name__)
                failed = True
            self._active_turn_id = None
            failures = min(failures + 1, 5) if failed else 0
            self._state = "retrying" if failed else "rearming"
            delay = min(30.0, self.retry_seconds * 2 ** (failures - 1)) if failed else 0.05
            if self._stop.wait(delay):
                return
