from datetime import datetime, timedelta
import unittest

import _stubs  # noqa: F401  (installs the browser-stack stand-ins)

from ns_signer import scheduler
from ns_signer.scheduler import retry_window_open, should_run_now


class SchedulerTimingTests(unittest.TestCase):
    def test_does_not_run_before_midnight_target(self):
        self.assertFalse(
            should_run_now(
                now=datetime(2026, 5, 9, 23, 59, 30),
                hour=0,
                minute=0,
                last_run_date=None,
            )
        )

    def test_runs_at_midnight_target(self):
        self.assertTrue(
            should_run_now(
                now=datetime(2026, 5, 10, 0, 0, 0),
                hour=0,
                minute=0,
                last_run_date=None,
            )
        )

    def test_does_not_run_twice_on_the_same_day(self):
        self.assertFalse(
            should_run_now(
                now=datetime(2026, 5, 10, 0, 0, 30),
                hour=0,
                minute=0,
                last_run_date=datetime(2026, 5, 10).date(),
            )
        )


class RetryWindowTests(unittest.TestCase):
    """A check-in that missed its slot is still worth having, so the day keeps being retried."""

    def test_window_is_open_shortly_after_the_target(self):
        target = datetime(2026, 5, 10, 0, 0, 0)
        self.assertTrue(retry_window_open(target + timedelta(hours=3), target, 20))

    def test_window_closes_once_the_day_is_gone(self):
        target = datetime(2026, 5, 10, 0, 0, 0)
        self.assertFalse(retry_window_open(target + timedelta(hours=21), target, 20))


class AttendanceCycleTests(unittest.TestCase):
    """Retrying every twenty minutes only makes sense while retrying could help."""

    def cycle(self, *, session, login_outcome="success", attendance=None, relogin=None):
        calls = {"attendance": 0, "logins": 0}

        def fake_get_valid_session(config):
            return (session, {}, login_outcome)

        def fake_login(config):
            calls["logins"] += 1
            return relogin or (None, login_outcome)

        def fake_attendance(config, cookies, tokens):
            calls["attendance"] += 1
            return dict({"completed": False, "session_expired": False, "cookies": [], "tokens": {}},
                        **(attendance or {}))

        original = (scheduler.get_valid_session, scheduler.login_with_outcome,
                    scheduler.do_attendance, scheduler.load_session)
        try:
            scheduler.get_valid_session = fake_get_valid_session
            scheduler.login_with_outcome = fake_login
            scheduler.do_attendance = fake_attendance
            scheduler.load_session = lambda: (session, {})
            return scheduler.run_attendance_cycle({}), calls
        finally:
            (scheduler.get_valid_session, scheduler.login_with_outcome,
             scheduler.do_attendance, scheduler.load_session) = original

    def test_a_completed_check_in_is_done(self):
        status, _ = self.cycle(session=[{"name": "session"}], attendance={"completed": True})
        self.assertEqual(status, scheduler.DONE)

    def test_a_login_that_needs_a_person_stops_the_day(self):
        status, calls = self.cycle(session=None, login_outcome="email-verification")
        self.assertEqual(status, scheduler.BLOCKED)
        self.assertEqual(calls["attendance"], 0)

    def test_a_login_that_merely_had_bad_luck_is_worth_retrying(self):
        status, _ = self.cycle(session=None, login_outcome="no-turnstile")
        self.assertEqual(status, scheduler.RETRY)

    def test_an_ordinary_check_in_failure_is_worth_retrying(self):
        status, calls = self.cycle(session=[{"name": "session"}])
        self.assertEqual(status, scheduler.RETRY)
        self.assertEqual(calls["logins"], 0)

    def test_an_expired_session_is_re_logged_in_once(self):
        status, calls = self.cycle(
            session=[{"name": "session"}],
            attendance={"session_expired": True},
            relogin=(None, "email-verification"),
        )
        self.assertEqual(status, scheduler.BLOCKED)
        self.assertEqual(calls["logins"], 1)


class SleepTests(unittest.TestCase):
    """The loop must not sleep through the thing it is waiting for."""

    SCHEDULES = [(0, 0), (13, 20)]

    def test_wakes_just_before_the_next_daily_target(self):
        now = datetime(2026, 5, 10, 6, 0, 0)
        self.assertEqual(scheduler.sleep_seconds(now, None, None, self.SCHEDULES), 30)

    def test_closes_in_on_a_target_that_is_nearly_here(self):
        now = datetime(2026, 5, 10, 13, 19, 20)
        self.assertEqual(scheduler.sleep_seconds(now, None, None, self.SCHEDULES), 10)

    def test_does_not_overshoot_a_pending_retry(self):
        now = datetime(2026, 5, 10, 1, 0, 0)
        retry_at = now + timedelta(seconds=45)
        self.assertEqual(
            scheduler.sleep_seconds(now, datetime(2026, 5, 10), retry_at, self.SCHEDULES), 15
        )

    def test_never_busy_loops(self):
        now = datetime(2026, 5, 10, 13, 19, 59)
        self.assertGreaterEqual(
            scheduler.sleep_seconds(now, None, None, self.SCHEDULES), 1
        )


class SessionPersistenceTests(unittest.TestCase):
    """A logged-out run must never overwrite a session that still works."""

    def capture_save(self, cookies):
        saved = []
        original = scheduler.save_session
        try:
            scheduler.save_session = lambda c, t: saved.append((c, t))
            scheduler.persist_refreshed_session(cookies, {"security_token": "t"})
        finally:
            scheduler.save_session = original
        return saved

    def test_refreshed_session_is_saved(self):
        cookies = [{"name": "session", "value": "abc"}]
        self.assertEqual(len(self.capture_save(cookies)), 1)

    def test_cookies_without_a_session_are_not_saved(self):
        self.assertEqual(self.capture_save([{"name": "colorscheme"}]), [])

    def test_empty_cookies_are_not_saved(self):
        self.assertEqual(self.capture_save([]), [])


if __name__ == "__main__":
    unittest.main()
