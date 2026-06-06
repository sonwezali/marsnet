# MarsNet Discrepancy Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Conventions:**
> - All Python commands use the project virtual environment at `.venv/`. Use `.venv/bin/python` and `.venv/bin/pytest`.
> - 🧑 **User step** — steps that run `git`. Do **not** run them yourself; pause and ask the user to run them, then continue.

**Goal:** Close three discrepancies between `docs/DESIGN.md` and the implementation: (1) the dashboard never draws contact lines because `contact_open` events omit `from`/`to`; (2) per-window volume constraints are designed but never enforced at routing time; (3) `plans/initial_plan.json` ships with `sim_start: 0.0`, making demo timestamps unreadable. Also mark the completed steps in the original plan.

**Architecture:** Four code changes plus one doc-bookkeeping change.
- The dashboard fix adds `from`/`to` to the `contact_open` event in `connection_handler.py` (the frontend `mars.js` already consumes them).
- Volume enforcement introduces a new `VolumeTracker` (bytes allocated per `(contact_id, window_start)`), extends `CGRResult` with the first-hop window start so allocations can be keyed correctly, and wires allocate/release calls into `ContactManager` (inject, reassign), `ConnectionHandler` (on BUNDLE_ACK), and the TTL reaper (on drop).
- A standalone `scripts/set_sim_start.py` rewrites a plan's `sim_start` to the current Unix time for demo runs.

**Tech Stack:** Python 3.11+ stdlib (`threading`, `json`, `argparse`), `pytest`. No new dependencies.

---

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `marsnet/node/connection_handler.py` | Modify | Add `from`/`to` to `contact_open`; add `on_bundle_acked` hook for volume release |
| `marsnet/node/volume_tracker.py` | Create | Track bytes allocated per `(contact_id, window_start)`, keyed by `bundle_id` for release |
| `tests/test_volume_tracker.py` | Create | Unit tests for `VolumeTracker` |
| `marsnet/node/cgr.py` | Modify | Add `first_hop_window_start` to `CGRResult`; track window start in walk-back |
| `tests/test_cgr.py` | Modify | Tests asserting `first_hop_window_start` |
| `marsnet/node/contact_manager.py` | Modify | Own a `VolumeTracker`; allocate on inject/reassign, release on ack/drop; pass `volume_used` to CGR |
| `tests/test_contact_manager.py` | Modify | Tests for allocation, full-window skip, release |
| `marsnet/node/main.py` | Modify | TTL drop callback releases volume; pass `on_bundle_acked` wiring already lives in ContactManager |
| `scripts/set_sim_start.py` | Create | CLI to set a plan's `sim_start` to now |
| `docs/DESIGN.md` | Modify | Update §5 `CGRResult` shape and volume-tracking note |
| `docs/plans/2026-05-25-marsnet.md` | Modify | Check off completed steps (Tasks 9–18) |

---

## Task 1: Dashboard contact lines — add `from`/`to` to `contact_open`

The frontend (`marsnet/dashboard/static/mars.js:46`) already reads `ev.from` / `ev.to` into `contactMap` and `drawContactLines()` uses it. The node side never sends them, so `contactMap` stays empty and no lines are drawn. The `ConnectionHandler` knows `self.contact_id` and holds `self.plan`, so it can look the contact up.

**Files:**
- Modify: `marsnet/node/connection_handler.py` (the `contact_open` post inside `_handshake`, currently lines 102-105)

- [ ] **Step 1: Add `from`/`to` to the `contact_open` event**

In `marsnet/node/connection_handler.py`, replace the `contact_open` block at the end of `_handshake`:

```python
        self._state = State.ACTIVE
        if self.reporter:
            self.reporter.post("contact_open", {
                "contact_id": self.contact_id, "ts": self.sim_time()
            })
```

with:

```python
        self._state = State.ACTIVE
        if self.reporter:
            contact = self.plan.contact_by_id(self.contact_id)
            evt = {"contact_id": self.contact_id, "ts": self.sim_time()}
            if contact is not None:
                evt["from"] = contact.from_node
                evt["to"] = contact.to_node
            self.reporter.post("contact_open", evt)
```

- [ ] **Step 2: Verify the existing suite still passes**

Run: `.venv/bin/pytest tests/ -q`
Expected: all PASS (no test asserts on `contact_open` payload; this change is non-breaking). The integration test `tests/test_integration.py` still delivers the image end-to-end.

