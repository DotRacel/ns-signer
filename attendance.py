"""
Attendance module: POST /api/attendance via browser's JavaScript fetch().

The nodeseek.com site blocks all non-browser HTTP clients (curl_cffi),
so we execute the API call via page.evaluate() in a lightweight
DynamicSession browser context. The saved session cookies are injected
via page_setup before navigation so the JS fetch carries them.

Query params:
  random=true  -> fixed 5 chicken legs
  random=false -> random chicken legs ("try your luck")
"""

import logging

from scrapling.fetchers import DynamicSession
from playwright.sync_api import Page

logger = logging.getLogger("nodeseek")


def do_attendance(config: dict, cookies: list) -> bool:
    """
    Perform daily check-in using a browser-based API call.

    Returns True on success, False on failure.
    """
    random_param = "true" if config["attendance_mode"] == "fixed" else "false"
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
    if data.get("success"):
        logger.info(
            "Check-in OK! %s | Balance: %s chicken legs",
            data.get("message", "Success"),
            data.get("current", "?"),
        )
        return True
    else:
        logger.warning("Check-in failed: %s", data.get("message", "Unknown error"))
        return False
