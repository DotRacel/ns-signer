"""
Login module: StealthySession + Cloudflare Turnstile solving + cookie persistence.

Cookies are saved to cookies.json and reused across sessions.
A fresh login is triggered only when the saved session expires.
"""

import json
import time
import random
import logging
from pathlib import Path
from typing import Optional

from scrapling.fetchers import StealthySession
from playwright.sync_api import Page

from .config import get_data_dir

logger = logging.getLogger("nodeseek")

SIGN_IN_URL = "https://www.nodeseek.com/signIn.html"

# Nodeseek gates /api/account/signIn behind a "fog" cookie: a script on the page
# buffers pointer telemetry, then trades a proof-of-work for the cookie over a
# WebSocket. It starts that round about ten seconds in and feeds the buffered
# telemetry into it, so an idle page never earns a cookie -- and without one the
# sign-in API answers with a bare 403 instead of JSON.
FOG_COOKIE = "fog"
FOG_WAIT_SECONDS = 90

# The Turnstile widget hands over a token within a second or so when it works at
# all; when it gets stuck it never recovers, so waiting longer buys nothing.
TURNSTILE_WAIT_SECONDS = 20

# Grace period for the Set-Cookie of an accepted login to reach the browser context.
SESSION_COOKIE_WAIT_SECONDS = 10

# A burst of sign-in attempts gets the endpoint rate-limited at Nodeseek's edge,
# so back off after any attempt that actually reached the API.
RETRY_BASE_DELAY = 5
RETRY_MAX_DELAY = 300


def get_cookies_file() -> Path:
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "cookies.json"


def save_cookies(cookies: list):
    cookies_file = get_cookies_file()
    cookies_file.write_text(json.dumps(cookies, indent=2, ensure_ascii=False))
    logger.info("Cookies saved to %s", cookies_file)


def load_cookies() -> Optional[list]:
    """Load saved Playwright-format cookies, or None if missing/corrupt."""
    cookies_file = get_cookies_file()
    if not cookies_file.exists():
        logger.info("No saved cookies at %s", cookies_file)
        return None
    try:
        cookies = json.loads(cookies_file.read_text())
        if cookies:
            logger.info("Loaded %d saved cookies from %s", len(cookies), cookies_file)
            return cookies
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Saved cookies file is unreadable: %s (%s)", cookies_file, e)
    return None


def check_session_valid(cookies: list) -> bool:
    """Check if the session cookie is still within its expiry window."""
    now = time.time()
    for c in cookies:
        if c.get("name") == "session":
            expires = c.get("expires", 0)
            if expires == -1:
                logger.info("Session cookie has no explicit expiry")
                return True
            if expires > now:
                remaining_days = (expires - now) / 86400
                logger.info("Session cookie valid for %.1f more days", remaining_days)
                return True
    logger.info("Session cookie expired or not found")
    return False


def has_session_cookie(cookies: list) -> bool:
    """Return True when browser cookies include a non-expired session cookie."""
    now = time.time()
    return any(
        c.get("name") == "session" and (c.get("expires", 0) == -1 or c.get("expires", 0) > now)
        for c in cookies
    )


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
        "Anti-bot cookie was not issued within %d s, the login API will likely refuse us",
        timeout,
    )
    return False


def get_turnstile_token(page: Page) -> str:
    """Read the embedded Turnstile response token, if the page has produced one."""
    return page.evaluate(
        """
        () => {
            const input = document.querySelector(
                'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
            );
            return input && input.value ? input.value : '';
        }
        """
    )


