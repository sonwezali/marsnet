# sim_start Bootstrap Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow nodes that start without a stamped `sim_start` to automatically adopt one from the first peer that has it, via the existing handshake protocol — no manual file copying required.

**Architecture:** A new `SimClock` object wraps `sim_start` as a mutable, shared reference. All components that previously took `sim_start: float` now take a `SimClock`. During the HANDSHAKE exchange each node advertises its own `sim_start`; a node without one requests the peer's plan unconditionally, which carries the real `sim_start` inside it. `ContactPlan.merge()` adopts `sim_start` from the incoming plan when its own is unset, and `on_plan_update` in `main.py` calls `sim_clock.adopt()` so timers are rescheduled with the correct epoch.

**Tech Stack:** Python 3.11+, `threading`, existing `marsnet.node.*` module tree, `pytest`

---

## File Map

| File | Action | Role |
|---|---|---|
| `marsnet/node/sim_clock.py` | **Create** | Thread-safe mutable `sim_start` holder |
| `tests/test_sim_clock.py` | **Create** | Unit tests for `SimClock` |
| `marsnet/node/protocol.py` | **Modify** | Add `sim_start` field to `HandshakePayload` |
| `marsnet/node/contact_plan.py` | **Modify** | `merge()` adopts `sim_start` when self is unset |
| `tests/test_contact_plan.py` | **Modify** | Tests for `sim_start` adoption in `merge()` |
| `marsnet/node/connection_handler.py` | **Modify** | `clock: SimClock` param; bootstrap request in `_handshake()` |
| `tests/test_connection_handler.py` | **Create** | Tests for bootstrap handshake logic |
| `marsnet/node/contact_manager.py` | **Modify** | `clock: SimClock` replaces `sim_start: float` |
| `marsnet/node/main.py` | **Modify** | Create `SimClock`; `on_plan_update` calls `sim_clock.adopt()` |
| `tests/test_contact_manager.py` | **Modify** | Update `make_manager()` to pass `SimClock` |
| `marsnet/node/tcp_listener.py` | **Modify** | Remove unused `sim_start` param |
| `marsnet/node/udp_listener.py` | **Modify** | `clock: SimClock` replaces `sim_start: float` |
| `marsnet/node/hello_broadcaster.py` | **Modify** | `clock: SimClock` replaces `sim_start: float` |
| `docs/DESIGN.md` | **Modify** | Document bootstrap flow |

---

### Task 1: SimClock — thread-safe mutable sim_start holder

**Files:**
- Create: `marsnet/node/sim_clock.py`
- Create: `tests/test_sim_clock.py`

- [x] **Step 1: Write the failing tests**

```python
# tests/test_sim_clock.py
import time
from marsnet.node.sim_clock import SimClock


def test_sim_time_returns_zero_when_unset():
    clock = SimClock()
    assert clock.sim_time() == 0.0


def test_sim_time_returns_elapsed_when_set():
    start = time.time() - 5.0
    clock = SimClock(start)
    assert 4.9 < clock.sim_time() < 5.5


def test_initialized_with_nonzero_is_set():
    assert SimClock(123.0).is_set() is True


def test_is_set_false_initially():
    assert SimClock().is_set() is False


def test_adopt_sets_start_when_zero():
    clock = SimClock()
    adopted = clock.adopt(12345.0)
    assert adopted is True
    assert clock.value == 12345.0
    assert clock.is_set() is True


def test_adopt_ignores_zero():
    clock = SimClock()
    adopted = clock.adopt(0.0)
    assert adopted is False
    assert clock.is_set() is False


def test_adopt_ignores_when_already_set():
    clock = SimClock(1000.0)
    adopted = clock.adopt(2000.0)
    assert adopted is False
    assert clock.value == 1000.0


def test_adopt_is_idempotent():
    clock = SimClock()
    clock.adopt(500.0)
    clock.adopt(600.0)
    assert clock.value == 500.0
```

- [x] **Step 2: Run tests to verify they fail**

```
.venv/bin/pytest tests/test_sim_clock.py -v
```

Expected: ImportError or 8 failures.

- [x] **Step 3: Implement SimClock**

```python
# marsnet/node/sim_clock.py
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
        """Set the epoch if not already set. Returns True if adopted."""
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
```

- [x] **Step 4: Run tests to verify they pass**

```
.venv/bin/pytest tests/test_sim_clock.py -v
```

