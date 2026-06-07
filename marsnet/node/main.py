from __future__ import annotations
import argparse
import json
import os
import signal
import threading

from marsnet.node.config import NodeConfig
from marsnet.node.contact_plan import ContactPlan
from marsnet.node.bundle_store import BundleStore
from marsnet.node.crypto import CryptoManager
from marsnet.node.contact_manager import ContactManager
from marsnet.node.sim_clock import SimClock
from marsnet.node.tcp_listener import TCPListener
from marsnet.node.udp_listener import UDPListener
from marsnet.node.hello_broadcaster import HELLOBroadcaster
from marsnet.node.ttl_reaper import TTLReaper
from marsnet.node.image_assembler import fragment_image, ImageAssembler
from marsnet.node.dashboard_reporter import DashboardReporter


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
        image_id = f"{self._cfg.name}-{stem}"
        counter = 2
        while image_id in self._tracked:
            image_id = f"{self._cfg.name}-{stem}_{counter}"
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


def parse_args():
    p = argparse.ArgumentParser(description="MarsNet node")
    p.add_argument("--config", required=True, help="Path to node config JSON")
    p.add_argument("--peers", required=True,
                   help="Path to peers JSON: {name: host:port}")
    p.add_argument("--ttl", type=float, default=300.0)
    return p.parse_args()


def main():
    args = parse_args()
    cfg = NodeConfig.from_file(args.config)
    plan = ContactPlan.load(cfg.plan_path)
    crypto = CryptoManager.load(cfg.key_path)
    bundle_store = BundleStore()

    with open(args.peers) as f:
        peers: dict[str, str] = json.load(f)

    sim_clock = SimClock(plan.sim_start)

    def resolve(node_name: str) -> tuple[str, int]:
        addr = peers[node_name]
        host, port = addr.rsplit(":", 1)
        return host, int(port)

    reporter = DashboardReporter(cfg.dashboard_url, cfg.name)
    reporter.start()

    broadcaster = HELLOBroadcaster(cfg.udp_port, cfg.port, cfg.name, plan, sim_clock)
    assembler = ImageAssembler(bundle_store, crypto,
                               output_dir=cfg.image_dir or ".")

    def on_plan_update(updated_plan: ContactPlan) -> None:
        sim_clock.adopt(updated_plan.sim_start)
        contact_mgr.rebuild_on_plan_update()
        reporter.post("plan_updated", {"version": updated_plan.version,
                                       "ts": sim_clock.sim_time()})
        if updated_plan.is_lost(sim_clock.sim_time()):
            broadcaster.start()

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

    contact_mgr = ContactManager(
        node_name=cfg.name, plan=plan, bundle_store=bundle_store,
        clock=sim_clock,
        resolve_fn=resolve,
        on_plan_update=on_plan_update,
        on_bundle_received=on_bundle_received,
        dashboard_reporter=reporter,
    )

    def on_bundle_dropped(bid: str) -> None:
        contact_mgr.release_volume(bid)
        reporter.post("bundle_dropped", {"bundle_id": bid, "ts": sim_clock.sim_time()})

    ttl_reaper = TTLReaper(bundle_store, on_drop=on_bundle_dropped)

    def on_contact_connection(sock, contact_id, _peer_name, peer_handshake=None):
        close_event = threading.Event()
        contact = plan.contact_by_id(contact_id)
        end_time_sim = contact.next_window(after=sim_clock.sim_time())[1] \
                       if contact else (sim_clock.sim_time() + 30)
        contact_mgr.accept_inbound(sock, contact_id, close_event, end_time_sim,
                                   peer_handshake=peer_handshake)

    def on_plan_received(received: ContactPlan) -> None:
        if plan.merge(received):
            on_plan_update(plan)

    tcp_listener = TCPListener(
        host=cfg.host, port=cfg.port,
        node_name=cfg.name, plan=plan,
        on_contact_connection=on_contact_connection,
        on_plan_received=on_plan_received,
    )
    udp_listener = UDPListener(cfg.udp_port, cfg.name, plan, sim_clock)

    tcp_listener.start()
    udp_listener.start()
    ttl_reaper.start()
    contact_mgr.start()

    if plan.is_lost(sim_clock.sim_time()):
        broadcaster.start()

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
            except EOFError:
                # stdin closed (e.g. piped input exhausted or /dev/null);
                # keep the process running — only SIGINT/SIGTERM should stop it
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

    cli_thread = threading.Thread(target=_cli_loop, daemon=True)
    cli_thread.start()
    stop_event.wait()

    tcp_listener.stop()
    udp_listener.stop()
    ttl_reaper.stop()
    broadcaster.stop()


if __name__ == "__main__":
    main()
