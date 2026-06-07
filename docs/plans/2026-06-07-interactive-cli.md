# Interactive Node CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the passive `stop_event.wait()` in `main.py` with an interactive CLI loop that lets a rover operator type image filenames to queue for transmission and check delivery progress.

**Architecture:** A `NodeCLI` class defined in `main.py` wraps the bundle store and contact manager. The relay destination is read from the `peers` dict (the `"relay"` key already present in `peers.json`) — no new config fields. The CLI runs on the main thread; all node threads continue in the background.

**Tech Stack:** Python stdlib only (`threading`, `signal`, `math`). Tests use `pytest` run via `.venv/bin/pytest`.

---

## File Map

| File | Change |
|------|--------|
| `marsnet/node/main.py` | Add `NodeCLI` class; replace `stop_event.wait()` with `_cli_loop()`; remove `--send-image`, `--destination`, `--image-id` args |
| `marsnet/node/contact_manager.py` | Remove dead `destination` parameter from `__init__` |
| `tests/test_node_cli.py` | New: unit tests for `NodeCLI` |

`marsnet/node/config.py` is **not** touched — no new config field needed.

---

## Background: how bundles work

- `fragment_image(image_path, src, dst, ttl, image_id, crypto)` reads an image file, splits it into 512-byte encrypted chunks, and returns a `list[Bundle]`.
- Each `Bundle` has: `bundle_id`, `src`, `dst`, `image_id`, `fragment_offset`, `total_size`, `data`.
- `contact_mgr.inject_bundle(bundle)` queues a bundle for routing.
- `bundle_store.all()` returns all bundles currently in the store (sent bundles are deleted from the store, expired ones are also deleted by the TTL reaper).
- Progress = `total_fragments - len([b for b in store.all() if b.image_id == image_id])`.

---

### Task 1: Add `NodeCLI` class and replace the wait loop in `main.py`

**Files:**
- Modify: `marsnet/node/main.py`
- Create: `tests/test_node_cli.py`

---

- [ ] **Step 1: Write failing tests**

Create `tests/test_node_cli.py`:

```python
from __future__ import annotations
import math
import os
import tempfile
import pytest

from marsnet.node.bundle_store import BundleStore
from marsnet.node.config import NodeConfig
from marsnet.node.crypto import CryptoManager
from marsnet.node.main import NodeCLI

TTL = 300.0
CHUNK_SIZE = 512


def make_cfg(name: str = "rover_a") -> NodeConfig:
    return NodeConfig(
        name=name, host="0.0.0.0", port=12487, udp_port=12487,
        plan_path="plans/3node_plan.json", key_path="shared.key",
        dashboard_url="http://127.0.0.1:8000", image_dir="received_images",
    )


def make_cli(cfg: NodeConfig | None = None):
    cfg = cfg or make_cfg()
    store = BundleStore()
    crypto = CryptoManager.load("shared.key")
    # inject_bundle just inserts into the store for testing
    def inject(bundle):
        store.insert(bundle)
    return NodeCLI(cfg=cfg, store=store, inject_fn=inject, crypto=crypto, ttl=TTL), store


def make_image(size_bytes: int = 1024) -> str:
    """Write a temp file of given size, return path."""
    f = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    f.write(os.urandom(size_bytes))
    f.close()
    return f.name


def test_send_inserts_correct_fragment_count():
    cli, store = make_cli()
    path = make_image(1024)
    try:
        cli.send(path)
        expected = math.ceil(1024 / CHUNK_SIZE)
        assert len(store.all()) == expected
    finally:
        os.unlink(path)


def test_send_sets_destination_to_relay():
    cli, store = make_cli()
    path = make_image(512)
    try:
        cli.send(path)
        assert all(b.dst == "relay" for b in store.all())
    finally:
        os.unlink(path)


def test_send_duplicate_filename_appends_suffix():
    cli, store = make_cli()
    path = make_image(512)
    try:
        cli.send(path)
        cli.send(path)
        image_ids = {b.image_id for b in store.all()}
        stem = os.path.splitext(os.path.basename(path))[0]
        assert stem in image_ids
        assert f"{stem}_2" in image_ids
    finally:
        os.unlink(path)


def test_send_missing_file_raises():
    cli, _ = make_cli()
    with pytest.raises(FileNotFoundError):
        cli.send("/nonexistent/path/photo.jpg")


def test_status_reflects_sent_count(capsys):
    cli, store = make_cli()
    path = make_image(1024)
    try:
        cli.send(path)
        total = math.ceil(1024 / CHUNK_SIZE)
        # remove half the bundles to simulate delivery
        bundles = store.all()
        for b in bundles[:total // 2]:
            store.delete(b.bundle_id)
        cli.status()
        captured = capsys.readouterr().out
        assert f"{total // 2}/{total}" in captured
    finally:
        os.unlink(path)


def test_status_shows_complete_when_all_sent(capsys):
    cli, store = make_cli()
    path = make_image(512)
    try:
        cli.send(path)
        for b in store.all():
            store.delete(b.bundle_id)
        cli.status()
        captured = capsys.readouterr().out
        assert "100%" in captured
    finally:
        os.unlink(path)
```

- [ ] **Step 2: Run tests to verify they fail**

```
.venv/bin/pytest tests/test_node_cli.py -v
```

Expected: all fail with `ImportError: cannot import name 'NodeCLI' from 'marsnet.node.main'`

---

- [ ] **Step 3: Add `NodeCLI` class to `main.py`**

