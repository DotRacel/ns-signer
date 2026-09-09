"""
End-to-end tests of the flows that otherwise only ever run against the live site.

These exist because the parts that had to be got right -- sending the device
headers, keeping the tokens out of the response, telling a real session from an
accepted-but-useless one -- are exactly the parts a unit test of any single
function walks straight past.
"""

import json
import os
import tempfile
import unittest

import _stubs  # noqa: F401  (installs the browser-stack stand-ins)
from fakes import FakePage, api_ok, fake_session

from ns_signer import attendance, browser, login, session_store


class FlowTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir.name
        self.original_open = browser.open_session

    def tearDown(self):
        browser.open_session = self.original_open
        if self.previous is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self.previous
        self.dir.cleanup()

    def use(self, page):
        browser.open_session = lambda config, **kwargs: fake_session(page)
        return page


CONFIG = {
    "email_address": "someone@example.com",
    "username": "someone",
    "password": "hunter2",
    "login_retries": 1,
    "attendance_mode": "fixed",
}

SIGNED_IN_COOKIES = [
    {"name": "fog", "value": "f"},
    {"name": "session", "value": "s3ss10n", "expires": 4102444800},
]


class CodeLoginTests(FlowTestCase):
    """The route a person bootstraps with: a code someone else asked for."""

    def page(self, sign_in, probe=None):
        return self.use(FakePage(
            cookies=list(SIGNED_IN_COOKIES),
            api_responses={
                login.EMAIL_SIGN_IN_API: sign_in,
                login.AUTH_PROBE_API: probe or api_ok({"count": 0}),
            },
        ))

    def test_a_good_code_produces_a_saved_verified_session(self):
        page = self.page(api_ok(
            {"success": True},
            headers={"x-security-token": "sec.tok", "x-csrf-token": "csrf.tok"},
        ))
        cookies = login.login_with_code(CONFIG, "123456")

        self.assertEqual([c["name"] for c in cookies], ["fog", "session"])
        saved_cookies, saved_tokens = session_store.load_session()
        self.assertEqual(saved_cookies, cookies)
        # The half of the session that response headers carry has to survive too.
        self.assertEqual(saved_tokens, {"security_token": "sec.tok", "csrf_token": "csrf.tok"})

    def test_the_code_is_sent_with_the_device_headers(self):
        page = self.page(api_ok({"success": True}))
        login.login_with_code(CONFIG, "123456")

        call = next(c for c in page.calls if c["path"] == login.EMAIL_SIGN_IN_API)
        self.assertEqual(call["payload"], {"email": "someone@example.com", "code": "123456"})
        self.assertIn("x-integrity-token", call["headers"])

    def test_the_address_can_be_overridden(self):
        page = self.page(api_ok({"success": True}))
        login.login_with_code(CONFIG, "123456", "other@example.com")
        call = next(c for c in page.calls if c["path"] == login.EMAIL_SIGN_IN_API)
        self.assertEqual(call["payload"]["email"], "other@example.com")

    def test_a_wrong_code_saves_nothing(self):
        self.page(api_ok({"success": False, "message": "邮箱登录失败"}))
        self.assertIsNone(login.login_with_code(CONFIG, "000000"))
        self.assertEqual(session_store.load_session(), ([], {}))

    def test_an_accepted_sign_in_that_does_not_work_is_not_saved(self):
        # The exact shape that went undetected for six weeks, in its other guise:
        # the API is happy, but the session it produced is not signed in.
        self.page(api_ok({"success": True}), probe={"status": 403, "headers": {},
                                                    "body": None, "raw": "", "error": ""})
        self.assertIsNone(login.login_with_code(CONFIG, "123456"))
        self.assertEqual(session_store.load_session(), ([], {}))

    def test_a_missing_address_is_refused_before_a_browser_starts(self):
        started = []
        browser.open_session = lambda config, **kwargs: started.append(1)
        self.assertIsNone(login.login_with_code({"email_address": ""}, "123456"))
        self.assertEqual(started, [])


class PasswordLoginTests(FlowTestCase):
    """The route production takes, including the answer that fooled it."""

    def run_attempt(self, sign_in, probe=None):
        page = self.use(FakePage(
            cookies=list(SIGNED_IN_COOKIES),
            api_responses={
                login.SIGN_IN_API: sign_in,
                login.AUTH_PROBE_API: probe or api_ok({"count": 0}),
            },
        ))
        return page, login.do_login_attempt(CONFIG, 1)

    def test_a_real_login_is_verified_and_returned(self):
        page, result = self.run_attempt(api_ok({"success": True},
                                               headers={"x-security-token": "sec"}))
        self.assertEqual(result["outcome"], login.OUTCOME_SUCCESS)
        self.assertEqual(result["tokens"], {"security_token": "sec"})

    def test_the_email_redirect_is_not_mistaken_for_a_login(self):
        _, result = self.run_attempt(api_ok(
            {"success": True, "redirect": "/emailSignIn.html?email=a%40b.c"}
        ))
        self.assertEqual(result["outcome"], login.OUTCOME_EMAIL_VERIFICATION)
        self.assertEqual(result["cookies"], [])

    def test_credentials_ride_with_the_captcha_and_device_headers(self):
        page, _ = self.run_attempt(api_ok({"success": True}))
        call = next(c for c in page.calls if c["path"] == login.SIGN_IN_API)
        self.assertEqual(call["payload"], {"username": "someone", "password": "hunter2"})
        self.assertEqual(call["headers"]["x-captcha-source"], "turnstile")
        self.assertEqual(call["headers"]["x-captcha-token"], "turnstile-token")
        self.assertIn("x-integrity-token", call["headers"])

    def test_a_page_that_never_loads_is_reported_as_such(self):
        self.use(FakePage(selector_error=RuntimeError("Timeout 20000ms exceeded")))
        result = login.do_login_attempt(CONFIG, 1)
        self.assertEqual(result["outcome"], login.OUTCOME_NO_PAGE)


class AttendanceFlowTests(FlowTestCase):
    """The daily call, and the session material it should carry and bring back."""

    def run_check_in(self, response, tokens=None):
        page = self.use(FakePage(
            cookies=list(SIGNED_IN_COOKIES),
            api_responses={f"{attendance.ATTENDANCE_API}?random=false": response},
        ))
        return page, attendance.do_attendance(CONFIG, SIGNED_IN_COOKIES, tokens or {})

    def test_a_successful_check_in_reports_and_returns_the_session(self):
        page, result = self.run_check_in(api_ok(
            {"success": True, "message": "签到成功", "current": 42},
            headers={"x-security-token": "fresh"},
        ))
        self.assertTrue(result["completed"])
        self.assertFalse(result["session_expired"])
        self.assertEqual(result["tokens"]["security_token"], "fresh")

    def test_the_saved_security_token_is_replayed(self):
        page, _ = self.run_check_in(api_ok({"success": True}), tokens={"security_token": "kept"})
        call = page.calls[0]
        self.assertEqual(call["headers"]["x-security-token"], "kept")

    def test_already_checked_in_counts_as_done(self):
        _, result = self.run_check_in(api_ok({"success": False, "message": "今天已完成签到"}))
        self.assertTrue(result["completed"])

    def test_a_rejected_session_is_reported_separately_from_a_failure(self):
        _, result = self.run_check_in({"status": 403, "headers": {}, "body": None,
                                       "raw": "", "error": ""})
        self.assertFalse(result["completed"])
        self.assertTrue(result["session_expired"])


if __name__ == "__main__":
    unittest.main()