- [ ] **Step 3: Commit** 🧑

> 🧑 **User step:** Run the git commands below yourself — do not execute as the agent.

```bash
git add marsnet/node/connection_handler.py
git commit -m "fix: include from/to in contact_open event so dashboard draws contact lines"
```

---

## Task 2: VolumeTracker

A small, thread-safe accumulator of bytes allocated to each `(contact_id, window_start)` window instance, keyed by `bundle_id` so an allocation can be released when the bundle is delivered, dropped, or rerouted. `used()` returns the snapshot dict that CGR consumes as `volume_used`.

**Files:**
- Create: `marsnet/node/volume_tracker.py`
- Create: `tests/test_volume_tracker.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_volume_tracker.py
from marsnet.node.volume_tracker import VolumeTracker


def test_allocate_accumulates():
    vt = VolumeTracker()
    vt.allocate("b1", "relay:1", 10.0, 100)
    vt.allocate("b2", "relay:1", 10.0, 50)
    assert vt.used()[("relay:1", 10.0)] == 150


def test_release_subtracts():
    vt = VolumeTracker()
    vt.allocate("b1", "relay:1", 10.0, 100)
    vt.allocate("b2", "relay:1", 10.0, 50)
    vt.release("b1")
    assert vt.used()[("relay:1", 10.0)] == 50


def test_release_removes_key_when_zero():
    vt = VolumeTracker()
    vt.allocate("b1", "relay:1", 10.0, 100)
    vt.release("b1")
    assert ("relay:1", 10.0) not in vt.used()


def test_reallocate_same_bundle_replaces_prior():
    vt = VolumeTracker()
    vt.allocate("b1", "relay:1", 10.0, 100)
    vt.allocate("b1", "relay:2", 20.0, 30)  # bundle rerouted
    used = vt.used()
    assert ("relay:1", 10.0) not in used
    assert used[("relay:2", 20.0)] == 30


def test_release_unknown_is_noop():
    vt = VolumeTracker()
    vt.release("nope")
    assert vt.used() == {}


def test_used_returns_copy():
    vt = VolumeTracker()
    vt.allocate("b1", "relay:1", 10.0, 100)
    snap = vt.used()
    snap[("relay:1", 10.0)] = 999
    assert vt.used()[("relay:1", 10.0)] == 100
```

- [ ] **Step 2: Run the tests — verify they fail**

Run: `.venv/bin/pytest tests/test_volume_tracker.py -v`
Expected: `ModuleNotFoundError: No module named 'marsnet.node.volume_tracker'`.

- [ ] **Step 3: Implement `marsnet/node/volume_tracker.py`**

```python
from __future__ import annotations
import threading


class VolumeTracker:
    """Tracks bytes allocated to each (contact_id, window_start) window instance
    so CGR can skip windows whose per-window capacity is exhausted.

    Allocations are keyed by bundle_id so they can be released when a bundle is
    delivered (BUNDLE_ACK), dropped (TTL), or rerouted (re-allocate replaces the
    prior allocation for that bundle).
    """

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
```

- [ ] **Step 4: Run the tests — verify they pass**

Run: `.venv/bin/pytest tests/test_volume_tracker.py -v`
Expected: all 6 PASS.

- [ ] **Step 5: Commit** 🧑

> 🧑 **User step:** Run the git commands below yourself — do not execute as the agent.

```bash
git add marsnet/node/volume_tracker.py tests/test_volume_tracker.py
git commit -m "feat: VolumeTracker for per-window byte allocation"
```

---

## Task 3: CGR reports the first-hop window start

To allocate volume against the correct window instance, the caller must know which window the first hop departs on. `CGRResult` currently exposes only `next_hop_contact` and `earliest_arrival`. Add `first_hop_window_start`. This requires the walk-back to carry the window start alongside `(contact, sender)`.

**Files:**
- Modify: `marsnet/node/cgr.py`
- Modify: `tests/test_cgr.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_cgr.py`:

