create table orders (
  id serial primary key,
  customer text not null,
  amount_cents integer not null
);
create table refunds (
  id uuid primary key,
  order_id integer not null references orders(id),
  amount_cents integer not null,
  idempotency_key text,
  created_at timestamptz not null default now()
);
-- A retried refund must never create a second row
create unique index refunds_idempotency_key on refunds (idempotency_key);

-- Masked sample data (never real customer records)
insert into orders (customer, amount_cents) values
  ('customer_001', 12000),
  ('customer_002', 89900),
  ('customer_003', 4500);
