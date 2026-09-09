"""
Persisted NodeSeek session: cookies plus the tokens its front end keeps in localStorage.

A NodeSeek session is more than a cookie. The site's own login code stores an
`x-security-token` (and a CSRF token) in localStorage, replays them as request
headers on later calls, and refreshes them from the response headers it gets back.
Restoring cookies alone therefore restores only half a session, so both halves are
saved here, in one file, and handed back together.
"""

import json
import time
import logging
from pathlib import Path

from .config import get_data_dir

logger = logging.getLogger("nodeseek")

SESSION_FILE = "session.json"
LEGACY_COOKIES_FILE = "cookies.json"

# localStorage keys NodeSeek's front end authenticates with.
SECURITY_TOKEN_KEY = "security_token"
CSRF_TOKEN_KEY = "csrf_token"
TOKEN_KEYS = (SECURITY_TOKEN_KEY, CSRF_TOKEN_KEY)

SESSION_COOKIE = "session"


def get_session_file() -> Path:
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / SESSION_FILE


def get_legacy_cookies_file() -> Path:
    return get_data_dir() / LEGACY_COOKIES_FILE


def read_state() -> dict:
    """Return the raw session file contents, or {} when there is nothing readable."""
    session_file = get_session_file()
    if not session_file.exists():
        return {}
    try:
        state = json.loads(session_file.read_text())
        return state if isinstance(state, dict) else {}
    except json.JSONDecodeError:
        return {}


def save_session(cookies: list, tokens: dict | None = None) -> None:
    """Write cookies and localStorage tokens out as one session."""
    payload = {
        "saved_at": time.time(),
        "cookies": cookies,
        "tokens": {k: v for k, v in (tokens or {}).items() if v},
    }
    get_session_file().write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    logger.info(
        "Session saved: %d cookies, tokens=%s",
        len(cookies),
        sorted(payload["tokens"]) or "none",
    )


def mark_login_blocked(reason: str) -> None:
    """
    Record that logging in cannot work until somebody intervenes.

    Without this the tool would keep trying: NodeSeek only turns the password
    route down once it has seen a Turnstile token, so confirming the refusal costs
    roughly twenty browser launches, every day, forever. Those launches are not
    free -- a burst of them measurably sours Cloudflare's opinion of the address
    they come from, which makes the token even harder to get once there is
    something worth using it for.
    """
    state = read_state()
    state["login_blocked"] = {"at": time.time(), "reason": reason}
    get_session_file().write_text(json.dumps(state, indent=2, ensure_ascii=False))


def login_block(max_age_days: float) -> dict | None:
    """Return the recorded login block while it is still worth honouring."""
    blocked = read_state().get("login_blocked")
    if not isinstance(blocked, dict):
        return None
    if time.time() - blocked.get("at", 0) > max_age_days * 86400:
        return None
    return blocked


def load_session() -> tuple[list, dict]:
    """
    Return the saved (cookies, tokens), each empty when there is nothing usable.

    Sessions written before tokens were persisted live in cookies.json; they are
    still worth loading, since a session cookie on its own often still works.
    """
    session_file = get_session_file()
    if session_file.exists():
        try:
            payload = json.loads(session_file.read_text())
            cookies = payload.get("cookies") or []
            tokens = payload.get("tokens") or {}
            if cookies:
                logger.info(
                    "Loaded session: %d cookies, tokens=%s",
                    len(cookies),
                    sorted(tokens) or "none",
                )
                return cookies, tokens
        except (json.JSONDecodeError, AttributeError, TypeError) as e:
            logger.warning("Saved session is unreadable: %s (%s)", session_file, e)

    legacy = get_legacy_cookies_file()
    if legacy.exists():
        try:
            cookies = json.loads(legacy.read_text())
            if cookies:
                logger.info("Loaded %d cookies from legacy %s", len(cookies), legacy)
                return cookies, {}
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Legacy cookie file is unreadable: %s (%s)", legacy, e)

    logger.info("No saved session at %s", session_file)
    return [], {}