```python
def test_first_hop_window_start_direct():
    contacts = [entry("base:1", "rover_a", "base", phase=10, period=120, duration=20)]
    result = cgr_route(contacts, source="rover_a", destination="base",
                       current_time=0.0, sim_start=SIM_START)
    assert result.first_hop_window_start == 10.0


def test_first_hop_window_start_multi_hop():
    contacts = [
        entry("relay:1", "relay", "rover_b", phase=5,  period=120, duration=20),
        entry("relay:2", "relay", "base",    phase=30, period=120, duration=20),
    ]
    result = cgr_route(contacts, source="rover_b", destination="base",
                       current_time=0.0, sim_start=SIM_START)
    # first hop is rover_b -> relay over relay:1, whose window opens at 5
    assert result.next_hop_contact == "relay:1"
    assert result.first_hop_window_start == 5.0


def test_first_hop_window_start_skips_full_window():
    contacts = [entry("base:1", "rover_a", "base", phase=10, period=120,
                      duration=5, rate_bps=8)]  # capacity = 5 bytes
    volume_used = {("base:1", 10.0): 5}  # window at 10 is full
    result = cgr_route(contacts, source="rover_a", destination="base",
                       current_time=0.0, sim_start=SIM_START,
                       volume_used=volume_used)
    assert result.first_hop_window_start == 130.0
```

- [ ] **Step 2: Run the tests — verify they fail**

Run: `.venv/bin/pytest tests/test_cgr.py -k first_hop_window_start -v`
Expected: FAIL with `AttributeError: 'CGRResult' object has no attribute 'first_hop_window_start'`.

- [ ] **Step 3: Implement the change in `marsnet/node/cgr.py`**

Replace the `CGRResult` dataclass:

```python
@dataclass
class CGRResult:
    next_hop_contact: str    
    earliest_arrival: float  
```

with:

```python
@dataclass
class CGRResult:
    next_hop_contact: str
    earliest_arrival: float
    first_hop_window_start: float
```

In `cgr_route`, change the `prev` type annotation and the relaxation store. Replace:

```python
    earliest: dict[str, float] = {source: current_time}
    prev: dict[str, tuple[ContactEntry, str]] = {}

    for (we, ws, contact, sender, receiver) in edges:
        if sender not in earliest:
            continue  # can't get a bundle to the sender yet

        # Bundle boards the window when it's ready, or when the window opens
        departure = max(ws, earliest[sender])

        if departure >= we:
            continue  # bundle misses this window entirely

        arrival = departure  # no distance delay

        if arrival < earliest.get(receiver, math.inf):
            earliest[receiver] = arrival
            prev[receiver] = (contact, sender)
```

with:

```python
    earliest: dict[str, float] = {source: current_time}
    prev: dict[str, tuple[ContactEntry, str, float]] = {}

    for (we, ws, contact, sender, receiver) in edges:
        if sender not in earliest:
            continue  # can't get a bundle to the sender yet

        # Bundle boards the window when it's ready, or when the window opens
        departure = max(ws, earliest[sender])

        if departure >= we:
            continue  # bundle misses this window entirely

        arrival = departure  # no distance delay

        if arrival < earliest.get(receiver, math.inf):
            earliest[receiver] = arrival
            prev[receiver] = (contact, sender, ws)
```

Replace the walk-back and return:

```python
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
```

with:

```python
    node = destination
    seen: set[str] = set()
    while True:
        if node in seen:
            raise RuntimeError(f"CGR walk-back cycle detected at {node!r}")
        seen.add(node)
        contact, sender, _ws = prev[node]
        if sender == source:
            break
        node = sender

    first_hop_contact, _, first_hop_ws = prev[node]
    return CGRResult(
        next_hop_contact=first_hop_contact.id,
        earliest_arrival=earliest[destination],
        first_hop_window_start=first_hop_ws,
    )
```

- [ ] **Step 4: Run the tests — verify they pass**

Run: `.venv/bin/pytest tests/test_cgr.py -v`
Expected: all PASS (the three new tests plus all pre-existing CGR tests, which do not construct `CGRResult` directly and so are unaffected).

- [ ] **Step 5: Commit** 🧑

> 🧑 **User step:** Run the git commands below yourself — do not execute as the agent.

```bash
git add marsnet/node/cgr.py tests/test_cgr.py
git commit -m "feat: CGRResult reports first_hop_window_start for volume allocation"
```

---

## Task 4: Enforce volume constraints in ContactManager

Wire a `VolumeTracker` into `ContactManager`. Allocate when a bundle is injected or reassigned, passing `volume_used` to CGR so full windows are skipped. Release when a bundle is acknowledged (custody transferred) or dropped (TTL). Allocation size is `len(bundle.data)` — the encrypted fragment payload.

