"""Backend settings, read from ``AUTOPPT_API_*`` environment variables."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.getenv(f"AUTOPPT_API_{name}", default)


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./var/autoppt-api.db"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", ""))
    storage_dir: Path = field(default_factory=lambda: Path(_env("STORAGE_DIR", "./var/storage")))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "http://127.0.0.1:8000"))
    # Development: return login codes in the API response instead of sending them.
    dev_echo_codes: bool = field(default_factory=lambda: _env("DEV_ECHO_CODES", "0") == "1")
    access_token_ttl_s: int = 15 * 60
    refresh_token_ttl_s: int = 30 * 24 * 3600
    login_code_ttl_s: int = 10 * 60
    login_code_max_attempts: int = 5
    login_codes_per_hour: int = 5
    upload_url_ttl_s: int = 3600
    download_url_ttl_s: int = 3600
    lease_timeout_s: int = 15 * 60
    max_upload_bytes: int = 100 * 1024 * 1024
    signup_bonus_credits: int = 0
    admin_emails: tuple[str, ...] = field(
        default_factory=lambda: tuple(e.strip().lower() for e in _env("ADMIN_EMAILS", "").split(",") if e.strip())
    )
    smtp_url: str = field(default_factory=lambda: _env("SMTP_URL", ""))
    mail_sender: str = field(default_factory=lambda: _env("MAIL_SENDER", "AutoPPT <noreply@localhost>"))
    # SMS for phone login and notifications: "" logs only, "webhook" posts to the operator's signed relay.
    sms_provider: str = field(default_factory=lambda: _env("SMS_PROVIDER", ""))
    sms_webhook_url: str = field(default_factory=lambda: _env("SMS_WEBHOOK_URL", ""))
    sms_webhook_secret: str = field(default_factory=lambda: _env("SMS_WEBHOOK_SECRET", ""))
    # Model gateway (API-key mode, D2).  Provider keys are read from this JSON file only on the gateway host:
    # [{"label": "openai-1", "key": "sk-...", "max_concurrency": 4}, ...]; empty = gateway disabled.
    provider_keys_file: str = field(default_factory=lambda: _env("PROVIDER_KEYS_FILE", ""))
    gateway_upstream_url: str = field(default_factory=lambda: _env("GATEWAY_UPSTREAM_URL", "https://api.openai.com/v1"))
    # Agent input tokens one job may spend through the gateway (J-plan-real-001 used 44 M end to end).
    gateway_job_budget_input_tokens: int = field(default_factory=lambda: int(_env("GATEWAY_JOB_BUDGET", "80000000")))
    gateway_token_ttl_s: int = 6 * 3600
    # WeChat website login (open.weixin.qq.com "网站应用"); disabled while empty.
    wechat_app_id: str = field(default_factory=lambda: _env("WECHAT_APP_ID", ""))
    wechat_app_secret: str = field(default_factory=lambda: _env("WECHAT_APP_SECRET", ""))
    # Malware scanner run on every new upload, e.g. "clamdscan --no-summary" (exit 1 = infected).
    scan_command: str = field(default_factory=lambda: _env("SCAN_COMMAND", ""))
    # Enables the signed mock payment provider (development and tests only).
    payment_mock_secret: str = field(default_factory=lambda: _env("PAYMENT_MOCK_SECRET", ""))
    signup_bonus_expires_days: int | None = None
    # Fernet key (urlsafe base64, 32 bytes) for encryption at rest; empty = plaintext (development only).
    storage_key: str = field(default_factory=lambda: _env("STORAGE_KEY", ""))

    def __post_init__(self) -> None:
        if not self.secret_key:
            # A random key per process is fine for tests and local runs; production must set one.
            self.secret_key = secrets.token_urlsafe(48)
        self.storage_dir = Path(self.storage_dir)
        if self.sms_provider == "webhook" and not (self.sms_webhook_url and self.sms_webhook_secret):
            raise ValueError("AUTOPPT_API_SMS_PROVIDER=webhook needs SMS_WEBHOOK_URL and SMS_WEBHOOK_SECRET")
