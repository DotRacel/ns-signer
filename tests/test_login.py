import time
import unittest

import _stubs  # noqa: F401  (installs the browser-stack stand-ins)

from ns_signer import login


class SignInClassificationTests(unittest.TestCase):
    """`success: true` is not the same as "signed in", and that gap cost a month of check-ins."""

    def test_plain_success_is_a_login(self):
        outcome, _ = login.classify_sign_in({"status": 200, "body": {"success": True}})
        self.assertEqual(outcome, login.OUTCOME_SUCCESS)

    def test_success_redirecting_home_is_a_login(self):
        outcome, _ = login.classify_sign_in(
            {"status": 200, "body": {"success": True, "redirect": "/"}}
        )
        self.assertEqual(outcome, login.OUTCOME_SUCCESS)

    def test_success_redirecting_to_email_verification_is_not_a_login(self):
        outcome, detail = login.classify_sign_in(
            {
                "status": 200,
                "body": {"success": True, "redirect": "/emailSignIn.html?email=a%40b.c"},
            }
        )
        self.assertEqual(outcome, login.OUTCOME_EMAIL_VERIFICATION)
        self.assertIn("emailSignIn", detail)

    def test_success_redirecting_to_sms_verification_is_not_a_login(self):
        outcome, _ = login.classify_sign_in(
            {"status": 200, "body": {"success": True, "redirect": "/smsSignIn.html"}}
        )
        self.assertEqual(outcome, login.OUTCOME_EMAIL_VERIFICATION)

    def test_two_factor_prompt_is_reported_separately(self):
        outcome, _ = login.classify_sign_in(
            {"status": 200, "body": {"success": True, "need2FA": True, "otpSession": "x"}}
        )
        self.assertEqual(outcome, login.OUTCOME_TWO_FACTOR)

    def test_refusal_carries_the_reason(self):
        outcome, detail = login.classify_sign_in(
            {"status": 200, "body": {"success": False, "message": "用户名或密码错误"}}
        )
        self.assertEqual(outcome, login.OUTCOME_REFUSED)
        self.assertEqual(detail, "用户名或密码错误")

    def test_non_json_answer_is_reported_as_blocked(self):
        outcome, _ = login.classify_sign_in(
            {"status": 403, "body": None, "raw": "<html>error</html>"}
        )
        self.assertEqual(outcome, login.OUTCOME_BLOCKED)


class SessionProbeTests(unittest.TestCase):
    """The probe is a second opinion; it must never be the reason a good login is lost."""

    def test_unauthorized_status_means_signed_out(self):
        self.assertTrue(login.probe_says_signed_out({"status": 403, "body": None}))

    def test_explicit_failure_body_means_signed_out(self):
        self.assertTrue(
            login.probe_says_signed_out({"status": 200, "body": {"success": False}})
        )

    def test_ordinary_answer_means_signed_in(self):
        self.assertFalse(
            login.probe_says_signed_out({"status": 200, "body": {"count": 0}})
        )

    def test_a_probe_that_moved_is_not_treated_as_signed_out(self):
        self.assertFalse(
            login.probe_says_signed_out({"status": 404, "body": None, "raw": "<html>"})
        )

    def test_a_probe_that_could_not_run_is_not_treated_as_signed_out(self):
        self.assertFalse(
            login.probe_says_signed_out({"status": 0, "body": None, "error": "TypeError"})
        )


class LoginDriver:
    """Drives do_login over canned attempt results."""

    def run_attempts(self, outcomes, retries=None, config=None, saved_tokens=None,
                     recorded_block=None, force=False):
        """Drive do_login over canned attempt results, returning its answer and sleeps."""
        config = dict(config or {})
        config.setdefault("login_retries", retries or len(outcomes))
        slept: list = []
        results = iter(
            {"outcome": o, "detail": "", "cookies": ["c"] if o == login.OUTCOME_SUCCESS else [], "tokens": {}}
            for o in outcomes
        )
        self.modes: list = []

        def next_attempt(cfg, attempt, mode=login.MODE_PASSWORD):
            self.modes.append(mode)
            return next(results)

        self.blocks: list = []
        original = (login.do_login_attempt, login.save_session, login.load_session,
                    login.login_block, login.mark_login_blocked, login.time.sleep)
        try:
            login.do_login_attempt = next_attempt
            login.save_session = lambda cookies, tokens: None
            login.load_session = lambda: ([], dict(saved_tokens or {}))
            login.login_block = lambda days: recorded_block
            login.mark_login_blocked = self.blocks.append
            login.time.sleep = slept.append
            cookies, self.outcome = login.login_with_outcome(config, force=force)
            return cookies, slept, len(self.modes)
        finally:
            (
                login.do_login_attempt,
                login.save_session,
                login.load_session,
                login.login_block,
                login.mark_login_blocked,
                login.time.sleep,
            ) = original


