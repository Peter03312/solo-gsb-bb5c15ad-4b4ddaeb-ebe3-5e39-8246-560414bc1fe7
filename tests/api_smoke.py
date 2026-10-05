"""API integration tests without a real PostgreSQL server.

A tiny in-memory connection stand-in implements the few methods the app uses
(fetchval/fetchrow/execute), so the full FastAPI stack incl. validation,
idempotency and conflict handling is exercised.
"""
import json
import sys

sys.path.insert(0, ".")

from fastapi.testclient import TestClient

import app.db as db
import app.main as main


class FakeConn:
    def __init__(self):
        self.rows = {}

    async def fetchval(self, q):
        return 1

    async def close(self):
        pass

    async def fetchrow(self, q, request_id):
        row = self.rows.get(request_id)
        if row is None:
            return None
        import asyncpg
        return asyncpg.Record  # placeholder (never used as such)

    async def execute(self, q, *args):
        if q.startswith("CREATE TABLE"):
            return
        if q.startswith("INSERT"):
            request_id, rhash, status, payload = args
            if request_id not in self.rows:
                self.rows[request_id] = (rhash, status, json.loads(payload))


# monkey-patch get_frozen/freeze to use the in-memory store
_store = {}
_batches = {}
_invs = {}


async def fake_get_frozen(conn, request_id):
    return _store.get(request_id)


async def fake_freeze(conn, request_id, rhash, status, payload):
    if request_id not in _store:
        _store[request_id] = (rhash, status, payload)


async def fake_connect(dsn=None):
    return FakeConn()


def _batch_view(b):
    return {**b, "available_nl":
            b["initial_nl"] - b["reserved_nl"] - b["used_nl"]}


async def fake_insert_batch(conn, batch_id, concentration, initial_nl):
    if batch_id in _batches:
        return False, _batch_view(_batches[batch_id])
    _batches[batch_id] = {"id": batch_id, "concentration": concentration,
                          "initial_nl": initial_nl,
                          "reserved_nl": 0, "used_nl": 0}
    return True, _batch_view(_batches[batch_id])


async def fake_get_batch(conn, batch_id):
    b = _batches.get(batch_id)
    return _batch_view(b) if b else None


async def fake_get_batches(conn, ids):
    return [_batch_view(_batches[i]) for i in ids if i in _batches]


async def fake_list_batches(conn):
    return [_batch_view(b) for _, b in sorted(_batches.items())]


async def fake_freeze_inventory(conn, request_id, rhash, payload, usage):
    if request_id in _store:
        return False
    for bid, amount in usage.items():
        if amount <= 0:
            continue
        b = _batches[bid]
        avail = b["initial_nl"] - b["used_nl"] - b["reserved_nl"]
        if avail < amount:
            raise db.InsufficientInventory(
                {bid: {"required_nl": amount, "available_nl": avail}})
    for bid, amount in usage.items():
        if amount > 0:
            _batches[bid]["reserved_nl"] += amount
    _store[request_id] = (rhash, "feasible", payload)
    _invs[request_id] = {
        "state": "reserved",
        "reservations": [{"batch_id": k, "amount_nl": v}
                         for k, v in sorted(usage.items()) if v > 0],
    }
    return True


async def _fake_transition(conn, request_id, target):
    rec = _invs.get(request_id)
    if rec is None:
        return None, None
    if rec["state"] == "reserved":
        for r in rec["reservations"]:
            b = _batches[r["batch_id"]]
            b["reserved_nl"] -= r["amount_nl"]
            if target == "confirmed":
                b["used_nl"] += r["amount_nl"]
        rec["state"] = target
    return rec["state"], rec["reservations"]


async def fake_confirm(conn, request_id):
    return await _fake_transition(conn, request_id, "confirmed")


async def fake_cancel(conn, request_id):
    return await _fake_transition(conn, request_id, "cancelled")


async def fake_get_state(conn, request_id):
    rec = _invs.get(request_id)
    if rec is None:
        return None, None
    return rec["state"], rec["reservations"]


db.connect = fake_connect
db.get_frozen = fake_get_frozen
db.freeze = fake_freeze
main.connect = fake_connect
main.get_frozen = fake_get_frozen
main.freeze = fake_freeze
for mod in (db, main):
    mod.insert_batch = fake_insert_batch
    mod.get_batch = fake_get_batch
    mod.get_batches = fake_get_batches
    mod.list_batches = fake_list_batches
    mod.freeze_inventory_plan = fake_freeze_inventory
    mod.confirm_reservation = fake_confirm
    mod.cancel_reservation = fake_cancel
    mod.get_reservation_state = fake_get_state

client = TestClient(main.app)


