from __future__ import annotations
import math
import os
import pathlib
import tempfile
import pytest

from marsnet.node.bundle_store import BundleStore
from marsnet.node.config import NodeConfig
from marsnet.node.crypto import CryptoManager
from marsnet.node.main import NodeCLI

TTL = 300.0
CHUNK_SIZE = 512
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


def make_image(size_bytes: int = 1024) -> str:
    f = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    f.write(os.urandom(size_bytes))
    f.close()
    return f.name


def test_send_inserts_correct_fragment_count():
    cli, store = make_cli()
    path = make_image(1024)
    try:
        cli.send(path)
        expected = math.ceil(1024 / CHUNK_SIZE)
        assert len(store.all()) == expected
    finally:
        os.unlink(path)


def test_send_sets_destination_to_relay():
    cli, store = make_cli()
    path = make_image(512)
    try:
        cli.send(path)
        assert all(b.dst == "relay" for b in store.all())
    finally:
        os.unlink(path)


def test_send_duplicate_filename_appends_suffix():
    cli, store = make_cli()
    path = make_image(512)
    try:
        cli.send(path)
        cli.send(path)
        image_ids = {b.image_id for b in store.all()}
        stem = os.path.splitext(os.path.basename(path))[0]
        assert stem in image_ids
        assert f"{stem}_2" in image_ids
    finally:
        os.unlink(path)


def test_send_missing_file_raises():
    cli, _ = make_cli()
    with pytest.raises(FileNotFoundError):
        cli.send("/nonexistent/path/photo.jpg")


def test_status_reflects_sent_count(capsys):
    cli, store = make_cli()
    path = make_image(1024)
    try:
        cli.send(path)
        total = math.ceil(1024 / CHUNK_SIZE)
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
    path = make_image(512)
    try:
        cli.send(path)
        for b in store.all():
            store.delete(b.bundle_id)
        cli.status()
        captured = capsys.readouterr().out
        assert "100%" in captured
    finally:
        os.unlink(path)
