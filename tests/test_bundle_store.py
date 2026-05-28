import time
from marsnet.node.bundle_store import Bundle, BundleStore


def make_bundle(bundle_id="rover_a:img001:0", src="rover_a", dst="base",
                ttl=120.0, image_id="img001", fragment_offset=0,
                total_size=1024, data=b"hello", next_hop_contact=None):
    return Bundle(
        bundle_id=bundle_id, src=src, dst=dst, ttl=ttl,
        created_at=time.time(), image_id=image_id,
        fragment_offset=fragment_offset, total_size=total_size,
        data=data, next_hop_contact=next_hop_contact,
    )


def test_insert_and_get():
    store = BundleStore()
    b = make_bundle()
    store.insert(b)
    assert store.get("rover_a:img001:0") is b


def test_delete():
    store = BundleStore()
    store.insert(make_bundle())
    store.delete("rover_a:img001:0")
    assert store.get("rover_a:img001:0") is None


def test_get_by_contact():
    store = BundleStore()
    store.insert(make_bundle("id:0", next_hop_contact="relay:1"))
    store.insert(make_bundle("id:1", next_hop_contact="base:1"))
    store.insert(make_bundle("id:2", next_hop_contact="relay:1"))
    result = store.get_by_contact("relay:1")
    assert {b.bundle_id for b in result} == {"id:0", "id:2"}


def test_sweep_expired():
    store = BundleStore()
    b = make_bundle(ttl=1.0)
    b.created_at -= 10.0   # already expired
    store.insert(b)
    dropped = store.sweep_expired()
    assert b.bundle_id in dropped
    assert store.get(b.bundle_id) is None


def test_sweep_keeps_live():
    store = BundleStore()
    b = make_bundle(ttl=60.0)
    store.insert(b)
    dropped = store.sweep_expired()
    assert b.bundle_id not in dropped
    assert store.get(b.bundle_id) is not None


def test_update_next_hop_missing_id():
    store = BundleStore()
    result = store.update_next_hop("nonexistent", "relay:1")
    assert result is False
