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

from config import get_data_dir

logger = logging.getLogger("nodeseek")


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
            if expires > now:
                remaining_days = (expires - now) / 86400
                logger.info("Session cookie valid for %.1f more days", remaining_days)
                return True
    logger.info("Session cookie expired or not found")
    return False


def do_login(config: dict) -> Optional[list]:
    """
    Login via StealthySession with solve_cloudflare=True.

    The page_action callback fills the login form and clicks the button,
    which triggers Cloudflare Turnstile. Scrapling auto-detects and solves
    the Turnstile. After the page redirects (success), cookies are captured
    from the browser context and persisted to disk.

    Returns the cookie list on success, None on failure.
    """
    username = config["username"]
    password = config["password"]
    captured_cookies: list = []

    def page_action(page: Page):
        page.wait_for_selector('#stacked-email', timeout=15000)
        logger.info("Login form ready, filling credentials...")

        page.fill('#stacked-email', username)
        page.fill('#stacked-password', password)

        page.click('button.pure-button')
        logger.info("Clicked login button, waiting for Turnstile...")

        # Give the Turnstile widget time to appear and Scrapling time to solve it
        page.wait_for_timeout(5000)

        logger.info(
            "Waiting for login API call + redirect (timeout: %d ms)...",
            config["login_timeout"],
        )

        try:
            page.wait_for_url(
                lambda url: "signin" not in url.lower(),
                timeout=config["login_timeout"],
            )
            logger.info("Logged in, redirected to: %s", page.url)
        except Exception:
            # Check for error toasts or messages
            page.wait_for_timeout(3000)
            for err_sel in ['[class*="error"]', '[class*="alert"]', ".toast-error", ".toast"]:
                err_el = page.locator(err_sel).first
                if err_el.count() > 0:
                    logger.error("Login error message: %s", err_el.text_content())
                    break
            else:
                logger.error(
                    "Login timed out -- Turnstile may not have been solved. "
                    "Try increasing LOGIN_TIMEOUT or set HEADLESS=false to debug."
                )
                # Take a quick peek at the page state
                logger.error("Current URL after timeout: %s", page.url)
            return

        page.wait_for_timeout(2000)
        cookies = page.context.cookies()
        logger.info("Captured %d cookies from browser", len(cookies))
        captured_cookies.extend(cookies)

    try:
        logger.info("Launching stealthy browser (headless=%s)...", config["headless"])
        with StealthySession(
            headless=config["headless"],
            solve_cloudflare=True,
            timeout=config["login_timeout"],
            block_ads=True,
        ) as session:
            session.fetch(
                "https://www.nodeseek.com/signIn.html",
                page_action=page_action,
            )
    except Exception as e:
        logger.error("Login session error: %s", e)
        return None

    if captured_cookies:
        save_cookies(captured_cookies)
        return captured_cookies

    logger.error("No cookies captured after login")
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
