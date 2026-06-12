"""Webhook ingestion from webhook-gateway.

Contract (matches gateway externalDeliveryService):
  - Gateway POSTs raw Shopify payloads with X-Shopify-Topic,
    X-Shopify-Hmac-Sha256, X-Shopify-Shop-Domain passed through, plus
    X-Gateway-Signature / X-Gateway-Timestamp / X-Retry-Attempt.
  - Verification: Shopify HMAC first; fall back to gateway signature.
  - Idempotency: the gateway does NOT send an event-id header, so event_id is
    derived deterministically (uuid5) from topic + payload identity. Gateway
    retries re-send identical bytes -> identical event_id -> no-op. Distinct
    real events differ on id/updated_at -> distinct event_ids.
  - Always 200 after recording; processing errors are stored on the event row
    (replayable) rather than bounced back to the gateway.
"""
import base64
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from config import EXTERNAL_HMAC_SECRET, SHOPIFY_WEBHOOK_SECRET
from dependencies import get_shopify_client, get_supabase_client
from services import ledger
from shopify_client import ShopifyClient

log = logging.getLogger("uvicorn.error")
router = APIRouter()
UTC = timezone.utc

EVENT_NAMESPACE = uuid.NAMESPACE_URL


def _b64_hmac(secret: str, raw: bytes) -> str:
    return base64.b64encode(
        hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    ).decode("utf-8")


def _verify_signature(raw: bytes, headers: dict) -> bool:
    shop_sig = headers.get("x-shopify-hmac-sha256")
    if SHOPIFY_WEBHOOK_SECRET and shop_sig:
        if hmac.compare_digest(_b64_hmac(SHOPIFY_WEBHOOK_SECRET, raw), shop_sig):
            return True
    gw_sig = headers.get("x-gateway-signature")
    if EXTERNAL_HMAC_SECRET and gw_sig:
        if hmac.compare_digest(_b64_hmac(EXTERNAL_HMAC_SECRET, raw), gw_sig):
            return True
        # Gateway may sign "timestamp.body" — accept that variant too
        ts = headers.get("x-gateway-timestamp")
        if ts:
            signed = f"{ts}.".encode("utf-8") + raw
            if hmac.compare_digest(_b64_hmac(EXTERNAL_HMAC_SECRET, signed), gw_sig):
                return True
    if not SHOPIFY_WEBHOOK_SECRET and not EXTERNAL_HMAC_SECRET:
        return True  # dev mode: no secrets configured
    return False


def _derive_event_id(header_value: Optional[str], topic: str, payload: dict, raw: bytes) -> str:
    """Deterministic idempotency key.

    Priority: explicit gateway header (if ever added) -> uuid5 of topic +
    payload identity + change marker -> uuid5 of raw bytes as last resort.
    """
    if header_value:
        try:
            return str(uuid.UUID(header_value))
        except ValueError:
            pass

    ident = payload.get("id") or payload.get("order_id") or payload.get("inventory_item_id")
    marker = payload.get("updated_at") or payload.get("created_at") or ""
    if ident is not None:
        return str(uuid.uuid5(EVENT_NAMESPACE, f"backorder:{topic}:{ident}:{marker}"))
    return str(uuid.uuid5(EVENT_NAMESPACE, f"backorder:{topic}:{hashlib.sha256(raw).hexdigest()}"))


def _ledger_has_order(sb, order_id: int) -> bool:
    resp = (
        sb.schema("backorder")
        .table("commitment_ledger")
        .select("id")
        .eq("order_id", order_id)
        .limit(1)
        .execute()
    )
    return bool(resp.data)


async def _dispatch(topic: str, event_id: str, payload: dict, shopify: ShopifyClient, sb) -> None:
    if topic == "orders/create":
        await ledger.process_order_created(event_id, payload, shopify, sb)
    elif topic == "orders/paid":
        # Idempotent guard: only backfill if orders/create was never captured.
        order_id = payload.get("id")
        if order_id and not _ledger_has_order(sb, order_id):
            await ledger.process_order_created(
                event_id, payload, shopify, sb, reason="order_backfill"
            )
    elif topic == "orders/fulfilled":
        ledger.process_order_fulfilled(event_id, payload, sb)
    elif topic == "orders/cancelled":
        ledger.process_order_cancelled(event_id, payload, sb)
    elif topic == "refunds/create":
        ledger.process_refund_created(event_id, payload, sb)
    elif topic == "inventory_levels/update":
        await ledger.process_inventory_level_update(payload, shopify, sb)
    elif topic == "products/update":
        ledger.process_product_update(payload, sb)
    elif topic in ("orders/updated", "products/create"):
        pass  # captured for audit; no lifecycle effect in v1
    else:
        log.info("[webhooks] unhandled topic %s (event %s)", topic, event_id)