# Browser extensions export cookies in their own shapes; these are the spellings
# of the same three fields that actually differ from Playwright's.
SAME_SITE_NAMES = {
    "no_restriction": "None",
    "unspecified": "Lax",
    "lax": "Lax",
    "strict": "Strict",
    "none": "None",
}


def normalise_cookie(raw: dict) -> dict | None:
    """Turn one exported cookie into the shape Playwright expects, or None."""
    name, value = raw.get("name"), raw.get("value")
    domain, path = raw.get("domain"), raw.get("path", "/")
    if not name or value is None or not domain:
        return None

    cookie = {"name": str(name), "value": str(value), "domain": str(domain), "path": str(path)}

    expires = raw.get("expires", raw.get("expirationDate"))
    if expires in (None, "", 0):
        cookie["expires"] = -1
    else:
        try:
            cookie["expires"] = float(expires)
        except (TypeError, ValueError):
            cookie["expires"] = -1

    for field in ("httpOnly", "secure"):
        if field in raw:
            cookie[field] = bool(raw[field])

    same_site = raw.get("sameSite")
    if isinstance(same_site, str):
        resolved = SAME_SITE_NAMES.get(same_site.lower())
        if resolved:
            cookie["sameSite"] = resolved
    return cookie


def import_session(payload) -> tuple[list, dict]:
    """
    Read a session exported from a real browser and save it.

    Accepts either a bare cookie array -- which is what cookie-exporting browser
    extensions produce -- or an object with "cookies" and optional "tokens". This
    is the way in when NodeSeek will not let the tool log in by itself: a person
    signs in once, hands over the result, and the tool keeps it alive from there.
    """
    if isinstance(payload, dict):
        raw_cookies = payload.get("cookies") or []
        tokens = {k: v for k, v in (payload.get("tokens") or {}).items() if k in TOKEN_KEYS and v}
    else:
        raw_cookies, tokens = payload or [], {}

    cookies = [c for c in (normalise_cookie(raw) for raw in raw_cookies if isinstance(raw, dict)) if c]
    if not cookies:
        raise ValueError("no usable cookies in the exported session")
    if not any(c["name"] == SESSION_COOKIE for c in cookies):
        raise ValueError(
            f"the export has no {SESSION_COOKIE!r} cookie, so it is not a signed-in session"
        )

    save_session(cookies, tokens)
    return cookies, tokens


def session_cookie_seconds_left(cookies: list) -> float | None:
    """
    Seconds until the session cookie expires.

    Returns None when there is no session cookie, and `inf` for one that the browser
    reports as a non-expiring session cookie.
    """
    for c in cookies:
        if c.get("name") != SESSION_COOKIE:
            continue
        expires = c.get("expires", 0)
        if expires in (-1, 0, None):
            return float("inf")
        return expires - time.time()
    return None


def describe_session(cookies: list) -> str:
    """One-line human summary of how much life the saved session has left."""
    seconds = session_cookie_seconds_left(cookies)
    if seconds is None:
        return "no session cookie"
    if seconds == float("inf"):
        return "session cookie without an expiry"
    if seconds <= 0:
        return f"session cookie expired {-seconds / 86400:.1f} days ago"
    return f"session cookie valid for {seconds / 86400:.1f} more days"


def session_is_fresh(cookies: list, renew_before_days: float) -> bool:
    """
    True when the session cookie is good for longer than the renewal margin.

    Renewing early matters more than it looks: a login can only be attempted while
    the current session is still alive, and NodeSeek is at its least cooperative at
    the midnight check-in rush, so waiting for actual expiry throws away every
    chance to retry on a quiet day.
    """
    seconds = session_cookie_seconds_left(cookies)
    if seconds is None:
        return False
    return seconds > renew_before_days * 86400
