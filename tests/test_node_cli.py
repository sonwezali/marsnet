from __future__ import annotations
import math
import os
import pathlib
import tempfile
import pytest

from PIL import Image

from marsnet.node.bundle_store import BundleStore
from marsnet.node.config import NodeConfig
from marsnet.node.crypto import CryptoManager
from marsnet.node.image_assembler import TILE_PX
from marsnet.node.main import NodeCLI

TTL = 300.0
REPO_ROOT = pathlib.Path(__file__).parent.parent


def make_cfg(name: str = "rover_a") -> NodeConfig:
    return NodeConfig(
        name=name, host="0.0.0.0", port=12487, udp_port=12487,
        plan_path="", key_path="shared.key",
        dashboard_url="http://127.0.0.1:8000", image_dir="received_images",
    )


def make_cli(cfg: NodeConfig | None = None):
    cfg = cfg or make_cfg()
    store = BundleStore()
    crypto = CryptoManager.load(str(REPO_ROOT / "shared.key"))
    def inject(bundle):
        store.insert(bundle)
    return NodeCLI(cfg=cfg, store=store, inject_fn=inject, crypto=crypto, ttl=TTL), store


def make_image(w: int = 100, h: int = 80) -> str:
    """Write a real, decodable JPEG and return its path."""
    f = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    f.close()
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x % 256, y % 256, (x + y) % 256)
    img.save(f.name, format="JPEG", quality=90)
    return f.name


def tile_count(w: int, h: int) -> int:
    return math.ceil(w / TILE_PX) * math.ceil(h / TILE_PX)


def test_send_inserts_correct_fragment_count():
    cli, store = make_cli()
    path = make_image(100, 80)
    try:
        cli.send(path)
        assert len(store.all()) == tile_count(100, 80)
    finally:
        os.unlink(path)


def test_send_sets_destination_to_relay():
    cli, store = make_cli()
    path = make_image(64, 64)
    try:
        cli.send(path)
        assert all(b.dst == "relay" for b in store.all())
    finally:
        os.unlink(path)


def test_send_duplicate_filename_appends_suffix():
    cli, store = make_cli()
    path = make_image(64, 64)
    try:
        cli.send(path)
        cli.send(path)
        image_ids = {b.image_id for b in store.all()}
        stem = os.path.splitext(os.path.basename(path))[0]
        assert f"rover_a-{stem}" in image_ids
        assert f"rover_a-{stem}_2" in image_ids
    finally:
        os.unlink(path)


def test_send_missing_file_raises():
    cli, _ = make_cli()
    with pytest.raises(FileNotFoundError):
        cli.send("/nonexistent/path/photo.jpg")


def test_status_reflects_sent_count(capsys):
    cli, store = make_cli()
    path = make_image(100, 80)
    try:
        cli.send(path)
        total = tile_count(100, 80)
        bundles = store.all()
        for b in bundles[:total // 2]:
            store.delete(b.bundle_id)
        cli.status()
        captured = capsys.readouterr().out
        assert f"{total // 2}/{total}" in captured
    finally:
        os.unlink(path)


def test_status_shows_complete_when_all_sent(capsys):
    cli, store = make_cli()
    path = make_image(64, 64)
    try:
        cli.send(path)
        for b in store.all():
            store.delete(b.bundle_id)
        cli.status()
        captured = capsys.readouterr().out
        assert "100%" in captured
    finally:
        os.unlink(path)
