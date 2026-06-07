# tests/test_connection_handler.py
from __future__ import annotations
import base64
import queue
import socket
import threading
from unittest.mock import MagicMock, patch

import marsnet.node.protocol as proto
from marsnet.node.bundle_store import BundleStore
from marsnet.node.connection_handler import ConnectionHandler
from marsnet.node.contact_plan import ContactEntry, ContactPlan
from marsnet.node.sim_clock import SimClock


def make_handler(clock: SimClock, plan: ContactPlan,
                 peer_handshake: proto.Message | None = None):
    sock_a, sock_b = socket.socketpair()
    handler = ConnectionHandler(
        sock=sock_a,
        contact_id="base:1",
        is_initiator=True,
        close_event=threading.Event(),
        end_time=9999.0,
        node_name="rover_a",
        plan=plan,
        bundle_store=BundleStore(),
        outbound_queue=queue.Queue(),
        on_failure=MagicMock(),
        on_plan_update=MagicMock(),
        on_bundle_received=MagicMock(),
        clock=clock,
        peer_handshake=peer_handshake,
    )
    return handler, sock_a, sock_b


def make_peer_handshake(plan_version: int, sim_start: float) -> proto.Message:
    return proto.Message(
        type="HANDSHAKE", sender="base", ts=0.0,
        payload={
            "contact_id": "base:1",
            "plan_version": plan_version,
            "sim_start": sim_start,
        },
    )


def test_handshake_requests_plan_when_clock_unset_and_peer_has_sim_start():
    plan = ContactPlan(version=1, sim_start=0.0, contacts=[])
    clock = SimClock()
    peer_hs = make_peer_handshake(plan_version=1, sim_start=12345.0)
    handler, sock_a, sock_b = make_handler(clock, plan, peer_handshake=peer_hs)

    with patch.object(handler, "_request_plan") as mock_req, \
         patch.object(handler, "_send_plan") as mock_send:
        handler._handshake()

    mock_req.assert_called_once()
    mock_send.assert_not_called()
    sock_a.close(); sock_b.close()


def test_handshake_does_not_request_plan_when_clock_set_and_versions_equal():
    plan = ContactPlan(version=1, sim_start=1000.0, contacts=[])
    clock = SimClock(1000.0)
    peer_hs = make_peer_handshake(plan_version=1, sim_start=1000.0)
    handler, sock_a, sock_b = make_handler(clock, plan, peer_handshake=peer_hs)

    with patch.object(handler, "_request_plan") as mock_req, \
         patch.object(handler, "_send_plan") as mock_send:
        handler._handshake()

    mock_req.assert_not_called()
    mock_send.assert_not_called()
    sock_a.close(); sock_b.close()


def test_handshake_requests_plan_when_peer_version_higher():
    plan = ContactPlan(version=1, sim_start=1000.0, contacts=[])
    clock = SimClock(1000.0)
    peer_hs = make_peer_handshake(plan_version=2, sim_start=1000.0)
    handler, sock_a, sock_b = make_handler(clock, plan, peer_handshake=peer_hs)

    with patch.object(handler, "_request_plan") as mock_req, \
         patch.object(handler, "_send_plan"):
        handler._handshake()

    mock_req.assert_called_once()
    sock_a.close(); sock_b.close()


def test_handshake_sends_plan_when_our_version_higher():
    plan = ContactPlan(version=3, sim_start=1000.0, contacts=[])
    clock = SimClock(1000.0)
    peer_hs = make_peer_handshake(plan_version=1, sim_start=1000.0)
    handler, sock_a, sock_b = make_handler(clock, plan, peer_handshake=peer_hs)

    with patch.object(handler, "_request_plan") as mock_req, \
         patch.object(handler, "_send_plan") as mock_send:
        handler._handshake()

    mock_send.assert_called_once()
    mock_req.assert_not_called()
    sock_a.close(); sock_b.close()


