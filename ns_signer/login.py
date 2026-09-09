"""
Login module: speak NodeSeek's real sign-in protocol and keep the whole session.

Signing in is not "POST the credentials, get a cookie". The site's own form
  1. waits for a Cloudflare Turnstile token,
  2. earns an anti-bot "fog" cookie,
  3. sends a device fingerprint as `x-integrity-token` (plus a stored
     `x-security-token` when it has one) along with the credentials, and
  4. keeps the `x-security-token`/`x-csrf-token` it gets back in response headers.

Skipping step 3 does not fail loudly, which is what made this so easy to get
wrong: the API answers `200 {"success": true, "redirect": "/emailSignIn.html"}`,
meaning "this device is not recognised, go verify by e-mail" -- not "you are
signed in". So `success` alone is never trusted here; an attempt only counts once
an authenticated endpoint answers for the session it produced.
"""

import time
import random
import logging
from typing import Optional

from playwright.sync_api import Page

from . import browser, email_code
from .session_store import (
    SECURITY_TOKEN_KEY,
    describe_session,
    load_session,
    login_block,
    mark_login_blocked,
    save_session,
    session_is_fresh,
)

logger = logging.getLogger("nodeseek")

SIGN_IN_URL = "https://www.nodeseek.com/signIn.html"
SIGN_IN_API = "/api/account/signIn"
USERNAME_SELECTOR = "#stacked-email"

# The e-mail route: ask for a code, read it out of the mailbox, hand it back. The
# code request needs a Turnstile token like the password route does, but the
# sign-in that follows needs none -- and unlike the password route it can finish
# on a device NodeSeek has never seen.
EMAIL_SIGN_IN_URL = "https://www.nodeseek.com/emailSignIn.html"
EMAIL_SIGN_IN_API = "/api/account/emailSignIn"
SEND_CODE_API = "/api/email"
EMAIL_SELECTOR = "input[placeholder='Email']"

MODE_PASSWORD = "password"
MODE_EMAIL = "email"

# Whether the session works is the only answer that matters, and this endpoint
# gives it cheaply: it needs an account and changes nothing.
AUTH_PROBE_API = "/api/notification/unread-count"

# The Turnstile widget hands over a token within a second or so when it works at
# all. When it does not, nothing done inside that browser brings it back -- not
# turnstile.reset(), not turnstile.execute(), not a reload -- so the wait is kept
# short and the whole browser is thrown away instead.
TURNSTILE_WAIT_SECONDS = 25

# Grace period for the Set-Cookie of an accepted login to reach the browser.
SESSION_COOKIE_WAIT_SECONDS = 15

# A burst of sign-in attempts gets rate-limited at NodeSeek's edge, so back off
# after any attempt that actually reached the API.
RETRY_BASE_DELAY = 5
RETRY_MAX_DELAY = 300

OUTCOME_SUCCESS = "success"
OUTCOME_NO_PAGE = "no-page"
OUTCOME_NO_TURNSTILE = "no-turnstile"
OUTCOME_EMAIL_VERIFICATION = "email-verification"
OUTCOME_TWO_FACTOR = "two-factor"
OUTCOME_REFUSED = "refused"
OUTCOME_BLOCKED = "blocked"
OUTCOME_UNVERIFIED = "unverified"
OUTCOME_NO_CODE = "no-code"
OUTCOME_ERROR = "error"

# Outcomes the account itself has to change for, and which therefore only get
# repeated often enough to rule out a fluke. Retrying past that just burns
# browser launches and pushes the account further into NodeSeek's rate limiter.
ATTEMPT_LIMITS = {
    OUTCOME_REFUSED: 1,
    OUTCOME_TWO_FACTOR: 1,
    OUTCOME_EMAIL_VERIFICATION: 2,
    OUTCOME_NO_CODE: 2,
}

# Outcomes that no amount of retrying today will turn into a session. The caller
# needs to know about these: retrying a check-in every twenty minutes around one
# of them would mean hundreds of pointless browser launches overnight.
BLOCKING_OUTCOMES = {
    OUTCOME_EMAIL_VERIFICATION,
    OUTCOME_TWO_FACTOR,
    OUTCOME_REFUSED,
    OUTCOME_NO_CODE,
}

# Outcomes that reached the sign-in API, and so count against its rate limit.
REACHED_API = {
    OUTCOME_SUCCESS,
    OUTCOME_EMAIL_VERIFICATION,
    OUTCOME_TWO_FACTOR,
    OUTCOME_REFUSED,
    OUTCOME_BLOCKED,
    OUTCOME_UNVERIFIED,
    OUTCOME_NO_CODE,
}


