"""
A NodeSeek stand-in, so the paths that only run against the live site can be tested.

The login and check-in flows are mostly conversation with a browser, and that is
exactly the part a unit test usually cannot reach. These doubles answer the same
JavaScript the real helpers send, so a test can drive a whole flow -- anti-bot
cookie, device headers, API call, response tokens, session verification -- and
find out whether the pieces actually fit together.
"""

import contextlib


class FakeMouse:
    def move(self, x, y):
        pass


class FakeContext:
    def __init__(self, cookies):
        self._cookies = list(cookies)

    def cookies(self):
        return list(self._cookies)

    def add_cookies(self, cookies):
        self._cookies.extend(cookies)


class FakePage:
    """Answers the JavaScript the browser helpers send, and records the API calls."""

    def __init__(self, *, cookies=None, api_responses=None, storage=None,
                 device_headers=None, selector_error=None):
        self.context = FakeContext(cookies or [{"name": "fog", "value": "f"}])
        self.mouse = FakeMouse()
        self.url = "https://www.nodeseek.com/"
        self.storage = dict(storage or {})
        self.device_headers = dict(device_headers or {"x-integrity-token": "fp"})
        self.api_responses = dict(api_responses or {})
        self.calls = []
        self.selector_error = selector_error

    def wait_for_selector(self, selector, timeout=None):
        if self.selector_error:
            raise self.selector_error

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script, arg=None):
        # The helpers are told apart by what their script asks for.
        if "cf-turnstile-response" in script:
            return "turnstile-token"
        if "preLogin-" in script:
            return {"source": "preLogin", "headers": dict(self.device_headers)}
        if "localStorage.setItem" in script:
            stored = {k: v for k, v in (arg or {}).items() if v}
            self.storage.update(stored)
            return stored
        if "localStorage.getItem" in script:
            return {k: self.storage[k] for k in (arg or []) if k in self.storage}
        if script.strip().startswith("async ([method"):
            method, path, payload, headers = arg
            self.calls.append({"method": method, "path": path, "payload": payload,
                               "headers": dict(headers or {})})
            response = self.api_responses.get(path)
            if callable(response):
                response = response(self)
            if response is None:
                response = {"status": 404, "headers": {}, "body": None, "raw": "", "error": ""}
            return response
        raise AssertionError(f"FakePage got JavaScript it does not know: {script[:80]}")


@contextlib.contextmanager
def fake_session(page):
    """Stand in for browser.open_session, handing every fetch the same page."""

    class Session:
        def fetch(self, url, page_setup=None, page_action=None):
            page.url = url
            if page_setup:
                page_setup(page)
            if page_action:
                page_action(page)

    yield Session()


def api_ok(body, headers=None):
    return {"status": 200, "headers": dict(headers or {}), "body": body, "raw": "", "error": ""}
