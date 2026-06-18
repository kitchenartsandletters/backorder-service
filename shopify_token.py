"""
shopify_token.py  (preorder-service)

Shopify Admin API token via the OAuth client credentials grant.

There is exactly ONE way to get a token now (the grant), exposed through two
doors so you don't have to restructure working sync code:

  - AsyncTokenManager / get_token_manager()  -> async callers (httpx async),
    e.g. the canonical ShopifyClient. Process-wide cache, auto-refresh, 401 retry.
  - get_token_sync()                          -> sync scripts / sync FastAPI routes
    (requests, httpx.Client), e.g. weekly_release_engine, routes/admin_preorders,
    nyt_reporter's week-sales fetch. Cached per process, refreshed near expiry.

Both call the same grant. Nothing here reads SHOPIFY_ACCESS_TOKEN — that env var
is retired.

Required env:
  SHOP_URL                e.g. castironbooks.myshopify.com
  SHOPIFY_CLIENT_ID       Dev Dashboard app Client ID
  SHOPIFY_CLIENT_SECRET   Dev Dashboard app Client secret

PREREQUISITE: the app must be INSTALLED on SHOP_URL's store with Admin API
scopes configured, or the grant returns 400/401.
"""

import os
import time
import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Optional

import httpx

TOKEN_EXPIRY_SAFETY_MARGIN = 300  # refresh this many seconds before stated expiry
log = logging.getLogger(__name__)


def normalize_domain(shop_url: str) -> str:
    if shop_url.startswith(("http://", "https://")):
        return shop_url.split("://", 1)[1].rstrip("/")
    return shop_url.rstrip("/")


@dataclass(frozen=True)
class _Config:
    domain: str
    client_id: str
    client_secret: str


def _load_config() -> _Config:
    shop_url = os.getenv("SHOP_URL")
    cid = os.getenv("SHOPIFY_CLIENT_ID")
    csec = os.getenv("SHOPIFY_CLIENT_SECRET")
    missing = [n for n, v in
               (("SHOP_URL", shop_url), ("SHOPIFY_CLIENT_ID", cid),
                ("SHOPIFY_CLIENT_SECRET", csec)) if not v]
    if missing:
        raise RuntimeError(
            "Missing required Shopify env vars: " + ", ".join(missing) +
            ". This app authenticates via the client credentials grant; there is "
            "no static access token."
        )
    return _Config(normalize_domain(shop_url), cid, csec)  # type: ignore[arg-type]


def _token_endpoint(domain: str) -> str:
    return f"https://{domain}/admin/oauth/access_token"


def _grant_hint(status_code: int) -> str:
    if status_code in (400, 401):
        return (" Likely causes: the app is not installed on this store, "
                "SHOPIFY_CLIENT_ID/SHOPIFY_CLIENT_SECRET are wrong, or the app's "
                "Admin API scopes are not configured.")
    return ""


def _parse_grant(resp: httpx.Response) -> tuple[str, int]:
    if resp.status_code != 200:
        raise RuntimeError(
            f"Shopify token request failed: status={resp.status_code} "
            f"body={resp.text[:300]}.{_grant_hint(resp.status_code)}"
        )
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Shopify token response missing access_token: {data}")
    return token, int(data.get("expires_in", 86400))


def _grant_payload(cfg: _Config) -> dict:
    return {
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "grant_type": "client_credentials",
    }


_GRANT_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}


# ── Async manager (canonical async ShopifyClient) ──────────────────────────────
class AsyncTokenManager:
    def __init__(self) -> None:
        self._cfg = _load_config()
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        log.info("[shopify] AsyncTokenManager ready (client_credentials)")

    @property
    def domain(self) -> str:
        return self._cfg.domain

    async def get_token(self, force_refresh: bool = False) -> str:
        now = time.monotonic()
        if not force_refresh and self._token and now < self._expires_at:
            return self._token
        async with self._lock:
            now = time.monotonic()
            if not force_refresh and self._token and now < self._expires_at:
                return self._token
            try:
                async with httpx.AsyncClient(timeout=30) as c:
                    resp = await c.post(_token_endpoint(self._cfg.domain),
                                        json=_grant_payload(self._cfg), headers=_GRANT_HEADERS)
            except httpx.HTTPError as e:
                raise RuntimeError(f"Shopify token request failed (network): {e}") from e
            token, expires_in = _parse_grant(resp)
            self._token = token
            self._expires_at = time.monotonic() + max(expires_in - TOKEN_EXPIRY_SAFETY_MARGIN, 30)
            log.info("[shopify] token refreshed (prefix=%s…, expires_in=%ss)", token[:8], expires_in)
            return self._token

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0


_tm: Optional[AsyncTokenManager] = None


def get_token_manager() -> AsyncTokenManager:
    global _tm
    if _tm is None:
        _tm = AsyncTokenManager()
    return _tm


# ── Sync getter (sync scripts / sync FastAPI routes) ───────────────────────────
_sync_lock = threading.Lock()
_sync_token: Optional[str] = None
_sync_expires_at: float = 0.0


def get_token_sync(force_refresh: bool = False) -> str:
    global _sync_token, _sync_expires_at
    now = time.monotonic()
    if not force_refresh and _sync_token and now < _sync_expires_at:
        return _sync_token
    with _sync_lock:
        now = time.monotonic()
        if not force_refresh and _sync_token and now < _sync_expires_at:
            return _sync_token
        cfg = _load_config()
        try:
            with httpx.Client(timeout=30) as c:
                resp = c.post(_token_endpoint(cfg.domain),
                              json=_grant_payload(cfg), headers=_GRANT_HEADERS)
        except httpx.HTTPError as e:
            raise RuntimeError(f"Shopify token request failed (network): {e}") from e
        token, expires_in = _parse_grant(resp)
        _sync_token = token
        _sync_expires_at = time.monotonic() + max(expires_in - TOKEN_EXPIRY_SAFETY_MARGIN, 30)
        log.info("[shopify] (sync) token refreshed (prefix=%s…, expires_in=%ss)", token[:8], expires_in)
        return _sync_token