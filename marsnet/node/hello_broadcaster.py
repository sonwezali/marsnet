from __future__ import annotations
import random
import socket
import threading
import time

from marsnet.node import protocol as proto
from marsnet.node.contact_plan import ContactPlan


class HELLOBroadcaster:
    def __init__(self, udp_port: int, tcp_port: int, node_name: str,
                 plan: ContactPlan, sim_start: float,
                 interval: float = 5.0):
        self.udp_port = udp_port
        self.tcp_port = tcp_port
        self.node_name = node_name
        self.plan = plan
        self.sim_start = sim_start
        self.interval = interval
        self._stop = threading.Event()
        self._running = False
        self._lock = threading.Lock()

    def start(self) -> threading.Thread | None:
        with self._lock:
            if self._running:
                return None
            self._running = True
            self._stop.clear()
        t = threading.Thread(target=self._run, daemon=True,
                             name="hello-broadcaster")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            while not self._stop.is_set():
                if not self.plan.is_lost(time.time() - self.sim_start):
                    break
                msg = proto.encode(proto.Message(
                    type="HELLO", sender=self.node_name,
                    ts=time.time() - self.sim_start,
                    payload=proto.HelloPayload(
                        tcp_port=self.tcp_port,
                        plan_version=self.plan.version,
                    ),
                ))
                sock.sendto(msg, ("255.255.255.255", self.udp_port))
                jitter = random.uniform(-0.5, 0.5)
                self._stop.wait(self.interval + jitter)
            sock.close()
        finally:
            with self._lock:
                self._running = False
