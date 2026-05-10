"""
Scheduler: long-running loop that fires daily attendance at the configured time.

Supports random delay jitter before each check-in.
"""

import sys
import time
import random
import logging
from datetime import datetime, timedelta

from .login import get_valid_cookies, do_login
from .attendance import do_attendance

logger = logging.getLogger("nodeseek")


def target_for_date(now: datetime, hour: int, minute: int) -> datetime:
    """Return today's configured target time."""
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def should_run_now(
    now: datetime,
    hour: int,
    minute: int,
    last_run_date,
    window_seconds: int = 60,
) -> bool:
    """Return True only during the window after today's configured target time."""
    target = target_for_date(now, hour, minute)
    seconds_after_target = (now - target).total_seconds()
    return (
        0 <= seconds_after_target < window_seconds
        and last_run_date != target.date()
    )


def next_target_after(now: datetime, hour: int, minute: int) -> datetime:
    """Return the next configured target time, today or tomorrow."""
    target = target_for_date(now, hour, minute)
    if now >= target:
        target += timedelta(days=1)
    return target


def run_scheduler(config: dict):
    """
    Blocking loop -- checks every ~30 s whether it's time for the daily
    check-in and fires once within a 60-second window of the target time.
    """
    try:
        hour, minute = map(int, config["attendance_time"].split(":"))
    except ValueError:
        logger.error("Invalid ATTENDANCE_TIME: %s", config["attendance_time"])
        sys.exit(1)

    logger.info(
        "Scheduler started -- daily check-in at %02d:%02d | mode=%s | delay_max=%ds",
        hour,
        minute,
        config["attendance_mode"],
        config["random_delay_max"],
    )

    last_date = None

    while True:
        now = datetime.now()
        if should_run_now(now, hour, minute, last_date):
            if config["random_delay_max"] > 0:
                delay = random.randint(0, config["random_delay_max"])
                logger.info("Random delay: %ds", delay)
                time.sleep(delay)

            logger.info("Starting daily check-in...")
            cookies = get_valid_cookies(config)
            if cookies:
                ok = do_attendance(config, cookies)
                if not ok:
                    logger.info("Attendance failed -- retrying after fresh login...")
                    cookies = do_login(config)
                    if cookies:
                        do_attendance(config, cookies)
            else:
                logger.error("Login failed, skipping today's attendance")

            last_date = target_for_date(now, hour, minute).date()

        target = next_target_after(datetime.now(), hour, minute)
        seconds_until = (target - datetime.now()).total_seconds()
        sleep_for = min(30, max(1, int(seconds_until) - 30)) if seconds_until > 30 else 10
        time.sleep(sleep_for)
