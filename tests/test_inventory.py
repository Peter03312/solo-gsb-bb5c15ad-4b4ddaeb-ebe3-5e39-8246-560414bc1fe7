"""Inventory-mode tests: batches, atomic reservation, confirm/cancel.

Uses the same in-memory stand-in approach as test_api.py; the fakes mirror
the real db.py semantics (atomic check-and-set, single transition per plan).
"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import app.db as db
import app.main as main


@pytest.fixture
def inv(monkeypatch):
    plans = {}    # request_id -> (hash, status, payload)
    batches = {}  # id -> mutable batch dict
    invs = {}     # request_id -> {"state", "reservations"}

    def view(b):
        return {**b, "available_nl":
                b["initial_nl"] - b["reserved_nl"] - b["used_nl"]}

    class FakeConn:
        async def fetchval(self, q):
            return 1

        async def close(self):
            pass

    async def fake_connect(dsn=None):
        return FakeConn()

    async def fake_get_frozen(conn, request_id):
        return plans.get(request_id)

    async def fake_freeze(conn, request_id, rhash, status, payload):
        plans.setdefault(request_id, (rhash, status, payload))

    async def fake_insert_batch(conn, batch_id, concentration, initial_nl):
        if batch_id in batches:
            return False, view(batches[batch_id])
        batches[batch_id] = {"id": batch_id, "concentration": concentration,
                             "initial_nl": initial_nl,
                             "reserved_nl": 0, "used_nl": 0}
        return True, view(batches[batch_id])

    async def fake_get_batch(conn, batch_id):
        b = batches.get(batch_id)
        return view(b) if b else None

    async def fake_get_batches(conn, ids):
        # Snapshot first, then yield: concurrent requests all solve against
        # the same pre-reservation snapshot, so the atomic guard in
        # freeze_inventory_plan is what actually serializes them.
        snapshot = [view(batches[i]) for i in ids if i in batches]
        await asyncio.sleep(0)
        return snapshot

    async def fake_list_batches(conn):
        return [view(b) for _, b in sorted(batches.items())]

    async def fake_freeze_inventory(conn, request_id, rhash, payload, usage):
        if request_id in plans:
            return False
        # atomic check-and-set: no awaits between check and mutation
        for bid, amount in usage.items():
            if amount <= 0:
                continue
            b = batches[bid]
            avail = b["initial_nl"] - b["used_nl"] - b["reserved_nl"]
            if avail < amount:
                raise db.InsufficientInventory(
                    {bid: {"required_nl": amount, "available_nl": avail}})
        for bid, amount in usage.items():
            if amount > 0:
                batches[bid]["reserved_nl"] += amount
        plans[request_id] = (rhash, "feasible", payload)
        invs[request_id] = {
            "state": "reserved",
            "reservations": [{"batch_id": k, "amount_nl": v}
                             for k, v in sorted(usage.items()) if v > 0],
        }
        return True

    async def fake_transition(conn, request_id, target):
        rec = invs.get(request_id)
        if rec is None:
            return None, None
        if rec["state"] == "reserved":
            for r in rec["reservations"]:
                b = batches[r["batch_id"]]
                b["reserved_nl"] -= r["amount_nl"]
                if target == "confirmed":
                    b["used_nl"] += r["amount_nl"]
            rec["state"] = target
        return rec["state"], rec["reservations"]

    async def fake_confirm(conn, request_id):
        return await fake_transition(conn, request_id, "confirmed")

    async def fake_cancel(conn, request_id):
        return await fake_transition(conn, request_id, "cancelled")

    async def fake_get_state(conn, request_id):
        rec = invs.get(request_id)
        if rec is None:
            return None, None
        return rec["state"], rec["reservations"]

    for mod in (db, main):
        monkeypatch.setattr(mod, "connect", fake_connect)
        monkeypatch.setattr(mod, "get_frozen", fake_get_frozen)
        monkeypatch.setattr(mod, "freeze", fake_freeze)
        monkeypatch.setattr(mod, "insert_batch", fake_insert_batch)
        monkeypatch.setattr(mod, "get_batch", fake_get_batch)
        monkeypatch.setattr(mod, "get_batches", fake_get_batches)
        monkeypatch.setattr(mod, "list_batches", fake_list_batches)
        monkeypatch.setattr(mod, "freeze_inventory_plan", fake_freeze_inventory)
        monkeypatch.setattr(mod, "confirm_reservation", fake_confirm)
        monkeypatch.setattr(mod, "cancel_reservation", fake_cancel)
        monkeypatch.setattr(mod, "get_reservation_state", fake_get_state)

    with TestClient(main.app) as c:
        yield SimpleNamespace(client=c, plans=plans, batches=batches, invs=invs)


BATCH = {"id": "B1", "concentration": "100", "initial_nl": 1_000_000}

INV_BODY = {
    "batch_ids": ["B1"],
    "diluent": {"available_nl": 10_000_000},
    "wells": [{"id": "A1", "final_volume_nl": 1000,
               "final_concentration": "40"}],
    "containers": [],
    "pipette": {"min_nl": 100, "max_nl": 1000, "step_nl": 50},
    "default_well_capacity_nl": 5000,
}

CLASSIC_BODY = {
    "stocks": [{"id": "S1", "concentration": "100", "available_nl": 1_000_000}],
    "diluent": {"available_nl": 10_000_000},
    "wells": [{"id": "A1", "final_volume_nl": 1000,
               "final_concentration": "40"}],
    "containers": [],
    "pipette": {"min_nl": 100, "max_nl": 1000, "step_nl": 50},
    "default_well_capacity_nl": 5000,
}


def make_inv(inv, rid="inv-1", body=None):
    return inv.client.post("/plans", json=body or INV_BODY,
                           headers={"X-Request-ID": rid})


# --------------------------------------------------------------------------- #
# Batch registration
# --------------------------------------------------------------------------- #


def test_register_batch_and_reread(inv):
    r = inv.client.post("/batches", json=BATCH)
    assert r.status_code == 201
    body = r.json()
    assert body["id"] == "B1"
    assert body["available_nl"] == 1_000_000
    assert body["reserved_nl"] == 0 and body["used_nl"] == 0

    r = inv.client.get("/batches/B1")
    assert r.status_code == 200 and r.json()["initial_nl"] == 1_000_000
    r = inv.client.get("/batches")
    assert [b["id"] for b in r.json()["batches"]] == ["B1"]
    assert inv.client.get("/batches/nope").status_code == 404


def test_batch_registration_idempotent_and_conflict(inv):
    assert inv.client.post("/batches", json=BATCH).status_code == 201
    r = inv.client.post("/batches", json=BATCH)
    assert r.status_code == 200  # same content -> idempotent re-read
    changed = dict(BATCH, initial_nl=500)
    r = inv.client.post("/batches", json=changed)
    assert r.status_code == 409
    assert r.json()["error"] == "batch_conflict"
    changed = dict(BATCH, concentration="50")
    assert inv.client.post("/batches", json=changed).status_code == 409


def test_batch_validation(inv):
    assert inv.client.post(
        "/batches", json=dict(BATCH, concentration=0.5)).status_code == 400
    assert inv.client.post(
        "/batches", json=dict(BATCH, initial_nl=0)).status_code == 400
    assert inv.client.post(
        "/batches", json=dict(BATCH, id="@bad")).status_code == 400


# --------------------------------------------------------------------------- #
# Inventory-mode planning
# --------------------------------------------------------------------------- #


def test_inventory_plan_reserves(inv):
    inv.client.post("/batches", json=BATCH)
    r = make_inv(inv)
    assert r.status_code == 201
    body = r.json()
    assert body["mode"] == "inventory"
    assert body["state"] == "reserved"
    # 1000 nl @ 40 from a 100-conc batch -> 400 nl drawn from B1
    assert body["reservations"] == [{"batch_id": "B1", "reserved_nl": 400}]
    drawn = sum(t["volume_nl"] for t in body["plan"]["transfers"]
                if t["source"]["kind"] == "stock")
    assert drawn == 400
    b = inv.batches["B1"]
    assert b["reserved_nl"] == 400
    assert b["initial_nl"] - b["reserved_nl"] - b["used_nl"] == 999_600


def test_inventory_retry_and_conflict(inv):
    inv.client.post("/batches", json=BATCH)
    r1 = make_inv(inv)
    assert r1.status_code == 201
    r2 = make_inv(inv)
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    # retry did not reserve twice
    assert inv.batches["B1"]["reserved_nl"] == 400

    changed = json.loads(json.dumps(INV_BODY))
    changed["wells"][0]["final_concentration"] = "30"
    r3 = make_inv(inv, body=changed)
    assert r3.status_code == 409
    assert r3.json()["error"] == "idempotency_conflict"
    assert inv.batches["B1"]["reserved_nl"] == 400


def test_unknown_batch_rejected(inv):
    r = make_inv(inv, body=dict(INV_BODY, batch_ids=["ghost"]))
    assert r.status_code == 400
    assert "ghost" in r.json()["detail"]
    assert inv.batches == {} and inv.plans == {}


def test_mode_exclusivity(inv):
    both = dict(INV_BODY, stocks=CLASSIC_BODY["stocks"])
    assert make_inv(inv, body=both).status_code == 400
    neither = {k: v for k, v in INV_BODY.items() if k != "batch_ids"}
    assert make_inv(inv, body=neither).status_code == 400
    dup = dict(INV_BODY, batch_ids=["B1", "B1"])
    assert make_inv(inv, body=dup).status_code == 400


def test_insufficient_inventory_422_and_not_frozen(inv):
    inv.client.post("/batches", json=dict(BATCH, initial_nl=300))
    # needs 400 nl of B1 but only 300 available -> solver stock_overdraft
    r = make_inv(inv)
    assert r.status_code == 422
    body = r.json()
    assert body["mode"] == "inventory"
    assert body["inventory"]["B1"]["available_nl"] == 300
    assert inv.batches["B1"]["reserved_nl"] == 0
    # failure is not frozen: a smaller follow-up with the same id succeeds
    smaller = json.loads(json.dumps(INV_BODY))
    smaller["wells"][0]["final_concentration"] = "20"  # needs 200 nl
    r2 = make_inv(inv, body=smaller)
    assert r2.status_code == 201
    assert inv.batches["B1"]["reserved_nl"] == 200


def test_inventory_infeasible_structural(inv):
    inv.client.post("/batches", json=BATCH)
    bad = json.loads(json.dumps(INV_BODY))
    bad["wells"][0]["final_concentration"] = "150"  # above batch conc 100
    r = make_inv(inv, body=bad)
    assert r.status_code == 422
    assert "target_above_max_stock_concentration" in r.json()["constraints"]["global"]
    assert inv.batches["B1"]["reserved_nl"] == 0


def test_classic_mode_untouched_by_inventory(inv):
    inv.client.post("/batches", json=BATCH)
    r = inv.client.post("/plans", json=CLASSIC_BODY,
                        headers={"X-Request-ID": "classic-1"})
    assert r.status_code == 201
    assert "mode" not in r.json()
    assert "reservations" not in r.json()
    assert inv.batches["B1"]["reserved_nl"] == 0
    # classic infeasible results are still frozen (422 on retry)
    bad = json.loads(json.dumps(CLASSIC_BODY))
    bad["stocks"][0]["available_nl"] = 50
    bad["wells"][0]["final_concentration"] = "90"
    r = inv.client.post("/plans", json=bad, headers={"X-Request-ID": "cl-2"})
    assert r.status_code == 422
    assert inv.client.post("/plans", json=bad,
                           headers={"X-Request-ID": "cl-2"}).status_code == 422


# --------------------------------------------------------------------------- #
# Confirm / cancel lifecycle
# --------------------------------------------------------------------------- #


def test_confirm_flow(inv):
    inv.client.post("/batches", json=BATCH)
    make_inv(inv)
    r = inv.client.post("/plans/inv-1/confirm")
    assert r.status_code == 200
    assert r.json()["state"] == "confirmed"
    b = inv.batches["B1"]
    assert b["reserved_nl"] == 0 and b["used_nl"] == 400

    # repeated confirm: same final state, no double counting
    r = inv.client.post("/plans/inv-1/confirm")
    assert r.status_code == 200
    assert inv.batches["B1"]["used_nl"] == 400

    # confirmed cannot be cancelled
    r = inv.client.post("/plans/inv-1/cancel")
    assert r.status_code == 409
    assert r.json()["error"] == "state_conflict"
    assert inv.batches["B1"]["used_nl"] == 400


def test_cancel_flow(inv):
    inv.client.post("/batches", json=BATCH)
    make_inv(inv)
    r = inv.client.post("/plans/inv-1/cancel")
    assert r.status_code == 200
    assert r.json()["state"] == "cancelled"
    b = inv.batches["B1"]
    assert b["reserved_nl"] == 0 and b["used_nl"] == 0

    # repeated cancel returns inventory only once
    r = inv.client.post("/plans/inv-1/cancel")
    assert r.status_code == 200
    assert inv.batches["B1"]["reserved_nl"] == 0
    assert inv.batches["B1"]["initial_nl"] - inv.batches["B1"]["used_nl"] == 1_000_000

    # cancelled cannot be confirmed
    r = inv.client.post("/plans/inv-1/confirm")
    assert r.status_code == 409


def test_confirm_cancel_unknown_and_classic(inv):
    assert inv.client.post("/plans/ghost/confirm").status_code == 404
    assert inv.client.post("/plans/ghost/cancel").status_code == 404
    inv.client.post("/plans", json=CLASSIC_BODY,
                    headers={"X-Request-ID": "classic-9"})
    assert inv.client.post("/plans/classic-9/confirm").status_code == 400
    assert inv.client.post("/plans/classic-9/cancel").status_code == 400


def test_reservation_state_endpoint(inv):
    inv.client.post("/batches", json=BATCH)
    make_inv(inv)
    r = inv.client.get("/plans/inv-1/reservation")
    assert r.status_code == 200
    assert r.json()["state"] == "reserved"
    assert r.json()["reservations"] == [{"batch_id": "B1", "amount_nl": 400}]
    inv.client.post("/plans/inv-1/confirm")
    assert inv.client.get("/plans/inv-1/reservation").json()["state"] == "confirmed"
    assert inv.client.get("/plans/ghost/reservation").status_code == 404
    # frozen creation payload is still retrievable and unchanged
    r = inv.client.get("/plans/inv-1")
    assert r.status_code == 200 and r.json()["state"] == "reserved"


def test_multiple_batches_split_draw(inv):
    inv.client.post("/batches", json=BATCH)
    inv.client.post("/batches",
                    json={"id": "B2", "concentration": "100",
                          "initial_nl": 1_000_000})
    body = json.loads(json.dumps(INV_BODY))
    body["batch_ids"] = ["B1", "B2"]
    body["wells"] = [
        {"id": "A1", "final_volume_nl": 1000, "final_concentration": "40"},
        {"id": "A2", "final_volume_nl": 1000, "final_concentration": "100"},
    ]
    r = inv.client.post("/plans", json=body, headers={"X-Request-ID": "mb-1"})
    assert r.status_code == 201
    total = sum(x["reserved_nl"] for x in r.json()["reservations"])
    drawn = sum(t["volume_nl"] for t in r.json()["plan"]["transfers"]
                if t["source"]["kind"] == "stock")
    assert total == drawn == 1400  # 400 for A1 + 1000 pure stock for A2


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def _gather_posts(paths_bodies):
    async def run():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as ac:
            return await asyncio.gather(*[
                ac.post(path, json=body, headers=headers)
                for path, body, headers in paths_bodies
            ])
    return asyncio.run(run())


def test_concurrent_plans_never_oversell(inv):
    inv.client.post("/batches", json=dict(BATCH, initial_nl=10_000))
    body = json.loads(json.dumps(INV_BODY))
    body["default_well_capacity_nl"] = 20_000
    body["wells"] = [{"id": "A1", "final_volume_nl": 10_000,
                      "final_concentration": "60"}]  # draws 6000 of B1
    calls = [("/plans", body, {"X-Request-ID": f"c-{i}"}) for i in range(6)]
    responses = _gather_posts(calls)
    codes = sorted(r.status_code for r in responses)
    assert codes.count(201) == 1
    assert codes.count(422) == 5
    for r in responses:
        if r.status_code == 422:
            assert r.json()["reason"] == "insufficient_inventory"
    b = inv.batches["B1"]
    assert b["reserved_nl"] == 6000  # exactly one winner, no oversell


def test_concurrent_identical_retry_reserves_once(inv):
    inv.client.post("/batches", json=BATCH)
    calls = [("/plans", INV_BODY, {"X-Request-ID": "same-id"})] * 2
    responses = _gather_posts(calls)
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 201]
    payloads = [r.json() for r in responses]
    assert payloads[0] == payloads[1]
    assert inv.batches["B1"]["reserved_nl"] == 400  # reserved exactly once


def test_concurrent_confirm_is_idempotent(inv):
    inv.client.post("/batches", json=BATCH)
    make_inv(inv)
    calls = [("/plans/inv-1/confirm", None, {})] * 4
    responses = _gather_posts(calls)
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["state"] == "confirmed" for r in responses)
    b = inv.batches["B1"]
    assert b["reserved_nl"] == 0 and b["used_nl"] == 400


def test_concurrent_confirm_vs_cancel_single_winner(inv):
    inv.client.post("/batches", json=BATCH)
    make_inv(inv)
    calls = [("/plans/inv-1/confirm", None, {}),
             ("/plans/inv-1/cancel", None, {})]
    responses = _gather_posts(calls)
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 409]
    states = {r.json()["state"] for r in responses if r.status_code == 200}
    assert len(states) == 1
    final = inv.client.get("/plans/inv-1/reservation").json()["state"]
    assert final in states


# --------------------------------------------------------------------------- #
# Hash compatibility for classic requests
# --------------------------------------------------------------------------- #


def test_classic_request_hash_unchanged(inv):
    from app.models import PlanRequest
    from app.planning import canonical_request
    canon = canonical_request(PlanRequest(**CLASSIC_BODY))
    assert b"batch_ids" not in canon
    assert b'"stocks"' in canon