Expected: 8 passed (plus a 9th `test_adopt_is_thread_safe_first_adopter_wins` added during review).

- [x] **Step 5: Commit** *(run this yourself)*

```bash
git add marsnet/node/sim_clock.py tests/test_sim_clock.py
git commit -m "feat: add SimClock — thread-safe mutable sim_start holder"
```

---

### Task 2: Protocol and ContactPlan.merge() adopt sim_start

**Files:**
- Modify: `marsnet/node/protocol.py`
- Modify: `marsnet/node/contact_plan.py`
- Modify: `tests/test_contact_plan.py`

- [x] **Step 1: Write the failing tests** (append to `tests/test_contact_plan.py`)

```python
def test_merge_adopts_sim_start_when_self_is_zero():
    plan_a = ContactPlan(version=1, sim_start=0.0, contacts=[])
    plan_b = ContactPlan(version=1, sim_start=1000.0, contacts=[])
    changed = plan_a.merge(plan_b)
    assert changed is True
    assert plan_a.sim_start == 1000.0


def test_merge_does_not_override_existing_sim_start():
    plan_a = ContactPlan(version=1, sim_start=500.0, contacts=[])
    plan_b = ContactPlan(version=1, sim_start=1000.0, contacts=[])
    plan_a.merge(plan_b)
    assert plan_a.sim_start == 500.0


def test_merge_returns_true_for_sim_start_only_change():
    e = make_entry("base:1")
    plan_a = ContactPlan(version=1, sim_start=0.0, contacts=[e])
    plan_b = ContactPlan(version=1, sim_start=1000.0, contacts=[e])
    changed = plan_a.merge(plan_b)
    assert changed is True
    assert plan_a.sim_start == 1000.0
```

- [x] **Step 2: Run tests to verify they fail**

```
.venv/bin/pytest tests/test_contact_plan.py::test_merge_adopts_sim_start_when_self_is_zero tests/test_contact_plan.py::test_merge_does_not_override_existing_sim_start tests/test_contact_plan.py::test_merge_returns_true_for_sim_start_only_change -v
```

Expected: 3 failures.

- [x] **Step 3: Add `sim_start: float = 0.0` to HandshakePayload in `protocol.py`**

Replace:

```python
@dataclass
class HandshakePayload:
    contact_id: str
    plan_version: int
```

With:

```python
@dataclass
class HandshakePayload:
    contact_id: str
    plan_version: int
    sim_start: float = 0.0
```

- [x] **Step 4: Update `ContactPlan.merge()` in `contact_plan.py`**

Replace the entire `merge` method:

```python
def merge(self, other: ContactPlan) -> bool:
    """
    Merge other plan into self. Rules:
    - Adopt sim_start from other if ours is unset (zero).
    - Cancellations from either side always win (one-way: active→cancelled).
    - New contacts from either side are added.
    Returns True if anything changed.
    """
    other_contacts = {c.id: c for c in other.contacts}
    changed = False
    with self._lock:
        if self.sim_start == 0.0 and other.sim_start > 0.0:
            self.sim_start = other.sim_start
            changed = True
        for cid, entry in other_contacts.items():
            if cid not in self._contacts:
                self._contacts[cid] = entry
                changed = True
            elif entry.status == "cancelled" and self._contacts[cid].status == "active":
                self._contacts[cid].status = "cancelled"
                changed = True
        if changed:
            self.version = max(self.version, other.version) + 1
    return changed
```

- [x] **Step 5: Run all contact plan tests**

```
.venv/bin/pytest tests/test_contact_plan.py -v
```

Expected: all passing (including the 3 new ones).

- [x] **Step 6: Run full test suite to check for regressions**

```
.venv/bin/pytest -x -q
```

Expected: all passing.

- [x] **Step 7: Commit** *(run this yourself)*

```bash
git add marsnet/node/protocol.py marsnet/node/contact_plan.py tests/test_contact_plan.py
git commit -m "feat: handshake advertises sim_start; plan merge adopts sim_start when unset"
```

---

### Task 3: ConnectionHandler — SimClock param + bootstrap handshake logic

**Files:**
- Modify: `marsnet/node/connection_handler.py`
- Create: `tests/test_connection_handler.py`

The key behavior: when `_handshake()` receives a peer's HANDSHAKE message and our clock is not yet set (`clock.is_set() == False`) but the peer's `sim_start > 0`, we request their plan unconditionally — even if our `plan_version` matches theirs. This triggers `merge()` which adopts `sim_start`, and `on_plan_update` propagates it.

