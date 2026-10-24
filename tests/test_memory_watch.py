"""The memory log writes a line when memory moves, not on every sample."""

from utils import memory_watch as mw


def test_the_first_sample_is_logged_and_small_wobbles_are_not():
    w = mw.Watch(step_mb=40, heartbeat=1800)

    assert w.observe(100, 0) == "start"
    assert w.observe(110, 2) is None
    assert w.observe(139, 4) is None


def test_a_climb_and_the_fall_back_are_each_logged_once():
    w = mw.Watch(step_mb=40, heartbeat=1800)
    w.observe(100, 0)

    assert w.observe(300, 2) == "rise"
    assert w.observe(310, 4) is None
    assert w.observe(105, 6) == "fall"


def test_a_quiet_process_still_reports_in_now_and_then():
    w = mw.Watch(step_mb=40, heartbeat=1800)
    w.observe(100, 0)

    assert w.observe(101, 1799) is None
    assert w.observe(101, 1800) == "heartbeat"
    assert w.observe(101, 1801) is None


def test_reading_memory_gives_numbers_or_nothing_never_an_error():
    sample = mw.read_memory_mb()

    assert sample is None or (sample[0] > 0 and sample[1] >= sample[0])
