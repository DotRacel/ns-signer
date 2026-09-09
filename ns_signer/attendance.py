"""
Attendance module: POST /api/attendance from inside a real nodeseek.com page.

The site blocks plain HTTP clients, so the call is made by the browser itself with
the saved session restored first -- cookies into the context, tokens into
localStorage, and the security token replayed as a request header, which is how
NodeSeek's own front end authenticates.

Whatever the site hands back on the way out is returned to the caller, so the
saved session can roll forward instead of slowly going stale.

Query params:
  random=false -> fixed 5 chicken legs
  random=true  -> random chicken legs ("try your luck")
"""

import logging

from playwright.sync_api import Page

from . import browser

logger = logging.getLogger("nodeseek")

ATTENDANCE_API = "/api/attendance"
ALREADY_ATTENDED_MARKERS = ("已完成签到", "already checked in", "already completed")
# NodeSeek says this in the body of an otherwise ordinary 200 when the session is
# no longer good, so it has to be read as "log in again", not as "check-in failed".
SESSION_EXPIRED_MARKERS = ("未登录", "登录已过期", "not logged in", "unauthorized")


def is_attendance_completed(data: dict) -> bool:
    """Return True when the attendance API says today's work is done."""
    if data.get("success"):
        return True

    message = str(data.get("message", "")).lower()
    return any(marker in message for marker in ALREADY_ATTENDED_MARKERS)


def is_session_expired(result: dict) -> bool:
    """Return True when the attendance API turned us away for want of a session."""
    if result.get("status") in (401, 403):
        return True
    body = result.get("body")
    if not isinstance(body, dict):
        return False
    if body.get("success"):
        return False
    message = str(body.get("message", "")).lower()
    return any(marker in message for marker in SESSION_EXPIRED_MARKERS)


def do_attendance(config: dict, cookies: list, tokens: dict | None = None) -> dict:
    """
    Perform the daily check-in with a restored session.

    Returns {"completed": bool, "session_expired": bool, "cookies": [...],
    "tokens": {...}}, where the cookies and tokens are the refreshed ones to save.
    """
    random_param = "true" if config["attendance_mode"] == "random" else "false"
    outcome = {"completed": False, "session_expired": False, "cookies": [], "tokens": {}}

    def page_setup(page: Page):
        """Inject the saved cookies into the browser context before navigation."""
        if cookies:
            page.context.add_cookies(cookies)

    def page_action(page: Page):
        browser.restore_tokens(page, tokens or {})
        # The same anti-bot cookie the login needs also gates this call, and it
        # only lasts about a day, so it is earned again rather than assumed.
        browser.wait_for_fog_cookie(page)

        headers = browser.token_headers(browser.read_tokens(page))
        result = browser.api_call(
            page, "POST", f"{ATTENDANCE_API}?random={random_param}", None, headers
        )
        browser.absorb_response_tokens(page, result.get("headers") or {})
        logger.info("Attendance API answered %s", browser.describe_api_result(result))

        outcome["session_expired"] = is_session_expired(result)
        body = result.get("body")
        if isinstance(body, dict):
            outcome["completed"] = is_attendance_completed(body)
            if outcome["completed"]:
                if body.get("success"):
                    logger.info(
                        "Check-in OK! %s | Balance: %s chicken legs",
                        body.get("message", "Success"),
                        body.get("current", "?"),
                    )
                else:
                    logger.info("Check-in already completed: %s", body.get("message", "Done"))
            elif not outcome["session_expired"]:
                logger.warning("Check-in failed: %s", body.get("message", "Unknown error"))

        outcome["cookies"] = page.context.cookies()
        outcome["tokens"] = browser.read_tokens(page)

    try:
        with browser.open_session(config, keep_profile=True) as session:
            session.fetch(browser.SITE_URL, page_setup=page_setup, page_action=page_action)
    except Exception as e:
        logger.error("Attendance session error: %s", e)

    return outcome