def read_turnstile_token(page: Page) -> str:
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
        token = read_turnstile_token(page)
        if token:
            logger.info("Turnstile token acquired")
            return token
        page.wait_for_timeout(500)

    logger.warning("Turnstile widget produced no token, discarding this browser")
    return ""


def classify_sign_in(result: dict) -> tuple[str, str]:
    """
    Turn a sign-in API result into an outcome and a reason worth logging.

    `success` on its own means only that NodeSeek understood the request. A
    `redirect` to the e-mail or SMS sign-in page rides along with it and means the
    opposite of success: the device was not recognised and the password alone will
    not do.
    """
    if result.get("error"):
        return OUTCOME_BLOCKED, f"request failed: {result['error']}"

    body = result.get("body")
    if not isinstance(body, dict):
        return OUTCOME_BLOCKED, browser.describe_api_result(result)

    if not body.get("success"):
        return OUTCOME_REFUSED, str(body.get("message") or browser.describe_api_result(result))

    if body.get("need2FA"):
        return OUTCOME_TWO_FACTOR, "the account is protected by a 2FA code"

    redirect = str(body.get("redirect") or "")
    if redirect.startswith("/emailSignIn") or redirect.startswith("/smsSignIn"):
        return OUTCOME_EMAIL_VERIFICATION, redirect

    return OUTCOME_SUCCESS, redirect or "/"


def probe_says_signed_out(result: dict) -> bool:
    """
    Return True only when the probe clearly says the session does not work.

    Anything it cannot read counts as "don't know", not as failure. A Turnstile
    token is won about one browser launch in ten, so throwing a good session away
    over a probe that moved or broke would cost far more than it saves -- the
    check that actually catches a fake login is the redirect in
    `classify_sign_in`, and this is only a second opinion.
    """
    if result.get("status") in (401, 403):
        return True
    body = result.get("body")
    return isinstance(body, dict) and body.get("success") is False


def session_is_authenticated(page: Page, headers: dict) -> bool:
    """Ask an account-only endpoint whether the browser is actually signed in."""
    result = browser.api_call(page, "GET", AUTH_PROBE_API, None, headers)

    if probe_says_signed_out(result):
        logger.error(
            "Session was rejected by %s -- %s",
            AUTH_PROBE_API,
            browser.describe_api_result(result),
        )
        return False

    if result.get("status") == 200 and isinstance(result.get("body"), dict):
        logger.info("Session verified against %s", AUTH_PROBE_API)
    else:
        logger.warning(
            "Could not verify the session against %s (%s); accepting it anyway",
            AUTH_PROBE_API,
            browser.describe_api_result(result),
        )
    return True


def wait_for_session_cookie(page: Page, timeout: int = SESSION_COOKIE_WAIT_SECONDS) -> list:
    """Wait for the accepted login's session cookie, returning [] if it never lands."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        cookies = page.context.cookies()
        if browser.has_cookie(cookies, "session"):
            return cookies
        page.wait_for_timeout(500)
    return []


def finish_session(page: Page, result: dict) -> None:
    """Collect and verify the session an accepted sign-in just produced."""
    cookies = wait_for_session_cookie(page)
    if not cookies:
        logger.warning("No session cookie appeared; checking the session anyway")
        cookies = page.context.cookies()

    tokens = browser.read_tokens(page)
    if not session_is_authenticated(page, browser.token_headers(tokens)):
        result["outcome"] = OUTCOME_UNVERIFIED
        result["detail"] = "sign-in was accepted but the session does not work"
        return

    result["outcome"] = OUTCOME_SUCCESS
    result["cookies"] = cookies
    result["tokens"] = tokens
    logger.info(
        "Login verified: cookies=%s tokens=%s",
        browser.cookie_names(cookies),
        sorted(tokens) or "none",
    )


def sign_in_by_password(page: Page, config: dict, captcha_token: str, result: dict) -> None:
    """Post the credentials the way the site's own login form does."""
    headers = dict(browser.protocol_headers(page))
    headers["x-captcha-source"] = "turnstile"
    headers["x-captcha-token"] = captcha_token

    logger.info("Submitting credentials...")
    response = browser.api_call(
        page,
        "POST",
        SIGN_IN_API,
        {"username": config["username"], "password": config["password"]},
        headers,
    )
    browser.absorb_response_tokens(page, response.get("headers") or {})

    result["outcome"], result["detail"] = classify_sign_in(response)
    if result["outcome"] == OUTCOME_SUCCESS:
        finish_session(page, result)