**Files:**
- Modify: `marsnet/node/contact_manager.py`
- Modify: `marsnet/node/connection_handler.py`
- Modify: `marsnet/node/main.py`
- Modify: `tests/test_contact_manager.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_contact_manager.py` (the helpers `make_entry`, `make_manager`, and imports `ContactEntry`, `ContactPlan`, `Bundle`, `BundleStore`, `ContactManager`, `time` already exist at the top of that file):

```python
def make_sized_bundle(bundle_id, data: bytes):
    return Bundle(
        bundle_id=bundle_id, src="rover_a", dst="base", ttl=120.0,
        created_at=time.time(), image_id="img",
        fragment_offset=0, total_size=1024, data=data,
        next_hop_contact=None,
    )


def test_inject_bundle_allocates_volume():
    mgr, plan, store = make_manager()
    mgr.start()
    b = make_sized_bundle("rover_a:img:0", b"0123456789")  # 10 bytes
    mgr.inject_bundle(b)
    # base:1 opens at phase=10
    assert mgr._volume.used()[("base:1", 10.0)] == 10


def test_inject_bundle_skips_full_window():
    # capacity = rate_bps * duration / 8 = 8 * 5 / 8 = 5 bytes
    contacts = [make_entry("base:1", from_node="rover_a", to_node="base",
                           phase=10.0, period=120.0, duration=5.0, rate_bps=8)]
    mgr, plan, store = make_manager(contacts=contacts)
    mgr.start()
    b1 = make_sized_bundle("rover_a:img:0", b"12345")   # fills window at 10
    b2 = make_sized_bundle("rover_a:img:5", b"67890")   # must spill to next window
    mgr.inject_bundle(b1)
    mgr.inject_bundle(b2)
    used = mgr._volume.used()
    assert used[("base:1", 10.0)] == 5
    assert used[("base:1", 130.0)] == 5   # 10 + period(120)
    assert b2.next_hop_contact == "base:1"


def test_release_volume_frees_window():
    contacts = [make_entry("base:1", from_node="rover_a", to_node="base",
                           phase=10.0, period=120.0, duration=5.0, rate_bps=8)]
    mgr, plan, store = make_manager(contacts=contacts)
    mgr.start()
    b1 = make_sized_bundle("rover_a:img:0", b"12345")
    mgr.inject_bundle(b1)
    assert mgr._volume.used()[("base:1", 10.0)] == 5
    mgr.release_volume("rover_a:img:0")
    assert ("base:1", 10.0) not in mgr._volume.used()


def test_on_bundle_acked_releases_and_deletes():
    mgr, plan, store = make_manager()
    mgr.start()
    b = make_sized_bundle("rover_a:img:0", b"0123456789")
    mgr.inject_bundle(b)
    assert store.get("rover_a:img:0") is b
    mgr.on_bundle_acked("rover_a:img:0")
    assert store.get("rover_a:img:0") is None
    assert ("base:1", 10.0) not in mgr._volume.used()
```

- [ ] **Step 2: Run the tests — verify they fail**

Run: `.venv/bin/pytest tests/test_contact_manager.py -k "volume or acked" -v`
Expected: FAIL with `AttributeError: 'ContactManager' object has no attribute '_volume'`.

- [ ] **Step 3: Add the `VolumeTracker` to `ContactManager`**

In `marsnet/node/contact_manager.py`, add the import near the other node imports:

```python
from marsnet.node.connection_handler import ConnectionHandler
from marsnet.node.volume_tracker import VolumeTracker
```

In `__init__`, after `self._outbound_queues: dict[str, queue.Queue] = {}`, add:

```python
        self._volume = VolumeTracker()
```

- [ ] **Step 4: Allocate on inject and reassign; pass `volume_used` to CGR**

Replace `inject_bundle`:

```python
    def inject_bundle(self, bundle) -> None:
        snapshot = self.plan.snapshot()
        result = cgr_route(snapshot, bundle.src, bundle.dst,
                           self.sim_time(), self.sim_start)
        bundle.next_hop_contact = result.next_hop_contact if result else None
        with self._manager_lock:
            self.bundle_store.insert(bundle)
            if bundle.next_hop_contact and \
               bundle.next_hop_contact in self._outbound_queues:
                self._outbound_queues[bundle.next_hop_contact].put(bundle)
```

