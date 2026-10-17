"""Periodic jobs: send notifications, purge expired data, check the ledger.

    python -m autoppt_api.maintenance notify|purge|ledger|all

Run from a scheduler (cron / Windows Task Scheduler) every few minutes.
Notifications go through SMTP when ``AUTOPPT_API_SMTP_URL`` is set
(``smtp://user:password@host:587`` or ``smtps://...``); otherwise they are
logged and marked ``logged`` so development never sends mail.
"""

from __future__ import annotations

import argparse
import json
import logging
import smtplib
import sys
from email.message import EmailMessage
from typing import Any, Sequence
from urllib.parse import unquote, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoppt_api import ledger
from autoppt_api.config import Settings
from autoppt_api.db import Base, make_engine, make_session_factory, session_scope
from autoppt_api.lifecycle import purge_expired
from autoppt_api.models import Notification, User, now
from autoppt_api.storage import LocalStorage

log = logging.getLogger("autoppt_api.maintenance")


def scanner_from_env(command: str) -> Any:
    """``AUTOPPT_API_SCAN_COMMAND`` (e.g. ``clamdscan --no-summary``) -> scanner callable; exit 1 = infected."""
    import shlex
    import subprocess

    argv = shlex.split(command)

    def scan(path) -> str:
        completed = subprocess.run([*argv, str(path)], capture_output=True, check=False)
        return {0: "clean", 1: "infected"}.get(completed.returncode, "scan_error")

    return scan


def send_notifications(session: Session, smtp_url: str, *, sender: str = "AutoPPT <noreply@localhost>", limit: int = 100) -> int:
    pending = session.scalars(select(Notification).where(Notification.status == "pending").limit(limit)).all()
    if not pending:
        return 0
    smtp = None
    if smtp_url:
        url = urlparse(smtp_url)
        smtp = (smtplib.SMTP_SSL if url.scheme == "smtps" else smtplib.SMTP)(url.hostname, url.port or 587, timeout=30)
        if url.scheme == "smtp":
            smtp.starttls()
        if url.username:
            smtp.login(unquote(url.username), unquote(url.password or ""))
    sent = 0
    try:
        for note in pending:
            user = session.get(User, note.user_id)
            if smtp is None or user is None:
                log.info("notification %s to %s: %s", note.id, user.email if user else "?", note.subject)
                note.status = "logged"
            else:
                message = EmailMessage()
                message["From"], message["To"], message["Subject"] = sender, user.email, note.subject
                message.set_content(note.body)
                try:
                    smtp.send_message(message)
                    note.status = "sent"
                except smtplib.SMTPException as exc:
                    log.warning("notification %s failed: %s", note.id, exc)
                    note.status = "failed"
            note.sent_at = now()
            sent += 1
    finally:
        if smtp is not None:
            smtp.quit()
    return sent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task", choices=["notify", "purge", "ledger", "all"])
    args = parser.parse_args(argv)
    settings = Settings()
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    result: dict[str, Any] = {}
    with session_scope(factory) as session:
        if args.task in ("notify", "all"):
            result["notified"] = send_notifications(session, settings.smtp_url)
        if args.task in ("purge", "all"):
            result["purged_jobs"] = purge_expired(session, LocalStorage(settings.storage_dir))
        if args.task in ("ledger", "all"):
            result["ledger_issues"] = ledger.check_ledger(session)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 1 if result.get("ledger_issues") else 0


if __name__ == "__main__":
    raise SystemExit(main())
