# MarsNet — Design Document

> This document records every design decision made during the planning phase, including the reasoning behind each choice. It is the authoritative reference for implementation. The implementation plan is at `docs/plans/2026-05-25-marsnet.md`.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Network Topology](#2-network-topology)
3. [Protocol Foundations](#3-protocol-foundations)
4. [Contact Plan](#4-contact-plan)
5. [CGR Algorithm](#5-cgr-algorithm)
6. [Contact Plan Versioning and Distribution](#6-contact-plan-versioning-and-distribution)
7. [Dynamic Contact Plan Discovery](#7-dynamic-contact-plan-discovery)
8. [Wire Protocol](#8-wire-protocol)
9. [Bundle Format and Image Transmission](#9-bundle-format-and-image-transmission)
10. [Node Architecture and Threading Model](#10-node-architecture-and-threading-model)
11. [Dashboard](#11-dashboard)
12. [Decision Log](#12-decision-log)

---

## 1. Project Overview

MarsNet simulates a communication network for a swarm of Mars rovers. The core problem is that standard networking protocols assume a continuous connection between sender and receiver. On Mars, links go down for extended periods due to terrain, dust storms, and orbital positions. There is no fixed infrastructure and no real-time help from Earth.

MarsNet implements two protocols NASA actually uses for deep-space missions:

- **Delay-Tolerant Networking (DTN):** Instead of dropping a message when the link is gone, a node stores it locally and forwards it the moment a connection becomes available. Every node acts as a store-and-forward relay.

- **Contact Graph Routing (CGR):** Each node holds a pre-shared schedule of when it will be in contact with its neighbors. When a bundle needs to be delivered, CGR finds the path that gets it there the earliest by reasoning over future contact windows.

The simulation runs on **real computers** (not containers). Nodes communicate over **TCP sockets**. Contact windows are **short real-time windows** (seconds to minutes) rather than hours, to make the simulation demonstrable.

---

## 2. Network Topology

Six nodes:

| Node | Type | Role |
|---|---|---|
| `base` | Ground station | Primary delivery destination; always on |
| `rover_a` | Ground rover | Has direct contact with base |
| `rover_b` | Ground rover | Has direct contact with base |
| `rover_c` | Ground rover | Relay-only path to base |
| `rover_d` | Ground rover | Relay-only path to base |
| `relay` | Orbiting satellite | Passes over all ground nodes periodically |

Rovers `c` and `d` have no direct contact with base — they can only reach it through the relay satellite. This creates multi-hop routing scenarios that exercise CGR meaningfully.

Each node is an independent Python process. Nodes know each other's IP addresses via a pre-distributed `peers.json` file.

---

## 3. Protocol Foundations

### Why TCP

Each contact window is modeled as a **short-lived TCP connection**. TCP is chosen because bundle delivery requires reliability: a lost bundle segment must be retransmitted, and the sender must know when the full bundle has been received before deleting its local copy (custody transfer). Reimplementing this reliability over UDP would have no benefit.

### Simulation time

All contact window timings are expressed as **seconds since simulation start** (`sim_start`), a Unix timestamp distributed in the initial contact plan. Current simulation time is always computed as `time.time() - sim_start`. This lets late-joining nodes correctly interpret contact windows without wall-clock synchronization.

---

## 4. Contact Plan

### All contacts are periodic

Every contact in MarsNet is **periodic** — it recurs on a fixed schedule. This is a deliberate simplification: real Mars orbital mechanics are inherently periodic, and the satellite is the most realistic node in the simulation (it has a fixed orbit period). One-off contact windows are not needed.

A contact is described by four timing fields:

| Field | Meaning |
|---|---|
| `phase` | Seconds from `sim_start` when the first window opens |
| `period` | Seconds between successive window openings |
| `duration` | Seconds each window remains open |
| `rate_bps` | Link speed in bits per second |

**Window computation:**

```
window_n_start = phase + n × period          (n = 0, 1, 2, …)
window_n_end   = window_n_start + duration
```

To find the next window at or after time `t`:

```
n = max(0, floor((t - phase) / period))
start = phase + n × period
if start + duration <= t:          # window n is already over
    n += 1
    start = phase + n × period
end = start + duration
```

### Per-window volume constraint

Each occurrence of a contact window has a capacity:

```
capacity_bytes = rate_bps × duration / 8
```

This resets every period — each TCP connection is independent and its bandwidth does not carry over between passes. The CGR engine tracks how many bytes have already been allocated to a given window instance and skips windows that are full.

### Contact plan document format

```json
{
  "version": 7,
  "sim_start": 1716840000.0,
  "contacts": [
    {
      "id": "relay:1",
      "created_by": "relay",
      "from": "relay",
      "to": "base",
      "phase": 0,
      "period": 120,
      "duration": 20,
      "rate_bps": 19200,
      "status": "active"
    }
  ]
}
```

### Contact status

`status` is either `"active"` or `"cancelled"`. Contacts transition only from `active` → `cancelled` and never back. Cancelled contacts **remain in the plan permanently** — removing them would make them indistinguishable from contacts the node has never heard of, which breaks plan merging.

### Node-namespaced contact IDs

Any node may create new contacts. The ID format is `<creator_name>:<local_counter>` (e.g. `relay:1`, `rover_a:3`). Because node names are globally unique (routing requires it), no two nodes can generate the same ID without coordination. The local counter never resets, even across restarts — a restarted node continues from where it left off, so a new contact after restart gets a new ID and is never confused with a cancelled old one.

### `from` and `to` fields

`from` is the **TCP initiator** (the node that calls `connect()`). `to` is the **TCP acceptor** (the node that calls `accept()`). The Contact Manager only schedules outbound connections for contacts where `from == self.name`. This prevents both sides from simultaneously trying to connect to each other.

---

## 5. CGR Algorithm

### What CGR does

CGR is a **time-aware modified Dijkstra** over the contact graph. Instead of minimizing hop count or static link cost, it computes the **earliest possible arrival time** at the destination by reasoning about when each contact window opens and closes.

### Inputs and outputs

```
Input:
  contacts      — snapshot of active contacts from the plan
  source        — name of the sending node
  destination   — name of the intended recipient
  current_time  — seconds since sim_start
  volume_used   — dict: (contact_id, window_start) → bytes already allocated

Output:
  CGRResult(next_hop_contact, earliest_arrival)
  or None if destination is unreachable
```

### Algorithm

```
1. Enumerate all contact windows within a planning horizon (3600s default)
   for each active contact, for each window (start, end) in the horizon:
     capacity = rate_bps × duration / 8
     remaining = capacity - volume_used.get((contact_id, window_start), 0)
     if remaining > 0: add (window_end, window_start, contact, remaining) to list

2. Sort the list by window_end (ascending)

3. Initialize:
     earliest[source] = current_time
     earliest[all other nodes] = ∞
     prev_contact[node] = None for all nodes

4. For each (window_end, window_start, contact, remaining) in sorted list:
     fn = contact.from_node
     tn = contact.to_node
     if fn not in earliest: skip           # can't reach transmitting node
     departure = max(window_start, earliest[fn])
     if departure >= window_end: skip      # bundle can't make it in time
     arrival = departure                   # no OWLT (propagation delay = 0)
     if arrival < earliest[tn]:
         earliest[tn] = arrival
         prev_contact[tn] = contact

5. Walk prev_contact[] backwards from destination to source
   to identify the first hop contact.

6. Return CGRResult(first_hop_contact.id, earliest[destination])
```

### Features included

| Feature | Included | Notes |
|---|---|---|
| Volume constraints | ✅ | Per-window, resets each period |
| Critical contact detection | ✅ | Run CGR without the contact; if unreachable → critical |
| OWLT (propagation delay) | ❌ | Explicitly excluded — at simulation scale it changes nothing |

### Critical contact detection

A contact is **critical** for a given bundle if removing it from the plan makes the destination unreachable:

```python
def is_critical_contact(contacts, contact_id, source, destination,
                         current_time, sim_start):
    filtered = [c for c in contacts if c.id != contact_id]
    return cgr_route(filtered, source, destination,
                     current_time, sim_start) is None
```

This is checked after each CGR computation. Critical contacts are logged and reported to the dashboard. If a critical contact fails, the re-route event is flagged prominently.

### CGR is stateless

The CGR engine is a **pure function** — it takes a snapshot of the contact plan and produces a result without modifying any shared state. This means it can be called safely from any thread after taking a snapshot under the plan lock. The plan lock is released before CGR computation begins.

### When CGR runs

CGR is invoked:
1. When a new bundle is injected into the node (to assign an initial next hop)
2. When a contact fails (to reassign all bundles whose next hop is now invalid)
3. When the plan updates (same as above — full reassignment)

### Volume tracking

`volume_used` is a dict mapping `(contact_id, window_start_time) → bytes_allocated`. It is updated by the Contact Manager when a bundle is enqueued for a contact. When a bundle is ACK'd or dropped, its allocation is released.

---

## 6. Contact Plan Versioning and Distribution

### Why versioning

Multiple nodes may detect a failure simultaneously and independently modify the plan. Without versioning, these concurrent modifications produce contradictory plan states that cannot be reconciled.

### Version number

`version` is a **monotonically increasing integer**. It increments by 1 every time any contact is added or cancelled. There is no global authority — any node may increment the version.

### Conflict: the same problem

Two nodes can independently increment the version from `N` to `N+1` with different changes. When they later exchange plans, both claim to have version `N+1`.

### Resolution: cancellation is one-way

The key insight that makes conflict resolution trivial: **contacts can only move from `active` to `cancelled`, never back**. Taking the union of all cancellations from two conflicting plans is always safe — you never lose information by doing so.

### Merge algorithm

When node A receives a plan from node B:

```
1. For every contact in B that does not exist in A:
     add it to A                                         → changed = True

2. For every contact in B that is "cancelled" but "active" in A:
     cancel it in A                                      → changed = True

3. If changed:
     A.version = max(A.version, B.version) + 1
```

This rule handles all cases:
- **B.version > A.version**: B's newer contacts are added; B's cancellations propagate
- **B.version < A.version**: only B's cancellations (if any) propagate; A gains nothing new otherwise
- **B.version == A.version**: union of cancellations; version increments only if anything changed

### Contact additions by any node

Any node may add contacts in its own namespace. Since namespaced IDs are globally unique, two nodes can never produce the same ID. There is no "conflicting contact" problem — two different IDs for the same pair of nodes are simply two separate contacts and CGR treats them as independent.

**Why not replace instead of merge?**

Replacing (discarding your plan and taking the peer's entirely) is only needed when contacts can be *deleted or modified*, not just cancelled. In MarsNet, contacts are immutable once created — they can only be cancelled. Cancelled contacts stay in the plan as permanent historical records. Merge is therefore always safe.

The only time replacing would be necessary is if a central authority pushed a versioned plan that removed contacts entirely (e.g. to handle a contact that was incorrectly defined). MarsNet does not support this.

### Mid-demo node failure flow

```
t=45s  Presenter kills rover_c

t=46s  rover_a: heartbeat to rover_c times out
         → contact_manager.report_failure("relay:4")  [relay→rover_c contact]
         → plan.cancel_contact("relay:4")
         → plan.version 5 → 6
         → CGR re-runs for all queued bundles
         → dashboard: contact_failed event

t=46s  relay: heartbeat to rover_c times out (simultaneous)
         → same cancellation
         → plan.version 5 → 6   ← conflict!

t=47s  rover_a and relay exchange plans during next heartbeat:
         → both version 6, same cancellation → identical, no change
         (if they cancelled different contacts → merge → version 7)
```

### Node rejoining after restart

When `rover_c` restarts:
1. It comes back with its original plan (version 1, or empty)
2. It broadcasts `HELLO` via UDP
3. A neighbor connects TCP and sends the current plan (version 6)
4. `rover_c` adopts version 6 — its own contacts are now marked cancelled
5. `rover_c` creates **new contacts** under new IDs (`rover_c:N+1`, etc.)
6. These propagate to the network via the next heartbeat plan sync

This is realistic: in real DTN, a restarted node cannot self-reinstate cancelled contacts.

---

## 7. Dynamic Contact Plan Discovery

### The "lost" state

A node is **lost** when its contact plan contains no future active windows — either because it has never received a plan, or because all its contacts have expired or been cancelled.

### UDP broadcast for HELLO

A lost node sends a `HELLO` message via **UDP broadcast** to `255.255.255.255` on a well-known port (7000). This requires zero configuration — the lost node does not need to know any peer addresses.

**Why UDP and not TCP to a known bootstrap address?**

UDP broadcast eliminates all bootstrap configuration. On a LAN of real computers, any node on the subnet can hear it. No hardcoded IP addresses are needed.

**Why does the response use TCP?**

A full contact plan can be several kilobytes. UDP has no reliability guarantees and a practical payload ceiling of ~1400 bytes per packet without fragmentation. The responder connects TCP to the lost node and sends the full plan reliably.

### Who responds to HELLO

A node only responds to `HELLO` if the **sender appears in its own contact plan** (either as `from_node` or `to_node` in any contact). This naturally simulates physical proximity — only nodes that "know about you" (have you in their plan) respond, which on Mars corresponds to nodes within communication range.

### HELLO broadcast flow

```
rover_c (lost) → UDP 255.255.255.255:7000
  { type: "HELLO", sender: "rover_c", tcp_port: 7004, plan_version: 0 }

base hears it, checks plan → rover_c appears in relay:4 → responds:
  base → TCP connect → rover_c:7004
  base → { type: "PLAN", payload: { plan: <full plan v6> } }

rover_a hears it, checks plan → rover_c in relay:4 → also responds
rover_c accepts both, takes the higher version, closes both connections
```

### HELLO broadcaster behavior

- Broadcasts every 5 seconds with ±0.5s random jitter (so two simultaneously-lost nodes don't lockstep)
- Stops automatically when the node is no longer lost (valid plan received)

---

## 8. Wire Protocol

### Transport and framing

All messages are **newline-delimited JSON (NDJSON)** over TCP. Each message is a single-line JSON object terminated by `\n`. Python reads with `socket.makefile("rb").readline()`. No binary framing, no length headers.

NDJSON is chosen because:
- Human-readable (easy to debug with Wireshark or `netcat`)
- Python's `json` module handles it without dependencies
- Line-delimited framing is trivial to implement

### Message envelope

Every message has this outer structure:

```json
{
  "type":    "HANDSHAKE",
  "sender":  "rover_a",
  "ts":      12.4,
  "payload": { ... }
}
```

`ts` is seconds since `sim_start`.

### Message types

| Type | Transport | Sender → Receiver | Purpose |
|---|---|---|---|
| `HANDSHAKE` | TCP (scheduled contact) | both directions | Opens a contact window, announces plan version |
| `REQUEST_PLAN` | TCP (scheduled contact) | lower-version → higher | Ask for full plan |
| `PLAN` | TCP (scheduled or HELLO response) | higher-version → lower | Full plan document |
| `BUNDLE` | TCP (scheduled contact) | any → next hop | One bundle (possibly a fragment) |
| `BUNDLE_ACK` | TCP (same connection) | receiver → sender | Confirms custody; sender deletes local copy |
| `HEARTBEAT` | TCP (scheduled contact) | both directions | Liveness ping |
| `HEARTBEAT_ACK` | TCP (same connection) | both directions | Liveness response |
| `HELLO` | UDP broadcast | lost node → subnet | Announces lost state, requests plan |

### Contact window lifecycle

```
[Contact Manager opens TCP connection at scheduled window start]

Initiator → Acceptor:  HANDSHAKE { contact_id, plan_version }
Acceptor  → Initiator: HANDSHAKE { contact_id, plan_version }

[if versions differ: lower-version sends REQUEST_PLAN, higher sends PLAN]
[CGR re-runs if plan changed]

[both sides now in ACTIVE state — full duplex]

Initiator → Acceptor:  BUNDLE { ... }
Acceptor  → Initiator: BUNDLE_ACK { bundle_id }

[every HEARTBEAT_INTERVAL seconds]
Initiator → Acceptor:  HEARTBEAT {}
Acceptor  → Initiator: HEARTBEAT_ACK {}

[Contact Manager fires scheduled-end timer]
[Connection Handler finishes in-flight transmission, closes socket]
```

### Heartbeat and failure detection

- Heartbeat interval: **2 seconds**
- Failure timeout: **6 seconds** (3 missed heartbeats)
- On timeout: `contact_manager.report_failure(contact_id)` is called
- The Contact Manager transitions the contact state to `FAILED`, cancels it in the plan, and re-runs CGR for all queued bundles

### Binary data in BUNDLE messages

The `data` field in `BUNDLE` payloads is **base64-encoded** in JSON. This handles the fact that encrypted image fragments are arbitrary binary data that cannot appear directly in JSON strings.

---

## 9. Bundle Format and Image Transmission

### Bundle fields

| Field | Type | Meaning |
|---|---|---|
| `bundle_id` | string | `<creator>:<image_id>:<fragment_offset>` — globally unique |
| `src` | string | Originating node |
| `dst` | string | Final destination |
| `ttl` | float | Seconds from `created_at` until expiry |
| `created_at` | float | Unix timestamp of creation |
| `image_id` | string | Identifies which image this fragment belongs to |
| `fragment_offset` | int | Byte offset of this fragment in the original image |
| `total_size` | int | Total original image size in bytes |
| `data` | bytes | Encrypted fragment payload |
| `next_hop_contact` | string\|None | CGR-assigned contact ID for next transmission |

### Image fragmentation

Images are split into fixed-size chunks (default: **512 bytes**). Each chunk is Fernet-encrypted independently and wrapped in a bundle. Fragmentation happens at the source node before injection into the bundle store.

```
original image (N bytes)
  → split into ceil(N / 512) chunks
  → each chunk: Fernet.encrypt(chunk) → encrypted_bytes
  → each encrypted_bytes → Bundle(fragment_offset = chunk_index × 512)
  → all bundles injected into bundle store
  → CGR assigns next_hop_contact to each
```

### Image reassembly

At the destination node, the `ImageAssembler` is notified whenever a new fragment lands in the bundle store. It checks whether all expected fragments have arrived:

```
expected_offsets = { 0, 512, 1024, …, last_chunk_start }
received_offsets = { b.fragment_offset for b in store if b.image_id == id }
if received_offsets == expected_offsets:
    decrypt each fragment in order → concatenate → trim to total_size → write to disk
```

### Progressive display

The dashboard is notified via a `fragment_received` event each time a new fragment arrives. The frontend renders a progress visualization: received fragments are shown as green bands, missing ones as grey. This gives a live "image loading" effect that is visually compelling for the demo.

### Application-layer encryption

**Choice: Fernet (symmetric AES-128-CBC + HMAC-SHA256).**

Fernet was chosen over raw AES because:
- Provides both confidentiality and integrity in one call
- Python's `cryptography` library implements it with a clean API
- Uses a random IV per encryption call (different ciphertext each time for same plaintext)
- The key is pre-distributed to all nodes before the simulation starts (one `shared.key` file, identical on every node)

Encryption is **per-fragment**, not per-image. This means the assembler decrypts each fragment separately before concatenation.

**What "secure" means here:** confidentiality (intercepted bundles reveal no image content) and integrity (any tampering is detected). This is application-layer security, not transport-layer (no TLS). The demo can show the encrypted bytes in transit and the decrypted image at the destination.

---

## 10. Node Architecture and Threading Model

### Thread inventory

| Thread | Count | Lifecycle | Responsibility |
|---|---|---|---|
| TCP Listener | 1 | always running | `socket.accept()` loop; routes to Connection Handler or plan updater |
| Connection Handler | 1 per active connection | one contact window | Full connection lifecycle using `select()` |
| Contact Manager | 1 | always running | `threading.Timer` scheduler; owns contact state machine |
| UDP Listener | 1 | always running | Receives `HELLO` broadcasts; responds with `PLAN` via TCP |
| HELLO Broadcaster | 1 | only when lost | Periodic UDP broadcast until valid plan received |
| TTL Reaper | 1 | always running | Sweeps expired bundles every second |
| Image Assembler | event-driven | called from TCP Listener thread | Checks for complete image sets; writes to disk |
| Dashboard Reporter | 1 | always running | Drains event queue; POSTs to dashboard via HTTP |

### Shared state and locking

| State | Accessed by | Lock type | Notes |
|---|---|---|---|
| Bundle Store | Connection Handler, TTL Reaper, Contact Manager | `threading.Lock` | All ops are short; no recursive access |
| Contact Plan | Connection Handler, Contact Manager, CGR, UDP Listener | `threading.Lock` | CGR takes snapshot and releases before computing |
| Contact States | Contact Manager (owner), Connection Handler (calls report_failure) | `threading.Lock` | `report_failure` is idempotent — checks state before acting |
| Dashboard Event Queue | all threads (put), Dashboard Reporter (get) | `queue.Queue` | Thread-safe by design; no additional lock needed |

**Lock ordering:** When multiple locks must be held simultaneously (Contact Manager's `report_failure` touches both Contact States and the Plan), they are **always acquired in the same order**: Contact States lock first, Plan lock second. Inverting this order anywhere would risk deadlock.

**`Lock` not `RLock`:** All lock-acquiring functions are written to be non-reentrant — no function acquires a lock and then calls another function that acquires the same lock. CGR takes a snapshot under the plan lock, releases, then computes. This makes plain `Lock` safe everywhere and avoids the subtle bugs that `RLock` can mask.

### Connection Handler: `select()` design

Each TCP connection is managed by a single Connection Handler thread. Rather than spawning two inner threads (one for send, one for receive), `select()` is used for multiplexing:

```
while not close_event.is_set() and sim_time < end_time:

    timeout = min(0.1, time_until_next_heartbeat)
    readable, _, _ = select.select([sock], [], [], timeout)

    if readable:
        msg = recv_message(sock_file)
        handle_incoming(msg)               # BUNDLE, BUNDLE_ACK, HEARTBEAT, etc.

    try:
        bundle = outbound_queue.get_nowait()
        send_bundle(sock, bundle)
    except queue.Empty:
        pass

    if time_since_last_heartbeat >= HEARTBEAT_INTERVAL:
        send_heartbeat(sock)

    if time_since_last_ack >= HEARTBEAT_TIMEOUT:
        contact_manager.report_failure(contact_id)
        return
```

The `select()` call with a short timeout (0.1s) ensures we wake up frequently to check the outbound queue and heartbeat timer without spinning. The connection is **full-duplex** — both sides can send and receive bundles on the same connection.

**Why `select()` over two inner threads:** Two threads per connection would cost an extra thread for every active connection (up to 5–6 simultaneously). `select()` achieves the same multiplexing in one thread with simpler control flow and no additional synchronization between sender and receiver.

### Contact Manager state machine

Contact Manager is the **sole owner** of contact states. No other component may change a contact state directly.

```
State transitions:
  PENDING → OPEN      (Contact Manager: timer fires, TCP connect succeeds)
  OPEN    → CLOSED    (Contact Manager: scheduled end timer fires)
  OPEN    → FAILED    (Contact Manager: report_failure called by Connection Handler)
  FAILED  → (terminal)
  CLOSED  → (terminal)
```

`report_failure` is **idempotent**: it checks the current state before acting. If the state is already `FAILED` or `CLOSED`, it returns immediately. This prevents the race condition where both a scheduled close and a heartbeat timeout fire within milliseconds of each other.

The Connection Handler never calls `socket.close()` directly in the failure path — it calls `contact_manager.report_failure()` and lets the Contact Manager clean up.

### Heartbeat Monitor

There is **no global Heartbeat Monitor thread**. Each Connection Handler is its own heartbeat monitor for its connection. It tracks `last_heartbeat_ack_time` and checks it on every `select()` loop iteration. This keeps failure detection local to the connection that failed and avoids a separate thread per connection.

### HELLO Broadcaster lifecycle

The HELLO Broadcaster is only started when the node is lost (`plan.is_lost(current_sim_time) == True`). It stops itself when it detects the plan is no longer lost (valid plan received from a peer). It is also explicitly stopped on clean shutdown.

---

## 11. Dashboard

### Architecture

```
Each node process
  └── DashboardReporter thread
        └── HTTP POST to /event (non-blocking, fire-and-forget)

Dashboard server (FastAPI + uvicorn)
  ├── POST /event  → receives events from nodes → broadcasts via WebSocket
  └── GET  /ws     → browser connects here for real-time updates

Browser (HTML/JS Canvas)
  └── WebSocket client → receives events → updates visualization
```

### Why not direct node-to-browser WebSocket

Nodes don't run web servers. Introducing a central dashboard server keeps the node code focused on networking and decouples the visualization from the simulation. If the dashboard is offline, nodes continue operating — `DashboardReporter` silently drops events that fail to POST.

### Event types posted by nodes

| Event | Posted by | Key fields |
|---|---|---|
| `contact_open` | Connection Handler | `contact_id`, `ts` |
| `contact_closed` | Connection Handler | `contact_id`, `ts` |
| `contact_failed` | Contact Manager | `contact_id`, `ts` |
| `bundle_sent` | Connection Handler | `bundle_id`, `from`, `contact_id`, `ts` |
| `bundle_received` | Connection Handler | `bundle_id`, `at`, `ts` |
| `bundle_dropped` | TTL Reaper | `bundle_id`, `ts` |
| `fragment_received` | Node main (on_bundle_received) | `image_id`, `fragment_offset`, `total_size`, `ts` |
| `plan_updated` | Node main (on_plan_update) | `version`, `ts` |
| `image_queued` | Node main (send-image path) | `image_id`, `fragments`, `ts` |

### Visualization elements

| Element | How it works |
|---|---|
| Mars surface | Static gradient background (dark red/brown) |
| Node icons | Fixed canvas positions; grey out when node fails |
| Satellite | Moves along an elliptical arc driven by wall-clock time, matching the relay's `period` |
| Contact lines | Appear between node pairs when `contact_open` event arrives; disappear on `contact_closed` or `contact_failed` |
| Bundle in-flight | Small dot animating along an active contact line on `bundle_sent` event |
| Image progress | Canvas panel showing green (received) vs grey (missing) bands per fragment; updates on each `fragment_received` event |
| Event log | Scrolling list of recent events, colour-coded by type |

### Dashboard tech stack

| Component | Choice | Reason |
|---|---|---|
| Backend | FastAPI + uvicorn | async, minimal, WebSocket support built-in, no new Python deps beyond requirements |
| Frontend | Plain HTML/CSS/JS | No build step, no framework — easy to demo from any browser |
| Real-time push | WebSocket | One persistent connection, no polling, sub-100ms latency |
| Node → dashboard | stdlib `urllib.request` | No extra deps on node side; async via `queue.Queue` in DashboardReporter |

---

## 12. Decision Log

A compact record of every explicit decision made during design, with the rejected alternatives.

| # | Decision | Alternatives considered | Reason chosen |
|---|---|---|---|
| 1 | **Short real-time contact windows** (seconds) | Simulated accelerated time | Eliminates a global time-simulation layer; simpler code, easier to demo |
| 2 | **All contacts periodic** | Mix of periodic and one-shot | Covers all nodes naturally (satellite orbits are periodic); simpler plan schema |
| 3 | **No OWLT** | Include propagation delay field | At simulation timescales, propagation delay changes no routing decisions; adds complexity for zero benefit |
| 4 | **Volume constraints per window** (not per contact lifetime) | Per-lifetime volume | Each TCP connection is independent; per-window resets naturally |
| 5 | **Critical contact detection** | No detection | Adds meaningful routing metadata; easy to implement as one CGR call with the contact removed |
| 6 | **Node-namespaced contact IDs** (`relay:1`) | UUIDs; central ID allocator | Eliminates all coordination for ID uniqueness; uses an invariant already required by routing (unique node names) |
| 7 | **Any node may create contacts** | Base station only | Some rovers have no direct base contact; they must be able to announce new contacts autonomously |
| 8 | **Any node may cancel contacts; base station is not authoritative** | Base station authoritative for additions | Consistent authority model for both operations; base may be unreachable |
| 9 | **Merge with cancellation union** | Replace on higher version | Cancellations are the only mutations; union is always safe; replace is only needed when contacts can be deleted or modified, which MarsNet does not support |
| 10 | **Cancelled contacts stay in plan permanently** | Remove cancelled contacts | A removed contact is indistinguishable from one never seen, which breaks merge; permanent cancellation records enable correct merge across reconnects |
| 11 | **TCP for contact windows** | UDP with reliability layer | Bundle delivery requires custody transfer (don't delete until ACK); reimplementing reliability over UDP has no benefit |
| 12 | **UDP broadcast for HELLO** | TCP to hardcoded bootstrap address; UDP multicast | Zero configuration; works on any LAN subnet without pre-distributed IP addresses |
| 13 | **Only nodes with sender in plan respond to HELLO** | All nodes respond | Simulates physical proximity (plan encodes who can reach whom); prevents spurious plan syncs from unrelated nodes |
| 14 | **`select()` in Connection Handler** | Two inner threads (send + receive) | At most 5–6 simultaneous connections; `select()` avoids extra threads and synchronization with equivalent behaviour |
| 15 | **No separate Heartbeat Monitor thread** | Global heartbeat thread; per-connection thread | Each Connection Handler monitors its own connection; failure detection is local and isolated |
| 16 | **Contact Manager owns all contact state transitions** | Distributed state | Prevents race conditions (double-close, double CGR) when scheduled close and heartbeat timeout fire simultaneously; `report_failure` is idempotent |
| 17 | **`Lock` everywhere, not `RLock`** | `RLock` for plan | All lock-acquiring functions are non-reentrant by design; `Lock` is simpler and makes bugs visible rather than hiding them |
| 18 | **Lock acquisition order: Contact States → Plan** | No fixed order | Consistent ordering prevents deadlock between two different locks held simultaneously |
| 19 | **Fernet per-fragment encryption** | TLS; per-image encryption; Bundle Security Protocol | Simple API, one import; per-fragment enables partial-image integrity checking; BSP is out of scope for a class project |
| 20 | **NDJSON over TCP** | Binary framing; HTTP; custom protocol | Human-readable (Wireshark-friendly); zero extra parsing code; line-delimited framing is trivial in Python |
| 21 | **DashboardReporter uses `queue.Queue` + background thread** | Synchronous HTTP POST in caller thread | No node thread ever blocks waiting for an HTTP response; dashboard can be offline without affecting simulation |
| 22 | **Progressive image display (fragment bands)** | Show only complete image | Visually demonstrates the DTN store-and-forward effect in real time; missing fragments are visible |
| 23 | **Real computers (not Docker containers)** | Docker Compose | Avoids container networking complexity; demo is more tangible with physical machines |