with:

```python
    def inject_bundle(self, bundle) -> None:
        snapshot = self.plan.snapshot()
        result = cgr_route(snapshot, bundle.src, bundle.dst,
                           self.sim_time(), self.sim_start,
                           volume_used=self._volume.used())
        bundle.next_hop_contact = result.next_hop_contact if result else None
        if result:
            self._volume.allocate(bundle.bundle_id, result.next_hop_contact,
                                  result.first_hop_window_start, len(bundle.data))
        with self._manager_lock:
            self.bundle_store.insert(bundle)
            if bundle.next_hop_contact and \
               bundle.next_hop_contact in self._outbound_queues:
                self._outbound_queues[bundle.next_hop_contact].put(bundle)
```

Replace `_reassign_all_bundles`:

```python
    def _reassign_all_bundles(self) -> None:
        snapshot = self.plan.snapshot()
        now = self.sim_time()
        reroutes = []
        for bundle in self.bundle_store.all():
            old_hop = bundle.next_hop_contact
            result = cgr_route(snapshot, bundle.src, bundle.dst, now,
                               self.sim_start)
            new_hop = result.next_hop_contact if result else None
            if new_hop != old_hop:
                reroutes.append((bundle, new_hop))
        with self._manager_lock:
            for bundle, new_hop in reroutes:
                bundle.next_hop_contact = new_hop
                self.bundle_store.update_next_hop(bundle.bundle_id, new_hop)
                if new_hop and new_hop in self._outbound_queues:
                    self._outbound_queues[new_hop].put(bundle)
```

with:

```python
    def _reassign_all_bundles(self) -> None:
        snapshot = self.plan.snapshot()
        now = self.sim_time()
        reroutes = []
        for bundle in self.bundle_store.all():
            old_hop = bundle.next_hop_contact
            # Release this bundle's prior allocation so it doesn't block itself,
            # then re-route against everyone else's current allocations.
            self._volume.release(bundle.bundle_id)
            result = cgr_route(snapshot, bundle.src, bundle.dst, now,
                               self.sim_start, volume_used=self._volume.used())
            new_hop = result.next_hop_contact if result else None
            if result:
                self._volume.allocate(bundle.bundle_id, new_hop,
                                      result.first_hop_window_start,
                                      len(bundle.data))
            if new_hop != old_hop:
                reroutes.append((bundle, new_hop))
        with self._manager_lock:
            for bundle, new_hop in reroutes:
                bundle.next_hop_contact = new_hop
                self.bundle_store.update_next_hop(bundle.bundle_id, new_hop)
                if new_hop and new_hop in self._outbound_queues:
                    self._outbound_queues[new_hop].put(bundle)
```

- [ ] **Step 5: Add the release hooks `on_bundle_acked` and `release_volume`**

In `marsnet/node/contact_manager.py`, add these two methods to `ContactManager` (place them just before `inject_bundle`):

```python
    def on_bundle_acked(self, bundle_id: str) -> None:
        """Custody transferred: free the window allocation and drop our copy."""
        self._volume.release(bundle_id)
        self.bundle_store.delete(bundle_id)

    def release_volume(self, bundle_id: str) -> None:
        """Free a window allocation without touching the store (e.g. TTL drop)."""
        self._volume.release(bundle_id)
```

- [ ] **Step 6: Route BUNDLE_ACK through `on_bundle_acked`**

In `marsnet/node/connection_handler.py`, add the optional callback to `__init__`. Change the signature line:

```python
        sim_start: float,
        dashboard_reporter=None,
        peer_handshake: Optional[proto.Message] = None,
    ):
```

to:

```python
        sim_start: float,
        dashboard_reporter=None,
        peer_handshake: Optional[proto.Message] = None,
        on_bundle_acked: Optional[Callable[[str], None]] = None,
    ):
```

And store it alongside the other assignments (after `self.peer_handshake = peer_handshake`):

```python
        self.on_bundle_acked       = on_bundle_acked
```

Then change the BUNDLE_ACK branch in `_handle_incoming`:

```python
        elif msg.type == "BUNDLE_ACK":
            self.bundle_store.delete(msg.payload["bundle_id"])
```

to:

```python
        elif msg.type == "BUNDLE_ACK":
            bid = msg.payload["bundle_id"]
            if self.on_bundle_acked is not None:
                self.on_bundle_acked(bid)
            else:
                self.bundle_store.delete(bid)
```

- [ ] **Step 7: Pass `on_bundle_acked` from both ContactManager handler sites**

In `marsnet/node/contact_manager.py`, both `_open_contact` and `accept_inbound` build a `ConnectionHandler`. In **each** of those two `ConnectionHandler(...)` constructions, add the argument next to `dashboard_reporter=self.reporter,`:

```python
            dashboard_reporter=self.reporter,
            on_bundle_acked=self.on_bundle_acked,
```

(For `accept_inbound`, which also passes `peer_handshake=peer_handshake`, keep that argument too — just add the `on_bundle_acked` line alongside it.)

- [ ] **Step 8: Release volume when the TTL reaper drops a bundle**

In `marsnet/node/main.py`, the `ttl_reaper` is currently constructed before `contact_mgr` with an inline `on_drop` lambda. Move its construction to **after** `contact_mgr` is defined and give it a drop handler that also releases volume.

Delete the current construction (lines around 57-59):

```python
    ttl_reaper = TTLReaper(bundle_store,
                           on_drop=lambda bid: reporter.post("bundle_dropped",
                                                             {"bundle_id": bid}))
```

Then, immediately after the `contact_mgr = ContactManager(...)` block (after its closing `)`), add:

```python
    def on_bundle_dropped(bid: str) -> None:
        contact_mgr.release_volume(bid)
        reporter.post("bundle_dropped", {"bundle_id": bid})

    ttl_reaper = TTLReaper(bundle_store, on_drop=on_bundle_dropped)
```

`ttl_reaper.start()` further down already runs after `contact_mgr` exists, so the reference resolves correctly.

- [ ] **Step 9: Run the tests — verify they pass**

Run: `.venv/bin/pytest tests/test_contact_manager.py -v`
Expected: all PASS, including the four new tests. Then run the full suite:

Run: `.venv/bin/pytest tests/ -q`
Expected: all PASS, including `tests/test_integration.py` (end-to-end image delivery still works; the ACK path now releases volume and deletes via `on_bundle_acked`).

- [ ] **Step 10: Commit** 🧑

> 🧑 **User step:** Run the git commands below yourself — do not execute as the agent.

```bash
git add marsnet/node/contact_manager.py marsnet/node/connection_handler.py marsnet/node/main.py tests/test_contact_manager.py
git commit -m "feat: enforce per-window volume constraints in CGR routing"
```

---

## Task 5: `set_sim_start.py` helper script

`plans/initial_plan.json` ships with `sim_start: 0.0`, which makes every event timestamp a raw Unix time (~1.7e9 seconds). Before a demo, all nodes must share a `sim_start` equal to the wall-clock start time. This script rewrites a plan file's `sim_start` in place.

**Files:**
- Create: `scripts/set_sim_start.py`

- [ ] **Step 1: Implement `scripts/set_sim_start.py`**