Note: `ContactManager` still passes `sim_start: float` to `ConnectionHandler` at this point — that will be fixed in Task 4. For now, add `clock: SimClock` as the primary parameter and keep backward compatibility by accepting `sim_start: float = 0.0` as a fallback (it will be removed in Task 4). Actually, to avoid a messy intermediate state, implement the `clock` param in Task 3 and update `ContactManager` in Task 4 in one atomic step. The tests here will construct `ConnectionHandler` directly, so they don't need `ContactManager` to be updated yet.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_connection_handler.py
from __future__ import annotations
import queue
import socket
import threading
from unittest.mock import MagicMock, patch

import marsnet.node.protocol as proto
from marsnet.node.bundle_store import BundleStore
from marsnet.node.connection_handler import ConnectionHandler
from marsnet.node.contact_plan import ContactPlan
from marsnet.node.sim_clock import SimClock


def make_handler(clock: SimClock, plan: ContactPlan,
                 peer_handshake: proto.Message | None = None):
    sock_a, sock_b = socket.socketpair()
    handler = ConnectionHandler(
        sock=sock_a,
        contact_id="base:1",
        is_initiator=True,
        close_event=threading.Event(),
        end_time=9999.0,
        node_name="rover_a",
        plan=plan,
        bundle_store=BundleStore(),
        outbound_queue=queue.Queue(),
        on_failure=MagicMock(),
        on_plan_update=MagicMock(),
        on_bundle_received=MagicMock(),
        clock=clock,
        peer_handshake=peer_handshake,
    )
    return handler, sock_a, sock_b


def make_peer_handshake(plan_version: int, sim_start: float) -> proto.Message:
    return proto.Message(
        type="HANDSHAKE", sender="base", ts=0.0,
        payload={
            "contact_id": "base:1",
            "plan_version": plan_version,
            "sim_start": sim_start,
        },
    )


def test_handshake_requests_plan_when_clock_unset_and_peer_has_sim_start():
    plan = ContactPlan(version=1, sim_start=0.0, contacts=[])
    clock = SimClock()
    peer_hs = make_peer_handshake(plan_version=1, sim_start=12345.0)
    handler, sock_a, sock_b = make_handler(clock, plan, peer_handshake=peer_hs)

    with patch.object(handler, "_request_plan") as mock_req, \
         patch.object(handler, "_send_plan") as mock_send:
        handler._handshake()

    mock_req.assert_called_once()
    mock_send.assert_not_called()
    sock_a.close(); sock_b.close()


def test_handshake_does_not_request_plan_when_clock_set_and_versions_equal():
    plan = ContactPlan(version=1, sim_start=1000.0, contacts=[])
    clock = SimClock(1000.0)
    peer_hs = make_peer_handshake(plan_version=1, sim_start=1000.0)
    handler, sock_a, sock_b = make_handler(clock, plan, peer_handshake=peer_hs)

    with patch.object(handler, "_request_plan") as mock_req, \
         patch.object(handler, "_send_plan") as mock_send:
        handler._handshake()

    mock_req.assert_not_called()
    mock_send.assert_not_called()
    sock_a.close(); sock_b.close()


def test_handshake_requests_plan_when_peer_version_higher():
    plan = ContactPlan(version=1, sim_start=1000.0, contacts=[])
    clock = SimClock(1000.0)
    peer_hs = make_peer_handshake(plan_version=2, sim_start=1000.0)
    handler, sock_a, sock_b = make_handler(clock, plan, peer_handshake=peer_hs)

    with patch.object(handler, "_request_plan") as mock_req, \
         patch.object(handler, "_send_plan"):
        handler._handshake()

    mock_req.assert_called_once()
    sock_a.close(); sock_b.close()


def test_handshake_sends_plan_when_our_version_higher():
    plan = ContactPlan(version=3, sim_start=1000.0, contacts=[])
    clock = SimClock(1000.0)
    peer_hs = make_peer_handshake(plan_version=1, sim_start=1000.0)
    handler, sock_a, sock_b = make_handler(clock, plan, peer_handshake=peer_hs)

    with patch.object(handler, "_request_plan") as mock_req, \
         patch.object(handler, "_send_plan") as mock_send:
        handler._handshake()

    mock_send.assert_called_once()
    mock_req.assert_not_called()
    sock_a.close(); sock_b.close()


