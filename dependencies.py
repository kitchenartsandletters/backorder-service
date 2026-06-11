import os
from typing import AsyncGenerator

from fastapi import Header, HTTPException

from shopify_client import ShopifyClient
from services.supabase_client import get_client as get_supabase_client_raw


# ---- Shopify ----

async def get_shopify_client() -> AsyncGenerator[ShopifyClient, None]:
    """Per-request Shopify client. Automatically closes after request lifecycle."""
    client = ShopifyClient()
    try:
        yield client
    finally:
        await client.close()


# ---- Supabase ----

def get_supabase_client():
    """Supabase is a singleton (service role client). No per-request close needed."""
    return get_supabase_client_raw()


# ---- Auth ----

def require_admin_token(x_admin_token: str = Header(default="")):
    expected = os.getenv("BACKORDER_ADMIN_TOKEN")
    if not expected or x_admin_token != expected:
        raise HTTPException(status_code=403, detail="Forbidden")
    return True
