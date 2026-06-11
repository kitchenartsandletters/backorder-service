#!/usr/bin/env python3
"""Backorder Order Tagger — Railway cron job.

Mirrors preorder-service tagger.py. Tags Shopify orders carrying open
backorder lines with 'backorder'; removes the tag once every backorder line
on the order is resolved (fulfilled/refunded/cancelled). The tag is consumed
by ops, customer service, and downstream supply-chain PO building.

Run: python -m workers.tagger
"""
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone

from config import TAG_BACKORDER, TAGGER_VERSION
from services.supabase_client import get_client
from shopify_client import ShopifyClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)
UTC = timezone.utc


def _record(sb, order_id: int, order_name: str, action: str, tags_after: list) -> None:
    sb.schema("backorder").table("tagger_processed_orders").upsert(
        {
            "order_id": order_id,
            "order_name": order_name,
            "last_action": action,
            "tags_after": tags_after,
            "processed_at": datetime.now(UTC).isoformat(),
        },
        on_conflict="order_id",
    ).execute()


async def main() -> None:
    started = time.monotonic()
    sb = get_client()
    shopify = ShopifyClient()

    tagged = untagged = 0
    errors = []

    try:
        open_resp = (
            sb.schema("backorder")
            .table("vw_order_overview")
            .select("order_id, order_name")
            .eq("has_open", True)
            .execute()
        )
        open_orders = {int(r["order_id"]): r for r in open_resp.data or []}

        processed_resp = (
            sb.schema("backorder")
            .table("tagger_processed_orders")
            .select("order_id, order_name, last_action")
            .eq("last_action", "tagged")
            .execute()
        )
        previously_tagged = {int(r["order_id"]): r for r in processed_resp.data or []}

        to_tag = [oid for oid in open_orders if oid not in previously_tagged]
        to_untag = [oid for oid in previously_tagged if oid not in open_orders]

        log.info(
            "[tagger] open=%d to_tag=%d to_untag=%d",
            len(open_orders), len(to_tag), len(to_untag),
        )

        for oid in to_tag:
            try:
                order = await shopify.fetch_order_tags(oid)
                tags = order.get("tags") or []
                if TAG_BACKORDER not in tags:
                    await shopify.add_order_tags(oid, [TAG_BACKORDER])
                    tags = tags + [TAG_BACKORDER]
                    tagged += 1
                    log.info("[tagger] tagged %s (%s)", order.get("name"), oid)
                _record(sb, oid, order.get("name") or open_orders[oid].get("order_name"), "tagged", tags)
            except Exception as exc:
                errors.append({"order_id": oid, "op": "tag", "error": str(exc)})
                log.error("[tagger] tag failed %s: %s", oid, exc)

        for oid in to_untag:
            try:
                order = await shopify.fetch_order_tags(oid)
                tags = order.get("tags") or []
                if TAG_BACKORDER in tags:
                    await shopify.remove_order_tags(oid, [TAG_BACKORDER])
                    tags = [t for t in tags if t != TAG_BACKORDER]
                    untagged += 1
                    log.info("[tagger] untagged %s (%s)", order.get("name"), oid)
                _record(sb, oid, order.get("name") or previously_tagged[oid].get("order_name"), "untagged", tags)
            except Exception as exc:
                errors.append({"order_id": oid, "op": "untag", "error": str(exc)})
                log.error("[tagger] untag failed %s: %s", oid, exc)

        sb.schema("backorder").table("tagger_run_log").insert(
            {
                "orders_scanned": len(open_orders) + len(to_untag),
                "orders_tagged": tagged,
                "orders_untagged": untagged,
                "errors": errors or None,
                "duration_seconds": round(time.monotonic() - started, 2),
                "tagger_version": TAGGER_VERSION,
            }
        ).execute()
        log.info("[tagger] done tagged=%d untagged=%d errors=%d", tagged, untagged, len(errors))
    finally:
        await shopify.close()


if __name__ == "__main__":
    asyncio.run(main())
