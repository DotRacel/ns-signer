#!/usr/bin/env python3
"""
Nodeseek Daily Auto Sign-in -- entry point.

Usage:
  python -m ns_signer.main               # Start the scheduler (daily loop)
  python -m ns_signer.main --once        # Check in once and exit
  python -m ns_signer.main --login       # Force re-login and exit
  python -m ns_signer.main --status      # Report on the saved session and exit
  python -m ns_signer.main --import-session cookies.json   # Adopt a browser session
"""

import sys
import json
import logging
import argparse

from .config import get_data_dir, load_env, get_config
from .login import do_login, refresh_session_if_stale, verify_saved_session
from .session_store import load_session, describe_session, import_session
from .scheduler import DONE, run_scheduler, run_attendance_cycle

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
        help="Force re-login, refresh the saved session, and exit",
    )
    parser.add_argument(
        "--renew",
        action="store_true",
        help="Log in only if the saved session is close to expiring, then exit",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Report what the saved session looks like and exit",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Ask NodeSeek whether the saved session still works, and exit",
    )
    parser.add_argument(
        "--import-session",
        metavar="FILE",
        help=(
            "Adopt a session exported from a real browser ('-' reads stdin). Takes a "
            "cookie array from a cookie-exporting extension, or {\"cookies\": [...], "
            "\"tokens\": {...}}"
        ),
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

    if args.import_session:
        try:
            raw = sys.stdin.read() if args.import_session == "-" else open(args.import_session).read()
            cookies, tokens = import_session(json.loads(raw))
        except (OSError, ValueError) as e:
            logger.error("Could not import the session: %s", e)
            sys.exit(1)
        logger.info(
            "Imported %d cookies and %s tokens (%s)",
            len(cookies),
            len(tokens),
            describe_session(cookies),
        )
        sys.exit(0 if verify_saved_session(config) else 1)

    if args.check:
        sys.exit(0 if verify_saved_session(config) else 1)

    if args.status:
        cookies, tokens = load_session()
        logger.info(
            "Saved session: %s | cookies=%d | tokens=%s",
            describe_session(cookies),
            len(cookies),
            sorted(tokens) or "none",
        )
        return

    if args.login:
        sys.exit(0 if do_login(config) else 1)

    if args.renew:
        sys.exit(0 if refresh_session_if_stale(config) else 1)

    if args.once:
        sys.exit(0 if run_attendance_cycle(config) == DONE else 1)

    run_scheduler(config)


if __name__ == "__main__":
    main()
