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


def get_config() -> dict:
    """Return typed configuration from environment."""
    return {
        "username": os.environ["NODESEEK_USERNAME"],
        "password": os.environ["NODESEEK_PASSWORD"],
        "attendance_time": os.environ.get("ATTENDANCE_TIME", "09:00"),
        "random_delay_max": int(os.environ.get("RANDOM_DELAY_MAX", "0")),
        "attendance_mode": os.environ.get("ATTENDANCE_MODE", "fixed"),
        "headless": os.environ.get("HEADLESS", "true").lower() == "true",
        "login_timeout": int(os.environ.get("LOGIN_TIMEOUT", "120000")),
        "login_retries": int(os.environ.get("LOGIN_RETRIES", "2")),
    }