def test_handshake_sends_sim_start_in_outgoing_message():
    plan = ContactPlan(version=1, sim_start=0.0, contacts=[])
    clock = SimClock(99999.0)
    peer_hs = make_peer_handshake(plan_version=1, sim_start=0.0)
    handler, sock_a, sock_b = make_handler(clock, plan, peer_handshake=peer_hs)

    with patch.object(handler, "_request_plan"), \
         patch.object(handler, "_send_plan"):
        handler._handshake()

    # Read what was sent on sock_b (the peer's side)
    sock_b.settimeout(1.0)
    data = sock_b.recv(4096)
    msg = proto.decode(data)
    assert msg.payload["sim_start"] == 99999.0
    sock_a.close(); sock_b.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```
.venv/bin/pytest tests/test_connection_handler.py -v
```

Expected: ImportError or failures (no `clock` param yet).

- [ ] **Step 3: Update `ConnectionHandler.__init__` in `connection_handler.py`**

Add the import at the top of the file (after existing imports):

```python
from marsnet.node.sim_clock import SimClock
```

Replace the `sim_start: float` parameter in `__init__` with `clock: SimClock`. The full updated signature:

```python
def __init__(
    self,
    sock,
    contact_id: str,
    is_initiator: bool,
    close_event: threading.Event,
    end_time: float,
    node_name: str,
    plan: ContactPlan,
    bundle_store: BundleStore,
    outbound_queue: queue.Queue,
    on_failure: Callable[[str], None],
    on_plan_update: Callable[[ContactPlan], None],
    on_bundle_received: Callable[[Bundle], None],
    clock: SimClock,
    dashboard_reporter=None,
    peer_handshake: Optional[proto.Message] = None,
    on_bundle_acked: Optional[Callable[[str], None]] = None,
):
```

In the body of `__init__`, replace:

```python
self.sim_start            = sim_start
```

With:

```python
self.clock                = clock
```

- [ ] **Step 4: Update `sim_time()` in `ConnectionHandler`**

Replace:

```python
def sim_time(self) -> float:
    return time.time() - self.sim_start
```

With:

```python
def sim_time(self) -> float:
    return self.clock.sim_time()
```

- [ ] **Step 5: Update `_handshake()` to send and use `sim_start`**

Replace the entire `_handshake` method:

```python
def _handshake(self) -> None:
    proto.send_message(self.sock, proto.Message(
        type="HANDSHAKE", sender=self.node_name, ts=self.sim_time(),
        payload=proto.HandshakePayload(
            contact_id=self.contact_id,
            plan_version=self.plan.version,
            sim_start=self.clock.value,
        )
    ))

    if self.peer_handshake is not None:
        msg = self.peer_handshake
    else:
        msg = proto.recv_message(self._sock_file)
    if not msg or msg.type != "HANDSHAKE":
        return

    peer_version = msg.payload["plan_version"]
    peer_sim_start = msg.payload.get("sim_start", 0.0)

    needs_plan = peer_version > self.plan.version
    if not self.clock.is_set() and peer_sim_start > 0.0:
        needs_plan = True

    if needs_plan:
        self._request_plan()
    elif peer_version < self.plan.version:
        self._send_plan()

    self._state = State.ACTIVE
    if self.reporter:
        contact = self.plan.contact_by_id(self.contact_id)
        evt = {"contact_id": self.contact_id, "ts": self.sim_time()}
        if contact is not None:
            evt["from"] = contact.from_node
            evt["to"] = contact.to_node
        self.reporter.post("contact_open", evt)
```

- [ ] **Step 6: Run the new tests**

```
.venv/bin/pytest tests/test_connection_handler.py -v
```

Expected: 5 passed.

- [ ] **Step 7: Run full test suite**

```
.venv/bin/pytest -x -q
```

Expected: test failures in `test_contact_manager.py` and `test_integration.py` because `ContactManager` still passes `sim_start=` to `ConnectionHandler`. That is expected — Task 4 fixes it. All other tests should pass.

- [ ] **Step 8: Commit** *(run this yourself)*

```bash
git add marsnet/node/connection_handler.py tests/test_connection_handler.py
git commit -m "feat: ConnectionHandler takes SimClock; handshake requests plan when sim_start unset"
```

---

### Task 4: Wire SimClock through ContactManager and main.py

