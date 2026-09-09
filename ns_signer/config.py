"""
Configuration: load .env file and provide typed config dict.
Values from the real environment take precedence over .env.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"


def load_env():
    """Load .env into os.environ (does not override existing vars)."""
    if not ENV_FILE.exists():
        return
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("\"'")
            if key not in os.environ:
                os.environ[key] = value


def get_data_dir() -> Path:
    """Return the directory used for runtime state such as cookies and logs."""
    return Path(os.environ.get("DATA_DIR", BASE_DIR)).expanduser()


def get_browser_profile_dir() -> str:
    """
    Return the browser profile directory, or "" to use a throwaway one.

    Cloudflare treats a browser that has been here before very differently from
    one that has not: a kept profile carries the cf_clearance it earned last time
    plus the ordinary traces of a used browser, and its Turnstile widget is far
    more likely to actually hand over a token.
    """
    if os.environ.get("BROWSER_PROFILE", "true").lower() != "true":
        return ""
    configured = os.environ.get("BROWSER_PROFILE_DIR")
    path = Path(configured).expanduser() if configured else get_data_dir() / "browser-profile"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def get_email_address() -> str:
    """The account's e-mail, falling back to the username when that is one."""
    configured = os.environ.get("EMAIL_ADDRESS", "").strip()
    if configured:
        return configured
    username = os.environ.get("NODESEEK_USERNAME", "")
    return username if "@" in username else ""


def get_config() -> dict:
    """Return typed configuration from environment."""
    email_address = get_email_address()
    return {
        "username": os.environ["NODESEEK_USERNAME"],
        "password": os.environ["NODESEEK_PASSWORD"],
        "attendance_time": os.environ.get("ATTENDANCE_TIME", "09:00"),
        "random_delay_max": int(os.environ.get("RANDOM_DELAY_MAX", "0")),
        "attendance_mode": os.environ.get("ATTENDANCE_MODE", "fixed"),
        "headless": os.environ.get("HEADLESS", "true").lower() == "true",
        "login_timeout": int(os.environ.get("LOGIN_TIMEOUT", "120000")),
        "login_retries": int(os.environ.get("LOGIN_RETRIES", "15")),
        "solve_cloudflare": os.environ.get("SOLVE_CLOUDFLARE", "true").lower() == "true",
        "browser_profile_dir": get_browser_profile_dir(),
        # A check-in that missed its slot is still worth having, so the day keeps
        # being retried instead of being written off at the first failure.
        "attendance_retry_minutes": int(os.environ.get("ATTENDANCE_RETRY_MINUTES", "20")),
        "attendance_retry_hours": float(os.environ.get("ATTENDANCE_RETRY_HOURS", "20")),
        # Logging in is the fragile part, so it is kept away from the midnight
        # check-in rush and done days before the session would actually run out.
        "session_check_time": os.environ.get("SESSION_CHECK_TIME", "13:20"),
        "session_renew_days": float(os.environ.get("SESSION_RENEW_DAYS", "7")),
        # NodeSeek will not let an unrecognised device in on a password alone, so
        # a mailbox it can read the login code from is what makes an unattended
        # first login possible at all. Everything below is optional.
        "email_address": email_address,
        "imap_host": os.environ.get("IMAP_HOST", ""),
        "imap_port": int(os.environ.get("IMAP_PORT", "993")),
        "imap_username": os.environ.get("IMAP_USERNAME", "") or email_address,
        "imap_password": os.environ.get("IMAP_PASSWORD", ""),
        "imap_mailbox": os.environ.get("IMAP_MAILBOX", "INBOX"),
        "imap_ssl": os.environ.get("IMAP_SSL", "true").lower() == "true",
        "email_code_timeout": int(os.environ.get("EMAIL_CODE_TIMEOUT", "180")),
    }
