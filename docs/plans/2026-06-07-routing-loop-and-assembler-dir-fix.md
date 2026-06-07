# Routing Loop Prevention and Assembler Directory Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop bundles from bouncing back and forth between adjacent nodes (a routing loop caused by each node independently re-running CGR with no memory of where a bundle came from), and make `ImageAssembler` create its output directory so received images are never silently dropped.

**Architecture:** Add a `prev_hop` field to `Bundle` that records which node a bundle was just received from. `ConnectionHandler._receive_bundle` stamps it (it's the only place that knows which contact — and therefore which neighboring node — delivered the bundle). `cgr_route` gains an `exclude_node` parameter that applies a "split-horizon" rule: the *first* hop out of the current node may never go straight back to `exclude_node`. `ContactManager.inject_bundle` and `_reassign_all_bundles` pass `bundle.prev_hop` through as `exclude_node`. Separately, `ImageAssembler.__init__` creates `output_dir` with `os.makedirs(..., exist_ok=True)` so `on_fragment` never fails with a silent `FileNotFoundError`.

**Tech Stack:** Python 3.11+ stdlib (`os`, `dataclasses`), `pytest` (`.venv/bin/pytest`).

---

## File Map

| File | Change |
|------|--------|
| `marsnet/node/bundle_store.py` | Add `prev_hop: Optional[str] = None` field to `Bundle` |
| `marsnet/node/cgr.py` | Add `exclude_node` parameter to `cgr_route`; apply split-horizon filter on first-hop edges |
| `marsnet/node/contact_manager.py` | Pass `bundle.prev_hop` as `exclude_node` in `inject_bundle` and `_reassign_all_bundles` |
| `marsnet/node/connection_handler.py` | Stamp `bundle.prev_hop` in `_receive_bundle` from the contact's other endpoint |
| `marsnet/node/image_assembler.py` | `ImageAssembler.__init__` creates `output_dir` via `os.makedirs(output_dir, exist_ok=True)` |
| `tests/test_cgr.py` | New tests for `exclude_node` split-horizon behavior |
| `tests/test_contact_manager.py` | New tests verifying `prev_hop` is forwarded to `cgr_route` |
| `tests/test_connection_handler.py` | New test verifying `_receive_bundle` stamps `prev_hop` |
| `tests/test_image_assembler.py` | New test verifying `ImageAssembler` creates a missing `output_dir` |

---

## Background

- `cgr_route(contacts, source, destination, current_time, sim_start, volume_used=None)` in `marsnet/node/cgr.py` builds a flat list of directed edges — **both directions** of every contact window (`edges.append((we, ws, c, c.from_node, c.to_node))` and the reversed pair) — then relaxes them in `(window_end, window_start)` order to find the earliest arrival at `destination`.
- A `ContactEntry` has `from_node` (TCP initiator) and `to_node` (TCP acceptor); `contact_by_id(contact_id)` looks one up by id.
- `Bundle` (in `marsnet/node/bundle_store.py`) is a `@dataclass` with fields `bundle_id, src, dst, ttl, created_at, image_id, fragment_offset, total_size, data, next_hop_contact: Optional[str] = None`.
- `ConnectionHandler._receive_bundle` (in `marsnet/node/connection_handler.py`) constructs the `Bundle` from the wire payload and calls `self.on_bundle_received(bundle)`. It has access to `self.contact_id`, `self.plan` (a `ContactPlan`), and `self.node_name` — enough to look up the contact and figure out which node is on the *other* end (the one that just sent us this bundle).
- `ContactManager.inject_bundle` and `ContactManager._reassign_all_bundles` (in `marsnet/node/contact_manager.py`) both call `cgr_route(...)` to (re)compute `bundle.next_hop_contact`.
- `ImageAssembler.__init__` (in `marsnet/node/image_assembler.py`) stores `output_dir` but never creates it; `on_fragment` later does `open(os.path.join(self.output_dir, f"{image_id}.jpg"), "wb")`, which raises `FileNotFoundError` (an `OSError` subclass) if the directory is missing — silently swallowed by `ConnectionHandler.run()`'s broad `except (ConnectionResetError, BrokenPipeError, OSError): pass`.

---

### Task 1: Loop prevention — `prev_hop` + `exclude_node` split-horizon routing

**Files:**
- Modify: `marsnet/node/bundle_store.py`
- Modify: `marsnet/node/cgr.py`
- Modify: `marsnet/node/contact_manager.py`
- Modify: `marsnet/node/connection_handler.py`
- Test: `tests/test_cgr.py`, `tests/test_contact_manager.py`, `tests/test_connection_handler.py`

---

- [ ] **Step 1: Write failing test for `cgr_route`'s `exclude_node` split-horizon behavior**

Add to `tests/test_cgr.py` (the `entry` helper at the top of the file already exists — reuse it):

```python
def test_exclude_node_skips_immediate_bounce_back():
    # relay <-> rover_a opens at phase 0; relay <-> rover_b opens at phase 10;
    # rover_a <-> rover_b opens at phase 20 (period 30, duration 10 throughout).
    contacts = [
        entry("relay:1", "relay", "rover_a", phase=0,  period=30, duration=10),
        entry("relay:2", "relay", "rover_b", phase=10, period=30, duration=10),
        entry("rover_a:1", "rover_a", "rover_b", phase=20, period=30, duration=10),
    ]
    # At t=21, a bundle has just arrived at rover_b FROM rover_a (prev_hop="rover_a")
    # and is bound for "relay". Without exclusion, CGR picks the "faster" route
    # that bounces straight back through rover_a (arrival=30). With exclusion,
    # it must use the slower direct route to relay instead (arrival=40).
    without_exclusion = cgr_route(contacts, source="rover_b", destination="relay",
                                  current_time=21.0, sim_start=SIM_START)
    assert without_exclusion.next_hop_contact == "rover_a:1"
    assert without_exclusion.earliest_arrival == 30

    with_exclusion = cgr_route(contacts, source="rover_b", destination="relay",
                               current_time=21.0, sim_start=SIM_START,
                               exclude_node="rover_a")
    assert with_exclusion is not None
    assert with_exclusion.next_hop_contact == "relay:2"
    assert with_exclusion.earliest_arrival == 40


def test_exclude_node_does_not_block_unrelated_routes():
    # When the bundle's previous hop isn't on the path at all, exclude_node
    # must not change the result.
    contacts = [entry("base:1", "rover_a", "base", phase=10, period=120, duration=20)]
    result = cgr_route(contacts, source="rover_a", destination="base",
                       current_time=0.0, sim_start=SIM_START,
                       exclude_node="some_other_node")
    assert result is not None
    assert result.next_hop_contact == "base:1"
    assert result.earliest_arrival == 10.0


def test_exclude_node_none_is_default_no_filtering():
    contacts = [entry("base:1", "rover_a", "base", phase=10, period=120, duration=20)]
    result = cgr_route(contacts, source="rover_a", destination="base",
                       current_time=0.0, sim_start=SIM_START)
    assert result is not None
    assert result.next_hop_contact == "base:1"
```

- [ ] **Step 2: Run the tests to verify they fail**

```
.venv/bin/pytest tests/test_cgr.py -v -k exclude_node
```

Expected: `test_exclude_node_skips_immediate_bounce_back` FAILS with `TypeError: cgr_route() got an unexpected keyword argument 'exclude_node'`. The other two pass already (they exercise default behavior with the new kwarg, which doesn't exist yet — they'll also fail with the same `TypeError`).

- [ ] **Step 3: Add `exclude_node` parameter to `cgr_route` and apply the split-horizon filter**

In `marsnet/node/cgr.py`, change the `cgr_route` signature and the edge-building loop:

```python
def cgr_route(
    contacts: list[ContactEntry],
    source: str,
    destination: str,
    current_time: float,
    sim_start: float,
    volume_used: Optional[dict[tuple[str, float], int]] = None,
    exclude_node: Optional[str] = None,
) -> Optional[CGRResult]:
    # volume_used: maps (contact_id, window_start) -> bytes already allocated.
    if volume_used is None:
        volume_used = {}

    # flat list of directed edges.
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
            # both directions — but never let the very first hop out of `source`
            # go straight back to the node the bundle just arrived from
            # (split-horizon loop prevention).
            if not (source == c.from_node and exclude_node == c.to_node):
                edges.append((we, ws, c, c.from_node, c.to_node))
            if not (source == c.to_node and exclude_node == c.from_node):
                edges.append((we, ws, c, c.to_node, c.from_node))

    edges.sort(key=lambda e: (e[0], e[1]))
```

The rest of `cgr_route` (the relaxation loop, walk-back, and `CGRResult` construction) is unchanged.

> Note: the filter only ever fires for edges whose `sender == source` (the first hop), because `exclude_node` is compared against the *receiver* of an edge that departs from `source`. Routes that legitimately pass back through `exclude_node` later in a multi-hop chain are unaffected — only the immediate bounce-back is forbidden.

- [ ] **Step 4: Run the tests to verify they pass**

```
.venv/bin/pytest tests/test_cgr.py -v -k exclude_node
```

Expected: all 3 pass.

- [ ] **Step 5: Run the full CGR test suite to check for regressions**

```
.venv/bin/pytest tests/test_cgr.py -v
```

Expected: all tests pass (the new `exclude_node` parameter defaults to `None`, which preserves the old behavior for every existing call site and test).

- [ ] **Step 6: Add `prev_hop` field to `Bundle`**

In `marsnet/node/bundle_store.py`, add the field to the `Bundle` dataclass (after `next_hop_contact`):

```python
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
```

No test needed for this step in isolation — it's a plain dataclass field with a default, and its behavior is exercised by the tests in Steps 7 and 10.

- [ ] **Step 7: Write failing test for `ConnectionHandler._receive_bundle` stamping `prev_hop`**

`tests/test_connection_handler.py` already has a `make_handler(clock, plan, peer_handshake=None)` helper that builds a `ConnectionHandler` with `node_name="rover_a"`, `contact_id="base:1"`, a `MagicMock()` for `on_bundle_received`, and a connected `socketpair()` (`sock_a, sock_b`). It imports `proto`, `ContactPlan`, `BundleStore`, `ConnectionHandler`, `SimClock`, `MagicMock`. Add `ContactEntry` to the existing `from marsnet.node.contact_plan import ContactPlan` import (making it `import ContactEntry, ContactPlan`), and add this test:

```python
import base64


def test_receive_bundle_stamps_prev_hop_from_contact_other_endpoint():
    # The handler's node is "rover_a" (per make_handler) and its contact_id is
    # "base:1". Build a plan where that contact's OTHER endpoint is "base", so
    # a received bundle must be stamped prev_hop="base" (never "rover_a" — the
    # handler's own name — and never anything not on the contact).
    plan = ContactPlan(version=1, sim_start=1000.0, contacts=[
        ContactEntry(id="base:1", created_by="base", from_node="base",
                     to_node="rover_a", phase=0.0, period=120.0,
                     duration=20.0, rate_bps=9600, status="active"),
    ])
    clock = SimClock(1000.0)
    handler, sock_a, sock_b = make_handler(clock, plan)

    # payload is a plain dict here (mirroring make_peer_handshake above) —
    # _receive_bundle reads it via dict-style p["bundle_id"], not attribute access.
    msg = proto.Message(
        type="BUNDLE", sender="base", ts=0.0,
        payload={
            "bundle_id": "base:img:0", "src": "base", "dst": "rover_a",
            "ttl": 300.0, "created_at": 0.0, "image_id": "img",
            "fragment_offset": 0, "total_size": 4,
            "data": base64.b64encode(b"data").decode("ascii"),
        },
    )
    handler._receive_bundle(msg)

    received = handler.on_bundle_received.call_args[0][0]
    assert received.prev_hop == "base"

    sock_a.close()
    sock_b.close()
```

> `_receive_bundle` decodes `p["data"]` with `base64.b64decode`, so the test encodes the raw bytes with `base64.b64encode(...).decode("ascii")` first — matching exactly what a real peer would send over the wire.

- [ ] **Step 8: Run the test to verify it fails**

```
.venv/bin/pytest tests/test_connection_handler.py -v -k prev_hop
```

Expected: FAILS — `prev_hop` is `None` (the field exists from Step 6 but nothing stamps it yet), so `assert ... == "rover_a"` fails.

- [ ] **Step 9: Stamp `prev_hop` in `_receive_bundle`**

In `marsnet/node/connection_handler.py`, modify `_receive_bundle`:

```python
def _receive_bundle(self, msg: proto.Message) -> None:
    p = msg.payload
    bundle = Bundle(
        bundle_id=p["bundle_id"], src=p["src"], dst=p["dst"],
        ttl=p["ttl"], created_at=p["created_at"],
        image_id=p["image_id"], fragment_offset=p["fragment_offset"],
        total_size=p["total_size"],
        data=base64.b64decode(p["data"]),
    )
    contact = self.plan.contact_by_id(self.contact_id)
    if contact is not None:
        bundle.prev_hop = (contact.from_node if contact.to_node == self.node_name
                           else contact.to_node)
    self.on_bundle_received(bundle)
    proto.send_message(self.sock, proto.Message(
        type="BUNDLE_ACK", sender=self.node_name, ts=self.sim_time(),
        payload=proto.BundleAckPayload(bundle_id=bundle.bundle_id),
    ))
    if self.reporter:
        self.reporter.post("bundle_received", {
            "bundle_id": bundle.bundle_id,
            "at": self.node_name, "ts": self.sim_time(),
        })
```

(Only the new `contact = ...` / `if contact is not None:` block is added, right after constructing `bundle` and before calling `self.on_bundle_received(bundle)`. Everything else in the method is unchanged.)

- [ ] **Step 10: Run the test to verify it passes**

```
.venv/bin/pytest tests/test_connection_handler.py -v -k prev_hop
```

Expected: PASS.

- [ ] **Step 11: Write failing tests for `ContactManager` forwarding `prev_hop` to `cgr_route`**

Read `tests/test_contact_manager.py` first — it already has `make_manager`, `make_bundle`, and `make_entry` helpers, and several tests patch `marsnet.node.contact_manager.cgr_route` with a `MagicMock` (e.g. `test_inject_bundle_routes_via_cgr`, `test_reassign_does_not_duplicate_unchanged_route`) to inspect call arguments. Follow that exact pattern.

Add:

```python
def test_inject_bundle_passes_prev_hop_as_exclude_node(monkeypatch):
    mgr = make_manager()
    mock_route = MagicMock(return_value=None)
    monkeypatch.setattr("marsnet.node.contact_manager.cgr_route", mock_route)

    b = make_bundle()
    b.prev_hop = "rover_c"
    mgr.inject_bundle(b)

    _, kwargs = mock_route.call_args
    assert kwargs.get("exclude_node") == "rover_c"


def test_reassign_all_bundles_passes_prev_hop_as_exclude_node(monkeypatch):
    mgr = make_manager()
    mock_route = MagicMock(return_value=None)
    monkeypatch.setattr("marsnet.node.contact_manager.cgr_route", mock_route)

    b = make_bundle()
    b.prev_hop = "rover_c"
    mgr.bundle_store.insert(b)
    mgr._reassign_all_bundles()

    _, kwargs = mock_route.call_args
    assert kwargs.get("exclude_node") == "rover_c"
```

> Adjust `monkeypatch.setattr` / `MagicMock` usage to match whatever mocking idiom the existing `cgr_route`-patching tests in this file use (e.g. they may use `mocker` from `pytest-mock`, a module-level patch, or `monkeypatch.setattr` directly — check `conftest.py` and the imports at the top of `tests/test_contact_manager.py` and mirror them exactly).

- [ ] **Step 12: Run the tests to verify they fail**

```
.venv/bin/pytest tests/test_contact_manager.py -v -k prev_hop
```

Expected: FAIL — `cgr_route` is currently called without an `exclude_node` kwarg, so `kwargs.get("exclude_node")` is `None`, not `"rover_c"`.

- [ ] **Step 13: Pass `bundle.prev_hop` through as `exclude_node` in `ContactManager`**

In `marsnet/node/contact_manager.py`, update the two `cgr_route(...)` call sites:

`inject_bundle` (currently):
```python
    def inject_bundle(self, bundle) -> None:
        snapshot = self.plan.snapshot()
        result = cgr_route(snapshot, bundle.src, bundle.dst,
                           self.sim_time(), self.clock.value,
                           volume_used=self._volume.used())
```
becomes:
```python
    def inject_bundle(self, bundle) -> None:
        snapshot = self.plan.snapshot()
        result = cgr_route(snapshot, bundle.src, bundle.dst,
                           self.sim_time(), self.clock.value,
                           volume_used=self._volume.used(),
                           exclude_node=bundle.prev_hop)
```

`_reassign_all_bundles` (currently):
```python
            result = cgr_route(snapshot, bundle.src, bundle.dst, now,
                               self.clock.value, volume_used=self._volume.used())
```
becomes:
```python
            result = cgr_route(snapshot, bundle.src, bundle.dst, now,
                               self.clock.value, volume_used=self._volume.used(),
                               exclude_node=bundle.prev_hop)
```

- [ ] **Step 14: Run the tests to verify they pass**

```
.venv/bin/pytest tests/test_contact_manager.py -v -k prev_hop
```

Expected: PASS.

- [ ] **Step 15: Run the full test suite to check for regressions**

```
.venv/bin/pytest -v
```

Expected: all tests pass.

- [ ] **Step 16: Commit** *(run this yourself)*

```bash
git add marsnet/node/bundle_store.py marsnet/node/cgr.py marsnet/node/contact_manager.py marsnet/node/connection_handler.py tests/test_cgr.py tests/test_contact_manager.py tests/test_connection_handler.py
git commit -m "fix: prevent routing loops via prev_hop split-horizon exclusion in CGR"
```

---

### Task 2: `ImageAssembler` creates its output directory

**Files:**
- Modify: `marsnet/node/image_assembler.py`
- Test: `tests/test_image_assembler.py`

---

- [ ] **Step 1: Write failing test**

Add to `tests/test_image_assembler.py` (it already imports `os`, `tempfile`, `BundleStore`, `CryptoManager`, `ImageAssembler`):

```python
def test_init_creates_missing_output_dir():
    crypto = CryptoManager.generate()
    store = BundleStore()
    with tempfile.TemporaryDirectory() as base_dir:
        out_dir = os.path.join(base_dir, "received_images")
        assert not os.path.isdir(out_dir)
        ImageAssembler(store, crypto, out_dir, chunk_size=CHUNK)
        assert os.path.isdir(out_dir)


def test_init_tolerates_already_existing_output_dir():
    crypto = CryptoManager.generate()
    store = BundleStore()
    with tempfile.TemporaryDirectory() as out_dir:
        assert os.path.isdir(out_dir)
        # must not raise even though the directory already exists
        ImageAssembler(store, crypto, out_dir, chunk_size=CHUNK)
        assert os.path.isdir(out_dir)
```

- [ ] **Step 2: Run the tests to verify they fail**

```
.venv/bin/pytest tests/test_image_assembler.py -v -k output_dir
```

Expected: `test_init_creates_missing_output_dir` FAILS with `assert os.path.isdir(out_dir)` → `False` (directory was never created).

- [ ] **Step 3: Make `ImageAssembler.__init__` create `output_dir`**

In `marsnet/node/image_assembler.py`, change:

```python
class ImageAssembler:
    def __init__(self, store: BundleStore, crypto: CryptoManager,
                 output_dir: str, chunk_size: int = CHUNK_SIZE):
        self.store = store
        self.crypto = crypto
        self.output_dir = output_dir
        self.chunk_size = chunk_size
        self._lock = threading.Lock()
```

to:

```python
class ImageAssembler:
    def __init__(self, store: BundleStore, crypto: CryptoManager,
                 output_dir: str, chunk_size: int = CHUNK_SIZE):
        self.store = store
        self.crypto = crypto
        self.output_dir = output_dir
        self.chunk_size = chunk_size
        self._lock = threading.Lock()
        os.makedirs(self.output_dir, exist_ok=True)
```

(`os` is already imported at the top of this file.)

- [ ] **Step 4: Run the tests to verify they pass**

```
.venv/bin/pytest tests/test_image_assembler.py -v -k output_dir
```

Expected: both PASS.

- [ ] **Step 5: Run the full image assembler test suite to check for regressions**

```
.venv/bin/pytest tests/test_image_assembler.py -v
```

Expected: all tests pass — including the pre-existing `test_fragment_reassemble_roundtrip`, `test_partial_returns_none`, and `test_non_adjacent_fragments_not_prematurely_complete`, all of which already construct `ImageAssembler` with a `tempfile.TemporaryDirectory()` (which always exists), so `os.makedirs(..., exist_ok=True)` is a no-op for them.

- [ ] **Step 6: Run the full test suite to check for regressions**

```
.venv/bin/pytest -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit** *(run this yourself)*

```bash
git add marsnet/node/image_assembler.py tests/test_image_assembler.py
git commit -m "fix: ImageAssembler creates its output directory if missing"
```

---

## Acceptance Criteria

- [ ] `cgr_route` accepts an `exclude_node: Optional[str] = None` kwarg; when set, the first hop out of `source` may not go directly to `exclude_node`, but routes that legitimately pass back through that node later in a multi-hop chain are unaffected.
- [ ] `Bundle` has a `prev_hop: Optional[str] = None` field.
- [ ] `ConnectionHandler._receive_bundle` stamps `bundle.prev_hop` with the *other* endpoint of the contact the bundle arrived over (not this node's own name).
- [ ] `ContactManager.inject_bundle` and `ContactManager._reassign_all_bundles` pass `bundle.prev_hop` as `exclude_node` to `cgr_route`.
- [ ] A bundle that just arrived from node X is never immediately routed back to X as the next hop (verified by `test_exclude_node_skips_immediate_bounce_back`).
- [ ] `ImageAssembler.__init__` creates `output_dir` (including any missing parent directories) via `os.makedirs(output_dir, exist_ok=True)`, and tolerates the directory already existing.
- [ ] All pre-existing tests still pass; `is_critical_contact` continues to work unchanged (it calls `cgr_route` without `exclude_node`, which defaults to `None`).
