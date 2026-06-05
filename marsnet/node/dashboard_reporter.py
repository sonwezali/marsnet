from __future__ import annotations
import json
import queue
import threading
import urllib.request
import urllib.error
from typing import Any


class DashboardReporter:
    def __init__(self, dashboard_url: str, node_name: str):
        self.url = dashboard_url.rstrip("/") + "/event"
        self.node_name = node_name
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()

    def post(self, event_type: str, data: dict[str, Any]) -> None:
        """Non-blocking. Puts event on queue for background delivery."""
        self._queue.put({"event": event_type, "node": self.node_name, **data})

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._run, daemon=True,
                             name="dashboard-reporter")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._send(event)

    def _send(self, event: dict) -> None:
        body = json.dumps(event).encode()
        req = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=2.0)
        except (urllib.error.URLError, OSError):
            pass  # dashboard may be offline; drop event silently
