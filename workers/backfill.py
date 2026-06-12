#!/usr/bin/env python3
"""Backorder backfill — reconstructs CURRENT open backorders.

Historical "available at time of sale" is unknowable after the fact, so this
uses present-state truth: for every variant that is backorder-eligible
(tracked, continue-selling ON, not preorder/OOP-tagged) with available < 0,
the owed quantity (-available) is allocated across that variant's unfulfilled
order lines OLDEST FIRST — matching the business policy that backorders are
filled chronologically as fresh inventory allows.

Ledger rows use reason 'order_backfill' with deterministic event ids
(uuid5 of order+line), so the script is fully idempotent and safe to re-run;
re-runs only add rows for newly discovered lines.

Usage:
  python -m workers.backfill --dry-run      # print allocation plan, write nothing
  python -m workers.backfill                # apply + rebuild rollups
  python -m workers.backfill --days 730     # widen the unfulfilled-order window
"""
import argparse
import asyncio
import logging
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from services import rollup
from services.classification import is_backorder_eligible
from services.ledger import insert_ledger_rows, upsert_product_facts
from services.supabase_client import get_client
from shopify_client import ShopifyClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger(__name__)

EVENT_NAMESPACE = uuid.NAMESPACE_URL

ORDERS_QUERY = """
query UnfulfilledOrders($cursor: String, $q: String!) {
  orders(first: 50, after: $cursor, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id name createdAt cancelledAt email
      customer { id }
      lineItems(first: 100) {
        edges { node {
          id quantity unfulfilledQuantity sku title
          variant { id product { id } }
        } }
      }
    } }
  }
}
"""


def _gid(g):
    if not g:
        return None
    try:
        return int(str(g).rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return None


async def collect_unfulfilled(shopify: ShopifyClient, days: int):
    """All unfulfilled/partial, non-cancelled order lines in the window,
    grouped by variant."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    q = (
        f"created_at:>={since} "
        "(fulfillment_status:unshipped OR fulfillment_status:partial) "
        "-status:cancelled"
    )
    cursor = None
    by_variant = defaultdict(list)
    scanned = 0
    while True:
        data = await shopify.graphql(ORDERS_QUERY, {"cursor": cursor, "q": q})
        conn = data.get("orders") or {}
        for edge in conn.get("edges", []):
            node = edge["node"]
            if node.get("cancelledAt"):
                continue
            scanned += 1
            order_id = _gid(node["id"])
            for le in (node.get("lineItems") or {}).get("edges", []):
                li = le["node"]
                unfulfilled = int(li.get("unfulfilledQuantity") or 0)
                variant = li.get("variant") or {}
                vid = _gid(variant.get("id"))
                if unfulfilled <= 0 or not vid:
                    continue
                by_variant[vid].append(
                    {
                        "order_id": order_id,
                        "order_name": node.get("name"),
                        "customer_id": _gid((node.get("customer") or {}).get("id")),
                        "customer_email": node.get("email"),
                        "line_item_id": _gid(li["id"]),
                        "qty_unfulfilled": unfulfilled,
                        "sku": li.get("sku"),
                        "title": li.get("title"),
                        "product_id": _gid((variant.get("product") or {}).get("id")),
                        "created_at": node.get("createdAt"),
                    }
                )
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    log.info("[backfill] scanned %d unfulfilled orders, %d candidate variants", scanned, len(by_variant))
    return by_variant


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365, help="unfulfilled-order lookback window")
    parser.add_argument("--dry-run", action="store_true", help="print allocation plan, write nothing")
    args = parser.parse_args()

    sb = get_client()
    shopify = ShopifyClient()
    rows = []
    plan = []

    try:
        by_variant = await collect_unfulfilled(shopify, args.days)

        for vid, lines in by_variant.items():
            try:
                snap = await shopify.fetch_variant_inventory(vid)
            except Exception as exc:
                log.error("[backfill] snapshot failed variant %s: %s", vid, exc)
                continue

            eligible, why = is_backorder_eligible(
                inventory_policy=snap.get("inventory_policy"),
                tracked=snap.get("tracked"),
                tags=snap.get("tags"),
            )
            available = snap.get("available")
            if not eligible or available is None or int(available) >= 0:
                continue

            owed = -int(available)
            lines.sort(key=lambda l: l["created_at"] or "")  # oldest first
            for line in lines:
                if owed <= 0:
                    break
                alloc = min(owed, line["qty_unfulfilled"])
                owed -= alloc
                plan.append((snap.get("title") or line["title"], line["order_name"], alloc))
                rows.append(
                    {
                        "event_id": str(
                            uuid.uuid5(
                                EVENT_NAMESPACE,
                                f"backorder:backfill:{line['order_id']}:{line['line_item_id']}",
                            )
                        ),
                        "topic": "backfill",
                        "reason": "order_backfill",
                        "occurred_at": line["created_at"],
                        "order_id": line["order_id"],
                        "order_name": line["order_name"],
                        "customer_id": line["customer_id"],
                        "customer_email": line["customer_email"],
                        "line_item_id": line["line_item_id"],
                        "product_id": snap.get("product_id") or line["product_id"],
                        "variant_id": snap.get("variant_id"),
                        "inventory_item_id": snap.get("inventory_item_id"),
                        "sku": snap.get("sku") or line["sku"],
                        "title": snap.get("title") or line["title"],
                        "delta_qty": alloc,
                    }
                )
            if owed > 0:
                log.warning(
                    "[backfill] variant %s: %d owed units could not be matched to "
                    "unfulfilled lines in window (widen --days?)",
                    vid, owed,
                )
            if not args.dry_run:
                upsert_product_facts(sb, snap)

        if args.dry_run:
            for title, order_name, qty in plan:
                print(f"  {title}  <-  {order_name}: {qty}")
            products = {r["product_id"] for r in rows}
            print(f"\nDRY RUN: {len(rows)} ledger rows across {len(products)} products. Nothing written.")
            return

        inserted = insert_ledger_rows(sb, rows)
        result = rollup.rebuild_all(sb)
        log.info("[backfill] inserted=%d (of %d planned; rest were already present) rollup=%s",
                 inserted, len(rows), result)
    finally:
        await shopify.close()


if __name__ == "__main__":
    asyncio.run(main())