**Files:**
- Modify: `marsnet/node/contact_manager.py`
- Modify: `marsnet/node/main.py`
- Modify: `tests/test_contact_manager.py`

This task fixes the failing tests from Task 3 and completes the core wiring.

- [ ] **Step 1: Add the SimClock import to `contact_manager.py`**

At the top of the existing imports, add:

```python
from marsnet.node.sim_clock import SimClock
```

- [ ] **Step 2: Replace `sim_start: float` with `clock: SimClock` in `ContactManager.__init__`**

Change the parameter in `__init__`:

```python
clock: SimClock,
```

In the body, replace:

```python
self.sim_start = sim_start
```

With:

```python
self.clock = clock
```

- [ ] **Step 3: Update `sim_time()` in `ContactManager`**

Replace:

```python
def sim_time(self) -> float:
    return time.time() - self.sim_start
```

With:

```python
def sim_time(self) -> float:
    return self.clock.sim_time()
```

- [ ] **Step 4: Update calls to `cgr_route` inside `ContactManager`**

`cgr_route` takes a `sim_start: float` as its 5th positional argument. All calls to `cgr_route` in `contact_manager.py` currently pass `self.sim_start`. Replace them with `self.clock.value`.

In `inject_bundle`:

```python
result = cgr_route(snapshot, bundle.src, bundle.dst,
                   self.sim_time(), self.clock.value,
                   volume_used=self._volume.used())
```

In `_reassign_all_bundles`:

```python
result = cgr_route(snapshot, bundle.src, bundle.dst, now,
                   self.clock.value, volume_used=self._volume.used())
```

- [ ] **Step 5: Update `ConnectionHandler` construction in `ContactManager`**

In `_open_contact`, replace `sim_start=self.sim_start` with `clock=self.clock`:

```python
handler = ConnectionHandler(
    sock=sock, contact_id=contact.id, is_initiator=True,
    close_event=close_event, end_time=we,
    node_name=self.node_name, plan=self.plan,
    bundle_store=self.bundle_store, outbound_queue=q,
    on_failure=self.report_failure,
    on_plan_update=self.on_plan_update,
    on_bundle_received=self.on_bundle_received,
    clock=self.clock,
    dashboard_reporter=self.reporter,
    on_bundle_acked=self.on_bundle_acked,
)
```

In `accept_inbound`, replace `sim_start=self.sim_start` with `clock=self.clock`:

```python
handler = ConnectionHandler(
    sock=sock, contact_id=contact_id, is_initiator=False,
    close_event=close_event, end_time=end_time,
    node_name=self.node_name, plan=self.plan,
    bundle_store=self.bundle_store, outbound_queue=q,
    on_failure=self.report_failure,
    on_plan_update=self.on_plan_update,
    on_bundle_received=self.on_bundle_received,
    clock=self.clock,
    dashboard_reporter=self.reporter,
    peer_handshake=peer_handshake,
    on_bundle_acked=self.on_bundle_acked,
)
```

- [ ] **Step 6: Update `make_manager()` in `tests/test_contact_manager.py`**

Add the import at the top:

```python
from marsnet.node.sim_clock import SimClock
```

In `make_manager()`, replace:

```python
mgr = ContactManager(
    node_name=node_name, plan=plan, bundle_store=store,
    destination="base", sim_start=plan.sim_start,
    resolve_fn=resolve_fn,
    on_plan_update=on_plan_update,
    on_bundle_received=on_bundle_received,
)
```

With:

```python
mgr = ContactManager(
    node_name=node_name, plan=plan, bundle_store=store,
    destination="base", clock=SimClock(plan.sim_start),
    resolve_fn=resolve_fn,
    on_plan_update=on_plan_update,
    on_bundle_received=on_bundle_received,
)
```

- [ ] **Step 7: Update `main.py`**

Add the import (with existing imports):

```python
from marsnet.node.sim_clock import SimClock
```

Replace:

```python
sim_start = plan.sim_start
```

With:

```python
sim_clock = SimClock(plan.sim_start)
```

**Note on `HELLOBroadcaster` and `UDPListener`:** those two classes still take `sim_start: float` and are updated in Task 5. For now, update their construction in `main.py` to pass `sim_clock.value` (a plain float):

```python
broadcaster = HELLOBroadcaster(cfg.udp_port, cfg.port, cfg.name, plan, sim_clock.value)
# ...
udp_listener = UDPListener(cfg.udp_port, cfg.name, plan, sim_clock.value)
```

