"""Logs the process's memory when it moves, so a RAM spike on the Railway graph can be
matched to what the bot was doing at that minute.

Railway bills memory by the minute, and its graph shows spikes of a few hundred MB over a
baseline near 100 MB. Nothing in the bot's own logs says what causes them, and the alert
clips downloaded here are only a few MB each. This writes one line when resident memory
climbs or falls by STEP_MB from the last line written, plus a heartbeat so the quiet
stretches are on record too. Each line names the coroutines running at that moment.

Reads /proc, so it does nothing where there is none (Windows, macOS): the bot runs on
Railway's Linux containers, and a dependency for the sake of a diagnostic is not worth it.
"""

import asyncio
import collections
import logging
import os

logger = logging.getLogger(__name__)

SAMPLE_SECONDS = 2
STEP_MB = 40
HEARTBEAT_SECONDS = 30 * 60


def read_memory_mb() -> tuple[float, float] | None:
    """(resident now, resident peak) in MB, or None where /proc is not there."""
    try:
        with open("/proc/self/status") as f:
            fields = dict(line.split(":", 1) for line in f if ":" in line)
        return (int(fields["VmRSS"].split()[0]) / 1024,
                int(fields["VmHWM"].split()[0]) / 1024)
    except (OSError, KeyError, ValueError, IndexError):
        return None


class Watch:
    """Decides which samples are worth a log line. Separate from the loop so the
    thresholds can be tested without waiting on a clock."""

    def __init__(self, step_mb: float = STEP_MB, heartbeat: float = HEARTBEAT_SECONDS):
        self.step_mb = step_mb
        self.heartbeat = heartbeat
        self.last_logged_mb: float | None = None
        self.last_logged_at: float | None = None

    def observe(self, rss_mb: float, now: float) -> str | None:
        """'start', 'rise', 'fall' or 'heartbeat' when the sample should be logged."""
        if self.last_logged_mb is None:
            reason = "start"
        elif rss_mb >= self.last_logged_mb + self.step_mb:
            reason = "rise"
        elif rss_mb <= self.last_logged_mb - self.step_mb:
            reason = "fall"
        elif now - self.last_logged_at >= self.heartbeat:
            reason = "heartbeat"
        else:
            return None
        self.last_logged_mb, self.last_logged_at = rss_mb, now
        return reason


def _running_coroutines(limit: int = 6) -> str:
    """The most common coroutine names among the loop's tasks, as 'name x count'."""
    names = collections.Counter()
    for task in asyncio.all_tasks():
        coro = task.get_coro()
        names[getattr(coro, "__qualname__", type(coro).__name__)] += 1
    return ", ".join(f"{n} x{c}" for n, c in names.most_common(limit)) or "-"


async def watch_memory() -> None:
    if read_memory_mb() is None:
        logger.info("[mem] /proc is not available here, memory logging is off")
        return
    watch = Watch()
    loop = asyncio.get_running_loop()
    while True:
        sample = read_memory_mb()
        if sample is not None:
            rss, peak = sample
            reason = watch.observe(rss, loop.time())
            if reason:
                logger.info(f"[mem] {reason} rss={rss:.0f}MB peak={peak:.0f}MB "
                            f"tasks={len(asyncio.all_tasks())} running: {_running_coroutines()}")
        await asyncio.sleep(SAMPLE_SECONDS)
