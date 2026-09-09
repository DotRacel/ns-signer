"""
Scheduler: long-running loop that keeps the session alive and the check-in done.

Two jobs run on different clocks, on purpose. The check-in itself is cheap and
happens at the configured time. Logging in is the fragile part -- it needs
Cloudflare to co-operate -- so it is done at a quiet hour, days before the session
would actually run out, rather than at the midnight rush with no time to spare.

A check-in that fails is not written off for the day either: it is retried at
intervals until it lands or the day is genuinely gone.
"""

import sys
import time
import random
import logging
from datetime import datetime, timedelta

from .login import (
    BLOCKING_OUTCOMES,
    get_valid_session,
    login_with_outcome,
    refresh_session_if_stale,
)
from .attendance import do_attendance
from .session_store import save_session, load_session, describe_session, SESSION_COOKIE

logger = logging.getLogger("nodeseek")

# How a check-in ended, and therefore whether coming back later could help.
DONE = "done"
RETRY = "retry"
BLOCKED = "blocked"


def parse_time_of_day(value: str, label: str) -> tuple[int, int]:
    """Parse "HH:MM", exiting with a clear message when it is malformed."""
    try:
        hour, minute = (int(part) for part in value.split(":"))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(value)
    except ValueError:
        logger.error("Invalid %s: %s (expected HH:MM)", label, value)
        sys.exit(1)
    return hour, minute


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


def retry_window_open(now: datetime, target: datetime, retry_hours: float) -> bool:
    """Return True while today's check-in is still worth retrying."""
    return now < target + timedelta(hours=retry_hours)


def should_pick_up_missed_check_in(
    now: datetime, target: datetime, completed_date, renewed: bool, retry_hours: float
) -> bool:
    """
    True when a just-renewed session should rescue a check-in given up on earlier.

    The two jobs run on different clocks, and the fragile one runs later: a
    check-in abandoned at midnight for want of a session should not stay abandoned
    once the afternoon session check has produced one.
    """
    if not renewed or completed_date == target.date():
        return False
    return target <= now < target + timedelta(hours=retry_hours)


def persist_refreshed_session(cookies: list, tokens: dict) -> None:
    """
    Save cookies and tokens back, but never trade a working session for a dead one.

    A run that got logged out comes back with a cookie jar that has no session in
    it; writing that over the saved session would turn one bad day into a
    permanent one.
    """
    if not cookies or not any(c.get("name") == SESSION_COOKIE for c in cookies):
        logger.info("Not saving refreshed cookies -- they carry no session cookie")
        return
    save_session(cookies, tokens)


def report_failed_login(outcome: str) -> str:
    """
    Explain a failed login and end the day's check-in.

    Retrying is what the check-in loop is for, but not this: a login has already
    spent its whole retry budget by the time it returns, so starting another one
    twenty minutes later would only repeat the same odds at ten times the cost --
    and a burst of browser launches makes Cloudflare less willing to hand over the
    Turnstile token the next login needs. The daily session check is the second
    chance; there is no point taking sixty.
    """
    if outcome in BLOCKING_OUTCOMES:
        logger.error(
            "Login cannot succeed without someone stepping in (%s); no more "
            "attempts today",
            outcome,
        )
    else:
        logger.error(
            "A full round of login attempts got no session (%s); waiting for the "
            "next daily session check rather than starting over",
            outcome,
        )
    return BLOCKED


def run_attendance_cycle(config: dict) -> str:
    """
    Check in once, logging in again if the saved session turns out to be dead.

    Returns DONE, RETRY, or BLOCKED. Only a check-in that failed with a session in
    hand is worth coming back to in twenty minutes; a failed login is not.
    """
    cookies, tokens, login_outcome = get_valid_session(config)
    if not cookies:
        return report_failed_login(login_outcome)

    result = do_attendance(config, cookies, tokens)
    persist_refreshed_session(result["cookies"], result["tokens"])
    if result["completed"]:
        return DONE

    if not result["session_expired"]:
        return RETRY

    logger.info("NodeSeek rejected the session, logging in again...")
    fresh, login_outcome = login_with_outcome(config)
    if not fresh:
        return report_failed_login(login_outcome)

    cookies, tokens = load_session()
    result = do_attendance(config, cookies, tokens)
    persist_refreshed_session(result["cookies"], result["tokens"])
    return DONE if result["completed"] else RETRY


