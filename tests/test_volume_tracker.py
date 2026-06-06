from marsnet.node.volume_tracker import VolumeTracker


def test_allocate_accumulates():
    vt = VolumeTracker()
    vt.allocate("b1", "relay:1", 10.0, 100)
    vt.allocate("b2", "relay:1", 10.0, 50)
    assert vt.used()[("relay:1", 10.0)] == 150


def test_release_subtracts():
    vt = VolumeTracker()
    vt.allocate("b1", "relay:1", 10.0, 100)
    vt.allocate("b2", "relay:1", 10.0, 50)
    vt.release("b1")
    assert vt.used()[("relay:1", 10.0)] == 50


def test_release_removes_key_when_zero():
    vt = VolumeTracker()
    vt.allocate("b1", "relay:1", 10.0, 100)
    vt.release("b1")
    assert ("relay:1", 10.0) not in vt.used()


def test_reallocate_same_bundle_replaces_prior():
    vt = VolumeTracker()
    vt.allocate("b1", "relay:1", 10.0, 100)
    vt.allocate("b1", "relay:2", 20.0, 30)  # bundle rerouted
    used = vt.used()
    assert ("relay:1", 10.0) not in used
    assert used[("relay:2", 20.0)] == 30


def test_release_unknown_is_noop():
    vt = VolumeTracker()
    vt.release("nope")
    assert vt.used() == {}


def test_used_returns_copy():
    vt = VolumeTracker()
    vt.allocate("b1", "relay:1", 10.0, 100)
    snap = vt.used()
    snap[("relay:1", 10.0)] = 999
    assert vt.used()[("relay:1", 10.0)] == 100
