from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Optional
from marsnet.node.contact_plan import ContactEntry

PLANNING_HORIZON = 3600.0  # seconds to look ahead


@dataclass
class CGRResult:
    next_hop_contact: str    # contact_id to use as first hop
    earliest_arrival: float  # sim_time when bundle reaches destination


def cgr_route(
    contacts: list[ContactEntry],
    source: str,
    destination: str,
    current_time: float,
    sim_start: float,
    volume_used: Optional[dict[tuple[str, float], int]] = None,
) -> Optional[CGRResult]:
    """
    Run CGR over a snapshot of active contacts.

    Contacts are treated as BIDIRECTIONAL for data: a bundle may travel
    either direction across an active contact window.

    volume_used: maps (contact_id, window_start) -> bytes already allocated.
    current_time is seconds since sim_start.
    sim_start is kept for API symmetry but unused internally.
    """
    if volume_used is None:
        volume_used = {}

    # Build flat list of directed edges.
    # Each contact window yields TWO directed edges (one per direction).
    # Each edge: (window_end, window_start, contact, sender, receiver)
    edges: list[tuple[float, float, ContactEntry, str, str]] = []
    for c in contacts:
        if c.status != "active":
            continue
        for (ws, we) in c.windows_in_horizon(from_time=current_time,
                                              horizon=PLANNING_HORIZON):
            used = volume_used.get((c.id, ws), 0)
            remaining = c.capacity_bytes - used
            if remaining <= 0:
                continue
            # Emit both directions
            edges.append((we, ws, c, c.from_node, c.to_node))
            edges.append((we, ws, c, c.to_node, c.from_node))

    # Earliest-deadline-first: sorting by window-end means whenever we relax an edge,
    # every edge that could have produced an earlier arrival at its sender has already
    # been processed.
    # Sort by window-end ascending, then window-start.
    # Use explicit key to avoid comparing ContactEntry objects (not orderable).
    edges.sort(key=lambda e: (e[0], e[1]))

    # earliest[node] = earliest sim_time a bundle can be at that node
    earliest: dict[str, float] = {source: current_time}
    # prev[receiver] = (contact, sender) that achieves earliest[receiver]
    prev: dict[str, tuple[ContactEntry, str]] = {}

    for (we, ws, contact, sender, receiver) in edges:
        if sender not in earliest:
            continue  # can't get a bundle to the sender yet

        # Bundle boards the window when it's ready, or when the window opens
        departure = max(ws, earliest[sender])

        if departure >= we:
            continue  # bundle misses this window entirely

        # No OWLT — arrival is instantaneous
        arrival = departure  # OWLT = 0, so arrival equals board (departure) time

        if arrival < earliest.get(receiver, math.inf):
            earliest[receiver] = arrival
            prev[receiver] = (contact, sender)

    if destination not in prev:
        return None

    # Walk back from destination to source to find the first-hop contact
    node = destination
    seen: set[str] = set()
    while True:
        if node in seen:
            raise RuntimeError(f"CGR walk-back cycle detected at {node!r}")
        seen.add(node)
        contact, sender = prev[node]
        if sender == source:
            break
        node = sender

    first_hop_contact, _ = prev[node]
    return CGRResult(
        next_hop_contact=first_hop_contact.id,
        earliest_arrival=earliest[destination],
    )


def is_critical_contact(
    contacts: list[ContactEntry],
    contact_id: str,
    source: str,
    destination: str,
    current_time: float,
    sim_start: float,
) -> bool:
    """Return True if removing contact_id makes destination unreachable."""
    filtered = [c for c in contacts if c.id != contact_id]
    return cgr_route(filtered, source, destination, current_time, sim_start) is None
