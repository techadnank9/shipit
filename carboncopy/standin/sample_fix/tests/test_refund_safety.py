import os
import uuid

import httpx

API = os.environ.get("API_URL", "http://localhost:8000")


def refund(order_id, cents, key):
    headers = {"Idempotency-Key": key} if key else {}
    return httpx.post(f"{API}/refunds", json={"order_id": order_id, "amount_cents": cents}, headers=headers)


def refunded(order_id):
    return next(o for o in httpx.get(f"{API}/orders").json() if o["id"] == order_id)["refunded_cents"]


def test_retry_with_same_key_refunds_once():
    key = f"retry-{uuid.uuid4()}"
    before = refunded(1)
    first, second = refund(1, 1000, key), refund(1, 1000, key)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert refunded(1) == before + 1000


def test_refund_over_500_is_blocked():
    r = refund(2, 60000, f"cap-{uuid.uuid4()}")
    assert r.status_code == 422
    assert "$500" in r.json()["detail"]


def test_missing_idempotency_key_is_rejected():
    assert refund(1, 100, None).status_code == 400


def test_refund_cannot_exceed_order_total():
    order = next(o for o in httpx.get(f"{API}/orders").json() if o["id"] == 1)
    remaining = order["amount_cents"] - order["refunded_cents"]
    r = refund(1, remaining + 1, f"total-{uuid.uuid4()}")  # one cent too many; creates nothing
    assert r.status_code == 422
    assert refunded(1) == order["refunded_cents"]