class RetryPolicyTests(LoginDriver, unittest.TestCase):
    """Turnstile misses are worth retrying; a refused account is not."""

    def test_success_after_turnstile_misses(self):
        result, slept, attempts = self.run_attempts(
            [login.OUTCOME_NO_TURNSTILE, login.OUTCOME_NO_TURNSTILE, login.OUTCOME_SUCCESS]
        )
        self.assertEqual(result, ["c"])
        self.assertEqual(attempts, 3)
        self.assertEqual(len(slept), 2)

    def test_attempts_that_reached_the_api_back_off(self):
        _, slept, _ = self.run_attempts(
            [login.OUTCOME_UNVERIFIED, login.OUTCOME_UNVERIFIED, login.OUTCOME_UNVERIFIED]
        )
        self.assertEqual(slept, [login.RETRY_BASE_DELAY, login.RETRY_BASE_DELAY * 2])

    def test_bad_credentials_stop_immediately(self):
        result, slept, attempts = self.run_attempts(
            [login.OUTCOME_REFUSED] * 10, retries=10
        )
        self.assertIsNone(result)
        self.assertEqual(attempts, 1)
        self.assertEqual(slept, [])

    def test_email_verification_stops_after_confirming_it(self):
        result, _, attempts = self.run_attempts(
            [login.OUTCOME_EMAIL_VERIFICATION] * 10, retries=10
        )
        self.assertIsNone(result)
        self.assertEqual(attempts, 2)

    def test_turnstile_misses_use_the_whole_budget(self):
        result, _, attempts = self.run_attempts([login.OUTCOME_NO_TURNSTILE] * 6, retries=6)
        self.assertIsNone(result)
        self.assertEqual(attempts, 6)


MAILBOX = {
    "email_address": "someone@example.com",
    "imap_host": "imap.example.com",
    "imap_username": "someone@example.com",
    "imap_password": "secret",
}


class LoginRouteTests(unittest.TestCase):
    """A device NodeSeek has never seen cannot get in on a password, whatever it is."""

    def test_without_a_mailbox_the_password_route_is_all_there_is(self):
        self.assertEqual(login.initial_login_mode({}, {}), login.MODE_PASSWORD)

    def test_an_unknown_device_goes_straight_to_the_email_route(self):
        self.assertEqual(login.initial_login_mode(MAILBOX, {}), login.MODE_EMAIL)

    def test_a_device_with_a_security_token_tries_the_password_route(self):
        self.assertEqual(
            login.initial_login_mode(MAILBOX, {"security_token": "abc"}), login.MODE_PASSWORD
        )


class RouteSwitchTests(LoginDriver, unittest.TestCase):
    """Being told "verify by e-mail" is a routing instruction, not a dead end."""

    def test_email_verification_reroutes_the_next_attempt(self):
        # A saved security token means the device was known once, so the password
        # route is tried first; being turned away reroutes the rest of the login.
        result, _, _ = self.run_attempts(
            [login.OUTCOME_EMAIL_VERIFICATION, login.OUTCOME_SUCCESS],
            config=MAILBOX,
            saved_tokens={"security_token": "abc"},
        )
        self.assertEqual(result, ["c"])
        self.assertEqual(self.modes, [login.MODE_PASSWORD, login.MODE_EMAIL])

    def test_rerouting_buys_the_email_route_a_turn(self):
        # Without clearing the count, the password route's refusal plus one more
        # would end the login before the e-mail route had been tried at all.
        outcomes = [
            login.OUTCOME_EMAIL_VERIFICATION,
            login.OUTCOME_NO_TURNSTILE,
            login.OUTCOME_SUCCESS,
        ]
        result, _, attempts = self.run_attempts(
            outcomes, retries=5, config=MAILBOX, saved_tokens={"security_token": "abc"}
        )
        self.assertEqual(result, ["c"])
        self.assertEqual(attempts, 3)
        self.assertEqual(self.modes[1:], [login.MODE_EMAIL, login.MODE_EMAIL])

    def test_a_dead_end_still_ends(self):
        # If even the e-mail route is told to go and verify by e-mail there is
        # nothing left to try, so the login stops rather than looping.
        result, _, attempts = self.run_attempts(
            [login.OUTCOME_EMAIL_VERIFICATION] * 6, retries=6, config=MAILBOX
        )
        self.assertIsNone(result)
        self.assertEqual(attempts, 2)

    def test_without_a_mailbox_it_still_gives_up_after_confirming_the_refusal(self):
        result, _, attempts = self.run_attempts(
            [login.OUTCOME_EMAIL_VERIFICATION] * 10, retries=10
        )
        self.assertIsNone(result)
        self.assertEqual(attempts, 2)


