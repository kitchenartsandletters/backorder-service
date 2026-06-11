"""Commitment ledger writers.

Every customer-owed quantity change is an append-only ledger delta keyed by
(event_id, line_item_id, reason) for idempotency. Derived state (order_lines,
product_state) is rebuilt from the ledger — never mutated directly by webhooks.

Why ledger-driven: committed-state changes do not emit Shopify webhooks and
committed cannot be adjusted via the Admin API, so order lifecycle events are
the only reliable real-time signal. Inventory restock alone never resolves a
backorder (partial receipts may cover only some live backorders); resolution
is driven by orders/fulfilled at the line-item level.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

from services import rollup
from services.classification import is_backorder_eligible

log = logging.getLogger(__name__)
UTC = timezone.utc


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def insert_ledger_rows(sb, rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    resp = (
        sb.schema("backorder")
        .table("commitment_ledger")
        .upsert(rows, on_conflict="event_id,line_item_id,reason", ignore_duplicates=True)
        .execute()
    )
    return len(resp.data or [])


def upsert_product_facts(sb, snap: Dict[str, Any]) -> None:
    """Merge inventory/product facts into product_state without touching
    rollup aggregates (PostgREST upsert only updates supplied columns)."""
    if not snap.get("product_id"):
        return
    sb.schema("backorder").table("product_state").upsert(
        {
            "product_id": snap["product_id"],
            "variant_id": snap.get("variant_id"),
            "inventory_item_id": snap.get("inventory_item_id"),
            "sku": snap.get("sku"),
            "title": snap.get("title"),
            "vendor": snap.get("vendor"),
            "tags": snap.get("tags") or [],
            "available": snap.get("available"),
            "inventory_policy": snap.get("inventory_policy"),
            "tracked": snap.get("tracked"),
            "last_event_at": _now_iso(),
            "updated_at": _now_iso(),
        },
        on_conflict="product_id",
    ).execute()


def _order_facts(payload: Dict[str, Any]) -> Dict[str, Any]:
    customer = payload.get("customer") or {}
    return {
        "order_id": payload.get("id"),
        "order_name": payload.get("name"),
        "customer_id": customer.get("id"),
        "customer_email": payload.get("email") or customer.get("email"),
    }


def _fetch_open_lines(sb, order_id: int) -> List[Dict[str, Any]]:
    resp = (
        sb.schema("backorder")
        .table("order_lines")
        .select("*")
        .eq("order_id", order_id)
        .in_("status", ["open", "partial"])
        .execute()
    )
    return resp.data or []


# ----------------------------------------------------------------------
# Topic processors
# ----------------------------------------------------------------------

async def process_order_created(
    event_id: str,
    payload: Dict[str, Any],
    shopify,
    sb,
    reason: str = "order_created",
) -> int:
    """At the time of sale, a line is backordered when the variant tracks
    inventory, continue-selling is ON, and post-sale available < 0.
    backordered_qty = min(line qty, -available_after_sale)."""
    facts = _order_facts(payload)
    order_id = facts["order_id"]
    if not order_id:
        return 0

    occurred_at = payload.get("created_at") or _now_iso()
    rows: List[Dict[str, Any]] = []
    touched_products: Set[int] = set()

    for li in payload.get("line_items") or []:
        variant_id = li.get("variant_id")
        qty = int(li.get("quantity") or 0)
        if not variant_id or qty <= 0:
            continue
        try:
            snap = await shopify.fetch_variant_inventory(int(variant_id))
        except Exception as exc:
            log.error("[ledger] variant snapshot failed %s: %s", variant_id, exc)
            continue

        upsert_product_facts(sb, snap)

        eligible, _why = is_backorder_eligible(
            inventory_policy=snap.get("inventory_policy"),
            tracked=snap.get("tracked"),
            tags=snap.get("tags"),
        )
        if not eligible:
            continue

        available = snap.get("available")
        if available is None:
            continue
        backordered = min(qty, max(0, -int(available)))
        if backordered <= 0:
            continue

        rows.append(
            {
                "event_id": event_id,
                "topic": "orders/create",
                "reason": reason,
                "occurred_at": occurred_at,
                **facts,
                "line_item_id": li.get("id"),
                "product_id": snap.get("product_id") or li.get("product_id"),
                "variant_id": snap.get("variant_id"),
                "inventory_item_id": snap.get("inventory_item_id"),
                "sku": snap.get("sku") or li.get("sku"),
                "title": snap.get("title") or li.get("title"),
                "delta_qty": backordered,
            }
        )
        if snap.get("product_id"):
            touched_products.add(snap["product_id"])

    inserted = insert_ledger_rows(sb, rows)
    if inserted:
        rollup.rebuild_order_lines(sb, order_id=order_id)
        for pid in touched_products:
            rollup.rebuild_product_state(sb, product_id=pid)
    return inserted


def process_order_fulfilled(event_id: str, payload: Dict[str, Any], sb) -> int:
    """Fulfillment is the resolution driver. A line's open quantity is
    consumed when its fulfillment_status becomes 'fulfilled'. Partial
    fulfillments are trued-up by reconciliation."""
    order_id = payload.get("id")
    if not order_id:
        return 0
    open_lines = {l["line_item_id"]: l for l in _fetch_open_lines(sb, order_id)}
    if not open_lines:
        return 0

    occurred_at = payload.get("updated_at") or _now_iso()
    rows = []
    touched_products: Set[int] = set()
    for li in payload.get("line_items") or []:
        line = open_lines.get(li.get("id"))
        if not line:
            continue
        if (li.get("fulfillment_status") or "") != "fulfilled":
            continue
        open_qty = int(line.get("open_qty") or 0)
        if open_qty <= 0:
            continue
        rows.append(
            {
                "event_id": event_id,
                "topic": "orders/fulfilled",
                "reason": "fulfilled",
                "occurred_at": occurred_at,
                "order_id": order_id,
                "order_name": line.get("order_name"),
                "customer_id": line.get("customer_id"),
                "customer_email": line.get("customer_email"),
                "line_item_id": line["line_item_id"],
                "product_id": line.get("product_id"),
                "variant_id": line.get("variant_id"),
                "inventory_item_id": line.get("inventory_item_id"),
                "sku": line.get("sku"),
                "title": line.get("title"),
                "delta_qty": -open_qty,
            }
        )
        if line.get("product_id"):
            touched_products.add(line["product_id"])

    inserted = insert_ledger_rows(sb, rows)
    if inserted:
        rollup.rebuild_order_lines(sb, order_id=order_id)
        for pid in touched_products:
            rollup.rebuild_product_state(sb, product_id=pid)
    return inserted


def process_order_cancelled(event_id: str, payload: Dict[str, Any], sb) -> int:
    order_id = payload.get("id")
    if not order_id:
        return 0
    occurred_at = payload.get("cancelled_at") or payload.get("updated_at") or _now_iso()
    rows = []
    touched_products: Set[int] = set()
    for line in _fetch_open_lines(sb, order_id):
        open_qty = int(line.get("open_qty") or 0)
        if open_qty <= 0:
            continue
        rows.append(
            {
                "event_id": event_id,
                "topic": "orders/cancelled",
                "reason": "cancelled",
                "occurred_at": occurred_at,
                "order_id": order_id,
                "order_name": line.get("order_name"),
                "customer_id": line.get("customer_id"),
                "customer_email": line.get("customer_email"),
                "line_item_id": line["line_item_id"],
                "product_id": line.get("product_id"),
                "variant_id": line.get("variant_id"),
                "inventory_item_id": line.get("inventory_item_id"),
                "sku": line.get("sku"),
                "title": line.get("title"),
                "delta_qty": -open_qty,
            }
        )
        if line.get("product_id"):
            touched_products.add(line["product_id"])

    inserted = insert_ledger_rows(sb, rows)
    if inserted:
        rollup.rebuild_order_lines(sb, order_id=order_id)
        for pid in touched_products:
            rollup.rebuild_product_state(sb, product_id=pid)
    return inserted


def process_refund_created(event_id: str, payload: Dict[str, Any], sb) -> int:
    order_id = payload.get("order_id")
    if not order_id:
        return 0
    open_lines = {l["line_item_id"]: l for l in _fetch_open_lines(sb, order_id)}
    if not open_lines:
        return 0

    occurred_at = payload.get("created_at") or _now_iso()
    rows = []
    touched_products: Set[int] = set()
    for rli in payload.get("refund_line_items") or []:
        line = open_lines.get(rli.get("line_item_id"))
        if not line:
            continue
        open_qty = int(line.get("open_qty") or 0)
        refunded = min(open_qty, int(rli.get("quantity") or 0))
        if refunded <= 0:
            continue
        rows.append(
            {
                "event_id": event_id,
                "topic": "refunds/create",
                "reason": "refunded",
                "occurred_at": occurred_at,
                "order_id": order_id,
                "order_name": line.get("order_name"),
                "customer_id": line.get("customer_id"),
                "customer_email": line.get("customer_email"),
                "line_item_id": line["line_item_id"],
                "product_id": line.get("product_id"),
                "variant_id": line.get("variant_id"),
                "inventory_item_id": line.get("inventory_item_id"),
                "sku": line.get("sku"),
                "title": line.get("title"),
                "delta_qty": -refunded,
            }
        )
        if line.get("product_id"):
            touched_products.add(line["product_id"])

    inserted = insert_ledger_rows(sb, rows)
    if inserted:
        rollup.rebuild_order_lines(sb, order_id=order_id)
        for pid in touched_products:
            rollup.rebuild_product_state(sb, product_id=pid)
    return inserted


# ----------------------------------------------------------------------
# Non-ledger fact updates
# ----------------------------------------------------------------------

async def process_inventory_level_update(payload: Dict[str, Any], shopify, sb) -> None:
    """inventory_levels/update: refresh `available` on product_state and detect
    restock transitions. Inventory levels are NOT reliable for resolution —
    partial receipts may cover only some live backorders — so this never
    closes a line; it only flags restock_pending for ops follow-through."""
    inventory_item_id = payload.get("inventory_item_id")
    if not inventory_item_id:
        return

    resp = (
        sb.schema("backorder")
        .table("product_state")
        .select("product_id, variant_id, available, open_backorder_qty")
        .eq("inventory_item_id", inventory_item_id)
        .limit(1)
        .execute()
    )
    row = (resp.data or [None])[0]

    if row is None:
        # Resolve inventory_item -> variant via supply-chain supplier_products,
        # then hydrate facts from Shopify. Unknown items are ignored (no open
        # backorder context).
        sp = (
            sb.schema("public")
            .table("supplier_products")
            .select("variant_id")
            .eq("inventory_item_id", str(inventory_item_id))
            .limit(1)
            .execute()
        )
        sp_row = (sp.data or [None])[0]
        if not sp_row or not sp_row.get("variant_id"):
            return
        try:
            snap = await shopify.fetch_variant_inventory(int(sp_row["variant_id"]))
        except Exception as exc:
            log.error("[ledger] inventory hydrate failed %s: %s", inventory_item_id, exc)
            return
        upsert_product_facts(sb, snap)
        return

    prev_available = row.get("available")
    new_available = payload.get("available")
    update: Dict[str, Any] = {
        "available": new_available,
        "last_event_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    if (
        new_available is not None
        and int(new_available) > 0
        and (prev_available is None or int(prev_available) <= 0)
    ):
        update["last_restock_at"] = _now_iso()

    sb.schema("backorder").table("product_state").update(update).eq(
        "product_id", row["product_id"]
    ).execute()
    rollup.rebuild_product_state(sb, product_id=row["product_id"])


def process_product_update(payload: Dict[str, Any], sb) -> None:
    """products/update: refresh tags/title/vendor/policy facts. May uncover a
    previously undetected Temporarily-OOS or OOP status on a live backorder."""
    product_id = payload.get("id")
    if not product_id:
        return
    exists = (
        sb.schema("backorder")
        .table("product_state")
        .select("product_id")
        .eq("product_id", product_id)
        .limit(1)
        .execute()
    )
    if not (exists.data or []):
        return  # only track products that have entered the backorder system

    tags_raw = payload.get("tags") or ""
    tags = (
        [t.strip() for t in tags_raw.split(",") if t.strip()]
        if isinstance(tags_raw, str)
        else list(tags_raw)
    )
    variants = payload.get("variants") or []
    v0 = variants[0] if variants else {}

    sb.schema("backorder").table("product_state").update(
        {
            "title": payload.get("title"),
            "vendor": payload.get("vendor"),
            "tags": tags,
            "inventory_policy": (v0.get("inventory_policy") or "").upper() or None,
            "sku": v0.get("sku"),
            "last_event_at": _now_iso(),
            "updated_at": _now_iso(),
        }
    ).eq("product_id", product_id).execute()
    rollup.rebuild_product_state(sb, product_id=product_id)
