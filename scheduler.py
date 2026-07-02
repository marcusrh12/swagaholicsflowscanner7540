"""
Scheduler for FlowScanner.

Two ways to run the three daily sessions:

  1. OS scheduler (recommended on Windows): configure Windows Task Scheduler to
     run `python main.py --run now` at each session time. See README.md.

  2. Built-in daemon: `python main.py` (no args) starts run_scheduler(), an
     in-process loop that sleeps until the next configured session time and then
     fires the scan callback. Handy for always-on machines without Task Scheduler.

Session times are defined in config.SCAN_SESSIONS (local machine time).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Awaitable, Callable

import config

logger = logging.getLogger("flowscanner.scheduler")


def _parse_hhmm(value: str) -> dt.time:
    hh, mm = value.split(":")
    return dt.time(int(hh), int(mm))


def next_session(now: dt.datetime) -> tuple[str, dt.datetime]:
    """Return (session_name, datetime) of the next scheduled session after `now`."""
    candidates: list[tuple[str, dt.datetime]] = []
    for name, hhmm in config.SCAN_SESSIONS.items():
        t = _parse_hhmm(hhmm)
        today_dt = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if today_dt <= now:
            today_dt = today_dt + dt.timedelta(days=1)
        candidates.append((name, today_dt))
    candidates.sort(key=lambda c: c[1])
    return candidates[0]


async def run_scheduler(scan_callback: Callable[[str], Awaitable[None]]) -> None:
    """
    Block forever, firing `scan_callback(session_name)` at each session time.
    `scan_callback` is an async function that performs one full scan.
    """
    logger.info(
        "Scheduler started. Sessions: %s",
        ", ".join(f"{k}={v}" for k, v in config.SCAN_SESSIONS.items()),
    )
    while True:
        now = dt.datetime.now()
        name, when = next_session(now)
        sleep_seconds = max(1.0, (when - now).total_seconds())
        logger.info(
            "Next session '%s' at %s (sleeping %.0f min)",
            name,
            when.strftime("%Y-%m-%d %H:%M"),
            sleep_seconds / 60.0,
        )
        await asyncio.sleep(sleep_seconds)
        try:
            await scan_callback(name)
        except Exception as exc:  # never let one failed session kill the daemon
            logger.exception("Scheduled scan '%s' failed: %s", name, exc)
