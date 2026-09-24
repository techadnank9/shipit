"""Refunds service: the demo 'customer system' Carbon Copy tests against."""
import json
import os
import uuid

import boto3
import psycopg
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

DATABASE_URL = os.environ["DATABASE_URL"]
S3_ENDPOINT = os.environ.get("S3_ENDPOINT")  # LocalStack inside the copy
RECEIPTS_BUCKET = os.environ.get("RECEIPTS_BUCKET", "refund-receipts")
MAX_REFUND_CENTS = 50_000  # $500 cap per refund

app = FastAPI(title="Refunds")


def db():
    return psycopg.connect(DATABASE_URL, autocommit=True)


def s3():
    return boto3.client("s3", endpoint_url=S3_ENDPOINT, region_name="us-west-1")


class RefundIn(BaseModel):
    order_id: int
    amount_cents: int


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/orders")
def orders():
    with db() as c:
        rows = c.execute(
            "select o.id, o.customer, o.amount_cents, coalesce(sum(r.amount_cents),0) "
            "from orders o left join refunds r on r.order_id=o.id group by o.id order by o.id"
        ).fetchall()
    return [
        {"id": r[0], "customer": r[1], "amount_cents": r[2], "refunded_cents": r[3]}
        for r in rows
    ]


def _existing(c, key: str):
    row = c.execute(
        "select id, order_id, amount_cents from refunds where idempotency_key=%s", (key,)
    ).fetchone()
    return row and {"id": str(row[0]), "order_id": row[1], "amount_cents": row[2]}


@app.post("/refunds", status_code=201)
def create_refund(body: RefundIn, response: Response, idempotency_key: str | None = Header(default=None)):
    if not idempotency_key:
        raise HTTPException(400, "Idempotency-Key header is required")
    if body.amount_cents <= 0:
        raise HTTPException(422, "Refund amount must be positive")
    if body.amount_cents > MAX_REFUND_CENTS:
        raise HTTPException(422, "Refund exceeds $500 limit")
    with db() as c:
        if prior := _existing(c, idempotency_key):
            response.status_code = 200  # retry of a refund we already made
            return prior
        order = c.execute(
            "select id, amount_cents from orders where id=%s", (body.order_id,)
        ).fetchone()
        if not order:
            raise HTTPException(404, "order not found")
        refunded = c.execute(
            "select coalesce(sum(amount_cents),0) from refunds where order_id=%s", (body.order_id,)
        ).fetchone()[0]
        if refunded + body.amount_cents > order[1]:
            raise HTTPException(422, "Refund exceeds order total")
        refund_id = str(uuid.uuid4())
        try:
            c.execute(
                "insert into refunds (id, order_id, amount_cents, idempotency_key) values (%s,%s,%s,%s)",
                (refund_id, body.order_id, body.amount_cents, idempotency_key),
            )
        except psycopg.errors.UniqueViolation:  # concurrent retry won the race
            response.status_code = 200
            return _existing(c, idempotency_key)
    s3().put_object(
        Bucket=RECEIPTS_BUCKET,
        Key=f"receipts/{refund_id}.json",
        Body=json.dumps({"refund_id": refund_id, **body.model_dump()}),
    )
    return {"id": refund_id, "order_id": body.order_id, "amount_cents": body.amount_cents}


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Refunds desk</title>
<style>body{font:15px system-ui;margin:40px;max-width:720px}table{border-collapse:collapse;width:100%}
td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left}button{padding:6px 12px}
#msg{margin-top:16px;font-weight:600}</style></head><body>
<h1>Refunds desk</h1>
<table><thead><tr><th>Order</th><th>Customer</th><th>Total</th><th>Refunded</th><th>Amount $</th><th></th></tr></thead>
<tbody id="rows"></tbody></table><div id="msg"></div>
<script>
const keys = {};
async function load(){
  const orders = await (await fetch('/orders')).json();
  document.getElementById('rows').innerHTML = orders.map(o => `<tr>
    <td>#${o.id}</td><td>${o.customer}</td><td>$${(o.amount_cents/100).toFixed(2)}</td>
    <td data-testid="refunded-${o.id}">$${(o.refunded_cents/100).toFixed(2)}</td>
    <td><input id="amt-${o.id}" aria-label="Refund amount for order ${o.id}" size="6"></td>
    <td><button onclick="refund(${o.id})">Refund order ${o.id}</button></td></tr>`).join('');
}
async function refund(id){
  keys[id] = keys[id] || crypto.randomUUID();   // one key per refund attempt, reused on retry
  const dollars = parseFloat(document.getElementById('amt-'+id).value || '0');
  const r = await fetch('/refunds', {method:'POST', headers:{'Content-Type':'application/json','Idempotency-Key':keys[id]},
    body: JSON.stringify({order_id:id, amount_cents: Math.round(dollars*100)})});
  const body = await r.json();
  document.getElementById('msg').textContent = r.ok ? 'Refund issued' : ('Refund failed: ' + (body.detail || r.status));
  load();
}
load();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def page():
    return PAGE
