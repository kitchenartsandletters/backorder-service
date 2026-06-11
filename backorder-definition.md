## 📙 Backorder Book

### ✅ Definition

A backorder book is a **post-release** title that is **temporarily out of stock** but still **in print and expected to restock**. Customers may be allowed to order or request notification.

---

### 🔧 Shopify Implementation

#### Inventory Settings

- `Track quantity`: ✅ Enabled

- `Inventory`: ≤ `0`

- `Continue selling when out of stock`: ✅ Must be ON for a book to be **considered backorderable**

  - If OFF, the book is either **Out of Stock** or **Out of Print** (OOP).

#### Cart Behavior

- If `Continue selling when out of stock` is OFF, default Shopify prevents checkout.

- If ON, cart is allowed and product is treated as **backordered**.

#### Notify Me Flow

- Custom Notify Me form is rendered:

  - Injected via snippet or `custom.js`

  - Uses `product.id` or `variant.id` to track interest in Supabase

- The flow can be **blacklisted** by Admins via the **Request Service dashboard**.

#### Restock Ops

- Upon restock:

  - Notify Me form disappears (via Liquid or JavaScript).

  - Admin manually sends notifications to customers.

- ⚠️ Automated notifications via Request Service are a **planned future enhancement**.

---

## 🔗 System Integration Plan

| Component               | Role                                                   |

|------------------------|--------------------------------------------------------|

| `Preorder Service`     | Webhook-triggered lifecycle management of preorder SKUs. |

| `Backorder Service`    | Tracks interest, stock restoration, and notification queueing. |

| `webhook-gateway`      | Ingests product updates (tags, inventory, pub date changes) for both services. |

| `NYT Weekly Tool`      | Tracks weekly presales, links with `Preorder Service` for pub week matching. |

> The `NYT Weekly Reporting Tool` will be **standalone**, with optional linkage to Preorder Service to produce comprehensive CSV snapshots for internal and publisher-facing reporting.

---

## 📌 Notes for Developers

- Preorder lifecycle is **driven by publication date**, not stock status.

- Backorder lifecycle is **driven by inventory + restock expectation**, not tags.

- Notify form logic must be tightly scoped to:

  - **In-stock status**

  - **Blacklist inclusion**

  - **Restock confirmation**

- Both services will include **Slack alert integrations**, **Supabase data logging**, and **Admin Dashboard visibility**.

# 📦 Backorder Service

The **Backorder Service** monitors inventory fluctuations and customer interest for **temporarily out-of-stock books** that are still in print and expected to restock.

It integrates with:
- `webhook-gateway`: Shopify data ingestion
- `Request Service`: Supabase-based logging of Notify Me requests
- `Admin Dashboard`: Visibility into customer interest and restock triggers

---

## 📦 Input Data

### ✅ Webhooks Ingested
- `inventory_levels/update`
- `products/update`
- `variants/update`

### ✅ Shopify Data Fields Used
| Field/Source                       | Purpose                            |
|------------------------------------|------------------------------------|
| `Track quantity`                   | Required to determine stock status |
| `Inventory level`                  | Drives in/out of backorder state   |
| `Continue selling when out of stock` | Used to permit/disallow backorder |
| `Tag: out-of-print` (optional)     | Exclusion logic for OOP books      |

---

## 🔄 Lifecycle Logic

| Condition                                | Action                               |
|------------------------------------------|--------------------------------------|
| Inventory ≤ 0 and continue selling = ON  | Mark as backorderable                |
| Inventory ≤ 0 and continue selling = OFF | Mark as out of stock                 |
| Inventory restocked > 0                  | Notify customers, close requests     |
| Notify form submission                   | Log to Supabase with `product_id`    |

---

## 🧾 Notify Me Logic

| Phase         | Behavior                                  |
|---------------|-------------------------------------------|
| Product OOS   | Form appears (unless blacklisted)         |
| Submission    | Sent to `request_service.requests` table  |
| Admin review  | View and manage via Admin Dashboard       |
| Restock       | Manual notifications (automated = future) |
| Closeout      | Mark request `status: closed`             |

> ⚠️ Future automation via webhook will allow real-time restock detection + customer contact.

---

## 🧠 Admin Dashboard UI

| Component        | Functionality                           |
|------------------|-----------------------------------------|
| Request Service  | View requests by product, status        |
| Blacklist Toggle | Prevent Notify form from appearing      |
| Slack Notification Log | Coming soon                        |

---

## 🛠️ TODO / Future

- [ ] Slack alerts when restock triggers active requests
- [ ] Supabase webhook on `request.status = open` to detect stale entries
- [ ] Bulk CSV upload for historical request import
- [ ] Integration with Preorder Service for dual-status SKUs
