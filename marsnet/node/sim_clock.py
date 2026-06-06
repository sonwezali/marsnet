from __future__ import annotations
import threading
import time


class SimClock:
    def __init__(self, start: float = 0.0):
        self._start = start
        self._lock = threading.Lock()

    def sim_time(self) -> float:
        with self._lock:
            if self._start == 0.0:
                return 0.0
            return time.time() - self._start

    def adopt(self, ts: float) -> bool:
        with self._lock:
            if self._start == 0.0 and ts > 0.0:
                self._start = ts
                return True
            return False

    def is_set(self) -> bool:
        with self._lock:
            return self._start > 0.0

    @property
    def value(self) -> float:
        with self._lock:
            return self._start
