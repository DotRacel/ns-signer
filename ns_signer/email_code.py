"""
Read NodeSeek's login code out of a mailbox over IMAP.

NodeSeek only lets an unrecognised device finish a sign-in with a code it mails
out, so a tool that has to log in unattended needs to be able to read that mail.
Everything here is standard library; the mailbox settings are optional and the
whole path stays inert until they are configured.
"""

import re
import time
import email
import imaplib
import logging
from email.header import decode_header, make_header

logger = logging.getLogger("nodeseek")

# The mail carries a short numeric code. Six digits is what NodeSeek sends, but
# the range is kept a little wider so a change of length does not break this.
CODE_PATTERN = re.compile(r"\b(\d{4,8})\b")

POLL_INTERVAL_SECONDS = 10
# Mail servers stamp arrival with their own clock, so a message that is really
# ours can look slightly older than the moment we asked for it.
CLOCK_SKEW_SECONDS = 120


def is_configured(config: dict) -> bool:
    """True when there is enough configuration to read the mailbox."""
    return bool(config.get("imap_host") and config.get("imap_username") and config.get("imap_password"))


def decode_text(value) -> str:
    """Decode a possibly MIME-encoded header into plain text."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return str(value)


def message_text(message) -> str:
    """Flatten a message into the text worth searching for a code."""
    parts = [decode_text(message.get("Subject"))]
    if message.is_multipart():
        payloads = [p for p in message.walk() if p.get_content_type() in ("text/plain", "text/html")]
    else:
        payloads = [message]
    for part in payloads:
        try:
            payload = part.get_payload(decode=True)
        except (AssertionError, ValueError):
            continue
        if payload:
            parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(parts)


def extract_code(text: str) -> str:
    """Pull the login code out of a message, preferring a six-digit one."""
    candidates = CODE_PATTERN.findall(text)
    for candidate in candidates:
        if len(candidate) == 6:
            return candidate
    return candidates[0] if candidates else ""


def looks_like_login_mail(message, address: str) -> bool:
    """Return True for a message that is plausibly NodeSeek's own login code."""
    sender = decode_text(message.get("From")).lower()
    subject = decode_text(message.get("Subject")).lower()
    recipients = " ".join(
        decode_text(message.get(field)) for field in ("To", "Delivered-To", "X-Original-To")
    ).lower()
    if address and address.lower() not in recipients:
        return False
    return "nodeseek" in sender or "nodeseek" in subject


def connect(config: dict) -> imaplib.IMAP4:
    """Open the configured mailbox."""
    host, port = config["imap_host"], config["imap_port"]
    if config.get("imap_ssl", True):
        client = imaplib.IMAP4_SSL(host, port)
    else:
        client = imaplib.IMAP4(host, port)
        client.starttls()
    client.login(config["imap_username"], config["imap_password"])
    return client


def newest_code(client: imaplib.IMAP4, config: dict, not_before: float) -> str:
    """Return the code from the newest matching message, or "" if there is none."""
    client.select(config.get("imap_mailbox", "INBOX"))
    status, data = client.search(None, "ALL")
    if status != "OK" or not data or not data[0]:
        return ""

    # Newest first: the code we want is the one that just arrived.
    for message_id in reversed(data[0].split()[-30:]):
        status, payload = client.fetch(message_id, "(RFC822)")
        if status != "OK" or not payload or not isinstance(payload[0], tuple):
            continue
        message = email.message_from_bytes(payload[0][1])
        if not looks_like_login_mail(message, config.get("email_address", "")):
            continue
        received = email.utils.parsedate_to_datetime(message.get("Date")) if message.get("Date") else None
        if received and received.timestamp() < not_before - CLOCK_SKEW_SECONDS:
            # Everything from here back is older than our request.
            break
        code = extract_code(message_text(message))
        if code:
            return code
    return ""


def wait_for_code(config: dict, not_before: float, timeout: int) -> str:
    """
    Poll the mailbox until NodeSeek's login code arrives.

    `not_before` is the moment the code was requested; anything older belongs to
    an earlier attempt and would be refused by the API.
    """
    logger.info("Waiting for the login code to arrive by e-mail (up to %d s)...", timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            client = connect(config)
        except (imaplib.IMAP4.error, OSError) as e:
            logger.error("Could not open the mailbox: %s", e)
            return ""
        try:
            code = newest_code(client, config, not_before)
        except (imaplib.IMAP4.error, OSError) as e:
            logger.warning("Mailbox read failed: %s", e)
            code = ""
        finally:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
        if code:
            logger.info("Login code received")
            return code
        time.sleep(POLL_INTERVAL_SECONDS)

    logger.error("No login code arrived within %d s", timeout)
    return ""