async def _handle(topic: str, request: Request, shopify: ShopifyClient) -> JSONResponse:
    raw = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    if not _verify_signature(raw, headers):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    sb = get_supabase_client()
    event_id = _derive_event_id(headers.get("x-gateway-event-id"), topic, payload, raw)

    inserted = (
        sb.schema("backorder")
        .table("events")
        .upsert(
            {
                "event_id": event_id,
                "topic": topic,
                "shop_domain": headers.get("x-shopify-shop-domain"),
                "payload": payload,
                "headers": {
                    k: v
                    for k, v in headers.items()
                    if k.startswith("x-shopify") or k.startswith("x-gateway") or k == "x-retry-attempt"
                },
            },
            on_conflict="event_id",
            ignore_duplicates=True,
        )
        .execute()
    )
    if not (inserted.data or []):
        existing = (
            sb.schema("backorder")
            .table("events")
            .select("processed_at")
            .eq("event_id", event_id)
            .limit(1)
            .execute()
        )
        row = (existing.data or [None])[0]
        if row and row.get("processed_at"):
            return JSONResponse({"status": "ok", "duplicate": True})
        # exists but never finished processing -> retry below

    log.info("[webhooks] topic=%s event_id=%s", topic, event_id)

    try:
        await _dispatch(topic, event_id, payload, shopify, sb)
    except Exception as exc:
        log.error("[webhooks] processing failed %s %s: %s", topic, event_id, exc)
        sb.schema("backorder").table("events").update(
            {"processing_error": str(exc)[:2000]}
        ).eq("event_id", event_id).execute()
        return JSONResponse({"status": "error", "recorded": True})

    sb.schema("backorder").table("events").update(
        {"processed_at": datetime.now(UTC).isoformat(), "processing_error": None}
    ).eq("event_id", event_id).execute()
    return JSONResponse({"status": "ok"})


@router.post("/webhooks")
@router.post("/webhooks/")
async def catch_all(request: Request, shopify: ShopifyClient = Depends(get_shopify_client)):
    """Gateway POSTs here with topic in X-Shopify-Topic."""
    topic = (request.headers.get("X-Shopify-Topic") or "").strip().lower()
    if not topic:
        raise HTTPException(status_code=400, detail="Missing X-Shopify-Topic header")
    return await _handle(topic, request, shopify)


@router.post("/webhooks/orders/create")
async def orders_create(request: Request, shopify: ShopifyClient = Depends(get_shopify_client)):
    return await _handle("orders/create", request, shopify)


@router.post("/webhooks/orders/paid")
async def orders_paid(request: Request, shopify: ShopifyClient = Depends(get_shopify_client)):
    return await _handle("orders/paid", request, shopify)


@router.post("/webhooks/orders/fulfilled")
async def orders_fulfilled(request: Request, shopify: ShopifyClient = Depends(get_shopify_client)):
    return await _handle("orders/fulfilled", request, shopify)


@router.post("/webhooks/orders/cancelled")
async def orders_cancelled(request: Request, shopify: ShopifyClient = Depends(get_shopify_client)):
    return await _handle("orders/cancelled", request, shopify)


@router.post("/webhooks/refunds/create")
async def refunds_create(request: Request, shopify: ShopifyClient = Depends(get_shopify_client)):
    return await _handle("refunds/create", request, shopify)


@router.post("/webhooks/inventory-levels")
async def inventory_levels(request: Request, shopify: ShopifyClient = Depends(get_shopify_client)):
    return await _handle("inventory_levels/update", request, shopify)


@router.post("/webhooks/products/update")
async def products_update(request: Request, shopify: ShopifyClient = Depends(get_shopify_client)):
    return await _handle("products/update", request, shopify)