def wait_for_turnstile_token(page: Page, timeout: int = TURNSTILE_WAIT_SECONDS) -> str:
    """Wait for the login form's Turnstile token, returning "" if it never arrives."""
    logger.info("Waiting for embedded Turnstile response token...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        token = get_turnstile_token(page)
        if token:
            logger.info("Embedded Turnstile response token is present")
            return token
        page.wait_for_timeout(1000)

    logger.warning("Turnstile widget did not produce a token, giving up on this attempt")
    return ""


def submit_credentials(page: Page, username: str, password: str, token: str) -> dict:
    """
    Post the credentials the way the login page's own script does.

    Clicking the form's submit button gets a bare 403 back from Nodeseek's edge,
    and the page then reports it only as a generic "操作失败" dialog whose overlay
    blocks every further click. Issuing the same request from the page instead gets
    a normal answer, and its JSON names the reason whenever a login is refused.
    """
    return page.evaluate(
        """
        async ([username, password, token]) => {
            const response = await fetch('/api/account/signIn', {
                method: 'POST',
                headers: {
                    'content-type': 'application/json',
                    'x-captcha-source': 'turnstile',
                    'x-captcha-token': token,
                },
                body: JSON.stringify({username, password}),
            });
            const body = await response.text();
            try {
                return Object.assign({status: response.status}, JSON.parse(body));
            } catch (error) {
                return {status: response.status, message: body.slice(0, 200).trim()};
            }
        }
        """,
        [username, password, token],
    )


def wait_for_session_cookie(page: Page, timeout: int = SESSION_COOKIE_WAIT_SECONDS) -> list:
    """Wait for the accepted login's session cookie, returning [] if it never lands."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        cookies = page.context.cookies()
        if has_session_cookie(cookies):
            return cookies
        page.wait_for_timeout(500)

    logger.error("Login was accepted but no session cookie appeared")
    return []


def do_login_attempt(config: dict, attempt: int) -> tuple[Optional[list], bool]:
    """
    Run one browser login attempt.

    Returns the captured cookies (None on failure) together with whether the
    credentials actually reached the API. Only that counts against Nodeseek's rate
    limit, so an attempt that gave up earlier can be retried right away.
    """
    captured_cookies: list = []
    submitted = False

    def page_action(page: Page):
        nonlocal submitted

        page.wait_for_selector('#stacked-email', timeout=15000)
        logger.info("Login form ready")

        # Check the token first: it arrives within a second or never, and a stuck
        # widget never recovers, so bail out before the slower anti-bot wait and
        # let the caller start over with a fresh browser.
        token = wait_for_turnstile_token(page)
        if not token:
            return

        wait_for_fog_cookie(page)

        logger.info("Submitting credentials...")
        result = submit_credentials(page, config["username"], config["password"], token)
        submitted = True

        if result.get("need2FA"):
            logger.error("Account asks for a 2FA code, which this tool cannot supply")
            return

        if not result.get("success"):
            logger.error(
                "Login refused (HTTP %s): %s",
                result.get("status"),
                result.get("message") or "no reason given",
            )
            return

        logger.info("Login accepted, collecting session cookies...")
        captured_cookies.extend(wait_for_session_cookie(page))

    try:
        logger.info(
            "Launching stealthy browser for login attempt %d/%d (headless=%s)...",
            attempt,
            config["login_retries"],
            config["headless"],
        )
        # Keep solve_cloudflare on even though the login page is never really
        # challenged: Scrapling classifies it as an "embedded" challenge and answers
        # with a click next to the Turnstile helper frame, and that click is what
        # makes the widget hand over a token at all. Without it the token never
        # arrives and every login is refused.
        with StealthySession(
            headless=config["headless"],
            solve_cloudflare=True,
            timeout=config["login_timeout"],
        ) as session:
            session.fetch(SIGN_IN_URL, page_action=page_action)
    except Exception as e:
        logger.error("Login session error: %s", e)
        return None, submitted

    if captured_cookies:
        logger.info("Captured %d cookies from browser", len(captured_cookies))
        return captured_cookies, submitted

    logger.error("No cookies captured after login attempt %d", attempt)
    return None, submitted


def do_login(config: dict) -> Optional[list]:
    """
    Login via StealthySession with solve_cloudflare=True.

    Scrapling solves Cloudflare before the page_action runs. The login action then
    waits for the form's Turnstile token and for Nodeseek's anti-bot cookie, posts
    the credentials to the sign-in API, and keeps the session cookies it hands back.

    Returns the cookie list on success, None on failure.
    """
    delay = RETRY_BASE_DELAY
    for attempt in range(1, config["login_retries"] + 1):
        captured_cookies, submitted = do_login_attempt(config, attempt)
        if captured_cookies:
            save_cookies(captured_cookies)
            return captured_cookies
        if attempt < config["login_retries"]:
            wait = delay if submitted else RETRY_BASE_DELAY
            logger.info("Login failed, retrying in %d seconds...", wait)
            time.sleep(wait)
            if submitted:
                delay = min(delay * 2, RETRY_MAX_DELAY)

    logger.error("No cookies captured after %d login attempts", config["login_retries"])
    return None


def get_valid_cookies(config: dict) -> Optional[list]:
    """Return valid cookies, triggering a fresh login only when needed."""
    cookies = load_cookies()

    if cookies and check_session_valid(cookies):
        logger.info("Existing session cookie is valid")
        return cookies

    if cookies:
        logger.info("Session cookie expired, re-logging in...")
    else:
        logger.info("No saved cookies, logging in...")

    return do_login(config)
