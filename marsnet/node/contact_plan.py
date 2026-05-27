from __future__ import annotations
import json
import math
import threading
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ContactEntry:
    id: str               # "relay:1"
    created_by: str       # node name that created this contact
    from_node: str        # TCP initiator
    to_node: str          # TCP acceptor
    phase: float          # seconds from sim_start when first window opens
    period: float         # seconds between window starts
    duration: float       # seconds each window stays open
    rate_bps: int         # bits per second
    status: str = "active"  # "active" | "cancelled"

    @property
    def capacity_bytes(self) -> int:
        return int(self.rate_bps * self.duration / 8)

    def next_window(self, after: float) -> tuple[float, float]:
        """Return (start, end) of the next window that starts at or before `after`
        and ends after `after`, or the next future window."""
        n = max(0, math.floor((after - self.phase) / self.period))
        # check if we are inside window n
        start = self.phase + n * self.period
        end = start + self.duration
        if end > after:
            return start, end
        # already past this window, return n+1
        n += 1
        start = self.phase + n * self.period
        return start, start + self.duration

    def windows_in_horizon(self, from_time: float, horizon: float) -> list[tuple[float, float]]:
        """Return all (start, end) windows within [from_time, from_time + horizon]."""
        until = from_time + horizon
        results = []
        n = max(0, math.floor((from_time - self.phase) / self.period))
        while True:
            start = self.phase + n * self.period
            if start > until:
                break
            end = start + self.duration
            if end > from_time:
                results.append((start, end))
            n += 1
        return results

    def to_dict(self) -> dict:
        return {
            "id": self.id, "created_by": self.created_by,
            "from": self.from_node, "to": self.to_node,
            "phase": self.phase, "period": self.period,
            "duration": self.duration, "rate_bps": self.rate_bps,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ContactEntry:
        return cls(id=d["id"], created_by=d["created_by"],
                   from_node=d["from"], to_node=d["to"],
                   phase=d["phase"], period=d["period"],
                   duration=d["duration"], rate_bps=d["rate_bps"],
                   status=d.get("status", "active"))


class ContactPlan:
    def __init__(self, version: int, sim_start: float,
                 contacts: list[ContactEntry]):
        self.version = version
        self.sim_start = sim_start
        self._contacts: dict[str, ContactEntry] = {c.id: c for c in contacts}
        self._lock = threading.Lock()

    @property
    def contacts(self) -> list[ContactEntry]:
        with self._lock:
            return list(self._contacts.values())

    def contact_by_id(self, contact_id: str) -> Optional[ContactEntry]:
        with self._lock:
            return self._contacts.get(contact_id)

    def snapshot(self) -> list[ContactEntry]:
        """Return a copy of active contacts for CGR (no lock held during computation)."""
        with self._lock:
            return [c for c in self._contacts.values() if c.status == "active"]

    def add_contact(self, entry: ContactEntry) -> None:
        with self._lock:
            self._contacts[entry.id] = entry
            self.version += 1

    def cancel_contact(self, contact_id: str) -> None:
        with self._lock:
            if contact_id in self._contacts:
                self._contacts[contact_id].status = "cancelled"
                self.version += 1

    def merge(self, other: ContactPlan) -> bool:
        """
        Merge other plan into self. Rules:
        - Take higher-version as base.
        - Cancellations from either side always win (one-way: active→cancelled).
        - New contacts from either side are added.
        Returns True if anything changed.
        """
        changed = False
        with self._lock:
            # Add any contacts present in other but not in self
            for cid, entry in other._contacts.items():
                if cid not in self._contacts:
                    self._contacts[cid] = entry
                    changed = True
                else:
                    # Propagate cancellation
                    if entry.status == "cancelled" and self._contacts[cid].status == "active":
                        self._contacts[cid].status = "cancelled"
                        changed = True
            if changed:
                self.version = max(self.version, other.version) + 1
        return changed

    def is_lost(self, current_sim_time: float) -> bool:
        """True if there are no future active contact windows."""
        with self._lock:
            for c in self._contacts.values():
                if c.status != "active":
                    continue
                _, end = c.next_window(after=current_sim_time)
                if end > current_sim_time:
                    return False
            return True

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "version": self.version,
                "sim_start": self.sim_start,
                "contacts": [c.to_dict() for c in self._contacts.values()],
            }

    @classmethod
    def from_dict(cls, d: dict) -> ContactPlan:
        contacts = [ContactEntry.from_dict(c) for c in d["contacts"]]
        return cls(version=d["version"], sim_start=d["sim_start"],
                   contacts=contacts)

    @classmethod
    def load(cls, path: str) -> ContactPlan:
        with open(path) as f:
            return cls.from_dict(json.load(f))
