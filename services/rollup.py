"""Deterministic rollups: ledger -> order_lines -> product_state.

Fully rebuildable (replayable) like preorder's lifecycle snapshotter. Rollups
never mutate Shopify state and never modify historical ledger rows.
"""
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.classification import derive_product_status

log = logging.getLogger(__name__)
UTC = timezone.utc

PAGE = 1000


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _paginate(query) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        resp = query.range(offset, offset + PAGE - 1).execute()
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        offset += PAGE


def rebuild_order_lines(sb, order_id: Optional[int] = None) -> int:
    q = sb.schema("backorder").table("commitment_ledger").select("*").order("id")
    if order_id is not None:
        q = q.eq("order_id", order_id)
    ledger = _paginate(q)

    groups: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for row in ledger:
        groups[(row["order_id"], row["line_item_id"])].append(row)

    upserts = []
    for (oid, lid), rows in groups.items():
        rows.sort(key=lambda r: (r["occurred_at"], r["id"]))
        positives = [r for r in rows if r["delta_qty"] > 0]
        initial = sum(r["delta_qty"] for r in positives)
        fulfilled = -sum(
            r["delta_qty"] for r in rows if r["reason"] == "fulfilled" and r["delta_qty"] < 0
        )
        refunded = -sum(
            r["delta_qty"] for r in rows if r["reason"] == "refunded" and r["delta_qty"] < 0
        )
        cancelled = -sum(
            r["delta_qty"] for r in rows if r["reason"] == "cancelled" and r["delta_qty"] < 0
        )
        # manual_adjustment / reconciliation negatives reduce the initial owed
        adjustments = sum(
            r["delta_qty"]
            for r in rows
            if r["reason"] in ("manual_adjustment", "reconciliation") and r["delta_qty"] < 0
        )
        initial = max(0, initial + adjustments)

        net = sum(r["delta_qty"] for r in rows)
        open_qty = max(0, net)

        if open_qty == 0 and cancelled > 0 and fulfilled == 0:
            status = "cancelled"
        elif open_qty == 0 and initial > 0:
            status = "resolved"
        elif 0 < open_qty < initial:
            status = "partial"
        else:
            status = "open"

        latest = rows[-1]
        first_pos = positives[0] if positives else rows[0]
        resolved_at = latest["occurred_at"] if open_qty == 0 and initial > 0 else None

        upserts.append(
            {
                "order_id": oid,
                "line_item_id": lid,
                "order_name": latest.get("order_name"),
                "customer_id": latest.get("customer_id"),
                "customer_email": latest.get("customer_email"),
                "product_id": latest.get("product_id"),
                "variant_id": latest.get("variant_id"),
                "inventory_item_id": latest.get("inventory_item_id"),
                "sku": latest.get("sku"),
                "title": latest.get("title"),
                "qty_backordered": initial,
                "qty_fulfilled": fulfilled,
                "qty_refunded": refunded,
                "qty_cancelled": cancelled,
                "open_qty": open_qty,
                "status": status,
                "order_created_at": first_pos.get("occurred_at"),
                "resolved_at": resolved_at,
                "updated_at": _now_iso(),
            }
        )

    if upserts:
        # Notification fields (last_customer_notified_at, notification_count)
        # are intentionally NOT included so upsert preserves them.
        sb.schema("backorder").table("order_lines").upsert(
            upserts, on_conflict="order_id,line_item_id"
        ).execute()
    return len(upserts)


def rebuild_product_state(sb, product_id: Optional[int] = None) -> int:
    q = (
        sb.schema("backorder")
        .table("order_lines")
        .select("product_id, order_id, open_qty, status, order_created_at")
    )
    if product_id is not None:
        q = q.eq("product_id", product_id)
    lines = _paginate(q)

    agg: Dict[int, Dict[str, Any]] = defaultdict(
        lambda: {"open_qty": 0, "orders": set(), "oldest": None}
    )
    for line in lines:
        pid = line.get("product_id")
        if pid is None:
            continue
        a = agg[pid]
        if line["status"] in ("open", "partial") and int(line.get("open_qty") or 0) > 0:
            a["open_qty"] += int(line["open_qty"])
            a["orders"].add(line["order_id"])
            created = line.get("order_created_at")
            if created and (a["oldest"] is None or created < a["oldest"]):
                a["oldest"] = created

    target_ids = [product_id] if product_id is not None else list(agg.keys())
    if not target_ids:
        return 0

    state_resp = (
        sb.schema("backorder")
        .table("product_state")
        .select("product_id, inventory_policy, tags, available")
        .in_("product_id", target_ids)
        .execute()
    )
    existing = {r["product_id"]: r for r in state_resp.data or []}

    updated = 0
    for pid in target_ids:
        a = agg.get(pid, {"open_qty": 0, "orders": set(), "oldest": None})
        facts = existing.get(pid, {})
        status = derive_product_status(
            open_backorder_qty=a["open_qty"],
            inventory_policy=facts.get("inventory_policy"),
            tags=facts.get("tags"),
            available=facts.get("available"),
        )
        sb.schema("backorder").table("product_state").upsert(
            {
                "product_id": pid,
                "open_backorder_qty": a["open_qty"],
                "open_orders_count": len(a["orders"]),
                "oldest_open_order_at": a["oldest"],
                "status": status,
                "updated_at": _now_iso(),
            },
            on_conflict="product_id",
        ).execute()
        updated += 1
    return updated


def rebuild_all(sb) -> Dict[str, int]:
    lines = rebuild_order_lines(sb)
    products = rebuild_product_state(sb)
    return {"order_lines": lines, "product_states": products}
