import os

import httpx

API = os.environ.get("API_URL", "http://localhost:8000")


def test_health():
    assert httpx.get(f"{API}/health").json() == {"ok": True}


def test_refund_creates_record():
    r = httpx.post(f"{API}/refunds", json={"order_id": 3, "amount_cents": 500},
                   headers={"Idempotency-Key": "t-basic-1"})
    assert r.status_code == 201
    assert r.json()["amount_cents"] == 500