```python
#!/usr/bin/env python3
"""Set a contact plan's sim_start to the current Unix time (or an explicit value).

All nodes must share the same sim_start, so generate it once and distribute the
updated plan file to every node before starting the simulation.

Usage:
    python scripts/set_sim_start.py plans/initial_plan.json
    python scripts/set_sim_start.py plans/initial_plan.json --sim-start 1717689600
"""
from __future__ import annotations
import argparse
import json
import time


def main() -> None:
    p = argparse.ArgumentParser(description="Set sim_start in a contact plan JSON")
    p.add_argument("plan_path", help="Path to the contact plan JSON file")
    p.add_argument("--sim-start", type=float, default=None,
                   help="Explicit sim_start (Unix seconds). Defaults to now.")
    args = p.parse_args()

    sim_start = args.sim_start if args.sim_start is not None else time.time()

    with open(args.plan_path) as f:
        plan = json.load(f)

    plan["sim_start"] = sim_start

    with open(args.plan_path, "w") as f:
        json.dump(plan, f, indent=2)
        f.write("\n")

    print(f"Set sim_start = {sim_start} in {args.plan_path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test the script on a copy**

Run:
```bash
cp plans/initial_plan.json /tmp/plan_test.json
.venv/bin/python scripts/set_sim_start.py /tmp/plan_test.json
.venv/bin/python -c "import json; v=json.load(open('/tmp/plan_test.json'))['sim_start']; print('sim_start now:', v); assert v > 1_000_000_000, 'sim_start was not updated'"
```
Expected: prints the new `sim_start` (a large Unix timestamp) and the assertion passes. The repo's `plans/initial_plan.json` is left untouched (we operated on the `/tmp` copy).

- [ ] **Step 3: Commit** 🧑

> 🧑 **User step:** Run the git commands below yourself — do not execute as the agent.

```bash
git add scripts/set_sim_start.py
git commit -m "feat: set_sim_start.py to stamp a plan's sim_start before a demo run"
```

---

## Task 6: Sync documentation

Bring `docs/DESIGN.md` §5 in line with the new `CGRResult` shape and the now-enforced volume tracking, and check off the completed steps in the original plan.

**Files:**
- Modify: `docs/DESIGN.md`
- Modify: `docs/plans/2026-05-25-marsnet.md`

- [ ] **Step 1: Update the `CGRResult` description in `docs/DESIGN.md`**

In the "Inputs and outputs" block of §5 (around the `CGRResult(next_hop_contact, earliest_arrival)` line), replace:

```
Output:
  CGRResult(next_hop_contact, earliest_arrival)
  or None if destination is unreachable
```

with:

```
Output:
  CGRResult(next_hop_contact, earliest_arrival, first_hop_window_start)
  or None if destination is unreachable

  first_hop_window_start identifies which window instance the first hop departs
  on, so the Contact Manager can charge the bundle's bytes against the correct
  (contact_id, window_start) entry in volume_used.
```

- [ ] **Step 2: Update the volume-tracking note in `docs/DESIGN.md`**

In §5 "Volume tracking", the existing text already describes the intended behaviour. Confirm it reads as enforced (it is now). Replace:

```
`volume_used` is a dict mapping `(contact_id, window_start_time) → bytes_allocated`. It is updated by the Contact Manager when a bundle is enqueued for a contact. When a bundle is ACK'd or dropped, its allocation is released.
```

with:

```
`volume_used` is a dict mapping `(contact_id, window_start_time) → bytes_allocated`, owned by the Contact Manager's `VolumeTracker` (`marsnet/node/volume_tracker.py`). The Contact Manager allocates a bundle's `len(data)` bytes against its first-hop window when the bundle is injected or reassigned, and passes `volume_used` into every `cgr_route` call so full windows are skipped. The allocation is released when the bundle is ACK'd (`on_bundle_acked`), dropped by the TTL reaper (`release_volume`), or rerouted (re-allocation replaces the prior entry).
```

- [ ] **Step 3: Check off the completed steps in `docs/plans/2026-05-25-marsnet.md`**

Tasks 1–8 are already checked. Tasks 9–18 are implemented but still show `- [ ]`. Mark every remaining unchecked box in that file as done:

Run:
```bash
sed -i 's/^- \[ \]/- [x]/' docs/plans/2026-05-25-marsnet.md
```

Then verify none remain:
```bash
grep -c '^- \[ \]' docs/plans/2026-05-25-marsnet.md
```
Expected: prints `0`.

- [ ] **Step 4: Commit** 🧑

> 🧑 **User step:** Run the git commands below yourself — do not execute as the agent.

```bash
git add docs/DESIGN.md docs/plans/2026-05-25-marsnet.md
git commit -m "docs: sync DESIGN with volume enforcement; mark original plan steps done"
```

---

## Run All Tests

```bash
.venv/bin/pytest tests/ -v
```

Expected: all tests PASS, including the new `tests/test_volume_tracker.py`, the new CGR window-start tests, and the new ContactManager volume tests, plus the existing end-to-end `tests/test_integration.py`.

---

## Manual Verification (dashboard contact lines)

Not automated — confirm Task 1 visually:

1. Stamp a fresh sim_start: `.venv/bin/python scripts/set_sim_start.py plans/initial_plan.json`
2. Start the dashboard: `.venv/bin/uvicorn marsnet.dashboard.server:app --host 0.0.0.0 --port 8000`
3. Start a couple of nodes (e.g. `base` and `rover_a`) with their configs.
4. When a contact window opens, a green line should now connect the two nodes on the canvas, and disappear when the window closes or fails.