def sign_in_by_email(page: Page, config: dict, captcha_token: str, result: dict) -> None:
    """Ask for a login code, read it out of the mailbox, and hand it back."""
    address = config["email_address"]
    logger.info("Asking NodeSeek to mail a login code to %s...", address)
    requested_at = time.time()
    sent = browser.api_call(
        page,
        "POST",
        SEND_CODE_API,
        {
            "email": address,
            "mode": "totp",
            "token": captcha_token,
            "source": "turnstile",
            "version": "v3",
        },
    )
    body = sent.get("body")
    if not (isinstance(body, dict) and body.get("success")):
        result["outcome"] = OUTCOME_NO_CODE
        result["detail"] = f"NodeSeek would not send a code -- {browser.describe_api_result(sent)}"
        return

    code = email_code.wait_for_code(config, requested_at, config["email_code_timeout"])
    if not code:
        result["outcome"] = OUTCOME_NO_CODE
        result["detail"] = "the login code never reached the mailbox"
        return

    logger.info("Signing in with the mailed code...")
    response = browser.api_call(
        page,
        "POST",
        EMAIL_SIGN_IN_API,
        {"email": address, "code": code},
        browser.protocol_headers(page),
    )
    browser.absorb_response_tokens(page, response.get("headers") or {})

    result["outcome"], result["detail"] = classify_sign_in(response)
    if result["outcome"] == OUTCOME_SUCCESS:
        finish_session(page, result)


def do_login_attempt(config: dict, attempt: int, mode: str = MODE_PASSWORD) -> dict:
    """
    Run one browser login attempt over the given route.

    Returns the outcome, a reason, and -- only when the session was proven to work
    -- the cookies and tokens that make it up.
    """
    result = {"outcome": OUTCOME_ERROR, "detail": "", "cookies": [], "tokens": {}}
    url = EMAIL_SIGN_IN_URL if mode == MODE_EMAIL else SIGN_IN_URL
    form_selector = EMAIL_SELECTOR if mode == MODE_EMAIL else USERNAME_SELECTOR

    def page_action(page: Page):
        # Scrapling catches whatever a page action raises and only logs it, so an
        # attempt that dies in here has to record its own reason or the retry loop
        # is left with nothing to report.
        try:
            run_attempt(page)
        except Exception as e:
            result["outcome"] = OUTCOME_ERROR
            result["detail"] = f"{type(e).__name__}: {e}".split("\nCall log")[0][:200]

    def run_attempt(page: Page):
        try:
            page.wait_for_selector(form_selector, timeout=20000)
        except Exception:
            result["outcome"] = OUTCOME_NO_PAGE
            result["detail"] = "the sign-in form never loaded (the edge likely refused the page)"
            return
        logger.info("Sign-in form ready")

        # Check the token first: it arrives within a second or never, so bail out
        # before the slower anti-bot wait and let the caller start over.
        captcha_token = wait_for_turnstile_token(page)
        if not captcha_token:
            result["outcome"] = OUTCOME_NO_TURNSTILE
            result["detail"] = "Turnstile produced no token"
            return

        browser.wait_for_fog_cookie(page)

        _, saved_tokens = load_session()
        browser.restore_tokens(page, saved_tokens)

        if mode == MODE_EMAIL:
            sign_in_by_email(page, config, captcha_token, result)
        else:
            sign_in_by_password(page, config, captcha_token, result)

    try:
        logger.info(
            "Login attempt %d/%d via the %s route",
            attempt,
            config["login_retries"],
            mode,
        )
        with browser.open_session(config, keep_profile=False) as session:
            session.fetch(url, page_action=page_action)
    except Exception as e:
        logger.error("Login session error: %s", e)
        result["outcome"] = OUTCOME_ERROR
        result["detail"] = str(e)

    return result


