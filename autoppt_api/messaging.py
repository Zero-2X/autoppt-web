"""Deliver login codes and notifications by email (SMTP) or SMS.

Login codes are sent right after the request's transaction ends (a background task), not through
the notification queue: the queue is flushed by the 5-minute maintenance job, and before 2026-10-18
a new user's code was never sent at all.

SMS providers (``AUTOPPT_API_SMS_PROVIDER``):

- ``""`` (default): log only (development; with ``DEV_ECHO_CODES`` the code is also returned).
- ``webhook``: POST JSON ``{"phone", "template", "params", "text"}`` to ``AUTOPPT_API_SMS_WEBHOOK_URL``
  with ``X-AutoPPT-Signature: sha256=<hex HMAC of the body>`` (``AUTOPPT_API_SMS_WEBHOOK_SECRET``).
  The operator's relay maps it to their SMS vendor's signed API and approved template (Aliyun /
  Tencent Cloud SMS need a company sign name and template approval, so the vendor call lives there).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import smtplib
from email.message import EmailMessage
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

log = logging.getLogger("autoppt_api.messaging")
_CN_MOBILE = re.compile(r"^1[3-9]\d{9}$")


def normalize_phone(phone: str) -> str:
    """Mainland China mobile numbers only (D4): '138 0013 8000', '+86-13800138000' -> '+8613800138000'."""
    digits = re.sub(r"[\s\-()]", "", phone.strip())
    if digits.startswith("+86"):
        digits = digits[3:]
    elif digits.startswith("0086"):
        digits = digits[4:]
    if not _CN_MOBILE.match(digits):
        raise ValueError("invalid phone")
    return "+86" + digits


def mask_phone(phone: str) -> str:
    return phone[:6] + "****" + phone[-4:] if len(phone) >= 10 else "****"


def smtp_connection(smtp_url: str) -> smtplib.SMTP:
    url = urlparse(smtp_url)
    smtp = (smtplib.SMTP_SSL if url.scheme == "smtps" else smtplib.SMTP)(url.hostname, url.port or 587, timeout=30)
    if url.scheme == "smtp":
        smtp.starttls()
    if url.username:
        smtp.login(unquote(url.username), unquote(url.password or ""))
    return smtp


def send_email(smtp_url: str, to: str, subject: str, body: str, *, sender: str) -> str:
    """Returns ``sent``, ``logged`` (no SMTP configured) or ``failed``."""
    if not smtp_url:
        log.info("email to %s: %s", to, subject)
        return "logged"
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, to, subject
    message.set_content(body)
    try:
        smtp = smtp_connection(smtp_url)
        try:
            smtp.send_message(message)
        finally:
            smtp.quit()
    except (OSError, smtplib.SMTPException) as exc:
        log.warning("email to %s failed: %s", to, exc)
        return "failed"
    return "sent"


def send_sms(settings: Any, phone: str, *, template: str, params: dict[str, str], text: str,
             client: httpx.Client | None = None) -> str:
    """Returns ``sent``, ``logged`` or ``failed``.  ``client`` is for tests."""
    provider = getattr(settings, "sms_provider", "")
    if not provider:
        log.info("sms to %s (%s): %s", mask_phone(phone), template, text if template != "login_code" else "<code>")
        return "logged"
    if provider != "webhook":
        log.warning("unknown SMS provider %r; message to %s not sent", provider, mask_phone(phone))
        return "failed"
    body = json.dumps({"phone": phone, "template": template, "params": params, "text": text},
                      ensure_ascii=False).encode("utf-8")
    signature = hmac.new(settings.sms_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    try:
        own = client is None
        client = client or httpx.Client(timeout=15)
        try:
            reply = client.post(settings.sms_webhook_url, content=body,
                                headers={"Content-Type": "application/json", "X-AutoPPT-Signature": f"sha256={signature}"})
        finally:
            if own:
                client.close()
    except httpx.HTTPError as exc:
        log.warning("sms to %s failed: %s", mask_phone(phone), exc)
        return "failed"
    if reply.status_code >= 300:
        log.warning("sms to %s rejected: %s %s", mask_phone(phone), reply.status_code, reply.text[:200])
        return "failed"
    return "sent"


def deliver_login_code(settings: Any, *, email: str | None, phone: str | None, code: str) -> str:
    minutes = settings.login_code_ttl_s // 60
    if phone:
        return send_sms(settings, phone, template="login_code", params={"code": code, "minutes": str(minutes)},
                        text=f"【AutoPPT】验证码 {code}，{minutes} 分钟内有效。请勿告诉他人。")
    return send_email(settings.smtp_url, email or "", "AutoPPT 登录验证码",
                      f"验证码 {code}，{minutes} 分钟内有效。如果不是你本人操作，请忽略这封邮件。",
                      sender=settings.mail_sender)
