from __future__ import annotations
import socket
import threading

from marsnet.node import protocol as proto
from marsnet.node.contact_plan import ContactPlan
from marsnet.node.sim_clock import SimClock


class UDPListener:
    def __init__(self, udp_port: int, node_name: str, plan: ContactPlan,
                 clock: SimClock):
        self.udp_port = udp_port
        self.node_name = node_name
        self.plan = plan
        self.clock = clock
        self._stop = threading.Event()

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._run, daemon=True, name="udp-listener")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.udp_port))
        sock.settimeout(1.0)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            try:
                msg = proto.decode(data)
            except (ValueError, KeyError):
                continue
            if msg.type == "HELLO":
                self._respond(addr[0], msg)
        sock.close()

    def _respond(self, sender_ip: str, msg: proto.Message) -> None:
        sender_name = msg.sender
        # Only respond if sender is in our contact plan
        known = {c.from_node for c in self.plan.contacts} | \
                {c.to_node for c in self.plan.contacts}
        if sender_name not in known:
            return
        tcp_port = msg.payload["tcp_port"]
        t = threading.Thread(target=self._send_plan, daemon=True,
                             args=(sender_ip, tcp_port))
        t.start()

    def _send_plan(self, host: str, port: int) -> None:
        try:
            sock = socket.create_connection((host, port), timeout=5.0)
            proto.send_message(sock, proto.Message(
                type="PLAN", sender=self.node_name,
                ts=self.sim_time(),
                payload=proto.PlanPayload(plan=self.plan.to_dict()),
            ))
            sock.close()
        except OSError:
            pass

    def sim_time(self) -> float:
        return self.clock.sim_time()
