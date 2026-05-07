"""
Login module: StealthySession + Cloudflare Turnstile solving + cookie persistence.

Cookies are saved to cookies.json and reused across sessions.
A fresh login is triggered only when the saved session expires.
"""

import json
import time
import logging
from pathlib import Path
from typing import Optional

from scrapling.fetchers import StealthySession
from playwright.sync_api import Page

from .config import get_data_dir

logger = logging.getLogger("nodeseek")
LOGIN_RESPONSE_KEYWORDS = ("signin", "login", "auth", "session", "account")


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
        return None
    try:
        cookies = json.loads(cookies_file.read_text())
        if cookies:
            return cookies
    except (json.JSONDecodeError, KeyError):
        pass
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


def get_turnstile_token(page: Page) -> str:
    """Read the embedded Turnstile response token, if the page exposes one."""
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


def wait_for_turnstile_token(page: Page, timeout: int) -> bool:
    """Wait for an embedded Turnstile token when the login form has a token field."""
    token_field = page.locator(
        'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
    )
    if token_field.count() == 0:
        logger.info("No embedded Turnstile response field found on login form")
        return True

    logger.info("Waiting for embedded Turnstile response token...")
    wait_ms = min(timeout, 15000)
    deadline = time.time() + (wait_ms / 1000)
    while time.time() < deadline:
        if get_turnstile_token(page):
            logger.info("Embedded Turnstile response token is present")
            return True
        page.wait_for_timeout(1000)

    logger.warning("Embedded Turnstile response token was not produced before submit")
    return False


def collect_page_errors(page: Page) -> list[str]:
    """Collect visible page text that often carries login failure reasons."""
    messages: list[str] = []
    selectors = [
        '[class*="error"]',
        '[class*="alert"]',
        ".toast-error",
        ".toast",
        "[role='alert']",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(min(locator.count(), 5)):
            item = locator.nth(index)
            if not item.is_visible():
                continue
            text = item.text_content()
            if text:
                cleaned = " ".join(text.split())
                if cleaned and cleaned not in messages:
                    messages.append(cleaned)
    return messages


def summarize_login_responses(responses: list[dict]) -> str:
    if not responses:
        return "none"
    return "; ".join(f"{r['status']} {r['url']}" for r in responses[-8:])


def has_cloudflare_challenge(page: Page) -> bool:
    """Detect a Cloudflare challenge that appears during or after login submit."""
    try:
        if any("challenges.cloudflare.com" in frame.url for frame in page.frames):
            return True

        selectors = [
            "iframe[src*='challenges.cloudflare.com']",
            "#cf-turnstile",
            "#cf_turnstile",
            ".cf-turnstile",
            ".turnstile",
        ]
        for selector in selectors:
            locator = page.locator(selector)
            if locator.count() > 0 and locator.first.is_visible():
                return True

        content = page.content().lower()
        return (
            "just a moment" in content
            or "verifying you are human" in content
        )
    except Exception as e:
        logger.debug("Cloudflare challenge detection failed: %s", e)
        return False


def solve_cloudflare_if_present(page: Page, session: StealthySession) -> bool:
    """Run Scrapling's Cloudflare solver when a challenge appears after submit."""
    if not has_cloudflare_challenge(page):
        return False

    logger.info("Cloudflare challenge detected after login submit, solving...")
    try:
        session._cloudflare_solver(page)
        page.wait_for_timeout(1000)
        logger.info("Post-submit Cloudflare challenge handling finished")
        return True
    except Exception as e:
        logger.error("Post-submit Cloudflare solving failed: %s", e)
        return False


def do_login_attempt(config: dict, attempt: int) -> Optional[list]:
    """Run one browser login attempt and return captured cookies on success."""
    username = config["username"]
    password = config["password"]
    captured_cookies: list = []
    login_responses: list[dict] = []

    def record_response(response):
        url = response.url.lower()
        if any(keyword in url for keyword in LOGIN_RESPONSE_KEYWORDS):
            login_responses.append({"url": response.url, "status": response.status})

    def page_action(page: Page, session: StealthySession):
        page.on("response", record_response)
        page.wait_for_selector('#stacked-email', timeout=15000)
        logger.info("Login form ready, filling credentials...")

        page.fill('#stacked-email', username)
        page.fill('#stacked-password', password)

        wait_for_turnstile_token(page, config["login_timeout"])

        submit_button = page.locator(
            "form button[type='submit'], form button.pure-button, button[type='submit'], button.pure-button"
        ).first
        disabled = submit_button.evaluate(
            "(button) => button.disabled || button.getAttribute('aria-disabled') === 'true'"
        )
        if disabled:
            logger.warning("Login button appears disabled before click")

        submit_button.click()
        logger.info("Clicked login button, waiting for login result...")

        logger.info(
            "Waiting for session cookie or redirect (timeout: %d ms)...",
            config["login_timeout"],
        )

        deadline = time.time() + (config["login_timeout"] / 1000)
        post_submit_solver_runs = 0
        while time.time() < deadline:
            cookies = page.context.cookies()
            if has_session_cookie(cookies):
                logger.info("Logged in, session cookie captured")
                captured_cookies.extend(cookies)
                return
            if "signin" not in page.url.lower():
                logger.info("Logged in, redirected to: %s", page.url)
                captured_cookies.extend(cookies)
                return

            if post_submit_solver_runs < 2 and solve_cloudflare_if_present(page, session):
                post_submit_solver_runs += 1
                continue

            errors = collect_page_errors(page)
            if errors:
                logger.error("Login error message: %s", " | ".join(errors))
                return

            page.wait_for_timeout(1000)

        token = get_turnstile_token(page)
        logger.error("Login attempt %d timed out", attempt)
        logger.error("Current URL after timeout: %s", page.url)
        logger.error("Embedded Turnstile token present: %s", bool(token))
        logger.error("Login-related responses: %s", summarize_login_responses(login_responses))

    try:
        logger.info(
            "Launching stealthy browser for login attempt %d/%d (headless=%s)...",
            attempt,
            config["login_retries"],
            config["headless"],
        )
        with StealthySession(
            headless=config["headless"],
            solve_cloudflare=True,
            timeout=config["login_timeout"],
        ) as session:
            session.fetch(
                "https://www.nodeseek.com/signIn.html",
                wait_selector="#stacked-email",
                page_action=lambda page: page_action(page, session),
            )
    except Exception as e:
        logger.error("Login session error: %s", e)
        return None

    if captured_cookies:
        logger.info("Captured %d cookies from browser", len(captured_cookies))
        return captured_cookies

    logger.error("No cookies captured after login attempt %d", attempt)
    return None


def do_login(config: dict) -> Optional[list]:
    """
    Login via StealthySession with solve_cloudflare=True.

    Scrapling solves Cloudflare challenges before the page_action runs. The login
    action then waits for any embedded Turnstile token, submits the form, and
    treats either a session cookie or a redirect away from the sign-in page as
    success.

    Returns the cookie list on success, None on failure.
    """
    for attempt in range(1, config["login_retries"] + 1):
        captured_cookies = do_login_attempt(config, attempt)
        if captured_cookies:
            save_cookies(captured_cookies)
            return captured_cookies
        if attempt < config["login_retries"]:
            logger.info("Login failed, retrying in 5 seconds...")
            time.sleep(5)

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
