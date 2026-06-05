from __future__ import annotations
import threading
import time
from typing import Callable

from marsnet.node.bundle_store import BundleStore


class TTLReaper:
    def __init__(self, bundle_store: BundleStore, interval: float = 1.0,
                 on_drop: Callable[[str], None] = None):
        self.store = bundle_store
        self.interval = interval
        self.on_drop = on_drop or (lambda bid: None)
        self._stop = threading.Event()

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._run, daemon=True, name="ttl-reaper")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            dropped = self.store.sweep_expired()
            for bid in dropped:
                self.on_drop(bid)
            self._stop.wait(self.interval)
