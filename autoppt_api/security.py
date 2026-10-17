"""Tokens and login codes.

* Access and refresh tokens are HS256 JWTs signed with the service secret
  (standard library only).  Refresh tokens are single-use: each refresh
  revokes the old one, and reusing a revoked token revokes the whole family.
* Login is by a 6-digit code sent to the user's email.  Only its hash is
  stored; codes expire, allow a few attempts, and sending is rate limited per
  email and per IP.
* Worker tokens are random secrets; only their sha256 is stored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autoppt_api.models import AuthCode, RefreshToken, now


class TokenError(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_jwt(payload: dict[str, Any], secret: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    signature = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(signature)}"


def verify_jwt(token: str, secret: str, *, expected_type: str) -> dict[str, Any]:
    try:
        header, body, signature = token.split(".")
    except ValueError as exc:
        raise TokenError("malformed token") from exc
    expected = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _unb64(signature)):
        raise TokenError("bad signature")
    if json.loads(_unb64(header)).get("alg") != "HS256":
        raise TokenError("unsupported algorithm")
    payload = json.loads(_unb64(body))
    if payload.get("typ") != expected_type:
        raise TokenError("wrong token type")
    if payload.get("exp", 0) < time.time():
        raise TokenError("token expired")
    return payload


def issue_tokens(session: Session, user_id: str, *, secret: str, access_ttl: int, refresh_ttl: int, family: str | None = None) -> dict[str, Any]:
    issued = int(time.time())
    refresh = RefreshToken(user_id=user_id, expires_at=now() + timedelta(seconds=refresh_ttl))
    session.add(refresh)
    session.flush()
    family = family or refresh.id
    return {
        "access_token": sign_jwt({"sub": user_id, "typ": "access", "iat": issued, "exp": issued + access_ttl}, secret),
        "refresh_token": sign_jwt(
            {"sub": user_id, "typ": "refresh", "jti": refresh.id, "fam": family, "iat": issued, "exp": issued + refresh_ttl}, secret
        ),
        "token_type": "bearer",
        "expires_in": access_ttl,
    }


def rotate_refresh(session: Session, token: str, *, secret: str, access_ttl: int, refresh_ttl: int) -> dict[str, Any]:
    payload = verify_jwt(token, secret, expected_type="refresh")
    record = session.get(RefreshToken, payload["jti"])
    if record is None:
        raise TokenError("unknown refresh token")
    if record.revoked:
        # Reuse of a rotated token: assume theft and revoke every token of the user.
        for other in session.scalars(select(RefreshToken).where(RefreshToken.user_id == record.user_id)):
            other.revoked = True
        session.flush()
        raise TokenError("refresh token reused; all sessions revoked")
    record.revoked = True
    tokens = issue_tokens(session, record.user_id, secret=secret, access_ttl=access_ttl, refresh_ttl=refresh_ttl,
                          family=payload.get("fam"))
    record.replaced_by = verify_jwt(tokens["refresh_token"], secret, expected_type="refresh")["jti"]
    return tokens


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_login_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if "@" not in email or len(email) > 254 or email.startswith("@") or email.endswith("@"):
        raise ValueError("invalid email")
    return email


def recent_codes(session: Session, *, email: str | None = None, ip: str | None = None, window_s: int = 3600) -> int:
    since = now() - timedelta(seconds=window_s)
    query = select(func.count(AuthCode.id)).where(AuthCode.created_at >= since)
    if email:
        query = query.where(AuthCode.email == email)
    if ip:
        query = query.where(AuthCode.requester_ip == ip)
    return int(session.scalar(query) or 0)


def create_login_code(session: Session, email: str, *, ttl_s: int, ip: str = "") -> str:
    code = new_login_code()
    for old in session.scalars(select(AuthCode).where(AuthCode.email == email, AuthCode.consumed.is_(False))):
        old.consumed = True  # only the newest code works
    session.add(AuthCode(email=email, code_hash=hash_secret(f"{email}:{code}"), expires_at=now() + timedelta(seconds=ttl_s),
                         requester_ip=ip[:64]))
    session.flush()
    return code


def consume_login_code(session: Session, email: str, code: str, *, max_attempts: int) -> bool:
    record = session.scalars(
        select(AuthCode).where(AuthCode.email == email, AuthCode.consumed.is_(False)).order_by(AuthCode.created_at.desc())
    ).first()
    if record is None:
        return False
    expires = record.expires_at if record.expires_at.tzinfo else record.expires_at.replace(tzinfo=now().tzinfo)
    if expires < now() or record.attempts >= max_attempts:
        record.consumed = True
        return False
    record.attempts += 1
    if not hmac.compare_digest(record.code_hash, hash_secret(f"{email}:{code.strip()}")):
        if record.attempts >= max_attempts:
            record.consumed = True
        return False
    record.consumed = True
    return True


def sign_url_token(secret: str, *, purpose: str, ref: str, ttl_s: int) -> str:
    """Short-lived capability for one upload or download URL."""
    exp = int(time.time()) + ttl_s
    message = f"{purpose}:{ref}:{exp}"
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()[:32]
    return _b64(f"{message}:{signature}".encode())


def verify_url_token(secret: str, token: str, *, purpose: str) -> str:
    try:
        text = _unb64(token).decode()
        token_purpose, ref, exp, signature = text.rsplit(":", 3)
    except (ValueError, UnicodeDecodeError) as exc:
        raise TokenError("malformed url token") from exc
    expected = hmac.new(secret.encode(), f"{token_purpose}:{ref}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    if token_purpose != purpose or not hmac.compare_digest(expected, signature):
        raise TokenError("bad url token")
    if int(exp) < time.time():
        raise TokenError("url token expired")
    return ref
