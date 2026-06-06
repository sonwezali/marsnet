import math
import time
from marsnet.node.contact_plan import ContactEntry, ContactPlan


def make_entry(id="base:1", from_node="base", to_node="rover_a",
               phase=10.0, period=60.0, duration=15.0, rate_bps=9600,
               status="active"):
    return ContactEntry(id=id, created_by=from_node, from_node=from_node,
                        to_node=to_node, phase=phase, period=period,
                        duration=duration, rate_bps=rate_bps, status=status)


def test_next_window_first():
    e = make_entry(phase=10.0, period=60.0, duration=15.0)
    start, end = e.next_window(after=0.0)
    assert start == 10.0
    assert end == 25.0


def test_next_window_skips_past():
    e = make_entry(phase=10.0, period=60.0, duration=15.0)
    start, end = e.next_window(after=30.0)
    assert start == 70.0
    assert end == 85.0


def test_next_window_mid_window():
    # if we're inside a window, next_window should return current window
    e = make_entry(phase=10.0, period=60.0, duration=15.0)
    start, end = e.next_window(after=12.0)
    assert start == 10.0
    assert end == 25.0


def test_windows_in_horizon():
    e = make_entry(phase=10.0, period=60.0, duration=15.0)
    windows = e.windows_in_horizon(from_time=0.0, horizon=200.0)
    assert windows == [(10.0, 25.0), (70.0, 85.0), (130.0, 145.0), (190.0, 205.0)]


def test_capacity_bytes():
    e = make_entry(rate_bps=9600, duration=15.0)
    assert e.capacity_bytes == 9600 * 15 // 8


def test_plan_merge_adds_new_contact():
    plan_a = ContactPlan(version=1, sim_start=0.0, contacts=[make_entry("base:1")])
    plan_b = ContactPlan(version=2, sim_start=0.0,
                         contacts=[make_entry("base:1"), make_entry("relay:1", from_node="relay", to_node="base")])
    changed = plan_a.merge(plan_b)
    assert changed is True
    assert plan_a.version == 3
    assert len(plan_a.contacts) == 2


def test_plan_merge_propagates_cancellation():
    entry = make_entry("base:1")
    plan_a = ContactPlan(version=2, sim_start=0.0, contacts=[entry])
    cancelled = make_entry("base:1", status="cancelled")
    plan_b = ContactPlan(version=2, sim_start=0.0, contacts=[cancelled])
    changed = plan_a.merge(plan_b)
    assert changed is True
    assert plan_a.contact_by_id("base:1").status == "cancelled"


def test_plan_merge_no_change():
    e = make_entry("base:1")
    plan_a = ContactPlan(version=3, sim_start=0.0, contacts=[e])
    plan_b = ContactPlan(version=3, sim_start=0.0, contacts=[e])
    changed = plan_a.merge(plan_b)
    assert changed is False
    assert plan_a.version == 3


def test_plan_add_contact():
    plan = ContactPlan(version=1, sim_start=0.0, contacts=[])
    plan.add_contact(make_entry("rover_a:1", from_node="rover_a"))
    assert plan.version == 2
    assert len(plan.contacts) == 1


def test_plan_cancel_contact():
    plan = ContactPlan(version=1, sim_start=0.0, contacts=[make_entry("base:1")])
    plan.cancel_contact("base:1")
    assert plan.version == 2
    assert plan.contact_by_id("base:1").status == "cancelled"


def test_cancel_contact_idempotent():
    plan = ContactPlan(version=1, sim_start=0.0, contacts=[make_entry("base:1")])
    plan.cancel_contact("base:1")
    assert plan.version == 2
    plan.cancel_contact("base:1")
    assert plan.version == 2


def test_cancel_contact_nonexistent():
    plan = ContactPlan(version=1, sim_start=0.0, contacts=[])
    plan.cancel_contact("nonexistent:1")
    assert plan.version == 1


def test_is_lost_no_contacts():
    plan = ContactPlan(version=1, sim_start=0.0, contacts=[])
    assert plan.is_lost(0.0) is True


def test_is_lost_with_future_window():
    e = make_entry(phase=10.0, period=60.0, duration=15.0)
    plan = ContactPlan(version=1, sim_start=0.0, contacts=[e])
    assert plan.is_lost(0.0) is False


def test_is_lost_all_cancelled():
    e = make_entry(phase=10.0, period=60.0, duration=15.0)
    plan = ContactPlan(version=1, sim_start=0.0, contacts=[e])
    plan.cancel_contact("base:1")
    assert plan.is_lost(0.0) is True


def test_plan_roundtrip():
    e = make_entry("base:1")
    plan = ContactPlan(version=3, sim_start=100.0, contacts=[e])
    d = plan.to_dict()
    restored = ContactPlan.from_dict(d)
    assert restored.version == 3
    assert restored.sim_start == 100.0
    assert len(restored.contacts) == 1
    assert restored.contact_by_id("base:1").from_node == "base"
    assert restored.contact_by_id("base:1").status == "active"


def test_plan_roundtrip_with_cancelled():
    e = make_entry("base:1", status="cancelled")
    plan = ContactPlan(version=2, sim_start=0.0, contacts=[e])
    restored = ContactPlan.from_dict(plan.to_dict())
    assert restored.contact_by_id("base:1").status == "cancelled"


def test_merge_adopts_sim_start_when_self_is_zero():
    plan_a = ContactPlan(version=1, sim_start=0.0, contacts=[])
    plan_b = ContactPlan(version=1, sim_start=1000.0, contacts=[])
    changed = plan_a.merge(plan_b)
    assert changed is True
    assert plan_a.sim_start == 1000.0


def test_merge_does_not_override_existing_sim_start():
    plan_a = ContactPlan(version=1, sim_start=500.0, contacts=[])
    plan_b = ContactPlan(version=1, sim_start=1000.0, contacts=[])
    plan_a.merge(plan_b)
    assert plan_a.sim_start == 500.0


def test_merge_returns_true_for_sim_start_only_change():
    e = make_entry("base:1")
    plan_a = ContactPlan(version=1, sim_start=0.0, contacts=[e])
    plan_b = ContactPlan(version=1, sim_start=1000.0, contacts=[e])
    changed = plan_a.merge(plan_b)
    assert changed is True
    assert plan_a.sim_start == 1000.0
