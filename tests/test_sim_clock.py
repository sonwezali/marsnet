import time
from marsnet.node.sim_clock import SimClock


def test_sim_time_returns_zero_when_unset():
    clock = SimClock()
    assert clock.sim_time() == 0.0


def test_sim_time_returns_elapsed_when_set():
    start = time.time() - 5.0
    clock = SimClock(start)
    assert 4.9 < clock.sim_time() < 5.5


def test_initialized_with_nonzero_is_set():
    assert SimClock(123.0).is_set() is True


def test_is_set_false_initially():
    assert SimClock().is_set() is False


def test_adopt_sets_start_when_zero():
    clock = SimClock()
    adopted = clock.adopt(12345.0)
    assert adopted is True
    assert clock.value == 12345.0
    assert clock.is_set() is True


def test_adopt_ignores_zero():
    clock = SimClock()
    adopted = clock.adopt(0.0)
    assert adopted is False
    assert clock.is_set() is False


def test_adopt_ignores_when_already_set():
    clock = SimClock(1000.0)
    adopted = clock.adopt(2000.0)
    assert adopted is False
    assert clock.value == 1000.0


def test_adopt_is_idempotent():
    clock = SimClock()
    clock.adopt(500.0)
    clock.adopt(600.0)
    assert clock.value == 500.0


def test_adopt_is_thread_safe_first_adopter_wins():
    import threading
    clock = SimClock()
    barrier = threading.Barrier(50)
    values_to_try = [float(i) for i in range(1, 51)]
    results: list[bool] = []

    def worker(ts: float) -> None:
        barrier.wait()
        results.append(clock.adopt(ts))

    threads = [threading.Thread(target=worker, args=(v,)) for v in values_to_try]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r is True) == 1
    assert clock.value in values_to_try
