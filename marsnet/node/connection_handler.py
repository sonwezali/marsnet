from __future__ import annotations
import base64
import queue
import select
import threading
import time
from enum import Enum, auto
from typing import Callable, Optional

from marsnet.node import protocol as proto
from marsnet.node.bundle_store import Bundle, BundleStore
from marsnet.node.contact_plan import ContactPlan

HEARTBEAT_INTERVAL = 2.0
HEARTBEAT_TIMEOUT  = 6.0


class State(Enum):
    HANDSHAKING = auto()
    SYNCING     = auto()
    ACTIVE      = auto()
    CLOSING     = auto()
    CLOSED      = auto()


class ConnectionHandler:
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
        outbound_queue: queue.Queue,        # bundles routed to this contact
        on_failure: Callable[[str], None],  # contact_manager.report_failure
        on_plan_update: Callable[[ContactPlan], None],
        on_bundle_received: Callable[[Bundle], None],
        sim_start: float,
        dashboard_reporter=None,
        peer_handshake: Optional[proto.Message] = None,
        on_bundle_acked: Optional[Callable[[str], None]] = None,
    ):
        self.sock                 = sock
        self.contact_id           = contact_id
        self.is_initiator         = is_initiator
        self.close_event          = close_event
        self.end_time             = end_time
        self.node_name            = node_name
        self.plan                 = plan
        self.bundle_store         = bundle_store
        self.outbound_queue       = outbound_queue
        self.on_failure           = on_failure
        self.on_plan_update       = on_plan_update
        self.on_bundle_received   = on_bundle_received
        self.sim_start            = sim_start
        self.reporter             = dashboard_reporter
        self.peer_handshake       = peer_handshake
        self.on_bundle_acked      = on_bundle_acked

        self._sock_file           = sock.makefile("rb", buffering=0)
        self._state               = State.HANDSHAKING
        self._last_heartbeat_sent = 0.0
        self._last_heartbeat_ack  = time.time()

    def sim_time(self) -> float:
        return time.time() - self.sim_start

    def run(self) -> None:
        try:
            self._handshake()
            if self._state == State.ACTIVE:
                self._active_loop()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            self._cleanup()

    def _handshake(self) -> None:
        proto.send_message(self.sock, proto.Message(
            type="HANDSHAKE", sender=self.node_name, ts=self.sim_time(),
            payload=proto.HandshakePayload(
                contact_id=self.contact_id,
                plan_version=self.plan.version,
            )
        ))

        if self.peer_handshake is not None:
            msg = self.peer_handshake
        else:
            msg = proto.recv_message(self._sock_file)
        if not msg or msg.type != "HANDSHAKE":
            return

        peer_version = msg.payload["plan_version"]
        if peer_version > self.plan.version:
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

    def _request_plan(self) -> None:
        proto.send_message(self.sock, proto.Message(
            type="REQUEST_PLAN", sender=self.node_name, ts=self.sim_time(),
            payload=proto.RequestPlanPayload(since_version=self.plan.version),
        ))
        msg = proto.recv_message(self._sock_file)
        if msg and msg.type == "PLAN":
            received = ContactPlan.from_dict(msg.payload["plan"])
            if self.plan.merge(received):
                self.on_plan_update(self.plan)

    def _send_plan(self) -> None:
        proto.send_message(self.sock, proto.Message(
            type="PLAN", sender=self.node_name, ts=self.sim_time(),
            payload=proto.PlanPayload(plan=self.plan.to_dict()),
        ))

    def _active_loop(self) -> None:
        self._last_heartbeat_ack = time.time()
        while not self.close_event.is_set():
            if self.sim_time() >= self.end_time:
                break

            timeout = min(0.1, max(0.0, HEARTBEAT_INTERVAL - (time.time() - self._last_heartbeat_sent)))
            readable, _, _ = select.select([self.sock], [], [], timeout)

            if readable:
                msg = proto.recv_message(self._sock_file)
                if not msg:
                    break
                self._handle_incoming(msg)

            self._try_send_bundle()
            self._maybe_heartbeat()

            if time.time() - self._last_heartbeat_ack > HEARTBEAT_TIMEOUT:
                self.on_failure(self.contact_id)
                return

    def _handle_incoming(self, msg: proto.Message) -> None:
        if msg.type == "BUNDLE":
            self._receive_bundle(msg)
        elif msg.type == "BUNDLE_ACK":
            bid = msg.payload["bundle_id"]
            if self.on_bundle_acked is not None:
                self.on_bundle_acked(bid)
            else:
                self.bundle_store.delete(bid)
        elif msg.type == "HEARTBEAT":
            proto.send_message(self.sock, proto.Message(type="HEARTBEAT_ACK", sender=self.node_name, ts=self.sim_time(), payload=proto.HeartbeatAckPayload()))
        elif msg.type == "HEARTBEAT_ACK":
            self._last_heartbeat_ack = time.time()
        elif msg.type == "REQUEST_PLAN":
            self._send_plan()
        elif msg.type == "PLAN":
            received = ContactPlan.from_dict(msg.payload["plan"])
            if self.plan.merge(received):
                self.on_plan_update(self.plan)

    def _receive_bundle(self, msg: proto.Message) -> None:
        p = msg.payload
        bundle = Bundle(
            bundle_id=p["bundle_id"], src=p["src"], dst=p["dst"],
            ttl=p["ttl"], created_at=p["created_at"],
            image_id=p["image_id"], fragment_offset=p["fragment_offset"],
            total_size=p["total_size"],
            data=base64.b64decode(p["data"]),
        )
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

    def _try_send_bundle(self) -> None:
        try:
            bundle = self.outbound_queue.get_nowait()
        except queue.Empty:
            return
        proto.send_message(self.sock, proto.Message(
            type="BUNDLE", sender=self.node_name, ts=self.sim_time(),
            payload=proto.BundlePayload(
                bundle_id=bundle.bundle_id, src=bundle.src, dst=bundle.dst,
                ttl=bundle.ttl, created_at=bundle.created_at,
                image_id=bundle.image_id,
                fragment_offset=bundle.fragment_offset,
                total_size=bundle.total_size, data=bundle.data,
            )
        ))
        if self.reporter:
            self.reporter.post("bundle_sent", {
                "bundle_id": bundle.bundle_id,
                "from": self.node_name,
                "contact_id": self.contact_id, "ts": self.sim_time(),
            })

    def _maybe_heartbeat(self) -> None:
        if time.time() - self._last_heartbeat_sent >= HEARTBEAT_INTERVAL:
            proto.send_message(self.sock, proto.Message(
                type="HEARTBEAT", sender=self.node_name, ts=self.sim_time(),
                payload=proto.HeartbeatPayload(),
            ))
            self._last_heartbeat_sent = time.time()

    def _cleanup(self) -> None:
        self._state = State.CLOSED
        try:
            self._sock_file.close()
            self.sock.close()
        except OSError:
            pass
        if self.reporter:
            self.reporter.post("contact_closed", {
                "contact_id": self.contact_id, "ts": self.sim_time()
            })
