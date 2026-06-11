"""Backorder eligibility per business policy (backorder-definition.md):

A backorder book is a post-release-date title, temporarily out of stock,
still in print and expected to restock, with:
  - Track quantity enabled (`tracked`)
  - Inventory at time of sale <= 0
  - Continue selling when out of stock = ON (`inventoryPolicy == CONTINUE`)

If continue-selling is OFF the title is Temporarily OOS / Out of Print, not a
backorder. Preorder products are excluded entirely (pub-date driven lifecycle
owned by preorder-service).
"""
from typing import Iterable, Optional, Tuple

from config import EXCLUDED_TAGS


def is_backorder_eligible(
    *,
    inventory_policy: Optional[str],
    tracked: Optional[bool],
    tags: Optional[Iterable[str]],
) -> Tuple[bool, str]:
    tags_lower = {str(t).strip().lower() for t in (tags or [])}
    if tags_lower & EXCLUDED_TAGS:
        return False, "excluded_tag"
    if not tracked:
        return False, "untracked"
    if (inventory_policy or "").upper() != "CONTINUE":
        return False, "policy_deny"
    return True, "ok"


def derive_product_status(
    *,
    open_backorder_qty: int,
    inventory_policy: Optional[str],
    tags: Optional[Iterable[str]],
    available: Optional[int],
) -> str:
    """Structural status for product_state. A backorder sale event may uncover
    a previously undetected Temporarily-OOS or OOP condition."""
    tags_lower = {str(t).strip().lower() for t in (tags or [])}
    if open_backorder_qty <= 0:
        return "resolved"
    if tags_lower & {"out-of-print", "oop"}:
        return "oop_suspect"
    if (inventory_policy or "").upper() != "CONTINUE":
        return "temporarily_oos"
    if available is not None and available > 0:
        # Stock has landed but committed backorders remain unfulfilled.
        # Per policy: restock does NOT auto-resolve; orders/fulfilled does.
        return "restock_pending"
    return "backorderable"
