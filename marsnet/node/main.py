from __future__ import annotations
import argparse
import json
import os
import signal
import threading
import time

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


def parse_args():
    p = argparse.ArgumentParser(description="MarsNet node")
    p.add_argument("--config", required=True, help="Path to node config JSON")
    p.add_argument("--peers", required=True,
                   help="Path to peers JSON: {name: host:port}")
    p.add_argument("--send-image", help="Path to image to transmit at startup")
    p.add_argument("--destination", default="base")
    p.add_argument("--ttl", type=float, default=300.0)
    p.add_argument("--image-id", default=None)
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

    broadcaster = HELLOBroadcaster(cfg.udp_port, cfg.port, cfg.name, plan, sim_clock.value)
    assembler = ImageAssembler(bundle_store, crypto,
                               output_dir=cfg.image_dir or ".")

    # contact_mgr is defined after the callbacks due to circular dependency;
    # Python's late-binding closures resolve the reference at call time.
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
        destination=args.destination, clock=sim_clock,
        resolve_fn=resolve,
        on_plan_update=on_plan_update,
        on_bundle_received=on_bundle_received,
        dashboard_reporter=reporter,
    )

    def on_bundle_dropped(bid: str) -> None:
        contact_mgr.release_volume(bid)
        reporter.post("bundle_dropped", {"bundle_id": bid})

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
        sim_start=sim_clock.value,
    )
    udp_listener = UDPListener(cfg.udp_port, cfg.name, plan, sim_clock.value)

    tcp_listener.start()
    udp_listener.start()
    ttl_reaper.start()
    contact_mgr.start()

    if plan.is_lost(sim_clock.sim_time()):
        broadcaster.start()

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

    tcp_listener.stop()
    udp_listener.stop()
    ttl_reaper.stop()
    broadcaster.stop()


if __name__ == "__main__":
    main()
