"""Admin Dashboard API for the Backorder module.

Answers, per product AND per order:
  - what is owed to customers (open quantities, ages)
  - what action has been taken (PO created? expected when? customer notified?)
  - what needs attention first (urgency score / bucket for the heatmap)

Auth: X-Admin-Token (BACKORDER_ADMIN_TOKEN), same pattern as preorder-service.
"""
import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from dependencies import get_shopify_client, get_supabase_client, require_admin_token
from services import reconciliation, rollup
from shopify_client import ShopifyClient

log = logging.getLogger("uvicorn.error")
router = APIRouter()
UTC = timezone.utc


# ----------------------------------------------------------------------
# Read endpoints
# ----------------------------------------------------------------------

@router.get("/summary")
def summary(ok: bool = Depends(require_admin_token)):
    sb = get_supabase_client()
    resp = (
        sb.schema("backorder")
        .table("vw_product_overview")
        .select(
            "product_id, open_backorder_qty, open_orders_count, urgency_bucket, on_order_qty"
        )
        .gt("open_backorder_qty", 0)
        .execute()
    )
    products = resp.data or []

    buckets = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    units_owed = 0
    not_on_order = 0
    orders_affected = 0
    for p in products:
        buckets[p.get("urgency_bucket") or "low"] += 1
        units_owed += int(p.get("open_backorder_qty") or 0)
        orders_affected += int(p.get("open_orders_count") or 0)
        if int(p.get("on_order_qty") or 0) <= 0:
            not_on_order += 1

    return {
        "open_products": len(products),
        "units_owed": units_owed,
        "orders_affected": orders_affected,
        "not_on_order": not_on_order,
        "buckets": buckets,
        "as_of": datetime.now(UTC).isoformat(),
    }


@router.get("/products")
def list_products(
    ok: bool = Depends(require_admin_token),
    bucket: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    include_resolved: bool = Query(default=False),
    search: Optional[str] = Query(default=None),
    limit: int = Query(default=200, le=500),
    offset: int = Query(default=0, ge=0),
):
    sb = get_supabase_client()
    q = sb.schema("backorder").table("vw_product_overview").select("*")
    if not include_resolved:
        q = q.gt("open_backorder_qty", 0)
    if bucket:
        q = q.eq("urgency_bucket", bucket)
    if status:
        q = q.eq("status", status)
    if search:
        q = q.ilike("title", f"%{search}%")
    resp = (
        q.order("urgency_score", desc=True)
        .order("open_backorder_qty", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return {"data": resp.data or [], "meta": {"count": len(resp.data or [])}}


@router.get("/products/{product_id}/orders")
def product_orders(product_id: int, ok: bool = Depends(require_admin_token)):
    sb = get_supabase_client()
    lines = (
        sb.schema("backorder")
        .table("order_lines")
        .select("*")
        .eq("product_id", product_id)
        .order("order_created_at", desc=False)
        .execute()
    )
    actions = (
        sb.schema("backorder")
        .table("actions")
        .select("*")
        .eq("product_id", product_id)
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    return {"lines": lines.data or [], "actions": actions.data or []}


@router.get("/orders")
def list_orders(
    ok: bool = Depends(require_admin_token),
    open_only: bool = Query(default=True),
    limit: int = Query(default=200, le=500),
    offset: int = Query(default=0, ge=0),
):
    sb = get_supabase_client()
    q = sb.schema("backorder").table("vw_order_overview").select("*")
    if open_only:
        q = q.eq("has_open", True)
    resp = (
        q.order("days_open", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return {"data": resp.data or [], "meta": {"count": len(resp.data or [])}}


@router.get("/orders/{order_id}")
def order_detail(order_id: int, ok: bool = Depends(require_admin_token)):
    sb = get_supabase_client()
    lines = (
        sb.schema("backorder")
        .table("order_lines")
        .select("*")
        .eq("order_id", order_id)
        .execute()
    )
    if not (lines.data or []):
        raise HTTPException(status_code=404, detail="No backorder lines for order")
    actions = (
        sb.schema("backorder")
        .table("actions")
        .select("*")
        .eq("order_id", order_id)
        .order("created_at", desc=True)
        .execute()
    )
    return {"lines": lines.data or [], "actions": actions.data or []}


@router.get("/actions")
def list_actions(
    ok: bool = Depends(require_admin_token),
    product_id: Optional[int] = Query(default=None),
    order_id: Optional[int] = Query(default=None),
    limit: int = Query(default=100, le=500),
):
    sb = get_supabase_client()
    q = sb.schema("backorder").table("actions").select("*")
    if product_id is not None:
        q = q.eq("product_id", product_id)
    if order_id is not None:
        q = q.eq("order_id", order_id)
    resp = q.order("created_at", desc=True).limit(limit).execute()
    return {"data": resp.data or []}


# ----------------------------------------------------------------------
# Write endpoints
# ----------------------------------------------------------------------

class ActionCreate(BaseModel):
    scope: str  # product | order | order_line
    action_type: str  # po_created | po_linked | vendor_inquiry | eta_updated | customer_notified | note | status_override
    product_id: Optional[int] = None
    order_id: Optional[int] = None
    line_item_id: Optional[int] = None
    details: Optional[Dict[str, Any]] = None
    purchase_order_id: Optional[str] = None  # uuid in public.purchase_orders
    eta_date: Optional[date] = None
    actor: Optional[str] = None


@router.post("/actions")
def create_action(body: ActionCreate, ok: bool = Depends(require_admin_token)):
    if body.scope not in ("product", "order", "order_line"):
        raise HTTPException(status_code=422, detail="Invalid scope")
    if body.scope == "product" and body.product_id is None:
        raise HTTPException(status_code=422, detail="product_id required")
    if body.scope in ("order", "order_line") and body.order_id is None:
        raise HTTPException(status_code=422, detail="order_id required")

    sb = get_supabase_client()
    row = body.model_dump(mode="json", exclude_none=True)
    inserted = sb.schema("backorder").table("actions").insert(row).execute()

    # customer_notified updates customer-facing bookkeeping on order lines
    if body.action_type == "customer_notified" and body.order_id is not None:
        q = (
            sb.schema("backorder")
            .table("order_lines")
            .select("order_id, line_item_id, notification_count")
            .eq("order_id", body.order_id)
        )
        if body.scope == "order_line" and body.line_item_id is not None:
            q = q.eq("line_item_id", body.line_item_id)
        lines = q.execute().data or []
        now = datetime.now(UTC).isoformat()
        for line in lines:
            sb.schema("backorder").table("order_lines").update(
                {
                    "last_customer_notified_at": now,
                    "notification_count": int(line.get("notification_count") or 0) + 1,
                }
            ).eq("order_id", line["order_id"]).eq(
                "line_item_id", line["line_item_id"]
            ).execute()

    return {"data": (inserted.data or [None])[0]}


@router.post("/rollup/rebuild")
def rebuild(ok: bool = Depends(require_admin_token)):
    sb = get_supabase_client()
    return rollup.rebuild_all(sb)


@router.post("/reconcile")
async def reconcile(
    ok: bool = Depends(require_admin_token),
    limit: int = Query(default=100, le=250),
    shopify: ShopifyClient = Depends(get_shopify_client),
):
    sb = get_supabase_client()
    return await reconciliation.run(shopify, sb, limit=limit)