def login_with_code(config: dict, code: str, address: str = "") -> Optional[list]:
    """
    Finish a sign-in with a code somebody else asked NodeSeek to send.

    Asking for the code is the only part of the e-mail route that needs a
    Turnstile token; handing the code back needs none. So when Cloudflare will not
    give this machine a token -- which is most of the time -- a person can press
    "send code" in their own browser and pass on the six digits, and the login
    still completes here. That matters beyond the one login: it ends with the
    security token that makes this device recognised, which is what lets later
    logins go through on the password alone.

    Returns the cookies on success, None on failure.
    """
    address = address or config.get("email_address", "")
    if not address:
        logger.error(
            "No e-mail address to sign in with. Set EMAIL_ADDRESS to the address "
            "NodeSeek sends the code to."
        )
        return None

    result = {"outcome": OUTCOME_ERROR, "detail": "", "cookies": [], "tokens": {}}

    def page_action(page: Page):
        try:
            run(page)
        except Exception as e:
            result["outcome"] = OUTCOME_ERROR
            result["detail"] = f"{type(e).__name__}: {e}".split("\nCall log")[0][:200]

    def run(page: Page):
        try:
            page.wait_for_selector(EMAIL_SELECTOR, timeout=20000)
        except Exception:
            result["outcome"] = OUTCOME_NO_PAGE
            result["detail"] = "the sign-in form never loaded (the edge likely refused the page)"
            return

        browser.wait_for_fog_cookie(page)
        _, saved_tokens = load_session()
        browser.restore_tokens(page, saved_tokens)

        logger.info("Signing in as %s with the code provided...", address)
        response = browser.api_call(
            page,
            "POST",
            EMAIL_SIGN_IN_API,
            {"email": address, "code": code},
            browser.protocol_headers(page),
        )
        browser.absorb_response_tokens(page, response.get("headers") or {})

        result["outcome"], result["detail"] = classify_sign_in(response)
        if result["outcome"] == OUTCOME_SUCCESS:
            finish_session(page, result)

    try:
        with browser.open_session(config, keep_profile=False) as session:
            session.fetch(EMAIL_SIGN_IN_URL, page_action=page_action)
    except Exception as e:
        logger.error("Login session error: %s", e)
        result["detail"] = str(e)

    log_outcome(result["outcome"], result["detail"])
    if result["outcome"] != OUTCOME_SUCCESS:
        return None

    save_session(result["cookies"], result["tokens"])
    return result["cookies"]


def initial_login_mode(config: dict, tokens: dict) -> str:
    """
    Pick the route a login should take.

    NodeSeek turns an unrecognised device away from the password route however
    good the password is, and the only thing that makes a device recognised is the
    security token handed out at the end of a successful sign-in. Without one the
    e-mail route is the only one that can finish -- so it is used first whenever a
    mailbox is configured to read the code from.
    """
    if not (email_code.is_configured(config) and config.get("email_address")):
        return MODE_PASSWORD
    if tokens.get(SECURITY_TOKEN_KEY):
        return MODE_PASSWORD
    return MODE_EMAIL


def log_outcome(outcome: str, detail: str) -> None:
    """Say what went wrong in terms the operator can act on."""
    if outcome == OUTCOME_SUCCESS:
        return
    if outcome == OUTCOME_EMAIL_VERIFICATION:
        logger.error(
            "NodeSeek does not recognise this device and wants an e-mail code (%s). "
            "No password will get past that, so either point IMAP_* at a mailbox so "
            "the code can be read, or sign in once in an ordinary browser and hand "
            "the session over with --import-session.",
            detail,
        )
    elif outcome == OUTCOME_TWO_FACTOR:
        logger.error("Login needs a 2FA code, which this tool cannot supply (%s)", detail)
    elif outcome == OUTCOME_REFUSED:
        logger.error("NodeSeek refused the credentials: %s", detail)
    elif outcome == OUTCOME_BLOCKED:
        logger.error("Sign-in API did not answer with JSON: %s", detail)
    elif outcome == OUTCOME_UNVERIFIED:
        logger.error("Login looked accepted but produced no working session: %s", detail)
    elif outcome == OUTCOME_NO_TURNSTILE:
        logger.info("No Turnstile token this time: %s", detail)
    elif outcome == OUTCOME_NO_PAGE:
        logger.warning("Could not even reach the sign-in form: %s", detail)
    elif outcome == OUTCOME_NO_CODE:
        logger.error("Could not get a login code by e-mail: %s", detail)
    else:
        logger.error("Login attempt failed: %s", detail)


def do_login(config: dict, force: bool = False) -> Optional[list]:
    """Log in and persist the resulting session, returning the cookies or None."""
    return login_with_outcome(config, force=force)[0]


def blocked_reason(config: dict) -> str:
    """
    Return why a login should not even be attempted, or "" to go ahead.

    A recorded block is only honoured while nothing has changed that could lift
    it: configuring a mailbox, or an imported session bringing a security token,
    both put the login back within reach.
    """
    blocked = login_block(config.get("login_block_days", 7))
    if not blocked:
        return ""
    if email_code.is_configured(config) and config.get("email_address"):
        return ""
    _, tokens = load_session()
    if tokens.get(SECURITY_TOKEN_KEY):
        return ""
    return str(blocked.get("reason") or "a previous login was refused")


