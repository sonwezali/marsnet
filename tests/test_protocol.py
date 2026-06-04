# tests/test_protocol.py
from marsnet.node.protocol import (
    Message, encode, decode,
    HandshakePayload, RequestPlanPayload, PlanPayload,
    BundlePayload, BundleAckPayload,
    HeartbeatPayload, HeartbeatAckPayload, HelloPayload,
)

def test_encode_decode_roundtrip():
    msg = Message(type="HEARTBEAT", sender="rover_a", ts=12.5,
                  payload=HeartbeatPayload())
    line = encode(msg)
    assert line.endswith(b"\n")
    decoded = decode(line)
    assert decoded.type == "HEARTBEAT"
    assert decoded.sender == "rover_a"
    assert decoded.ts == 12.5


def test_handshake_payload():
    msg = Message(type="HANDSHAKE", sender="base", ts=0.0,
                  payload=HandshakePayload(contact_id="base:1", plan_version=3))
    decoded = decode(encode(msg))
    assert decoded.payload["contact_id"] == "base:1"
    assert decoded.payload["plan_version"] == 3


def test_bundle_payload_preserves_bytes():
    import base64
    data = b"\x00\xff\xfe binary data"
    msg = Message(type="BUNDLE", sender="rover_a", ts=5.0,
                  payload=BundlePayload(
                      bundle_id="rover_a:img001:0", src="rover_a", dst="base",
                      ttl=120.0, created_at=0.0, image_id="img001",
                      fragment_offset=0, total_size=512, data=data))
    decoded = decode(encode(msg))
    assert base64.b64decode(decoded.payload["data"]) == data


def test_unknown_type_is_decoded():
    # protocol should not raise on unknown message types — just pass through
    line = b'{"type":"FUTURE_MSG","sender":"x","ts":0.0,"payload":{}}\n'
    msg = decode(line)
    assert msg.type == "FUTURE_MSG"
