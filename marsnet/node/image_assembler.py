from __future__ import annotations
import io
import json
import os
import threading
import time
from typing import Optional

from PIL import Image

from marsnet.node.bundle_store import Bundle, BundleStore
from marsnet.node.crypto import CryptoManager

TILE_PX = 64  # tile edge length in pixels


def _pack_tile(header: dict, jpeg: bytes) -> bytes:
    """One-line JSON header + newline + tile JPEG bytes."""
    return json.dumps(header, separators=(",", ":")).encode() + b"\n" + jpeg


def _unpack_tile(plaintext: bytes) -> tuple[dict, bytes]:
    head, _, jpeg = plaintext.partition(b"\n")
    return json.loads(head.decode()), jpeg


def tile_image(
    src: str, dst: str, ttl: float, image_id: str,
    crypto: CryptoManager, tile_px: int = TILE_PX,
    image_path: Optional[str] = None, raw_data: Optional[bytes] = None,
) -> list[Bundle]:
    """Split an image into tile_px x tile_px tiles; one encrypted bundle per tile.

    Each bundle carries fragment_offset = tile index (row-major) and
    total_size = total tile count. The encrypted payload is a JSON geometry
    header followed by the tile's own complete JPEG.
    """
    if raw_data is not None:
        with Image.open(io.BytesIO(raw_data)) as src_img:
            img = src_img.convert("RGB")
    else:
        with Image.open(image_path) as src_img:
            img = src_img.convert("RGB")
    W, H = img.size
    cols = -(-W // tile_px)  # ceil
    rows = -(-H // tile_px)
    total = cols * rows

    bundles: list[Bundle] = []
    idx = 0
    for r in range(rows):
        for c in range(cols):
            x0, y0 = c * tile_px, r * tile_px
            x1, y1 = min(W, x0 + tile_px), min(H, y0 + tile_px)
            tile = img.crop((x0, y0, x1, y1))
            buf = io.BytesIO()
            tile.save(buf, format="JPEG", quality=85)
            header = {"iw": W, "ih": H,
                      "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}
            bundles.append(Bundle(
                bundle_id=f"{src}:{image_id}:{idx}",
                src=src, dst=dst, ttl=ttl,
                created_at=time.time(),
                image_id=image_id,
                fragment_offset=idx,
                total_size=total,
                data=crypto.encrypt(_pack_tile(header, buf.getvalue())),
            ))
            idx += 1
    return bundles


class ImageAssembler:
    def __init__(self, store: BundleStore, crypto: CryptoManager,
                 output_dir: str):
        self.store = store
        self.crypto = crypto
        self.output_dir = output_dir
        self._lock = threading.Lock()
        os.makedirs(self.output_dir, exist_ok=True)

    def on_fragment(self, image_id: str) -> Optional[str]:
        """If all tiles for image_id are present, paste them into the full
        image, write it, and return the path. Otherwise return None."""
        with self._lock:
            tiles = [b for b in self.store.all() if b.image_id == image_id]
            if not tiles:
                return None

            total = tiles[0].total_size
            if {b.fragment_offset for b in tiles} != set(range(total)):
                return None

            canvas = None
            for b in tiles:
                header, jpeg = _unpack_tile(self.crypto.decrypt(b.data))
                if canvas is None:
                    canvas = Image.new("RGB", (header["iw"], header["ih"]))
                with Image.open(io.BytesIO(jpeg)) as tile:
                    canvas.paste(tile, (header["x"], header["y"]))

            out_path = os.path.join(self.output_dir, f"{image_id}.jpg")
            canvas.save(out_path, format="JPEG")
            canvas.close()
            return out_path
