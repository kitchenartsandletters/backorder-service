## 📙 Backorder Book

### ✅ Definition

A backorder book is a **post-release-date** title that is **temporarily out of stock** but still **in print and expected to restock**. Customers may be allowed to order or request notification. Backorders are distinct from other book/product statuses (i.e. preorder, out-of-print, requests, etc) and involves a committed quantity and sales deficit currently owed to the customer.

---

### 🔧 Shopify Implementation

#### Inventory Settings

- `Track quantity`: ✅ Enabled

- `Inventory` (at the time of sale): ≤ `0`

- `Continue selling when out of stock`: ✅ Must be ON for a book to be **considered backorderable**

  - If OFF, the book is either **Out of Stock** or **Out of Print** (OOP).
  - A Backorder sale event may uncover an "Temporarily OOS" or OOP status that previous went undetected

#### Cart Behavior

- If `Continue selling when out of stock` is OFF, default Shopify prevents checkout.

- If ON, cart is allowed and product is treated as **backordered**.

#### Restock Ops

- Upon restock:

  - Backorders are filled chronologically as fresh inventory allows

- ⚠️ Automated backorder customer notifications, alerts as well as back office operations notifications and alerts are a **planned future enhancement**.

---

## 🔗 System Integration Plan

| Component               | Role                                                   |

|------------------------|--------------------------------------------------------|

| `Preorder Service`     | Lifecycle management of preorder SKUs. Negative inventory or committed quantities are handled distinctly and separately from the Backorder Service |

| `Backorder Service`    | Will track committed sales quantities, stock restoration, and notification queueing. This is distinct from the preorder service |

| `webhook-gateway`      | Ingests product and inventory updates for all services. |

---

## 📌 Notes for Developers

- Preorder lifecycle is **driven by publication date**, not stock status.

- Backorder lifecycle is **driven by inventory + restock expectation**, not tags.

- The backorder service will make use of **Mailtrap API integrations**, **Supabase data logging**, and **Admin Dashboard visibility**.

# 📦 Backorder Service

The **Backorder Service** monitors inventory fluctuations for **temporarily out-of-stock books** that are still in print and expected to restock.

It integrates with:
- `webhook-gateway`: Shopify data ingestion
- `Admin Dashboard`: Visibility into customer interest and restock triggers

---

## 📦 Input Data

### ✅ Webhooks Ingested

- Order creation
https://webhook-gateway-production.up.railway.app/webhooks/shopify • JSON
- Order fulfillment
https://webhook-gateway-production.up.railway.app/webhooks/shopify • JSON
- Order cancellation
https://webhook-gateway-production.up.railway.app/webhooks/shopify • JSON
- Refund create
https://webhook-gateway-production.up.railway.app/webhooks/shopify • JSON
- Inventory level update
https://webhook-gateway-production.up.railway.app/webhooks/shopify • JSON
- Product update
https://webhook-gateway-production.up.railway.app/webhooks/shopify • JSON
- Order payment
https://webhook-gateway-production.up.railway.app/webhooks/shopify • JSON
- Product creation
https://webhook-gateway-production.up.railway.app/webhooks/shopify • JSON

### ✅ Shopify Data Fields Used
- see apps-inventory-mgmt.md for GraphQL data handling.

## 🔄 Lifecycle Logic

| Condition                                | Action                               |
|------------------------------------------|--------------------------------------|
| Inventory ≤ 0 and continue selling = ON  | Mark as backorderable                |
| Inventory ≤ 0 and continue selling = OFF | Out of stock; orders not allowed     |
| Inventory restocked                      | Inventory levels are not reliable    |
|                                          | since partial receipts could         |
|                                          | potentially cover only some of the   |
|                                          | live backorders. Inventory is        |
|                                          | received and then backorders are     |
|                                          | fulfilled. Committed quantity is only|
|                                          | affected on the order level so is a  |
|                                          | stronger indication of an order's    |
|                                          | backorder status.                    |

---

## 🧠 Admin Dashboard UI

- refer to github repo admin-dashboard.