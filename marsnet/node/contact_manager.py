from __future__ import annotations
import queue
import socket
import threading
import time
from enum import Enum, auto
from typing import Callable

from marsnet.node.contact_plan import ContactEntry, ContactPlan
from marsnet.node.bundle_store import BundleStore
from marsnet.node.cgr import cgr_route
from marsnet.node.connection_handler import ConnectionHandler
from marsnet.node.sim_clock import SimClock
from marsnet.node.volume_tracker import VolumeTracker


class ContactState(Enum):
    PENDING = auto()
    OPEN    = auto()
    FAILED  = auto()
    CLOSED  = auto()


class ContactManager:
    def __init__(
        self,
        node_name: str,
        plan: ContactPlan,
        bundle_store: BundleStore,
        clock: SimClock,
        resolve_fn: Callable[[str], tuple[str, int]],
        on_plan_update: Callable,
        on_bundle_received: Callable,
        dashboard_reporter=None,
    ):
        self.node_name = node_name
        self.plan = plan
        self.bundle_store = bundle_store
        self.clock = clock
        self.resolve_fn = resolve_fn
        self.on_plan_update = on_plan_update
        self.on_bundle_received = on_bundle_received
        self.reporter = dashboard_reporter

        self._states: dict[str, ContactState] = {}
        self._states_lock = threading.Lock()
        self._manager_lock = threading.Lock()
        self._timers: list[threading.Timer] = []
        self._outbound_queues: dict[str, queue.Queue] = {}
        self._volume = VolumeTracker()

    def sim_time(self) -> float:
        return self.clock.sim_time()

    def start(self) -> None:
        with self._manager_lock:
            self._rebuild_timers()

    def rebuild_on_plan_update(self) -> None:
        with self._manager_lock:
            for t in self._timers:
                t.cancel()
            self._timers.clear()
            self._rebuild_timers()
        self._reassign_all_bundles()

    def _rebuild_timers(self) -> None:
        now = self.sim_time()
        for contact in self.plan.contacts:
            if contact.status != "active":
                continue
            if contact.from_node != self.node_name:
                continue
            for (ws, we) in contact.windows_in_horizon(from_time=now, horizon=3600.0):
                delay_open = max(0.0, ws - now)
                delay_close = max(0.0, we - now)
                close_event = threading.Event()
                t_open = threading.Timer(
                    delay_open, self._open_contact,
                    args=(contact, we, close_event)
                )
                t_close = threading.Timer(delay_close, close_event.set)
                t_open.daemon = True
                t_close.daemon = True
                self._timers += [t_open, t_close]
                t_open.start()
                t_close.start()

    def _open_contact(self, contact: ContactEntry, we: float,
                      close_event: threading.Event) -> None:
        with self._states_lock:
            self._states[contact.id] = ContactState.OPEN

        try:
            host, port = self.resolve_fn(contact.to_node)
            sock = socket.create_connection((host, port), timeout=5.0)
        except OSError:
            self.report_failure(contact.id)
            return

        q = queue.Queue()
        with self._manager_lock:
            self._outbound_queues[contact.id] = q
            for bundle in self.bundle_store.get_by_contact(contact.id):
                q.put(bundle)

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
        t = threading.Thread(target=handler.run, daemon=True,
                             name=f"conn-{contact.id}")
        t.start()
        t.join()

        with self._states_lock:
            if self._states.get(contact.id) != ContactState.FAILED:
                self._states[contact.id] = ContactState.CLOSED
        with self._manager_lock:
            self._outbound_queues.pop(contact.id, None)

    def report_failure(self, contact_id: str) -> None:
        with self._states_lock:
            if self._states.get(contact_id) in (ContactState.FAILED,
                                                 ContactState.CLOSED):
                return
            self._states[contact_id] = ContactState.FAILED

        self.plan.cancel_contact(contact_id)
        self.on_plan_update(self.plan)
        self._reassign_all_bundles()

        if self.reporter:
            self.reporter.post("contact_failed", {
                "contact_id": contact_id, "ts": self.sim_time()
            })

    def _reassign_all_bundles(self) -> None:
        snapshot = self.plan.snapshot()
        now = self.sim_time()
        reroutes = []
        for bundle in self.bundle_store.all():
            old_hop = bundle.next_hop_contact
            self._volume.release(bundle.bundle_id)
            result = cgr_route(snapshot, bundle.src, bundle.dst, now,
                               self.clock.value, volume_used=self._volume.used())
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

    def accept_inbound(self, sock, contact_id: str, close_event: threading.Event,
                       end_time: float, peer_handshake=None) -> None:
        q = queue.Queue()
        with self._manager_lock:
            for bundle in self.bundle_store.get_by_contact(contact_id):
                q.put(bundle)
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
        t = threading.Thread(target=handler.run, daemon=True,
                             name=f"conn-{contact_id}-in")
        t.start()

    def on_bundle_acked(self, bundle_id: str) -> None:
        self._volume.release(bundle_id)
        self.bundle_store.delete(bundle_id)

    def release_volume(self, bundle_id: str) -> None:
        self._volume.release(bundle_id)

    def inject_bundle(self, bundle) -> None:
        snapshot = self.plan.snapshot()
        result = cgr_route(snapshot, bundle.src, bundle.dst,
                           self.sim_time(), self.clock.value,
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
