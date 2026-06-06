from __future__ import annotations
import base64
import json
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class Message:
    type: str
    sender: str
    ts: float
    payload: Any  # dict after decode, dataclass before encode


@dataclass
class HandshakePayload:
    contact_id: str
    plan_version: int
    sim_start: float = 0.0

@dataclass
class RequestPlanPayload:
    since_version: int = 0

@dataclass
class PlanPayload:
    plan: dict

@dataclass
class BundlePayload:
    bundle_id: str
    src: str
    dst: str
    ttl: float
    created_at: float
    image_id: str
    fragment_offset: int
    total_size: int
    data: bytes  # encoded as base64 in JSON

@dataclass
class BundleAckPayload:
    bundle_id: str

@dataclass
class HeartbeatPayload:
    pass

@dataclass
class HeartbeatAckPayload:
    pass

@dataclass
class HelloPayload:
    tcp_port: int
    plan_version: int


def encode(msg: Message) -> bytes:
    payload = msg.payload
    if hasattr(payload, "__dataclass_fields__"):
        d = asdict(payload)
    else:
        d = dict(payload)  # copy so we never mutate the caller's

    if msg.type == "BUNDLE" and "data" in d and isinstance(d["data"], bytes):
        d["data"] = base64.b64encode(d["data"]).decode()

    obj = {"type": msg.type, "sender": msg.sender, "ts": msg.ts, "payload": d}
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()


def decode(line: bytes) -> Message:
    obj = json.loads(line.decode().strip())
    return Message(type=obj["type"], sender=obj["sender"],
                   ts=obj["ts"], payload=obj["payload"])


def recv_message(sock_file) -> Message | None:
    line = sock_file.readline()
    if not line:
        return None
    return decode(line)


def send_message(sock, msg: Message) -> None:
    sock.sendall(encode(msg))