class LoginBlockTests(LoginDriver, unittest.TestCase):
    """Confirming a refusal costs ~20 browser launches, so it is not re-confirmed daily."""

    def test_a_confirmed_refusal_is_recorded(self):
        self.run_attempts([login.OUTCOME_EMAIL_VERIFICATION] * 4, retries=4)
        self.assertEqual(len(self.blocks), 1)
        self.assertIn("email-verification", self.blocks[0])

    def test_a_recorded_block_stops_the_login_before_any_browser_starts(self):
        result, _, attempts = self.run_attempts(
            [login.OUTCOME_SUCCESS], retries=4,
            recorded_block={"at": 0, "reason": "email-verification"},
        )
        self.assertIsNone(result)
        self.assertEqual(attempts, 0)

    def test_a_mailbox_lifts_the_block(self):
        result, _, attempts = self.run_attempts(
            [login.OUTCOME_SUCCESS], retries=4, config=MAILBOX,
            recorded_block={"at": 0, "reason": "email-verification"},
        )
        self.assertEqual(result, ["c"])
        self.assertEqual(attempts, 1)

    def test_a_security_token_lifts_the_block(self):
        result, _, attempts = self.run_attempts(
            [login.OUTCOME_SUCCESS], retries=4,
            saved_tokens={"security_token": "abc"},
            recorded_block={"at": 0, "reason": "email-verification"},
        )
        self.assertEqual(result, ["c"])
        self.assertEqual(attempts, 1)

    def test_an_explicit_command_pushes_past_a_recorded_block(self):
        result, _, attempts = self.run_attempts(
            [login.OUTCOME_SUCCESS], retries=4, force=True,
            recorded_block={"at": 0, "reason": "email-verification"},
        )
        self.assertEqual(result, ["c"])
        self.assertEqual(attempts, 1)

    def test_bad_luck_is_never_recorded_as_a_block(self):
        self.run_attempts([login.OUTCOME_NO_TURNSTILE] * 3, retries=3)
        self.assertEqual(self.blocks, [])


class SessionReuseTests(unittest.TestCase):
    def test_valid_saved_session_is_reused_without_logging_in(self):
        saved = ([{"name": "session", "expires": time.time() + 86400}], {"security_token": "t"})
        original = (login.load_session, login.login_with_outcome)
        try:
            login.load_session = lambda: saved
            login.login_with_outcome = lambda config: self.fail("should not log in for a live session")
            cookies, tokens, outcome = login.get_valid_session({})
            self.assertEqual((cookies, tokens), saved)
            self.assertEqual(outcome, login.OUTCOME_SUCCESS)
        finally:
            login.load_session, login.login_with_outcome = original

    def test_expired_saved_session_triggers_a_login(self):
        expired = [{"name": "session", "expires": time.time() - 10}]
        calls = []
        original = (login.load_session, login.login_with_outcome)
        try:
            login.load_session = lambda: (expired, {})
            login.login_with_outcome = (
                lambda config: calls.append(config) or (None, login.OUTCOME_NO_TURNSTILE)
            )
            cookies, _, outcome = login.get_valid_session({})
            self.assertIsNone(cookies)
            self.assertEqual(outcome, login.OUTCOME_NO_TURNSTILE)
            self.assertEqual(len(calls), 1)
        finally:
            login.load_session, login.login_with_outcome = original

    def test_a_failed_login_reports_why_so_the_caller_can_stop(self):
        expired = [{"name": "session", "expires": time.time() - 10}]
        original = (login.load_session, login.login_with_outcome)
        try:
            login.load_session = lambda: (expired, {})
            login.login_with_outcome = lambda config: (None, login.OUTCOME_EMAIL_VERIFICATION)
            _, _, outcome = login.get_valid_session({})
            self.assertIn(outcome, login.BLOCKING_OUTCOMES)
        finally:
            login.load_session, login.login_with_outcome = original


class ProactiveRenewalTests(unittest.TestCase):
    def renew(self, seconds_left):
        cookies = [{"name": "session", "expires": time.time() + seconds_left}]
        calls = []
        original = (login.load_session, login.login_with_outcome)
        try:
            login.load_session = lambda: (cookies, {})
            login.login_with_outcome = lambda config: (calls.append(1) or ["c"], login.OUTCOME_SUCCESS)
            ok = login.refresh_session_if_stale({"session_renew_days": 7})
            return ok, len(calls)
        finally:
            login.load_session, login.login_with_outcome = original

    def test_session_with_room_to_spare_is_left_alone(self):
        self.assertEqual(self.renew(20 * 86400), (True, 0))

    def test_session_close_to_expiry_is_renewed_early(self):
        self.assertEqual(self.renew(3 * 86400), (True, 1))


if __name__ == "__main__":
    unittest.main()
