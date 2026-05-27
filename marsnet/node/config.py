from __future__ import annotations
import json
from dataclasses import dataclass


@dataclass
class NodeConfig:
    name: str           # e.g. "rover_a"
    host: str           # bind address, e.g. "0.0.0.0"
    port: int           # TCP listen port, e.g. 7001
    udp_port: int       # UDP listen/broadcast port, e.g. 7000
    plan_path: str      # path to initial contact plan JSON
    key_path: str       # path to shared Fernet key file
    dashboard_url: str  # e.g. "http://192.168.1.99:8000"
    image_dir: str      # directory of images to transmit (source nodes only)

    @classmethod
    def from_file(cls, path: str) -> NodeConfig:
        with open(path) as f:
            return cls(**json.load(f))