def test_handshake_sends_sim_start_in_outgoing_message():
    plan = ContactPlan(version=1, sim_start=0.0, contacts=[])
    clock = SimClock(99999.0)
    peer_hs = make_peer_handshake(plan_version=1, sim_start=0.0)
    handler, sock_a, sock_b = make_handler(clock, plan, peer_handshake=peer_hs)

    with patch.object(handler, "_request_plan"), \
         patch.object(handler, "_send_plan"):
        handler._handshake()

    # Read what was sent on sock_b (the peer's side)
    sock_b.settimeout(1.0)
    data = sock_b.recv(4096)
    msg = proto.decode(data)
    assert msg.payload["sim_start"] == 99999.0
    sock_a.close(); sock_b.close()


def test_bootstrap_sets_clock_after_receiving_plan():
    """Full bootstrap: unset clock + peer has sim_start → clock becomes set after plan received."""
    sim_start_val = 12345.0
    plan_a = ContactPlan(version=1, sim_start=0.0, contacts=[])
    plan_b = ContactPlan(version=1, sim_start=sim_start_val, contacts=[])
    clock = SimClock()
    peer_hs = make_peer_handshake(plan_version=1, sim_start=sim_start_val)
    handler, sock_a, sock_b = make_handler(clock, plan_a, peer_handshake=peer_hs)

    # Simulate the peer sending a PLAN_REQUEST response with plan_b
    def fake_request_plan():
        handler.plan.merge(plan_b)
        handler.clock.adopt(handler.plan.sim_start)
        handler.on_plan_update(handler.plan)

    with patch.object(handler, "_request_plan", side_effect=fake_request_plan), \
         patch.object(handler, "_send_plan"):
        handler._handshake()

    assert clock.is_set() is True
    assert clock.value == sim_start_val
    sock_a.close(); sock_b.close()


def test_handshake_does_not_request_plan_when_both_clocks_unset():
    """Neither side has sim_start yet — no spurious plan request."""
    plan = ContactPlan(version=1, sim_start=0.0, contacts=[])
    clock = SimClock()
    peer_hs = make_peer_handshake(plan_version=1, sim_start=0.0)
    handler, sock_a, sock_b = make_handler(clock, plan, peer_handshake=peer_hs)

    with patch.object(handler, "_request_plan") as mock_req, \
         patch.object(handler, "_send_plan") as mock_send:
        handler._handshake()

    mock_req.assert_not_called()
    mock_send.assert_not_called()
    sock_a.close(); sock_b.close()


def test_receive_bundle_stamps_prev_hop_from_contact_other_endpoint():
    # The handler's node is "rover_a" (per make_handler) and its contact_id is
    # "base:1". Build a plan where that contact's OTHER endpoint is "base", so
    # a received bundle must be stamped prev_hop="base" (never "rover_a" — the
    # handler's own name — and never anything not on the contact).
    plan = ContactPlan(version=1, sim_start=1000.0, contacts=[
        ContactEntry(id="base:1", created_by="base", from_node="base",
                     to_node="rover_a", phase=0.0, period=120.0,
                     duration=20.0, rate_bps=9600, status="active"),
    ])
    clock = SimClock(1000.0)
    handler, sock_a, sock_b = make_handler(clock, plan)

    # payload is a plain dict here (mirroring make_peer_handshake above) —
    # _receive_bundle reads it via dict-style p["bundle_id"], not attribute access.
    msg = proto.Message(
        type="BUNDLE", sender="base", ts=0.0,
        payload={
            "bundle_id": "base:img:0", "src": "base", "dst": "rover_a",
            "ttl": 300.0, "created_at": 0.0, "image_id": "img",
            "fragment_offset": 0, "total_size": 4,
            "data": base64.b64encode(b"data").decode("ascii"),
        },
    )
    handler._receive_bundle(msg)

    received = handler.on_bundle_received.call_args[0][0]
    assert received.prev_hop == "base"

    sock_a.close()
    sock_b.close()
