from datetime import datetime
import sys
import types
import unittest


fetchers = types.ModuleType("scrapling.fetchers")
fetchers.DynamicSession = object
fetchers.StealthySession = object
scrapling = types.ModuleType("scrapling")
scrapling.fetchers = fetchers
sys.modules.setdefault("scrapling", scrapling)
sys.modules.setdefault("scrapling.fetchers", fetchers)

playwright = types.ModuleType("playwright")
sync_api = types.ModuleType("playwright.sync_api")
sync_api.Page = object
playwright.sync_api = sync_api
sys.modules.setdefault("playwright", playwright)
sys.modules.setdefault("playwright.sync_api", sync_api)

from ns_signer.scheduler import should_run_now


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

    def test_runs_within_minute_after_target(self):
        self.assertTrue(
            should_run_now(
                now=datetime(2026, 5, 10, 0, 0, 59),
                hour=0,
                minute=0,
                last_run_date=None,
            )
        )

    def test_does_not_run_twice_for_same_target_date(self):
        self.assertFalse(
            should_run_now(
                now=datetime(2026, 5, 10, 0, 0, 30),
                hour=0,
                minute=0,
                last_run_date=datetime(2026, 5, 10).date(),
            )
        )


if __name__ == "__main__":
    unittest.main()
