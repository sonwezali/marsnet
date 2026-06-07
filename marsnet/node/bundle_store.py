import threading
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class Bundle:
    bundle_id: str
    src: str
    dst: str
    ttl: float
    created_at: float
    image_id: str
    fragment_offset: int
    total_size: int
    data: bytes
    next_hop_contact: Optional[str] = None
    prev_hop: Optional[str] = None

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl

    def is_expired(self) -> bool:
        return time.time() > self.expires_at


class BundleStore:
    def __init__(self):
        self._store: dict[str, Bundle] = {}
        self._lock = threading.Lock()

    def insert(self, bundle: Bundle) -> None:
        with self._lock:
            self._store[bundle.bundle_id] = bundle

    def get(self, bundle_id: str) -> Optional[Bundle]:
        with self._lock:
            return self._store.get(bundle_id)

    def delete(self, bundle_id: str) -> None:
        with self._lock:
            self._store.pop(bundle_id, None)

    def get_by_contact(self, contact_id: str) -> list[Bundle]:
        with self._lock:
            return [b for b in self._store.values()
                    if b.next_hop_contact == contact_id]

    def all(self) -> list[Bundle]:
        with self._lock:
            return list(self._store.values())

    def update_next_hop(self, bundle_id: str, contact_id: Optional[str]) -> None:
        with self._lock:
            if bundle_id in self._store:
                self._store[bundle_id].next_hop_contact = contact_id

    def sweep_expired(self) -> list[str]:
        now = time.time()
        with self._lock:
            dropped = [bid for bid, b in self._store.items()
                       if b.expires_at < now]
            for bid in dropped:
                del self._store[bid]
        return dropped
