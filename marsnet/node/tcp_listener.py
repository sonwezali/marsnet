from __future__ import annotations
import socket
import threading
from typing import Callable

from marsnet.node import protocol as proto
from marsnet.node.contact_plan import ContactPlan


class TCPListener:
    def __init__(
        self,
        host: str,
        port: int,
        node_name: str,
        plan: ContactPlan,
        on_contact_connection: Callable,   # (sock, contact_id, peer_name) → None
        on_plan_received: Callable,        # (plan: ContactPlan) → None
        sim_start: float,
    ):
        self.host = host
        self.port = port
        self.node_name = node_name
        self.plan = plan
        self.on_contact_connection = on_contact_connection
        self.on_plan_received = on_plan_received
        self.sim_start = sim_start
        self._stop = threading.Event()

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._run, daemon=True, name="tcp-listener")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(16)
        server.settimeout(1.0)
        while not self._stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            t = threading.Thread(target=self._handle, args=(conn,),
                                 daemon=True)
            t.start()
        server.close()

    def _handle(self, conn: socket.socket) -> None:
        sock_file = conn.makefile("rb", buffering=0)
        msg = proto.recv_message(sock_file)
        if not msg:
            conn.close()
            return

        if msg.type == "HANDSHAKE":
            # Scheduled contact window opened by remote Contact Manager
            contact_id = msg.payload.get("contact_id", "unknown")
            self.on_contact_connection(conn, contact_id, msg.sender, msg)

        elif msg.type == "PLAN":
            # Response to our HELLO broadcast
            received = ContactPlan.from_dict(msg.payload["plan"])
            self.on_plan_received(received)
            conn.close()

        else:
            conn.close()
