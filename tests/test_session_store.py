import json
import os
import tempfile
import time
import unittest

import _stubs  # noqa: F401  (installs the browser-stack stand-ins)

from ns_signer import session_store


class SessionFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir.name

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self.previous
        self.dir.cleanup()

    def test_round_trips_cookies_and_tokens(self):
        cookies = [{"name": "session", "value": "abc", "expires": time.time() + 86400}]
        session_store.save_session(cookies, {"security_token": "s", "csrf_token": "c"})
        loaded_cookies, loaded_tokens = session_store.load_session()
        self.assertEqual(loaded_cookies, cookies)
        self.assertEqual(loaded_tokens, {"security_token": "s", "csrf_token": "c"})

    def test_reads_a_session_written_before_tokens_were_kept(self):
        cookies = [{"name": "session", "value": "abc"}]
        session_store.get_legacy_cookies_file().write_text(json.dumps(cookies))
        self.assertEqual(session_store.load_session(), (cookies, {}))

    def test_missing_session_is_reported_as_empty(self):
        self.assertEqual(session_store.load_session(), ([], {}))

    def test_corrupt_session_is_reported_as_empty(self):
        session_store.get_session_file().write_text("{not json")
        self.assertEqual(session_store.load_session(), ([], {}))


class ImportTests(SessionFileTests):
    """The way in when NodeSeek will not let the tool log in by itself."""

    def test_adopts_a_cookie_editor_style_export(self):
        export = [
            {"name": "session", "value": "abc", "domain": "www.nodeseek.com", "path": "/",
             "expirationDate": 1790000000.5, "httpOnly": True, "sameSite": "lax"},
            {"name": "cf_clearance", "value": "xyz", "domain": ".nodeseek.com", "path": "/",
             "expirationDate": 1790000000, "secure": True, "sameSite": "no_restriction"},
        ]
        cookies, _ = session_store.import_session(export)
        self.assertEqual([c["name"] for c in cookies], ["session", "cf_clearance"])
        self.assertEqual(cookies[0]["expires"], 1790000000.5)
        self.assertEqual(cookies[0]["sameSite"], "Lax")
        self.assertEqual(cookies[1]["sameSite"], "None")
        self.assertEqual(session_store.load_session()[0], cookies)

    def test_adopts_cookies_and_tokens_together(self):
        payload = {
            "cookies": [{"name": "session", "value": "abc", "domain": "www.nodeseek.com"}],
            "tokens": {"security_token": "s", "nonsense": "drop me"},
        }
        _, tokens = session_store.import_session(payload)
        self.assertEqual(tokens, {"security_token": "s"})

    def test_an_export_without_a_session_cookie_is_refused(self):
        with self.assertRaises(ValueError):
            session_store.import_session([{"name": "colorscheme", "value": "light",
                                           "domain": "www.nodeseek.com"}])

    def test_an_empty_export_is_refused(self):
        with self.assertRaises(ValueError):
            session_store.import_session([])

    def test_a_cookie_missing_its_essentials_is_dropped(self):
        cookies, _ = session_store.import_session([
            {"name": "session", "value": "abc", "domain": "www.nodeseek.com"},
            {"name": "broken"},
        ])
        self.assertEqual(len(cookies), 1)


class SessionLifetimeTests(unittest.TestCase):
    def test_no_session_cookie_is_never_fresh(self):
        self.assertFalse(session_store.session_is_fresh([{"name": "fog"}], 7))

    def test_session_expiring_inside_the_margin_is_not_fresh(self):
        cookies = [{"name": "session", "expires": time.time() + 3 * 86400}]
        self.assertFalse(session_store.session_is_fresh(cookies, 7))

    def test_session_beyond_the_margin_is_fresh(self):
        cookies = [{"name": "session", "expires": time.time() + 20 * 86400}]
        self.assertTrue(session_store.session_is_fresh(cookies, 7))

    def test_cookie_without_an_expiry_is_treated_as_open_ended(self):
        cookies = [{"name": "session", "expires": -1}]
        self.assertTrue(session_store.session_is_fresh(cookies, 30))

    def test_expired_session_is_described_as_expired(self):
        cookies = [{"name": "session", "expires": time.time() - 86400}]
        self.assertIn("expired", session_store.describe_session(cookies))


if __name__ == "__main__":
    unittest.main()
