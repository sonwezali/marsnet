from __future__ import annotations
import math
import os
import threading
import time
from typing import Optional

from marsnet.node.bundle_store import Bundle, BundleStore
from marsnet.node.crypto import CryptoManager


CHUNK_SIZE = 512  # bytes per fragment


def fragment_image(
    src: str, dst: str, ttl: float, image_id: str,
    crypto: CryptoManager, chunk_size: int = CHUNK_SIZE,
    image_path: str = None, raw_data: bytes = None,
) -> list[Bundle]:
    if raw_data is None:
        with open(image_path, "rb") as f:
            raw_data = f.read()

    total_size = len(raw_data)
    bundles = []
    offset = 0
    while offset < total_size:
        chunk = raw_data[offset: offset + chunk_size]
        encrypted = crypto.encrypt(chunk)
        bundle = Bundle(
            bundle_id=f"{src}:{image_id}:{offset}",
            src=src, dst=dst, ttl=ttl,
            created_at=time.time(),
            image_id=image_id,
            fragment_offset=offset,
            total_size=total_size,
            data=encrypted,
        )
        bundles.append(bundle)
        offset += chunk_size
    return bundles


class ImageAssembler:
    def __init__(self, store: BundleStore, crypto: CryptoManager,
                 output_dir: str):
        self.store = store
        self.crypto = crypto
        self.output_dir = output_dir
        self._lock = threading.Lock()

    def on_fragment(self, image_id: str) -> Optional[str]:
        """
        Called whenever a new fragment for image_id lands in the store.
        If all fragments are present, reassembles, decrypts, writes to disk.
        Returns output path on completion, None if still incomplete.
        """
        with self._lock:
            fragments = [b for b in self.store.all()
                         if b.image_id == image_id]
            if not fragments:
                return None

            total_size = fragments[0].total_size
            offsets = {b.fragment_offset for b in fragments}
            
            # Determine chunk_size from fragments
            if len(fragments) > 1:
                sorted_f = sorted(fragments, key=lambda b: b.fragment_offset)
                chunk_size = sorted_f[1].fragment_offset - sorted_f[0].fragment_offset
            else:
                # Single fragment: use standard chunk size to calculate expected count
                chunk_size = CHUNK_SIZE

            # Calculate expected number of fragments
            num_fragments = math.ceil(total_size / chunk_size)
            expected_offsets = {i * chunk_size for i in range(num_fragments)}

            if offsets != expected_offsets:
                return None  # still waiting for fragments

            # Reassemble
            sorted_f = sorted(fragments, key=lambda b: b.fragment_offset)
            raw = b"".join(self.crypto.decrypt(b.data) for b in sorted_f)
            raw = raw[:total_size]

            out_path = os.path.join(self.output_dir, f"{image_id}.jpg")
            with open(out_path, "wb") as f:
                f.write(raw)
            return out_path
