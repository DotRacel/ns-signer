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


if __name__ == "__main__":
    unittest.main()
