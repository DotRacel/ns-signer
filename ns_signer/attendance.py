"""
Attendance module: POST /api/attendance via browser's JavaScript fetch().

The nodeseek.com site blocks all non-browser HTTP clients (curl_cffi),
so we execute the API call via page.evaluate() in a lightweight
DynamicSession browser context. The saved session cookies are injected
via page_setup before navigation so the JS fetch carries them.

Query params:
  random=false -> fixed 5 chicken legs
  random=true  -> random chicken legs ("try your luck")
"""

import logging

from scrapling.fetchers import DynamicSession
from playwright.sync_api import Page

logger = logging.getLogger("nodeseek")
ALREADY_ATTENDED_MARKERS = ("已完成签到", "already checked in", "already completed")


def is_attendance_completed(data: dict) -> bool:
    """Return True when the attendance API says today's work is done."""
    if data.get("success"):
        return True

    message = str(data.get("message", "")).lower()
    return any(marker in message for marker in ALREADY_ATTENDED_MARKERS)


def do_attendance(config: dict, cookies: list) -> bool:
    """
    Perform daily check-in using a browser-based API call.

    Returns True on success, False on failure.
    """
    random_param = "true" if config["attendance_mode"] == "random" else "false"
    result_container: list = []

    def page_setup(page: Page):
        """Inject saved cookies into the browser context before navigation."""
        page.context.add_cookies(cookies)

    def page_action(page: Page):
        data = page.evaluate(
            """
            async ([url, randomParam]) => {
                const resp = await fetch(url + '?random=' + randomParam, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                });
                return await resp.json();
            }
            """,
            ["https://www.nodeseek.com/api/attendance", random_param],
        )
        result_container.append(data)
        logger.info("Attendance API response: %s", data)

    try:
        with DynamicSession(
            headless=True,
            disable_resources=True,
            timeout=30000,
        ) as session:
            session.fetch(
                "https://www.nodeseek.com/",
                page_setup=page_setup,
                page_action=page_action,
            )
    except Exception as e:
        logger.error("Attendance session error: %s", e)
        return False

    if not result_container:
        logger.error("No response from attendance API")
        return False

    data = result_container[0]
    if is_attendance_completed(data):
        if not data.get("success"):
            logger.info("Check-in already completed: %s", data.get("message", "Done"))
            return True

        logger.info(
            "Check-in OK! %s | Balance: %s chicken legs",
            data.get("message", "Success"),
            data.get("current", "?"),
        )
        return True

    logger.warning("Check-in failed: %s", data.get("message", "Unknown error"))
    return False
