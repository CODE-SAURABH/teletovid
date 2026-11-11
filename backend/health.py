from __future__ import annotations

import json
import logging
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

if __package__:
    from .state import AppState
else:
    from state import AppState

logger = logging.getLogger(__name__)


class HealthServer:
    def __init__(self, port: int, state: AppState, worker) -> None:
        self.port = port
        self.state = state
        self.worker = worker
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._server is not None:
            return
        state = self.state
        worker = self.worker

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.split("?")[0] not in ("/", "/healthz"):
                    self.send_error(404)
                    return
                current = worker.current_job
                remaining = state.daily_remaining
                payload = {
                    "ok": worker.ready,
                    "paused": state.paused,
                    "queued": len(state.queue),
                    "daily": {
                        "date": state.daily_date,
                        "used": state.daily_count,
                        "limit": state.daily_limit,
                        "remaining": remaining,
                    },
                    "current": (
                        {"id": current.id, "topic": current.topic, "status": current.status}
                        if current is not None
                        else None
                    ),
                    "error": worker.init_error,
                }
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args) -> None:
                logger.debug("health: " + fmt, *args)

        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="health", daemon=True)
        self._thread.start()
        logger.info("Health server listening on port %s", self.port)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None


class KeepAlive:
    """Ping the public health endpoint so a free Render service stays awake."""

    def __init__(
        self,
        base_url: str,
        interval_seconds: int = 840,
        initial_delay: int = 60,
    ) -> None:
        self.target = base_url.rstrip("/") + "/healthz"
        self.interval_seconds = max(120, int(interval_seconds))
        self.initial_delay = max(10, int(initial_delay))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="keepalive", daemon=True)
        self._thread.start()
        logger.info(
            "Keepalive started: %s every %ss", self.target, self.interval_seconds
        )

    def _run(self) -> None:
        if self._stop.wait(self.initial_delay):
            return
        while not self._stop.is_set():
            try:
                request = urllib.request.Request(
                    self.target,
                    headers={"User-Agent": "tele-to-vid-keepalive/1.0"},
                )
                with urllib.request.urlopen(request, timeout=20) as response:
                    response.read(256)
                logger.info("Keepalive ping ok")
            except Exception as exc:
                logger.warning("Keepalive ping failed: %s", exc)
            if self._stop.wait(self.interval_seconds):
                return

    def stop(self) -> None:
        self._stop.set()