GOOD = {
    "stocks": [{"id": "S1", "concentration": "100", "available_nl": 1_000_000}],
    "diluent": {"available_nl": 10_000_000},
    "wells": [{
        "id": "A1", "final_volume_nl": 1000,
        "final_concentration": "40",
    }],
    "containers": [],
    "pipette": {"min_nl": 100, "max_nl": 1000, "step_nl": 50},
    "default_well_capacity_nl": 5000,
}


def test_feasible():
    r = client.post("/plans", json=GOOD, headers={"X-Request-ID": "req-1"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "feasible"
    total = sum(t["volume_nl"] for t in body["plan"]["transfers"])
    assert total == 1000
    wells = body["plan"]["wells"]
    assert wells[0]["final_concentration"] == "40"
    print("feasible -> 201,", len(body["plan"]["transfers"]), "transfers")


def test_idempotent_retry():
    r = client.post("/plans", json=GOOD, headers={"X-Request-ID": "req-1"})
    assert r.status_code == 200, r.text
    print("same retry -> 200 frozen plan")


def test_conflict():
    changed = json.loads(json.dumps(GOOD))
    changed["wells"][0]["final_concentration"] = "30"
    r = client.post("/plans", json=changed, headers={"X-Request-ID": "req-1"})
    assert r.status_code == 409, r.text
    print("different content same id -> 409")


def test_get_plan():
    r = client.get("/plans/req-1")
    assert r.status_code == 200
    r = client.get("/plans/nope")
    assert r.status_code == 404
    print("GET frozen 200 / missing 404")


def test_infeasible_frozen():
    bad = json.loads(json.dumps(GOOD))
    bad["stocks"][0]["available_nl"] = 50
    bad["wells"][0]["final_concentration"] = "90"
    r = client.post("/plans", json=bad, headers={"X-Request-ID": "req-2"})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["status"] == "infeasible"
    assert body["blocked"]
    print("infeasible -> 422, blocked:", body["blocked"])
    r2 = client.post("/plans", json=bad, headers={"X-Request-ID": "req-2"})
    assert r2.status_code == 422
    print("infeasible retry -> 422 frozen")


def test_bad_float_concentration():
    bad = json.loads(json.dumps(GOOD))
    bad["wells"][0]["final_concentration"] = 0.5
    r = client.post("/plans", json=bad, headers={"X-Request-ID": "req-3"})
    assert r.status_code == 400
    print("float concentration -> 400")


def test_bad_volume():
    bad = json.loads(json.dumps(GOOD))
    bad["wells"][0]["final_volume_nl"] = -5
    r = client.post("/plans", json=bad, headers={"X-Request-ID": "req-4"})
    assert r.status_code == 400
    print("negative volume -> 400")


def test_bad_pipette_range():
    bad = json.loads(json.dumps(GOOD))
    bad["pipette"]["min_nl"] = 2000
    r = client.post("/plans", json=bad, headers={"X-Request-ID": "req-5"})
    assert r.status_code == 400
    print("pipette min > max -> 400")


def test_missing_request_id():
    r = client.post("/plans", json=GOOD)
    assert r.status_code == 400, r.text
    print("missing X-Request-ID -> 400")


def test_rational_concentration():
    body = json.loads(json.dumps(GOOD))
    # rational 25 = "50/2" string form: high share 250 nl, diluent 750 nl
    body["wells"][0]["final_concentration"] = "50/2"
    r = client.post("/plans", json=body, headers={"X-Request-ID": "req-6"})
    assert r.status_code == 201, r.text
    shares = sorted(t["volume_nl"] for t in r.json()["plan"]["transfers"])
    assert shares == [250, 750]
    print("50/2 rational-string target -> 201 with shares 250/750 nl")


def test_serial_via_api():
    body = {
        "stocks": [{"id": "S1", "concentration": "1000",
                    "available_nl": 1_000_000}],
        "diluent": {"available_nl": 10_000_000},
        "wells": [{"id": "A1", "final_volume_nl": 1000,
                   "final_concentration": "100"}],
        "containers": [{"id": "tube-1", "capacity_nl": 10_000}],
        "pipette": {"min_nl": 500, "max_nl": 1000, "step_nl": 100},
        "default_well_capacity_nl": 5000,
    }
    r = client.post("/plans", json=body, headers={"X-Request-ID": "req-7"})
    assert r.status_code == 201, r.text
    plan = r.json()["plan"]
    assert plan["intermediates"][0]["container"] == "tube-1"
    assert plan["intermediates"][0]["concentration"] == "100"
    print("serial dilution via API -> 201, container label tube-1")


# --------------------------------------------------------------------------- #
# Inventory mode: batches, atomic reservation, confirm/cancel
# --------------------------------------------------------------------------- #

INV_BODY = {
    "batch_ids": ["B1"],
    "diluent": {"available_nl": 10_000_000},
    "wells": [{"id": "A1", "final_volume_nl": 1000,
               "final_concentration": "40"}],
    "containers": [],
    "pipette": {"min_nl": 100, "max_nl": 1000, "step_nl": 50},
    "default_well_capacity_nl": 5000,
}


def test_register_batch():
    r = client.post("/batches", json={"id": "B1", "concentration": "100",
                                      "initial_nl": 1_000_000})
    assert r.status_code == 201, r.text
    assert r.json()["available_nl"] == 1_000_000
    r = client.post("/batches", json={"id": "B1", "concentration": "100",
                                      "initial_nl": 1_000_000})
    assert r.status_code == 200  # idempotent re-registration
    r = client.post("/batches", json={"id": "B1", "concentration": "50",
                                      "initial_nl": 1_000_000})
    assert r.status_code == 409  # same id, different content
    print("batch register -> 201, same -> 200, different -> 409")


def test_inventory_plan_and_reservation():
    r = client.post("/plans", json=INV_BODY, headers={"X-Request-ID": "inv-1"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["mode"] == "inventory" and body["state"] == "reserved"
    assert body["reservations"] == [{"batch_id": "B1", "reserved_nl": 400}]
    assert _batches["B1"]["reserved_nl"] == 400
    print("inventory plan -> 201, reserved 400 nl of B1")

    r = client.post("/plans", json=INV_BODY, headers={"X-Request-ID": "inv-1"})
    assert r.status_code == 200 and _batches["B1"]["reserved_nl"] == 400
    print("same id+body retry -> 200, no double reservation")

    changed = json.loads(json.dumps(INV_BODY))
    changed["wells"][0]["final_concentration"] = "30"
    r = client.post("/plans", json=changed, headers={"X-Request-ID": "inv-1"})
    assert r.status_code == 409
    print("same id, different body -> 409")

    r = client.get("/plans/inv-1/reservation")
    assert r.status_code == 200 and r.json()["state"] == "reserved"
    print("GET /plans/inv-1/reservation -> state reserved")


def test_unknown_batch_and_insufficient():
    r = client.post("/plans", json=dict(INV_BODY, batch_ids=["ghost"]),
                    headers={"X-Request-ID": "inv-x"})
    assert r.status_code == 400 and "ghost" in r.json()["detail"]
    print("unknown batch -> 400, nothing reserved")

    small = json.loads(json.dumps(INV_BODY))
    small["batch_ids"] = ["B2"]
    small["wells"][0]["final_concentration"] = "40"  # needs 400 of B2
    client.post("/batches", json={"id": "B2", "concentration": "100",
                                  "initial_nl": 300})
    r = client.post("/plans", json=small, headers={"X-Request-ID": "inv-y"})
    assert r.status_code == 422
    assert r.json()["inventory"]["B2"]["available_nl"] == 300
    assert _batches["B2"]["reserved_nl"] == 0
    print("insufficient inventory -> 422, nothing reserved")


def test_confirm_and_cancel_lifecycle():
    # confirm: reserved -> used, cannot cancel afterwards
    r = client.post("/plans/inv-1/confirm")
    assert r.status_code == 200 and r.json()["state"] == "confirmed"
    assert _batches["B1"]["used_nl"] == 400
    assert _batches["B1"]["reserved_nl"] == 0
    r = client.post("/plans/inv-1/confirm")
    assert r.status_code == 200  # repeated confirm: same final state
    assert _batches["B1"]["used_nl"] == 400
    r = client.post("/plans/inv-1/cancel")
    assert r.status_code == 409  # confirmed cannot be cancelled
    print("confirm -> used 400; repeat confirm ok; cancel after confirm -> 409")

    # cancel: releases the hold exactly once
    r = client.post("/plans", json=INV_BODY, headers={"X-Request-ID": "inv-2"})
    assert r.status_code == 201
    r = client.post("/plans/inv-2/cancel")
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert _batches["B1"]["reserved_nl"] == 0
    r = client.post("/plans/inv-2/cancel")
    assert r.status_code == 200  # repeated cancel releases nothing more
    assert _batches["B1"]["reserved_nl"] == 0
    r = client.post("/plans/inv-2/confirm")
    assert r.status_code == 409  # cancelled cannot be confirmed
    print("cancel -> released once; repeat cancel ok; confirm after cancel -> 409")


if __name__ == "__main__":
    with client:
        test_feasible()
        test_idempotent_retry()
        test_conflict()
        test_get_plan()
        test_infeasible_frozen()
        test_bad_float_concentration()
        test_bad_volume()
        test_bad_pipette_range()
        test_missing_request_id()
        test_rational_concentration()
        test_serial_via_api()
        test_register_batch()
        test_inventory_plan_and_reservation()
        test_unknown_batch_and_insufficient()
        test_confirm_and_cancel_lifecycle()
    print("\nALL API TESTS PASSED")
