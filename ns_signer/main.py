#!/usr/bin/env python3
"""
Nodeseek Daily Auto Sign-in -- entry point.

Usage:
  python -m ns_signer.main               # Start the scheduler (daily loop)
  python -m ns_signer.main --once         # Check in once and exit
  python -m ns_signer.main --login        # Force re-login and exit
"""

import sys
import logging
import argparse

from .config import get_data_dir, load_env, get_config
from .login import get_valid_cookies, do_login
from .attendance import do_attendance
from .scheduler import run_scheduler

load_env()

DATA_DIR = get_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = DATA_DIR / "signer.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("nodeseek")


def main():
    parser = argparse.ArgumentParser(description="Nodeseek Daily Auto Sign-in")
    parser.add_argument("--once", action="store_true", help="Check in once and exit")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Force re-login, refresh saved cookies, and exit",
    )
    args = parser.parse_args()

    load_env()

    try:
        config = get_config()
    except KeyError as e:
        logger.error(
            "Missing env var: %s. Copy .env.example to .env and fill in credentials.", e
        )
        sys.exit(1)

    if args.login:
        cookies = do_login(config)
        sys.exit(0 if cookies else 1)

    if args.once:
        cookies = get_valid_cookies(config)
        if not cookies:
            logger.error("Login failed")
            sys.exit(1)
        do_attendance(config, cookies)
        return

    run_scheduler(config)


if __name__ == "__main__":
    main()
