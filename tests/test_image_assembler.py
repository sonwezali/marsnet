# tests/test_image_assembler.py
import io
import os
import tempfile

from PIL import Image

from marsnet.node.bundle_store import BundleStore
from marsnet.node.crypto import CryptoManager
from marsnet.node.image_assembler import (
    ImageAssembler, tile_image, _unpack_tile,
)


def make_image(w=100, h=80):
    """A deterministic RGB gradient image, returned as JPEG bytes."""
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x % 256, y % 256, (x + y) % 256)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_tile_image_grid_and_geometry():
    crypto = CryptoManager.generate()
    raw = make_image(100, 80)  # tile_px=64 -> cols=2, rows=2 -> 4 tiles
    bundles = tile_image(
        src="rover_a", dst="relay", ttl=120.0, image_id="img001",
        crypto=crypto, tile_px=64, raw_data=raw,
    )
    assert len(bundles) == 4
    assert {b.fragment_offset for b in bundles} == {0, 1, 2, 3}
    assert all(b.total_size == 4 for b in bundles)

    # First tile: full 64x64 at origin; header reports the whole image size.
    h0, jpeg0 = _unpack_tile(crypto.decrypt(bundles[0].data))
    assert (h0["iw"], h0["ih"]) == (100, 80)
    assert (h0["x"], h0["y"], h0["w"], h0["h"]) == (0, 0, 64, 64)
    assert jpeg0[:2] == b"\xff\xd8"  # JPEG SOI marker

    # Right-edge tile (idx 1) is narrower; bottom-edge tile (idx 2) is shorter.
    h1, _ = _unpack_tile(crypto.decrypt(bundles[1].data))
    assert (h1["x"], h1["w"]) == (64, 36)
    h2, _ = _unpack_tile(crypto.decrypt(bundles[2].data))
    assert (h2["y"], h2["h"]) == (64, 16)


def test_reassemble_writes_full_image():
    crypto = CryptoManager.generate()
    raw = make_image(120, 90)
    bundles = tile_image(src="rover_a", dst="relay", ttl=120.0,
                         image_id="img002", crypto=crypto, raw_data=raw)
    with tempfile.TemporaryDirectory() as out_dir:
        store = BundleStore()
        for b in bundles:
            store.insert(b)
        assembler = ImageAssembler(store, crypto, out_dir)
        result = None
        for b in bundles:
            result = assembler.on_fragment(b.image_id)
        assert result is not None
        with Image.open(result) as got:
            assert got.size == (120, 90)


def test_partial_returns_none():
    crypto = CryptoManager.generate()
    raw = make_image(120, 90)
    bundles = tile_image(src="rover_a", dst="relay", ttl=120.0,
                         image_id="img003", crypto=crypto, raw_data=raw)
    with tempfile.TemporaryDirectory() as out_dir:
        store = BundleStore()
        for b in bundles[:-1]:  # drop the last tile
            store.insert(b)
        assembler = ImageAssembler(store, crypto, out_dir)
        assert assembler.on_fragment("img003") is None


def test_init_creates_missing_output_dir():
    crypto = CryptoManager.generate()
    store = BundleStore()
    with tempfile.TemporaryDirectory() as base_dir:
        out_dir = os.path.join(base_dir, "received_images")
        assert not os.path.isdir(out_dir)
        ImageAssembler(store, crypto, out_dir)
        assert os.path.isdir(out_dir)


def test_init_tolerates_already_existing_output_dir():
    crypto = CryptoManager.generate()
    store = BundleStore()
    with tempfile.TemporaryDirectory() as out_dir:
        assert os.path.isdir(out_dir)
        ImageAssembler(store, crypto, out_dir)  # must not raise
        assert os.path.isdir(out_dir)
