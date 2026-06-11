# webhook-gateway integration (Bitbucket — apply manually)

The gateway already ingests every topic the backorder service needs. The only
change is adding a second forwarding target alongside the existing preorder
routing in `src/services/topicHandlers.ts` (or current path), plus one env var.

## 1. Environment

```env
BACKORDER_WEBHOOK_URL=https://backorder-service-production.up.railway.app/webhooks
```

```ts
const BACKORDER_WEBHOOK_URL = (process.env.BACKORDER_WEBHOOK_URL || '') as string;
```

`forwardJson` already no-ops when the URL is unset, so this is safe to deploy
before the Railway service exists.

## 2. Topic routing additions

Add a backorder forward inside each existing handler (same pattern as the
preorder forwards — fire-and-forget with error logging):

```ts
// orders/create
forwardJson('orders/create', payload, BACKORDER_WEBHOOK_URL)
  .catch(err => console.error('[Error] forwarding orders/create (backorder):', err));

// orders/paid  (idempotent backfill guard downstream — forward if handler exists)
forwardJson('orders/paid', payload, BACKORDER_WEBHOOK_URL)
  .catch(err => console.error('[Error] forwarding orders/paid (backorder):', err));

// orders/fulfilled
forwardJson('orders/fulfilled', payload, BACKORDER_WEBHOOK_URL)
  .catch(err => console.error('[Error] forwarding orders/fulfilled (backorder):', err));

// orders/cancelled
forwardJson('orders/cancelled', payload, BACKORDER_WEBHOOK_URL)
  .catch(err => console.error('[Error] forwarding orders/cancelled (backorder):', err));

// refunds/create
forwardJson('refunds/create', payload, BACKORDER_WEBHOOK_URL)
  .catch(err => console.error('[Error] forwarding refunds/create (backorder):', err));
```

For `inventory_levels/update` and `products/update`, extend the existing
`Promise.allSettled` blocks with one more entry:

```ts
'inventory_levels/update': (payload) => {
  Promise.allSettled([
    (async () => { /* existing used-books + preorder forward */ })(),
    forwardJson('inventory_levels/update', payload, BACKORDER_WEBHOOK_URL),
  ]).then(results => { /* existing per-target error logging */ });
},

'products/update': (payload) => {
  Promise.allSettled([
    (async () => { /* existing used-books + preorder forward */ })(),
    forwardJson('products/update', payload, BACKORDER_WEBHOOK_URL),
  ]).then(results => { /* existing per-target error logging */ });
},
```

## 3. Downstream contract (already implemented by backorder-service)

- Topic arrives via `X-Shopify-Topic` (gateway catch-all POST to `/webhooks`).
- Verification: Shopify `X-Shopify-Hmac-Sha256` first, then gateway
  `X-Gateway-Signature` (HMAC-SHA256 base64 over raw body with
  `EXTERNAL_HMAC_SECRET`). Note: the gateway's internal `forwardJson`
  re-serializes payloads and re-signs with `SHOPIFY_WEBHOOK_SECRET`, so set the
  same `SHOPIFY_WEBHOOK_SECRET` in backorder-service env. If you instead route
  through `externalDeliveryService` raw-buffer forwarding, the original Shopify
  signature verifies as-is.
- Idempotency: `X-Gateway-Event-ID` becomes `backorder.events.event_id` (PK);
  duplicate deliveries are acknowledged and skipped.
- The service always returns 200 after recording; processing failures are
  stored on the event row for replay (`webhook_logs` replay endpoint works
  unchanged).