def run_scheduler(config: dict):
    """Blocking loop that runs the daily check-in and keeps the session renewed."""
    attendance_hour, attendance_minute = parse_time_of_day(
        config["attendance_time"], "ATTENDANCE_TIME"
    )
    session_hour, session_minute = parse_time_of_day(
        config["session_check_time"], "SESSION_CHECK_TIME"
    )

    logger.info(
        "Scheduler started -- check-in at %02d:%02d (retry every %d min for %.0f h) | "
        "session check at %02d:%02d (renew %.0f days early) | mode=%s | delay_max=%ds",
        attendance_hour,
        attendance_minute,
        config["attendance_retry_minutes"],
        config["attendance_retry_hours"],
        session_hour,
        session_minute,
        config["session_renew_days"],
        config["attendance_mode"],
        config["random_delay_max"],
    )
    cookies, _ = load_session()
    logger.info("Saved session on startup: %s", describe_session(cookies))

    last_attendance_date = None
    last_session_check_date = None
    completed_date = None
    pending_target = None
    next_attempt_at = None

    while True:
        now = datetime.now()

        if should_run_now(now, session_hour, session_minute, last_session_check_date):
            last_session_check_date = target_for_date(now, session_hour, session_minute).date()
            logger.info("Daily session check...")
            renewed = refresh_session_if_stale(config)
            now = datetime.now()
            # A renewal that worked is also the day's second chance: the check-in
            # may have been abandoned this morning for want of the very session
            # that now exists.
            missed = target_for_date(now, attendance_hour, attendance_minute)
            if should_pick_up_missed_check_in(
                now, missed, completed_date, renewed, config["attendance_retry_hours"]
            ):
                logger.info("Session is usable again; picking up today's missed check-in")
                pending_target = missed
                next_attempt_at = now

        if should_run_now(now, attendance_hour, attendance_minute, last_attendance_date):
            target = target_for_date(now, attendance_hour, attendance_minute)
            last_attendance_date = target.date()
            pending_target = target
            delay = random.randint(0, config["random_delay_max"]) if config["random_delay_max"] > 0 else 0
            if delay:
                logger.info("Random delay: %ds", delay)
            next_attempt_at = now + timedelta(seconds=delay)

        if pending_target and now >= next_attempt_at:
            logger.info("Starting check-in for %s...", pending_target.date())
            status = run_attendance_cycle(config)
            if status == DONE:
                logger.info("Check-in for %s is done", pending_target.date())
                completed_date = pending_target.date()
                pending_target = None
            elif status == BLOCKED:
                logger.info(
                    "Check-in for %s is on hold until there is a usable session",
                    pending_target.date(),
                )
                pending_target = None
            elif retry_window_open(datetime.now(), pending_target, config["attendance_retry_hours"]):
                next_attempt_at = datetime.now() + timedelta(
                    minutes=config["attendance_retry_minutes"]
                )
                logger.info("Check-in did not land, retrying at %s", next_attempt_at.strftime("%H:%M:%S"))
            else:
                logger.error("Giving up on the check-in for %s", pending_target.date())
                pending_target = None

        time.sleep(sleep_seconds(datetime.now(), pending_target, next_attempt_at,
                                 [(attendance_hour, attendance_minute), (session_hour, session_minute)]))


def sleep_seconds(now: datetime, pending_target, next_attempt_at, schedules) -> int:
    """Sleep until just before whichever of the pending events comes first."""
    wake_times = [next_target_after(now, hour, minute) for hour, minute in schedules]
    if pending_target and next_attempt_at:
        wake_times.append(next_attempt_at)
    seconds_until = min((t - now).total_seconds() for t in wake_times)
    if seconds_until > 30:
        return min(30, max(1, int(seconds_until) - 30))
    return 5
