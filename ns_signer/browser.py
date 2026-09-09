"""
The browser-side half of NodeSeek's protocol.

Everything NodeSeek checks -- the Turnstile token, the anti-bot cookie, the device
fingerprint, the tokens it hands back in response headers -- can only be produced
inside a real page on nodeseek.com, so all of it lives here and both the login and
the check-in drive it through the same helpers.

One quirk shapes this module: Scrapling runs `page.evaluate` in an *isolated*
world, so page globals such as `window.turnstile` and `window.__config__` are not
visible from here. The DOM, cookies, localStorage and dynamic `import()` are
shared, which is enough -- and it is why the fingerprint is obtained by importing
the site's own module rather than by reading a page global.
"""

import time
import fcntl
import random
import logging
import shutil
from contextlib import contextmanager
from pathlib import Path

from scrapling.fetchers import StealthySession
from playwright.sync_api import Page

from .session_store import TOKEN_KEYS

logger = logging.getLogger("nodeseek")

SITE_URL = "https://www.nodeseek.com/"

# Nodeseek gates its APIs behind a "fog" cookie: a script on the page buffers
# pointer telemetry, then trades a proof-of-work for the cookie over a WebSocket.
# It starts that round about ten seconds in and feeds the buffered telemetry into
# it, so an idle page never earns a cookie -- and without one the API answers with
# a bare 403 instead of JSON.
FOG_COOKIE = "fog"
FOG_WAIT_SECONDS = 90

# Chromium refuses to open a profile another instance still holds. A crashed run
# leaves those locks behind, and they would otherwise break every later launch.
PROFILE_LOCKS = ("SingletonLock", "SingletonSocket", "SingletonCookie")


@contextmanager
def open_session(config: dict, *, keep_profile: bool, **overrides):
    """
    Yield a browser session, on the kept profile or on a throwaway one.

    `keep_profile` is not a performance knob, it is about independence. Logging in
    is a gamble on Turnstile that only pays off about one launch in ten, and the
    whole retry strategy assumes each launch is a fresh draw -- a reused profile
    correlates them, so a browser Cloudflare has taken a dislike to stays disliked
    and every retry fails the same way. Work that already has a session, on the
    other hand, is only helped by coming back as the same browser.

    Chromium also corrupts a profile that two instances open at once, and running
    `--once` by hand while the scheduler is up would do exactly that, so the kept
    profile is taken under a lock and a throwaway one is used when it is busy.
    """
    with claim_profile(config.get("browser_profile_dir") if keep_profile else "") as profile:
        options = {
            "headless": config.get("headless", True),
            "solve_cloudflare": config.get("solve_cloudflare", True),
            "timeout": config.get("login_timeout", 120000),
        }
        # Cloudflare's verdict tracks the address far more than the browser: a
        # datacenter one gets a Turnstile token on roughly a tenth of page loads,
        # where an ordinary connection gets one nearly every time.
        if config.get("proxy"):
            options["proxy"] = config["proxy"]
        if profile:
            options["user_data_dir"] = profile
        options.update(overrides)
        logger.info(
            "Launching browser (headless=%s, solve_cloudflare=%s, profile=%s)",
            options["headless"],
            options["solve_cloudflare"],
            profile or "temporary",
        )
        with StealthySession(**options) as session:
            yield session


