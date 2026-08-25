import sys
import time
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

from ns_signer import login


class CookieReuseTests(unittest.TestCase):
    def test_get_valid_cookies_reuses_unexpired_session_cookie(self):
        saved_cookies = [{"name": "session", "expires": time.time() + 3600}]
        original_load_cookies = login.load_cookies
        original_do_login = login.do_login

        def fail_if_login_called(config):
            self.fail("do_login should not be called for a valid saved session cookie")

        try:
            login.load_cookies = lambda: saved_cookies
            login.do_login = fail_if_login_called
            self.assertIs(login.get_valid_cookies({}), saved_cookies)
        finally:
            login.load_cookies = original_load_cookies
            login.do_login = original_do_login


class RetryBackoffTests(unittest.TestCase):
    """A burst of sign-in POSTs gets rate-limited, so only those attempts back off."""

    def run_attempts(self, outcomes):
        """Drive do_login over canned attempt results and return the sleeps it took."""
        config = {"login_retries": len(outcomes)}
        slept: list = []
        calls = iter(outcomes)

        original_attempt = login.do_login_attempt
        original_save = login.save_cookies
        original_sleep = login.time.sleep
        try:
            login.do_login_attempt = lambda cfg, attempt: next(calls)
            login.save_cookies = lambda cookies: None
            login.time.sleep = slept.append
            return login.do_login(config), slept
        finally:
            login.do_login_attempt = original_attempt
            login.save_cookies = original_save
            login.time.sleep = original_sleep

    def test_submitted_attempts_back_off_exponentially(self):
        cookies, slept = self.run_attempts([(None, True)] * 4)
        self.assertIsNone(cookies)
        self.assertEqual(slept, [5, 10, 20])

    def test_attempts_that_never_reached_the_api_retry_at_base_delay(self):
        cookies, slept = self.run_attempts([(None, False)] * 4)
        self.assertIsNone(cookies)
        self.assertEqual(slept, [5, 5, 5])

    def test_backoff_only_grows_for_submitted_attempts(self):
        cookies, slept = self.run_attempts(
            [(None, True), (None, False), (None, True), (None, True)]
        )
        self.assertIsNone(cookies)
        self.assertEqual(slept, [5, 5, 10])

    def test_successful_attempt_stops_retrying(self):
        saved = [{"name": "session"}]
        cookies, slept = self.run_attempts([(saved, True), (None, True)])
        self.assertIs(cookies, saved)
        self.assertEqual(slept, [])


if __name__ == "__main__":
    unittest.main()