Task 5 replaces `sim_clock.value` with `sim_clock` (the full object) once those classes accept `SimClock`.

Replace `on_plan_update` closure:

```python
def on_plan_update(updated_plan: ContactPlan) -> None:
    sim_clock.adopt(updated_plan.sim_start)
    contact_mgr.rebuild_on_plan_update()
    reporter.post("plan_updated", {"version": updated_plan.version,
                                   "ts": sim_clock.sim_time()})
    if updated_plan.is_lost(sim_clock.sim_time()):
        broadcaster.start()
```

Replace `on_bundle_received` closure (change `time.time() - sim_start` to `sim_clock.sim_time()`):

```python
def on_bundle_received(bundle) -> None:
    if bundle.dst == cfg.name:
        bundle_store.insert(bundle)
        assembler.on_fragment(bundle.image_id)
        reporter.post("fragment_received", {
            "image_id": bundle.image_id,
            "fragment_offset": bundle.fragment_offset,
            "total_size": bundle.total_size,
            "ts": sim_clock.sim_time(),
        })
    else:
        contact_mgr.inject_bundle(bundle)
```

Replace `ContactManager` construction:

```python
contact_mgr = ContactManager(
    node_name=cfg.name, plan=plan, bundle_store=bundle_store,
    destination=args.destination, clock=sim_clock,
    resolve_fn=resolve,
    on_plan_update=on_plan_update,
    on_bundle_received=on_bundle_received,
    dashboard_reporter=reporter,
)
```

Replace `on_contact_connection` closure (change `time.time() - sim_start` to `sim_clock.sim_time()`):

```python
def on_contact_connection(sock, contact_id, _peer_name, peer_handshake=None):
    close_event = threading.Event()
    contact = plan.contact_by_id(contact_id)
    end_time_sim = contact.next_window(after=sim_clock.sim_time())[1] \
                   if contact else (sim_clock.sim_time() + 30)
    contact_mgr.accept_inbound(sock, contact_id, close_event, end_time_sim,
                               peer_handshake=peer_handshake)
```

Replace `image_queued` reporter call:

```python
reporter.post("image_queued", {
    "image_id": image_id,
    "fragments": len(bundles),
    "ts": sim_clock.sim_time(),
})
```

Replace the `is_lost` check before starting broadcaster:

```python
if plan.is_lost(sim_clock.sim_time()):
    broadcaster.start()
```

- [ ] **Step 8: Run contact_manager tests**

```
.venv/bin/pytest tests/test_contact_manager.py -v
```

Expected: all passing.

- [ ] **Step 9: Run full test suite (excluding integration)**

```
.venv/bin/pytest -x -q --ignore=tests/test_integration.py
```

Expected: all passing.

- [ ] **Step 10: Commit** *(run this yourself)*

```bash
git add marsnet/node/contact_manager.py marsnet/node/main.py tests/test_contact_manager.py
git commit -m "feat: wire SimClock through ContactManager and main.py"
```

---

### Task 5: Wire SimClock through TCPListener, UDPListener, HELLOBroadcaster

**Files:**
- Modify: `marsnet/node/tcp_listener.py`
- Modify: `marsnet/node/udp_listener.py`
- Modify: `marsnet/node/hello_broadcaster.py`
- Modify: `marsnet/node/main.py` (three remaining construction sites)

These are mechanical changes — `sim_start: float` in each class is either unused (TCPListener) or only used in `sim_time()` calls (the other two).

- [ ] **Step 1: Remove `sim_start` from `TCPListener`**

`TCPListener` stores `self.sim_start` but never uses it in any method. Remove it entirely.

In `tcp_listener.py`, remove `sim_start: float` from `__init__` signature and remove `self.sim_start = sim_start` from the body.

In `main.py`, remove `sim_start=sim_start` (or `sim_start=sim_clock.value`) from the `TCPListener` construction:

```python
tcp_listener = TCPListener(
    host=cfg.host, port=cfg.port,
    node_name=cfg.name, plan=plan,
    on_contact_connection=on_contact_connection,
    on_plan_received=on_plan_received,
)
```

- [ ] **Step 2: Update `UDPListener` to use `SimClock`**

In `udp_listener.py`, add the import:

```python
from marsnet.node.sim_clock import SimClock
```

