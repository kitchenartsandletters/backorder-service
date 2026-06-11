# 📦 Backorder Service

Tracks committed sales quantities **owed to customers** for temporarily out-of-stock,
in-print, post-release titles — and surfaces them (product-level AND order-level)
in the Admin Dashboard's **Backorder** module, with action tracking (has it been
ordered? when is it expected? has the customer been notified?) and urgency-scored
heatmap prioritization.

Distinct from `preorder-service`: preorder lifecycle is **publication-date driven**;
backorder lifecycle is **inventory + restock-expectation driven**. Products tagged
`preorder` / `out-of-print` are excluded by classification.

## Architecture

```
Shopify ─▶ webhook-gateway ─▶ backorder-service ─▶ Supabase (supply-chain-service / `backorder` schema)
                                      │                          │
                                      ▼                          ▼
                              Shopify GraphQL          Admin Dashboard (Backorder module)
                              (snapshots, tags)        + supply-chain PO joins (on order / ETA)
```

Event-driven and **replayable**: every owed-quantity change is an append-only delta
in `backorder.commitment_ledger`, keyed `(event_id, line_item_id, reason)` for
idempotency. Derived state (`order_lines`, `product_state`) is deterministically
rebuilt from the ledger and never mutated directly by webhooks.

### Why ledger-driven (key Shopify constraints)

- `committed` inventory-state changes **do not emit webhooks**, and `committed`
  **cannot be adjusted via the Admin API** — it only moves with order creation
  and fulfillment. Order lifecycle events are therefore the only reliable
  real-time signal for customer-owed quantity.
- Restocked inventory levels are **not reliable** for resolution: partial
  receipts may cover only some live backorders. Resolution is driven by
  `orders/fulfilled` at the line-item level; restock merely flags
  `restock_pending` for ops follow-through.
- Committed quantities are polled via GraphQL for **reconciliation** only
  (`backorder.reconciliation_log`).

### Backorder classification (per backorder-definition.md)

A line is backordered at the time of sale when:
- `Track quantity` enabled (`inventoryItem.tracked`)
- `Continue selling when out of stock` ON (`inventoryPolicy == CONTINUE`)
- post-sale `available` < 0 → `backordered_qty = min(line_qty, -available)`
- product is not tagged `preorder` / `out-of-print` / `oop`

If continue-selling is OFF, the title is Temporarily OOS / Out of Print — not a
backorder. A backorder sale event may uncover a previously undetected OOS/OOP
status (surfaced as `temporarily_oos` / `oop_suspect`).

## Stack

- **Backend:** Python / FastAPI (mirrors `preorder-service` patterns)
- **Database:** Supabase Postgres — `backorder` schema in the **supply-chain-service**
  project (RLS enabled on all tables; service-role access only)
- **Ingestion:** `webhook-gateway` (see `docs/gateway-integration.md`)
- **Hosting:** Railway (web service + cron worker)
- **Frontend:** Admin Dashboard `BackorderService` module (see `admin-dashboard` repo)

## Schema (Supabase, `backorder.*`)

| Object | Purpose |
|---|---|
| `events` | Raw ingested webhooks; idempotency boundary (PK `event_id` = `X-Gateway-Event-ID`) |
| `commitment_ledger` | Append-only owed-quantity deltas (reasons: order_created, order_backfill, fulfilled, cancelled, refunded, manual_adjustment, reconciliation) |
| `order_lines` | Derived per-line state: qty backordered/fulfilled/refunded/cancelled, open_qty, status, customer notification bookkeeping |
| `product_state` | Derived per-product state: open qty, order count, oldest open order, inventory facts, structural status |
| `actions` | Operational log: po_created/po_linked, vendor_inquiry, eta_updated, customer_notified, note (FK to `public.purchase_orders`) |
| `tagger_run_log` / `tagger_processed_orders` | Order tagger bookkeeping |
| `reconciliation_log` | Ledger vs Shopify `committed` comparisons |
| `vw_product_overview` | Read layer: + on_order_qty / next_expected_at / po_numbers / lead_time_days from supply-chain PO tables, urgency_score + urgency_bucket |
| `vw_order_overview` | Read layer: order-level rollup |

### Urgency score (heatmap)

`0–100 = age(≤40: days open capped at 30) + qty(≤20: capped at 10 units) +
not-on-order(25) or overdue-PO(15) + customers-unnotified(15)`
Buckets: `critical ≥ 70`, `high ≥ 45`, `medium ≥ 25`, else `low`.

## API

Webhooks (gateway → service): `POST /webhooks` (topic via `X-Shopify-Topic`),
plus explicit subpaths (`/webhooks/orders/create`, `/orders/paid`, `/orders/fulfilled`,
`/orders/cancelled`, `/refunds/create`, `/inventory-levels`, `/products/update`).
Verification: Shopify HMAC first, gateway `X-Gateway-Signature` fallback.
Always 200 after recording; failures stored on `events.processing_error` (replayable).

Admin (X-Admin-Token = `BACKORDER_ADMIN_TOKEN`):

| Endpoint | Purpose |
|---|---|
| `GET /admin/backorders/summary` | Heatmap/summary cards: units owed, products, orders affected, not-on-order, bucket counts |
| `GET /admin/backorders/products` | Product overview, urgency-sorted (`bucket`, `status`, `search`, `include_resolved`, paging) |
| `GET /admin/backorders/products/{id}/orders` | Per-product order lines + action history |
| `GET /admin/backorders/orders` | Order-level rollup (`open_only`) |
| `GET /admin/backorders/orders/{id}` | Order lines + actions |
| `GET/POST /admin/backorders/actions` | Action log; `customer_notified` also stamps order lines |
| `POST /admin/backorders/rollup/rebuild` | Full deterministic rebuild from ledger |
| `POST /admin/backorders/reconcile` | Committed-quantity reconciliation |

## Workers

- `python -m workers.tagger` — Railway cron. Tags orders carrying open backorder
  lines with `backorder`; removes the tag when all lines resolve. Status also
  feeds supply-chain-service PO building.

## Dev start

```bash
cp .env.example .env
pip install -r requirements.txt
uvicorn main:app --reload
```

## Deployment (Railway)

1. New Railway service from this repo (Dockerfile or Procfile).
2. Set env vars from `.env.example`.
3. Add a cron service/schedule for `python -m workers.tagger` (e.g. every 30 min).
4. **Supabase**: in the supply-chain-service project → Settings → API → add
   `backorder` to **Exposed schemas** (required for `.schema("backorder")` via
   supabase-py). Migrations already applied; copies in `sql/`.
5. Wire the gateway: `docs/gateway-integration.md` (BACKORDER_WEBHOOK_URL routing).
6. Admin Dashboard: set `VITE_BACKORDER_BASE_URL` + `VITE_BACKORDER_ADMIN_TOKEN`.

## Future enhancements (planned)

- Automated backorder customer notifications via Mailtrap (notification queueing)
- Back-office alerts on critical-bucket transitions
- Auto-suggested PO lines into supply-chain-service from open backorder demand
- Backfill importer for historical orders (reason `order_backfill`)
