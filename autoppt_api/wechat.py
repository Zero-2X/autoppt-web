"""WeChat website login (微信开放平台「网站应用」扫码登录, OAuth 2.0 authorization code).

1. ``/api/auth/wechat/start`` returns the QR-connect URL with a signed ``state``.
2. WeChat redirects the browser to ``/api/auth/wechat/callback?code=…&state=…``; the server exchanges
   the code for ``openid``/``unionid`` (the AppSecret never reaches the browser) and redirects to the
   front end with a one-time ticket, never with tokens in the URL.
3. The front end trades the ticket for tokens at ``/api/auth/wechat/exchange``.

Needs an approved website application (AppID, AppSecret, the callback domain registered with WeChat);
disabled while ``AUTOPPT_API_WECHAT_APP_ID`` is empty.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

AUTHORIZE_URL = "https://open.weixin.qq.com/connect/qrconnect"
TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"


class WeChatError(Exception):
    pass


def authorize_url(app_id: str, redirect_uri: str, state: str) -> str:
    return (f"{AUTHORIZE_URL}?appid={quote(app_id)}&redirect_uri={quote(redirect_uri, safe='')}"
            f"&response_type=code&scope=snsapi_login&state={quote(state)}#wechat_redirect")


def exchange_code(app_id: str, app_secret: str, code: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """Returns ``{"openid", "unionid"}``; ``unionid`` is empty when the app is not bound to an open-platform account."""
    params = {"appid": app_id, "secret": app_secret, "code": code, "grant_type": "authorization_code"}
    own = client is None
    client = client or httpx.Client(timeout=15)
    try:
        reply = client.get(TOKEN_URL, params=params)
        data = reply.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WeChatError(f"WeChat token exchange failed: {exc}") from exc
    finally:
        if own:
            client.close()
    if data.get("errcode") or not data.get("openid"):
        raise WeChatError(f"WeChat rejected the code: {data.get('errcode')} {data.get('errmsg', '')}".strip())
    return {"openid": data["openid"], "unionid": data.get("unionid", "")}