@contextmanager
def claim_profile(profile_dir: str):
    """Yield the profile directory while holding it exclusively, or "" if it is busy."""
    if not profile_dir:
        yield ""
        return

    lock_file = Path(profile_dir).with_name(Path(profile_dir).name + ".lock")
    handle = open(lock_file, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        logger.info("Browser profile is already in use, falling back to a temporary one")
        yield ""
        return

    try:
        clear_profile_locks(profile_dir)
        yield profile_dir
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def clear_profile_locks(profile_dir: str) -> None:
    """Drop the lock files a previous crash may have left in the browser profile."""
    for name in PROFILE_LOCKS:
        lock = Path(profile_dir) / name
        try:
            if lock.is_symlink() or lock.is_file():
                lock.unlink()
            elif lock.is_dir():
                shutil.rmtree(lock, ignore_errors=True)
        except OSError as e:  # pragma: no cover - depends on the filesystem
            logger.warning("Could not clear browser profile lock %s: %s", lock, e)


def cookie_names(cookies: list) -> list:
    return sorted(str(c.get("name")) for c in cookies)


def has_cookie(cookies: list, name: str) -> bool:
    """Return True when a cookie of that name is present, whatever its value."""
    return any(c.get("name") == name for c in cookies)


def move_pointer(page: Page, seconds: float) -> None:
    """Trace curved pointer paths so Nodeseek's telemetry records real movement."""
    deadline = time.time() + seconds
    x, y = 400.0, 300.0
    while time.time() < deadline:
        target_x = random.uniform(60, 1180)
        target_y = random.uniform(60, 700)
        steps = random.randint(8, 20)
        for step in range(1, steps + 1):
            page.mouse.move(
                x + (target_x - x) * step / steps,
                y + (target_y - y) * step / steps,
            )
            page.wait_for_timeout(random.randint(12, 45))
        x, y = target_x, target_y


def wait_for_fog_cookie(page: Page, timeout: int = FOG_WAIT_SECONDS) -> bool:
    """Move the pointer until Nodeseek's anti-bot script issues the fog cookie."""
    if has_cookie(page.context.cookies(), FOG_COOKIE):
        return True

    logger.info("Waiting for Nodeseek anti-bot cookie (up to %d s)...", timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        move_pointer(page, 2.0)
        if has_cookie(page.context.cookies(), FOG_COOKIE):
            logger.info("Anti-bot cookie issued")
            return True

    logger.warning(
        "Anti-bot cookie was not issued within %d s, the API will likely refuse us",
        timeout,
    )
    return False


PROTOCOL_HEADERS_JS = r"""
async () => {
    // The site ships preLogin() and the token module it builds on; importing them
    // keeps us honest about whatever those headers happen to contain today.
    const candidates = new Set([
        ...performance.getEntriesByType('resource').map(r => r.name),
        ...[...document.querySelectorAll('link[rel="modulepreload"], script[type="module"][src]')]
            .map(e => e.href || e.src),
    ].filter(Boolean));

    const importMatching = async (pattern, pick) => {
        for (const url of [...candidates].filter(u => pattern.test(u))) {
            try {
                const value = await pick(await import(url));
                if (value) return value;
            } catch (e) { /* try the next candidate */ }
        }
        return null;
    };

    const built = await importMatching(/\/preLogin-[^/]*\.js$/, async module => {
        const build = module.g || Object.values(module).find(v => typeof v === 'function');
        return build ? await build() : null;
    });
    if (built && Object.keys(built).length) return {source: 'preLogin', headers: built};

    const integrity = await importMatching(/\/tokens-[^/]*\.js$/, async module =>
        typeof module.a === 'function' ? await module.a() : null);
    if (integrity) {
        const headers = {'x-integrity-token': integrity};
        try {
            const security = localStorage.getItem('security_token');
            if (security) headers['x-security-token'] = security;
        } catch (e) { /* storage may be blocked */ }
        return {source: 'tokens', headers};
    }

    return {source: 'none', headers: {}};
}
"""


def protocol_headers(page: Page) -> dict:
    """
    Return the device headers NodeSeek expects on an account API call.

    Its front end sends `x-integrity-token` (a browser fingerprint) and, once it
    has one, `x-security-token`. Sending neither does not fail loudly -- the API
    answers 200 and asks for e-mail verification instead -- so this is not
    optional decoration, it is how a device gets recognised.
    """
    try:
        result = page.evaluate(PROTOCOL_HEADERS_JS)
    except Exception as e:
        logger.warning("Could not build device headers: %s", e)
        return {}

    headers = result.get("headers") or {}
    if headers:
        logger.info(
            "Device headers built from %s: %s", result.get("source"), sorted(headers)
        )
    else:
        logger.error(
            "Could not build device headers -- NodeSeek will treat this as an "
            "unknown device and refuse to hand out a session"
        )
    return headers


API_CALL_JS = r"""
async ([method, path, payload, headers]) => {
    const init = {method, credentials: 'include', headers: Object.assign({}, headers)};
    if (payload !== null) {
        init.headers['content-type'] = 'application/json';
        init.body = JSON.stringify(payload);
    }
    let response;
    try {
        response = await fetch(path, init);
    } catch (e) {
        return {status: 0, headers: {}, body: null, raw: '', error: String(e)};
    }
    const text = await response.text();
    const responseHeaders = {};
    response.headers.forEach((value, name) => { responseHeaders[name] = value; });
    let body = null;
    let raw = '';
    try {
        body = JSON.parse(text);
    } catch (e) {
        raw = text.slice(0, 300).trim();
    }
    return {status: response.status, headers: responseHeaders, body, raw, error: ''};
}
"""


def api_call(page: Page, method: str, path: str, payload=None, headers: dict | None = None) -> dict:
    """
    Call a NodeSeek API from inside the page and return status, headers and body.

    The response headers matter as much as the body: NodeSeek refreshes the
    session's `x-security-token` through them on every call.
    """
    return page.evaluate(API_CALL_JS, [method, path, payload, headers or {}])


def describe_api_result(result: dict) -> str:
    """A short, log-safe description of an API result."""
    if result.get("error"):
        return f"request failed: {result['error']}"
    body = result.get("body")
    if body is None:
        return f"HTTP {result.get('status')} (non-JSON): {result.get('raw') or 'empty body'}"
    return f"HTTP {result.get('status')}: {body}"


STORE_TOKENS_JS = r"""
(tokens) => {
    const stored = {};
    try {
        for (const [key, value] of Object.entries(tokens)) {
            if (value) { localStorage.setItem(key, value); stored[key] = value; }
        }
    } catch (e) { /* storage may be blocked */ }
    return stored;
}
"""

READ_TOKENS_JS = r"""
(keys) => {
    const out = {};
    try {
        for (const key of keys) {
            const value = localStorage.getItem(key);
            if (value) out[key] = value;
        }
    } catch (e) { /* storage may be blocked */ }
    return out;
}
"""


def absorb_response_tokens(page: Page, response_headers: dict) -> dict:
    """
    Mirror the site's postLogin(): keep the tokens a response hands back.

    NodeSeek returns a refreshed `x-security-token`/`x-csrf-token` in the headers
    of its account calls and expects them replayed on the next one. A client that
    ignores them is signed in for exactly one request.
    """
    refreshed = {
        "security_token": response_headers.get("x-security-token"),
        "csrf_token": response_headers.get("x-csrf-token"),
    }
    refreshed = {k: v for k, v in refreshed.items() if v}
    if not refreshed:
        return {}
    stored = page.evaluate(STORE_TOKENS_JS, refreshed)
    logger.info("Refreshed tokens from response: %s", sorted(stored))
    return stored


def restore_tokens(page: Page, tokens: dict) -> None:
    """Put saved tokens back into the page's localStorage before calling an API."""
    if not tokens:
        return
    page.evaluate(STORE_TOKENS_JS, {k: v for k, v in tokens.items() if k in TOKEN_KEYS})


def read_tokens(page: Page) -> dict:
    """Read the session tokens the page currently holds."""
    return page.evaluate(READ_TOKENS_JS, list(TOKEN_KEYS))


def token_headers(tokens: dict) -> dict:
    """Turn saved tokens into the request headers NodeSeek replays them as."""
    headers = {}
    if tokens.get("security_token"):
        headers["x-security-token"] = tokens["security_token"]
    if tokens.get("csrf_token"):
        headers["x-csrf-token"] = tokens["csrf_token"]
    return headers
