from __future__ import annotations
import threading


class VolumeTracker:
    def __init__(self):
        self._used: dict[tuple[str, float], int] = {}
        self._alloc: dict[str, tuple[str, float, int]] = {}
        self._lock = threading.Lock()

    def allocate(self, bundle_id: str, contact_id: str,
                 window_start: float, nbytes: int) -> None:
        with self._lock:
            self._release_locked(bundle_id)
            key = (contact_id, window_start)
            self._used[key] = self._used.get(key, 0) + nbytes
            self._alloc[bundle_id] = (contact_id, window_start, nbytes)

    def release(self, bundle_id: str) -> None:
        with self._lock:
            self._release_locked(bundle_id)

    def _release_locked(self, bundle_id: str) -> None:
        prev = self._alloc.pop(bundle_id, None)
        if prev is None:
            return
        contact_id, window_start, nbytes = prev
        key = (contact_id, window_start)
        remaining = self._used.get(key, 0) - nbytes
        if remaining > 0:
            self._used[key] = remaining
        else:
            self._used.pop(key, None)

    def used(self) -> dict[tuple[str, float], int]:
        with self._lock:
            return dict(self._used)