def login_with_outcome(config: dict, force: bool = False) -> tuple[Optional[list], str]:
    """
    Log in and persist the resulting session, reporting how it ended.

    Returns the cookies on success, None on failure, along with the outcome that
    settled it so the caller can tell "try again later" from "this will not work
    until somebody changes something". Outcomes of the second kind stop the loop
    early instead of burning every retry.

    `force` ignores a recorded refusal. Scheduled work should leave it alone --
    that record is what stops twenty browser launches a day going nowhere -- but
    somebody who types the command has said what they want.
    """
    skip = "" if force else blocked_reason(config)
    if skip:
        logger.error(
            "Not attempting a login: %s. Confirming that again would cost about "
            "twenty browser launches for nothing. Finish one sign-in with "
            "--login-code, adopt a browser session with --import-session, or set "
            "IMAP_* so the code can be read here.",
            skip,
        )
        return None, OUTCOME_EMAIL_VERIFICATION

    delay = RETRY_BASE_DELAY
    seen: dict[str, int] = {}
    outcome = OUTCOME_ERROR
    _, saved_tokens = load_session()
    mode = initial_login_mode(config, saved_tokens)

    for attempt in range(1, config["login_retries"] + 1):
        result = do_login_attempt(config, attempt, mode)
        outcome, detail = result["outcome"], result["detail"]
        log_outcome(outcome, detail)

        if outcome == OUTCOME_SUCCESS:
            save_session(result["cookies"], result["tokens"])
            return result["cookies"], OUTCOME_SUCCESS

        if outcome == OUTCOME_EMAIL_VERIFICATION and mode == MODE_PASSWORD:
            if email_code.is_configured(config) and config.get("email_address"):
                logger.info("Switching to the e-mail code route for the rest of this login")
                mode = MODE_EMAIL
                seen.pop(OUTCOME_EMAIL_VERIFICATION, None)

        seen[outcome] = seen.get(outcome, 0) + 1
        if seen[outcome] >= ATTEMPT_LIMITS.get(outcome, config["login_retries"] + 1):
            logger.error("Giving up after %d %s outcome(s); retrying cannot fix it", seen[outcome], outcome)
            if outcome in BLOCKING_OUTCOMES:
                mark_login_blocked(f"{outcome} ({detail})" if detail else outcome)
            return None, outcome

        if attempt >= config["login_retries"]:
            break

        if outcome in REACHED_API:
            wait = delay
            delay = min(delay * 2, RETRY_MAX_DELAY)
        else:
            wait = RETRY_BASE_DELAY + random.uniform(0, RETRY_BASE_DELAY)
        logger.info("Retrying login in %.0f seconds...", wait)
        time.sleep(wait)

    logger.error("No working session after %d login attempts", config["login_retries"])
    return None, outcome


def get_valid_session(config: dict) -> tuple[Optional[list], dict, str]:
    """
    Return (cookies, tokens, outcome) for a usable session, logging in if needed.

    The outcome is what a login ended on, so a caller deciding whether to try
    again later can tell a passing failure from one that needs a person.
    """
    cookies, tokens = load_session()

    if cookies and session_is_fresh(cookies, 0):
        logger.info("Reusing saved session (%s)", describe_session(cookies))
        return cookies, tokens, OUTCOME_SUCCESS

    logger.info("Saved session unusable (%s), logging in...", describe_session(cookies))
    fresh, outcome = login_with_outcome(config)
    if not fresh:
        return None, {}, outcome
    cookies, tokens = load_session()
    return cookies, tokens, OUTCOME_SUCCESS


def verify_saved_session(config: dict) -> bool:
    """Open the site with the saved session restored and see whether it is signed in."""
    cookies, tokens = load_session()
    if not cookies:
        logger.error("There is no saved session to check")
        return False

    verified = {"value": False}

    def page_setup(page: Page):
        page.context.add_cookies(cookies)

    def page_action(page: Page):
        browser.restore_tokens(page, tokens)
        verified["value"] = session_is_authenticated(
            page, browser.token_headers(browser.read_tokens(page))
        )

    try:
        with browser.open_session(config, keep_profile=True) as session:
            session.fetch(browser.SITE_URL, page_setup=page_setup, page_action=page_action)
    except Exception as e:
        logger.error("Could not check the saved session: %s", e)
        return False
    return verified["value"]


def refresh_session_if_stale(config: dict) -> bool:
    """
    Log in ahead of time when the saved session is close to running out.

    Returns True when a usable session is in place afterwards. Renewing days early
    is the whole point: it moves the fragile part of the day away from the
    midnight check-in and leaves room to try again tomorrow if today goes badly.
    """
    cookies, _ = load_session()
    if session_is_fresh(cookies, config["session_renew_days"]):
        logger.info("Session still fresh (%s), no login needed", describe_session(cookies))
        return True

    logger.info("Renewing session ahead of expiry (%s)", describe_session(cookies))
    return login_with_outcome(config)[0] is not None