Add this class directly in `marsnet/node/main.py`, before the `parse_args()` function:

```python
class NodeCLI:
    def __init__(self, cfg: NodeConfig, store: BundleStore,
                 inject_fn, crypto: CryptoManager, ttl: float):
        self._cfg = cfg
        self._store = store
        self._inject = inject_fn
        self._crypto = crypto
        self._ttl = ttl
        self._tracked: dict[str, int] = {}  # image_id → total_fragments

    def send(self, path: str) -> None:
        stem = os.path.splitext(os.path.basename(path))[0]
        image_id = stem
        counter = 2
        while image_id in self._tracked:
            image_id = f"{stem}_{counter}"
            counter += 1

        bundles = fragment_image(
            image_path=path, src=self._cfg.name,
            dst="relay", ttl=self._ttl,
            image_id=image_id, crypto=self._crypto,
        )
        for b in bundles:
            self._inject(b)
        self._tracked[image_id] = len(bundles)
        print(f"Queued {len(bundles)} fragments  [{image_id}] → relay")

    def status(self) -> None:
        if not self._tracked:
            print("  No images queued yet.")
            return
        for image_id, total in self._tracked.items():
            remaining = sum(
                1 for b in self._store.all() if b.image_id == image_id
            )
            sent = total - remaining
            pct = int(sent / total * 100) if total > 0 else 100
            filled = int(pct / 5)
            bar = "█" * filled + "░" * (20 - filled)
            print(f"  {image_id:<24} [{bar}] {sent}/{total}  ({pct}%)")
```

Note: `dst="relay"` uses the key name from `peers.json` directly — no hardcoded string in business logic since this convention mirrors what's already in `peers.json`.

---

- [ ] **Step 4: Run new tests to verify they pass**

```
.venv/bin/pytest tests/test_node_cli.py -v
```

Expected: all 6 tests pass.

---

- [ ] **Step 5: Update `parse_args()` — remove old image args**

Replace the current `parse_args()` function:

```python
def parse_args():
    p = argparse.ArgumentParser(description="MarsNet node")
    p.add_argument("--config", required=True, help="Path to node config JSON")
    p.add_argument("--peers", required=True,
                   help="Path to peers JSON: {name: host:port}")
    p.add_argument("--ttl", type=float, default=300.0)
    return p.parse_args()
```

---

- [ ] **Step 6: Replace `stop_event.wait()` block in `main()`**

Remove these lines from `main()`:

```python
if args.send_image:
    image_id = args.image_id or os.path.splitext(
        os.path.basename(args.send_image))[0]
    bundles = fragment_image(
        image_path=args.send_image, src=cfg.name,
        dst=args.destination, ttl=args.ttl,
        image_id=image_id, crypto=crypto,
    )
    for b in bundles:
        contact_mgr.inject_bundle(b)
    reporter.post("image_queued", {
        "image_id": image_id,
        "fragments": len(bundles),
        "ts": sim_clock.sim_time(),
    })

stop_event = threading.Event()
signal.signal(signal.SIGINT, lambda *_: stop_event.set())
signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
stop_event.wait()
```

Also remove the `destination=args.destination` line from the `ContactManager(...)` constructor call (it becomes unused). Check what `destination` was used for in `ContactManager` and pass a sensible default or remove it — see note below.

Replace with:

```python
stop_event = threading.Event()
signal.signal(signal.SIGINT,  lambda *_: stop_event.set())
signal.signal(signal.SIGTERM, lambda *_: stop_event.set())

cli = NodeCLI(
    cfg=cfg,
    store=bundle_store,
    inject_fn=contact_mgr.inject_bundle,
    crypto=crypto,
    ttl=args.ttl,
)

print(f"[{cfg.name}] Node ready. Type an image path to send, 'status' to check progress, 'q' to quit.")

def _cli_loop() -> None:
    while not stop_event.is_set():
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            stop_event.set()
            break
        if line in ("q", "quit"):
            stop_event.set()
            break
        elif line == "status":
            cli.status()
        elif line == "":
            print('  Type an image path, "status", or "q".')
        else:
            try:
                cli.send(line)
            except FileNotFoundError:
                print(f"  File not found: {line}")

_cli_loop()
```

> **`destination` in `ContactManager` is a dead field** — it is stored in `__init__` but never read anywhere. Remove it: delete the `destination: str` parameter from `ContactManager.__init__` (line 30 of `marsnet/node/contact_manager.py`), the `self.destination = destination` assignment (line 40), and the `destination=args.destination` keyword argument from the `ContactManager(...)` constructor call in `main()`.

---

- [ ] **Step 7: Run full test suite**

```
.venv/bin/pytest -v
```

Expected: all tests pass (no regressions from removing `--send-image`/`--destination`/`--image-id`).

---

- [ ] **Step 8: Commit** *(run this yourself)*

```bash
git add marsnet/node/main.py tests/test_node_cli.py
git commit -m "feat: interactive CLI for image queuing and progress tracking"
```

---

## Acceptance Criteria

- [ ] `> ` prompt appears after node startup.
- [ ] Typing an image path queues it and prints fragment count.
- [ ] Typing the same filename twice produces two separate image IDs.
- [ ] `status` shows a progress bar per image with `sent/total (pct%)`.
- [ ] `status` shows `100%` once all bundles have left the store.
- [ ] `q` and Ctrl-C both shut down cleanly (same teardown path as before).
- [ ] `--send-image`, `--destination`, `--image-id` args no longer exist.
- [ ] All pre-existing tests still pass.
