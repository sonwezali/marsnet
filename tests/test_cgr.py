# tests/test_cgr.py
from marsnet.node.contact_plan import ContactEntry
from marsnet.node.cgr import cgr_route, is_critical_contact, CGRResult

SIM_START = 0.0

def entry(id, from_node, to_node, phase, period, duration, rate_bps=9600):
    return ContactEntry(id=id, created_by=from_node, from_node=from_node,
                        to_node=to_node, phase=phase, period=period,
                        duration=duration, rate_bps=rate_bps, status="active")


def test_direct_route():
    contacts = [entry("base:1", "rover_a", "base", phase=10, period=120, duration=20)]
    result = cgr_route(contacts, source="rover_a", destination="base",
                       current_time=0.0, sim_start=SIM_START)
    assert result is not None
    assert result.next_hop_contact == "base:1"
    assert result.earliest_arrival == 10.0


def test_multi_hop_route():
    # rover_b → relay → base
    contacts = [
        entry("relay:1", "relay", "rover_b", phase=5,  period=120, duration=20),
        entry("relay:2", "relay", "base",    phase=30, period=120, duration=20),
    ]
    # rover_b needs to wait for relay to contact it (phase=5), then relay contacts base (phase=30)
    result = cgr_route(contacts, source="rover_b", destination="base",
                       current_time=0.0, sim_start=SIM_START)
    assert result is not None
    assert result.next_hop_contact == "relay:1"
    assert result.earliest_arrival == 30.0


def test_no_route_returns_none():
    contacts = [entry("base:1", "rover_a", "base", phase=10, period=120, duration=20)]
    result = cgr_route(contacts, source="rover_b", destination="base",
                       current_time=0.0, sim_start=SIM_START)
    assert result is None


def test_volume_constraint_skips_full_contact():
    contacts = [entry("base:1", "rover_a", "base", phase=10, period=120,
                      duration=5, rate_bps=8)]  # capacity = 5 bytes
    # Mark the window as full
    volume_used = {("base:1", 10.0): 5}
    result = cgr_route(contacts, source="rover_a", destination="base",
                       current_time=0.0, sim_start=SIM_START,
                       volume_used=volume_used)
    # Should fall back to the next occurrence at phase+period=130
    assert result is not None
    assert result.earliest_arrival == 130.0
    assert result.next_hop_contact == "base:1"


def test_bundle_must_arrive_before_window_ends():
    # rover_b reaches the relay only AFTER the relay->base window has closed,
    # so the bundle must wait for the relay's NEXT pass over base.
    contacts = [
        # relay contacts rover_b at t=20 (rover_b can send to relay during [20,30])
        entry("relay:1", "relay", "rover_b", phase=20, period=120, duration=10),
        # relay contacts base early at [5,15] -- already closed by t=20
        entry("relay:2", "relay", "base",    phase=5,  period=120, duration=10),
    ]
    result = cgr_route(contacts, source="rover_b", destination="base",
                       current_time=0.0, sim_start=SIM_START)
    # rover_b arrives at relay at t=20; relay:2's [5,15] window has closed,
    # so the bundle waits for the next relay->base window at t=125.
    assert result is not None
    assert result.earliest_arrival == 125.0


def test_critical_contact_detected():
    contacts = [entry("base:1", "rover_a", "base", phase=10, period=120, duration=20)]
    assert is_critical_contact(contacts, "base:1", "rover_a", "base", 0.0, SIM_START)


def test_non_critical_contact():
    contacts = [
        entry("base:1", "rover_a", "base",  phase=10, period=120, duration=20),
        entry("base:2", "rover_a", "base",  phase=50, period=120, duration=20),
    ]
    assert not is_critical_contact(contacts, "base:1", "rover_a", "base", 0.0, SIM_START)
