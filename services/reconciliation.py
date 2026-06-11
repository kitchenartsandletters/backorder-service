"""Committed-quantity reconciliation.

Shopify's `committed` state is the strongest order-level signal but emits no
webhooks and cannot be adjusted via the Admin API — so we poll it here and
compare against ledger-derived open quantities. Note `committed` includes ALL
unfulfilled order units (in-stock and backordered), so ledger_open_qty should
always be <= shopify_committed for a product; the reverse is flagged.
"""
import logging
from typing import Any, Dict

log = logging.getLogger(__name__)


async def run(shopify, sb, limit: int = 100) -> Dict[str, Any]:
    resp = (
        sb.schema("backorder")
        .table("product_state")
        .select("product_id, variant_id, open_backorder_qty")
        .gt("open_backorder_qty", 0)
        .order("open_backorder_qty", desc=True)
        .limit(limit)
        .execute()
    )
    products = resp.data or []

    checked, flagged, errors = 0, 0, []
    for p in products:
        if not p.get("variant_id"):
            continue
        try:
            snap = await shopify.fetch_variant_inventory(int(p["variant_id"]))
        except Exception as exc:
            errors.append({"product_id": p["product_id"], "error": str(exc)})
            continue

        committed = snap.get("committed")
        available = snap.get("available")
        ledger_open = int(p.get("open_backorder_qty") or 0)
        is_flagged = committed is not None and ledger_open > int(committed)

        sb.schema("backorder").table("reconciliation_log").insert(
            {
                "product_id": p["product_id"],
                "variant_id": p["variant_id"],
                "ledger_open_qty": ledger_open,
                "shopify_committed": committed,
                "shopify_available": available,
                "delta": (int(committed) - ledger_open) if committed is not None else None,
                "flagged": is_flagged,
                "notes": "ledger_open exceeds shopify committed" if is_flagged else None,
            }
        ).execute()

        # Refresh available while we are here
        sb.schema("backorder").table("product_state").update(
            {"available": available}
        ).eq("product_id", p["product_id"]).execute()

        checked += 1
        if is_flagged:
            flagged += 1

    summary = {"checked": checked, "flagged": flagged, "errors": errors}
    log.info("[reconciliation] %s", summary)
    return summary
