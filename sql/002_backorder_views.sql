-- Applied to supply-chain-service (khpxjdwunjkrfbbaqsqp) as migration: backorder_views_and_urgency
-- Read layer: product/order overviews + urgency scoring for the heatmap.
-- Joins live supply-chain PO data so the dashboard answers
-- "has it been ordered, when is it expected".

create or replace view backorder.vw_product_overview
with (security_invoker = on) as
with po as (
  select
    pol.variant_id as variant_id_text,
    sum(greatest(pol.quantity_ordered - pol.quantity_received - pol.quantity_cancelled, 0)) as on_order_qty,
    min(p.expected_at) as next_expected_at,
    array_agg(distinct p.po_number) as po_numbers
  from public.purchase_order_lines pol
  join public.purchase_orders p on p.id = pol.purchase_order_id
  where p.status in ('draft','submitted','confirmed','partial')
    and p.archived_at is null
    and coalesce(p.is_test, false) = false
    and pol.status in ('open','partial','backordered')
  group by pol.variant_id
),
notif as (
  select
    product_id,
    max(last_customer_notified_at) as last_customer_notified_at,
    count(*) filter (where status in ('open','partial') and last_customer_notified_at is null) as unnotified_open_lines
  from backorder.order_lines
  group by product_id
),
lead as (
  select sp.variant_id as variant_id_text, min(sp.lead_time_days) as lead_time_days
  from public.supplier_products sp
  where sp.is_active and sp.lead_time_days is not null
  group by sp.variant_id
)
select
  ps.product_id,
  ps.variant_id,
  ps.inventory_item_id,
  ps.sku,
  ps.title,
  ps.vendor,
  ps.tags,
  ps.available,
  ps.inventory_policy,
  ps.tracked,
  ps.status,
  ps.open_backorder_qty,
  ps.open_orders_count,
  ps.oldest_open_order_at,
  ps.last_restock_at,
  ps.updated_at,
  coalesce(po.on_order_qty, 0)        as on_order_qty,
  po.next_expected_at,
  po.po_numbers,
  lead.lead_time_days,
  n.last_customer_notified_at,
  coalesce(n.unnotified_open_lines, 0) as unnotified_open_lines,
  greatest(0, floor(extract(epoch from (now() - ps.oldest_open_order_at)) / 86400))::int as days_open,
  least(100, round(
      least(greatest(extract(epoch from (now() - coalesce(ps.oldest_open_order_at, now()))) / 86400.0, 0), 30) / 30.0 * 40
    + least(ps.open_backorder_qty, 10) / 10.0 * 20
    + case
        when coalesce(po.on_order_qty, 0) <= 0 then 25
        when po.next_expected_at is not null and po.next_expected_at < now() then 15
        else 0
      end
    + case when coalesce(n.unnotified_open_lines, 0) > 0 then 15 else 0 end
  ))::int as urgency_score,
  case
    when least(100, round(
        least(greatest(extract(epoch from (now() - coalesce(ps.oldest_open_order_at, now()))) / 86400.0, 0), 30) / 30.0 * 40
      + least(ps.open_backorder_qty, 10) / 10.0 * 20
      + case
          when coalesce(po.on_order_qty, 0) <= 0 then 25
          when po.next_expected_at is not null and po.next_expected_at < now() then 15
          else 0
        end
      + case when coalesce(n.unnotified_open_lines, 0) > 0 then 15 else 0 end
    )) >= 70 then 'critical'
    when least(100, round(
        least(greatest(extract(epoch from (now() - coalesce(ps.oldest_open_order_at, now()))) / 86400.0, 0), 30) / 30.0 * 40
      + least(ps.open_backorder_qty, 10) / 10.0 * 20
      + case
          when coalesce(po.on_order_qty, 0) <= 0 then 25
          when po.next_expected_at is not null and po.next_expected_at < now() then 15
          else 0
        end
      + case when coalesce(n.unnotified_open_lines, 0) > 0 then 15 else 0 end
    )) >= 45 then 'high'
    when least(100, round(
        least(greatest(extract(epoch from (now() - coalesce(ps.oldest_open_order_at, now()))) / 86400.0, 0), 30) / 30.0 * 40
      + least(ps.open_backorder_qty, 10) / 10.0 * 20
      + case
          when coalesce(po.on_order_qty, 0) <= 0 then 25
          when po.next_expected_at is not null and po.next_expected_at < now() then 15
          else 0
        end
      + case when coalesce(n.unnotified_open_lines, 0) > 0 then 15 else 0 end
    )) >= 25 then 'medium'
    else 'low'
  end as urgency_bucket
from backorder.product_state ps
left join po   on po.variant_id_text = ps.variant_id::text
left join notif n on n.product_id = ps.product_id
left join lead on lead.variant_id_text = ps.variant_id::text;

create or replace view backorder.vw_order_overview
with (security_invoker = on) as
select
  ol.order_id,
  ol.order_name,
  ol.customer_id,
  ol.customer_email,
  min(ol.order_created_at) as order_created_at,
  count(*)                 as backorder_lines,
  sum(ol.qty_backordered)  as total_backordered,
  sum(ol.open_qty)         as open_qty,
  bool_or(ol.status in ('open','partial')) as has_open,
  max(ol.last_customer_notified_at) as last_customer_notified_at,
  max(ol.resolved_at)      as resolved_at,
  greatest(0, floor(extract(epoch from (now() - min(ol.order_created_at))) / 86400))::int as days_open
from backorder.order_lines ol
group by ol.order_id, ol.order_name, ol.customer_id, ol.customer_email;

grant select on backorder.vw_product_overview to service_role;
grant select on backorder.vw_order_overview to service_role;
