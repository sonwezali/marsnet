import time
import threading
from unittest.mock import MagicMock

from marsnet.node.contact_plan import ContactEntry, ContactPlan
from marsnet.node.bundle_store import Bundle, BundleStore
from marsnet.node.contact_manager import ContactManager, ContactState
from marsnet.node.sim_clock import SimClock


def make_entry(cid="base:1", from_node="rover_a", to_node="base",
               phase=10.0, period=3600.0, duration=20.0, rate_bps=9600,
               status="active"):
    return ContactEntry(id=cid, created_by=from_node, from_node=from_node,
                        to_node=to_node, phase=phase, period=period,
                        duration=duration, rate_bps=rate_bps, status=status)


def make_bundle(bundle_id="rover_a:img:0", src="rover_a", dst="base",
                ttl=120.0, next_hop_contact=None):
    return Bundle(
        bundle_id=bundle_id, src=src, dst=dst, ttl=ttl,
        created_at=time.time(), image_id="img",
        fragment_offset=0, total_size=1024, data=b"data",
        next_hop_contact=next_hop_contact,
    )


def make_manager(contacts=None, node_name="rover_a"):
    if contacts is None:
        contacts = [make_entry()]
    plan = ContactPlan(version=1, sim_start=time.time(), contacts=contacts)
    store = BundleStore()
    on_plan_update = MagicMock()
    on_bundle_received = MagicMock()
    resolve_fn = MagicMock(return_value=("127.0.0.1", 9999))
    mgr = ContactManager(
        node_name=node_name, plan=plan, bundle_store=store,
        clock=SimClock(plan.sim_start),
        resolve_fn=resolve_fn,
        on_plan_update=on_plan_update,
        on_bundle_received=on_bundle_received,
    )
    return mgr, plan, store


def test_report_failure_cancels_contact():
    mgr, plan, _ = make_manager()
    mgr.start()
    assert plan.contact_by_id("base:1").status == "active"
    mgr.report_failure("base:1")
    assert plan.contact_by_id("base:1").status == "cancelled"


def test_report_failure_idempotent():
    mgr, plan, _ = make_manager()
    mgr.start()
    mgr.report_failure("base:1")
    version_after_first = plan.version
    mgr.report_failure("base:1")
    assert plan.version == version_after_first


def test_report_failure_calls_on_plan_update():
    mgr, _, _ = make_manager()
    mgr.start()
    mgr.report_failure("base:1")
    mgr.on_plan_update.assert_called_once()


def test_report_failure_nonexistent_contact():
    mgr, plan, _ = make_manager()
    mgr.start()
    version_before = plan.version
    mgr.report_failure("nonexistent:1")
    assert plan.version == version_before


def test_inject_bundle_routes_via_cgr():
    mgr, plan, store = make_manager()
    mgr.start()
    b = make_bundle()
    mgr.inject_bundle(b)
    assert b.next_hop_contact == "base:1"
    assert store.get(b.bundle_id) is b


def test_inject_bundle_no_route():
    contacts = [make_entry(from_node="relay", to_node="base")]
    mgr, _, store = make_manager(contacts=contacts)
    mgr.start()
    b = make_bundle(src="rover_a", dst="base")
    mgr.inject_bundle(b)
    assert b.next_hop_contact is None
    assert store.get(b.bundle_id) is b


def test_inject_bundle_stores_even_without_route():
    contacts = []
    mgr, _, store = make_manager(contacts=contacts)
    mgr.start()
    b = make_bundle()
    mgr.inject_bundle(b)
    assert store.get(b.bundle_id) is b


def test_rebuild_on_plan_update():
    mgr, plan, _ = make_manager()
    mgr.start()
    initial_timer_count = len(mgr._timers)
    plan.add_contact(make_entry("rover_a:2", phase=50.0, period=3600.0))
    mgr.rebuild_on_plan_update()
    assert len(mgr._timers) > initial_timer_count


def test_reassign_does_not_duplicate_unchanged_route():
    mgr, plan, store = make_manager()
    mgr.start()
    b = make_bundle()
    mgr.inject_bundle(b)
    assert b.next_hop_contact == "base:1"
    # Simulate an open queue for the current hop
    import queue
    q = queue.Queue()
    q.put(b)  # bundle already in queue
    with mgr._manager_lock:
        mgr._outbound_queues["base:1"] = q
    # Trigger reassignment — route hasn't changed
    mgr._reassign_all_bundles()
    # Queue should still have exactly 1 item (no duplicate)
    assert q.qsize() == 1


def test_reassign_updates_hop_on_contact_failure():
    # Two contacts: one via rover_a→base, another via rover_a→relay
    contacts = [
        make_entry("base:1", from_node="rover_a", to_node="base",
                   phase=10.0, period=3600.0, duration=20.0),
    ]
    mgr, plan, store = make_manager(contacts=contacts)
    mgr.start()
    b = make_bundle()
    mgr.inject_bundle(b)
    assert b.next_hop_contact == "base:1"
    # Cancel the only contact
    mgr._states["base:1"] = ContactState.OPEN
    mgr.report_failure("base:1")
    # No route available now
    assert b.next_hop_contact is None


def make_sized_bundle(bundle_id, data: bytes):
    return Bundle(
        bundle_id=bundle_id, src="rover_a", dst="base", ttl=120.0,
        created_at=time.time(), image_id="img",
        fragment_offset=0, total_size=1024, data=data,
        next_hop_contact=None,
    )


def test_inject_bundle_allocates_volume():
    mgr, plan, store = make_manager()
    mgr.start()
    b = make_sized_bundle("rover_a:img:0", b"0123456789")  # 10 bytes
    mgr.inject_bundle(b)
    # base:1 opens at phase=10
    assert mgr._volume.used()[("base:1", 10.0)] == 10


def test_inject_bundle_skips_full_window():
    # capacity = rate_bps * duration / 8 = 8 * 5 / 8 = 5 bytes
    contacts = [make_entry("base:1", from_node="rover_a", to_node="base",
                           phase=10.0, period=120.0, duration=5.0, rate_bps=8)]
    mgr, plan, store = make_manager(contacts=contacts)
    mgr.start()
    b1 = make_sized_bundle("rover_a:img:0", b"12345")   # fills window at 10
    b2 = make_sized_bundle("rover_a:img:5", b"67890")   # must spill to next window
    mgr.inject_bundle(b1)
    mgr.inject_bundle(b2)
    used = mgr._volume.used()
    assert used[("base:1", 10.0)] == 5
    assert used[("base:1", 130.0)] == 5   # 10 + period(120)
    assert b2.next_hop_contact == "base:1"


def test_release_volume_frees_window():
    contacts = [make_entry("base:1", from_node="rover_a", to_node="base",
                           phase=10.0, period=120.0, duration=5.0, rate_bps=8)]
    mgr, plan, store = make_manager(contacts=contacts)
    mgr.start()
    b1 = make_sized_bundle("rover_a:img:0", b"12345")
    mgr.inject_bundle(b1)
    assert mgr._volume.used()[("base:1", 10.0)] == 5
    mgr.release_volume("rover_a:img:0")
    assert ("base:1", 10.0) not in mgr._volume.used()


def test_on_bundle_acked_releases_and_deletes():
    mgr, plan, store = make_manager()
    mgr.start()
    b = make_sized_bundle("rover_a:img:0", b"0123456789")
    mgr.inject_bundle(b)
    assert store.get("rover_a:img:0") is b
    mgr.on_bundle_acked("rover_a:img:0")
    assert store.get("rover_a:img:0") is None
    assert ("base:1", 10.0) not in mgr._volume.used()
