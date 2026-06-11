-- Applied to supply-chain-service (khpxjdwunjkrfbbaqsqp) as migration: backorder_schema_core
-- Kept in-repo for reference/replay. See README for context.

create schema if not exists backorder;

create table backorder.events (
  event_id    uuid primary key,
  topic       text not null,
  shop_domain text,
  payload     jsonb not null,
  headers     jsonb,
  received_at timestamptz not null default now(),
  processed_at timestamptz,
  processing_error text
);
create index idx_backorder_events_topic on backorder.events (topic, received_at desc);

create table backorder.commitment_ledger (
  id              bigint generated always as identity primary key,
  event_id        uuid not null,
  topic           text not null,
  reason          text not null check (reason in
                    ('order_created','order_backfill','fulfilled','cancelled',
                     'refunded','manual_adjustment','reconciliation')),
  occurred_at     timestamptz not null,
  order_id        bigint not null,
  order_name      text,
  customer_id     bigint,
  customer_email  text,
  line_item_id    bigint not null,
  product_id      bigint,
  variant_id      bigint,
  inventory_item_id bigint,
  sku             text,
  title           text,
  delta_qty       integer not null,
  created_at      timestamptz not null default now(),
  unique (event_id, line_item_id, reason)
);
create index idx_bo_ledger_product on backorder.commitment_ledger (product_id);
create index idx_bo_ledger_order   on backorder.commitment_ledger (order_id);
create index idx_bo_ledger_variant on backorder.commitment_ledger (variant_id);

create table backorder.order_lines (
  order_id        bigint not null,
  line_item_id    bigint not null,
  order_name      text,
  customer_id     bigint,
  customer_email  text,
  product_id      bigint,
  variant_id      bigint,
  inventory_item_id bigint,
  sku             text,
  title           text,
  qty_backordered int not null default 0,
  qty_fulfilled   int not null default 0,
  qty_refunded    int not null default 0,
  qty_cancelled   int not null default 0,
  open_qty        int not null default 0,
  status          text not null default 'open'
                    check (status in ('open','partial','resolved','cancelled')),
  order_created_at timestamptz,
  resolved_at     timestamptz,
  last_customer_notified_at timestamptz,
  notification_count int not null default 0,
  updated_at      timestamptz not null default now(),
  primary key (order_id, line_item_id)
);
create index idx_bo_lines_product on backorder.order_lines (product_id);
create index idx_bo_lines_status  on backorder.order_lines (status);

create table backorder.product_state (
  product_id      bigint primary key,
  variant_id      bigint,
  inventory_item_id bigint,
  sku             text,
  title           text,
  vendor          text,
  tags            text[] not null default '{}',
  available       int,
  inventory_policy text,
  tracked         boolean,
  status          text not null default 'backorderable'
                    check (status in ('backorderable','temporarily_oos','oop_suspect','restock_pending','resolved')),
  open_backorder_qty int not null default 0,
  open_orders_count  int not null default 0,
  oldest_open_order_at timestamptz,
  last_restock_at  timestamptz,
  last_event_at    timestamptz,
  updated_at       timestamptz not null default now()
);

create table backorder.actions (
  id          uuid primary key default gen_random_uuid(),
  scope       text not null check (scope in ('product','order','order_line')),
  product_id  bigint,
  order_id    bigint,
  line_item_id bigint,
  action_type text not null check (action_type in
                ('po_created','po_linked','vendor_inquiry','eta_updated',
                 'customer_notified','note','status_override')),
  details     jsonb,
  purchase_order_id uuid references public.purchase_orders(id),
  eta_date    date,
  actor       text,
  created_at  timestamptz not null default now()
);
create index idx_bo_actions_product on backorder.actions (product_id, created_at desc);
create index idx_bo_actions_order   on backorder.actions (order_id, created_at desc);

create table backorder.tagger_run_log (
  id          uuid primary key default gen_random_uuid(),
  ran_at      timestamptz not null default now(),
  orders_scanned int,
  orders_tagged  int,
  orders_untagged int,
  errors      jsonb,
  duration_seconds numeric,
  tagger_version text
);
create table backorder.tagger_processed_orders (
  order_id    bigint primary key,
  order_name  text,
  last_action text check (last_action in ('tagged','untagged','noop')),
  tags_after  text[],
  processed_at timestamptz not null default now()
);

create table backorder.reconciliation_log (
  id          uuid primary key default gen_random_uuid(),
  ran_at      timestamptz not null default now(),
  product_id  bigint,
  variant_id  bigint,
  ledger_open_qty int,
  shopify_committed int,
  shopify_available int,
  delta       int,
  flagged     boolean not null default false,
  notes       text
);

alter table backorder.events enable row level security;
alter table backorder.commitment_ledger enable row level security;
alter table backorder.order_lines enable row level security;
alter table backorder.product_state enable row level security;
alter table backorder.actions enable row level security;
alter table backorder.tagger_run_log enable row level security;
alter table backorder.tagger_processed_orders enable row level security;
alter table backorder.reconciliation_log enable row level security;

grant usage on schema backorder to service_role;
grant all on all tables in schema backorder to service_role;
grant usage, select on all sequences in schema backorder to service_role;
alter default privileges in schema backorder grant all on tables to service_role;
alter default privileges in schema backorder grant usage, select on sequences to service_role;