Replace `sim_start: float` in `__init__` with `clock: SimClock`. In the body, replace `self.sim_start = sim_start` with `self.clock = clock`.

Replace `sim_time()`:

```python
def sim_time(self) -> float:
    return self.clock.sim_time()
```

In `main.py`, update `UDPListener` construction (now passing the `SimClock` object, not a float):

```python
udp_listener = UDPListener(cfg.udp_port, cfg.name, plan, sim_clock)
```

- [ ] **Step 3: Update `HELLOBroadcaster` to use `SimClock`**

In `hello_broadcaster.py`, add the import:

```python
from marsnet.node.sim_clock import SimClock
```

Replace `sim_start: float` in `__init__` with `clock: SimClock`. In the body, replace `self.sim_start = sim_start` with `self.clock = clock`.

Replace the two `time.time() - self.sim_start` expressions in `_run()`:

```python
def _run(self) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    while not self._stop.is_set():
        if not self.plan.is_lost(self.clock.sim_time()):
            break
        msg = proto.encode(proto.Message(
            type="HELLO", sender=self.node_name,
            ts=self.clock.sim_time(),
            payload=proto.HelloPayload(
                tcp_port=self.tcp_port,
                plan_version=self.plan.version,
            ),
        ))
        sock.sendto(msg, ("255.255.255.255", self.udp_port))
        jitter = random.uniform(-0.5, 0.5)
        self._stop.wait(self.interval + jitter)
    sock.close()
```

In `main.py`, update `HELLOBroadcaster` construction:

```python
broadcaster = HELLOBroadcaster(cfg.udp_port, cfg.port, cfg.name, plan, sim_clock)
```

- [ ] **Step 4: Run full test suite including integration**

```
.venv/bin/pytest -q
```

Expected: all passing (including `test_integration.py`).

- [ ] **Step 5: Commit** *(run this yourself)*

```bash
git add marsnet/node/tcp_listener.py marsnet/node/udp_listener.py marsnet/node/hello_broadcaster.py marsnet/node/main.py
git commit -m "refactor: replace sim_start float with SimClock in TCPListener, UDPListener, HELLOBroadcaster"
```

---

### Task 6: Update DESIGN.md — bootstrap protocol documentation

**Files:**
- Modify: `docs/DESIGN.md`

Find the section that describes the contact plan / handshake and add a sub-section explaining the bootstrap flow. The new text to add (find the right insertion point in the existing doc, under plan versioning / handshake):

```markdown
### sim_start Bootstrap

`sim_start` is the shared Unix epoch from which all simulation timestamps are measured. All nodes must agree on it for contact windows to open at the right wall-clock times.

**Bootstrap flow (no manual file copying required):**

1. Run `scripts/set_sim_start.py plans/initial_plan.json` on one designated node (typically `base`) before starting it. This stamps `sim_start` into the plan and bumps no version — the plan file is version 1 on all machines.
2. Start all nodes. Nodes without a stamped `sim_start` (i.e., `sim_start == 0` in their plan) start a `SimClock` that returns `0.0` for `sim_time()`, so their contact timers see all windows as infinitely far in the past and no contacts open.
3. During the first TCP contact handshake, each node includes its current `sim_start` in the `HANDSHAKE` message. A node with `sim_start == 0` that receives a peer's `sim_start > 0` requests the peer's plan unconditionally — regardless of plan version.
4. The received plan is merged via `ContactPlan.merge()`, which adopts `sim_start` when the receiver's own value is zero. `on_plan_update` then calls `sim_clock.adopt()`, which updates the shared `SimClock` object, and `rebuild_on_plan_update()` reschedules all contact timers with the correct epoch.

After this first handshake, the bootstrapped node behaves identically to a node that started with a pre-stamped plan. Subsequent contacts propagate as normal via version-based plan sync.
```

- [ ] **Step 1: Open `docs/DESIGN.md` and locate the handshake / plan versioning section**

Read the file to find where to insert the new sub-section.

- [ ] **Step 2: Insert the bootstrap documentation in the appropriate place**

Add the `### sim_start Bootstrap` block under the existing plan versioning / handshake discussion.

- [ ] **Step 3: Verify the markdown renders cleanly** (visual check — no broken headers or lists).

- [ ] **Step 4: Commit** *(run this yourself)*

```bash
git add docs/DESIGN.md
git commit -m "docs: document sim_start bootstrap via handshake"
```
