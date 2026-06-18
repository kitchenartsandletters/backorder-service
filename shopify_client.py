import asyncio
import logging
import os
from typing import Any, Dict, Optional

import httpx

from shopify_token import get_token_manager

logger = logging.getLogger(__name__)


class ShopifyGraphQLError(Exception):
    pass


class ShopifyHTTPError(Exception):
    pass


class ShopifyClient:
    """Async GraphQL client. The Admin API token is supplied per-request by the
    shared client-credentials token manager (shopify_token); this client never
    reads SHOPIFY_ACCESS_TOKEN. Mirrors preorder-service shopify_client.py."""

    def __init__(self) -> None:
        self.shop_url = os.getenv("SHOP_URL")
        if not self.shop_url:
            raise ValueError("SHOP_URL is not set")

        # Accept both version env names; standardize on SHOPIFY_API_VERSION.
        self.api_version = (
            os.getenv("SHOPIFY_API_VERSION")
            or os.getenv("API_VERSION")
            or "2025-10"
        )
        domain = self.shop_url.split("://", 1)[-1].rstrip("/")
        self.endpoint = f"https://{domain}/admin/api/{self.api_version}/graphql.json"

        self._tokens = get_token_manager()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"Content-Type": "application/json"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def graphql(
        self,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
        max_retries: int = 5,
    ) -> Dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        attempt = 0
        backoff = 0.5
        did_auth_retry = False

        while True:
            attempt += 1
            self._client.headers["X-Shopify-Access-Token"] = await self._tokens.get_token()

            try:
                response = await self._client.post(self.endpoint, json=payload)
            except httpx.RequestError as exc:
                if attempt >= max_retries:
                    raise ShopifyHTTPError(
                        f"Network error after {attempt} attempts: {exc}"
                    ) from exc
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)
                continue

            # Expired/invalid token: refresh once, retry (off the retry budget).
            if response.status_code == 401 and not did_auth_retry:
                logger.warning("[shopify] 401; refreshing token and retrying once.")
                did_auth_retry = True
                self._tokens.invalidate()
                self._client.headers["X-Shopify-Access-Token"] = \
                    await self._tokens.get_token(force_refresh=True)
                continue

            if response.status_code >= 500:
                if attempt >= max_retries:
                    raise ShopifyHTTPError(
                        f"Shopify 5xx error: {response.status_code} {response.text}"
                    )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)
                continue

            if response.status_code != 200:
                raise ShopifyHTTPError(
                    f"Shopify HTTP error {response.status_code}: {response.text}"
                )

            data = response.json()
            if "errors" in data:
                errors = data["errors"]
                throttled = isinstance(errors, list) and any(
                    isinstance(e, dict) and e.get("extensions", {}).get("code") == "THROTTLED"
                    for e in errors
                )
                if throttled and attempt < max_retries:
                    logger.warning("[shopify] THROTTLED; backing off %.1fs", backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 10)
                    continue
                raise ShopifyGraphQLError(errors)
            return data.get("data", {})

    # ------------------------------------------------------------------
    # Inventory snapshots
    # ------------------------------------------------------------------
    # NOTE: `committed` cannot be adjusted via the Admin API and committed-state
    # changes do not emit webhooks. We read it for reconciliation only; the
    # ledger (order lifecycle events) remains the source of truth for what is
    # owed to customers.

    VARIANT_INVENTORY_QUERY = """
    query VariantInventory($id: ID!) {
      productVariant(id: $id) {
        id
        sku
        inventoryPolicy
        inventoryQuantity
        product { id title vendor tags }
        inventoryItem {
          id
          tracked
          inventoryLevels(first: 10) {
            edges {
              node {
                quantities(names: ["available", "committed"]) {
                  name
                  quantity
                }
              }
            }
          }
        }
      }
    }
    """

    @staticmethod
    def _gid_to_int(gid: Optional[str]) -> Optional[int]:
        if not gid:
            return None
        try:
            return int(str(gid).rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            return None

    async def fetch_variant_inventory(self, variant_id: int) -> Dict[str, Any]:
        """Canonical per-variant snapshot: policy, tracked, available, committed,
        plus product facts (tags, title, vendor)."""
        gid = f"gid://shopify/ProductVariant/{variant_id}"
        result = await self.graphql(self.VARIANT_INVENTORY_QUERY, {"id": gid})
        variant = result.get("productVariant")
        if not variant:
            raise ShopifyGraphQLError(f"Variant {variant_id} not found")

        item = variant.get("inventoryItem") or {}
        available = 0
        committed = 0
        has_levels = False
        for edge in (item.get("inventoryLevels") or {}).get("edges", []):
            has_levels = True
            for q in (edge.get("node") or {}).get("quantities", []):
                if q.get("name") == "available":
                    available += int(q.get("quantity") or 0)
                elif q.get("name") == "committed":
                    committed += int(q.get("quantity") or 0)

        product = variant.get("product") or {}
        return {
            "variant_id": self._gid_to_int(variant.get("id")),
            "sku": variant.get("sku"),
            "inventory_policy": variant.get("inventoryPolicy"),
            "inventory_quantity": variant.get("inventoryQuantity"),
            "tracked": bool(item.get("tracked")),
            "inventory_item_id": self._gid_to_int(item.get("id")),
            "available": available if has_levels else variant.get("inventoryQuantity"),
            "committed": committed if has_levels else None,
            "product_id": self._gid_to_int(product.get("id")),
            "title": product.get("title"),
            "vendor": product.get("vendor"),
            "tags": product.get("tags") or [],
        }

    # ------------------------------------------------------------------
    # Order tags (used by workers/tagger.py)
    # ------------------------------------------------------------------

    ORDER_TAGS_QUERY = """
    query OrderTags($id: ID!) {
      order(id: $id) { id name tags }
    }
    """

    TAGS_ADD_MUTATION = """
    mutation TagsAdd($id: ID!, $tags: [String!]!) {
      tagsAdd(id: $id, tags: $tags) { userErrors { field message } }
    }
    """

    TAGS_REMOVE_MUTATION = """
    mutation TagsRemove($id: ID!, $tags: [String!]!) {
      tagsRemove(id: $id, tags: $tags) { userErrors { field message } }
    }
    """

    async def fetch_order_tags(self, order_id: int) -> Dict[str, Any]:
        gid = f"gid://shopify/Order/{order_id}"
        result = await self.graphql(self.ORDER_TAGS_QUERY, {"id": gid})
        order = result.get("order")
        if not order:
            raise ShopifyGraphQLError(f"Order {order_id} not found")
        return order

    async def add_order_tags(self, order_id: int, tags: list) -> None:
        gid = f"gid://shopify/Order/{order_id}"
        result = await self.graphql(self.TAGS_ADD_MUTATION, {"id": gid, "tags": tags})
        errors = (result.get("tagsAdd") or {}).get("userErrors") or []
        if errors:
            raise ShopifyGraphQLError(errors)

    async def remove_order_tags(self, order_id: int, tags: list) -> None:
        gid = f"gid://shopify/Order/{order_id}"
        result = await self.graphql(
            self.TAGS_REMOVE_MUTATION, {"id": gid, "tags": tags}
        )
        errors = (result.get("tagsRemove") or {}).get("userErrors") or []
        if errors:
            raise ShopifyGraphQLError(errors)


def get_shopify_client() -> ShopifyClient:
    return ShopifyClient()