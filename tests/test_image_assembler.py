# tests/test_image_assembler.py
import os
import time
import tempfile
from marsnet.node.bundle_store import Bundle, BundleStore
from marsnet.node.crypto import CryptoManager
from marsnet.node.image_assembler import ImageAssembler, fragment_image

CHUNK = 64


def make_image_data(size=200):
    return bytes(range(256)) * (size // 256 + 1)


def test_fragment_reassemble_roundtrip():
    crypto = CryptoManager.generate()
    raw = make_image_data(200)
    bundles = fragment_image(
        image_path=None, raw_data=raw,
        src="rover_a", dst="base", ttl=120.0,
        image_id="img001", chunk_size=CHUNK, crypto=crypto,
    )
    assert len(bundles) == -(-200 // CHUNK)  # ceil div

    with tempfile.TemporaryDirectory() as out_dir:
        store = BundleStore()
        for b in bundles:
            store.insert(b)
        assembler = ImageAssembler(store, crypto, out_dir, chunk_size=CHUNK)
        for b in bundles:
            result = assembler.on_fragment(b.image_id)
        assert result is not None
        with open(result, "rb") as f:
            assert f.read() == raw


def test_partial_returns_none():
    crypto = CryptoManager.generate()
    raw = make_image_data(200)
    bundles = fragment_image(raw_data=raw, src="rover_a", dst="base",
                             ttl=120.0, image_id="img002",
                             chunk_size=CHUNK, crypto=crypto)
    # bundles has 4 fragments at offsets 0, 64, 128, 192
    with tempfile.TemporaryDirectory() as out_dir:
        store = BundleStore()
        # Insert fragments 0, 1, and 3 - missing fragment at offset 128
        store.insert(bundles[0])
        store.insert(bundles[1])
        store.insert(bundles[3])
        assembler = ImageAssembler(store, crypto, out_dir, chunk_size=CHUNK)
        result = assembler.on_fragment("img002")
        assert result is None


def test_non_adjacent_fragments_not_prematurely_complete():
    crypto = CryptoManager.generate()
    raw = make_image_data(200)
    bundles = fragment_image(raw_data=raw, src="rover_a", dst="base",
                             ttl=120.0, image_id="img003",
                             chunk_size=CHUNK, crypto=crypto)
    # bundles has 4 fragments at offsets 0, 64, 128, 192
    with tempfile.TemporaryDirectory() as out_dir:
        store = BundleStore()
        # Insert only non-adjacent fragments 0 and 128
        store.insert(bundles[0])
        store.insert(bundles[2])
        assembler = ImageAssembler(store, crypto, out_dir, chunk_size=CHUNK)
        result = assembler.on_fragment("img003")
        assert result is None
