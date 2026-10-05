"""HTTP API for microplate dilution planning.

Two planning modes share ``POST /plans`` and the X-Request-ID idempotency
namespace:

* classic   - inline ``stocks``; the plan is computed and frozen, nothing else.
* inventory - ``batch_ids`` referencing registered batches; the volumes the
              plan actually draws from each batch are reserved atomically and
              the reservation can later be confirmed (counted as used) or
              cancelled (released back to available, exactly once).
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .db import (
    InsufficientInventory,
    cancel_reservation,
    confirm_reservation,
    connect,
    freeze,
    freeze_inventory_plan,
    get_batch,
    get_batches,
    get_frozen,
    get_reservation_state,
    insert_batch,
    list_batches,
)
from .models import BatchIn, PlanRequest
from .planning import (
    build_problem,
    container_ids,
    cross_validate_references,
    relabel_plan,
    request_hash,
    stock_usage,
)
from .solver import solve


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await connect()
    yield
    await app.state.db.close()


app = FastAPI(
    title="Microplate Dilution Planner",
    version="1.1.0",
    description=(
        "Computes an exact, executable pipetting plan for microplate wells. "
        "Volumes are integer nanoliters; concentrations are rationals. "
        "Plans may draw from registered stock batches with atomic, "
        "confirmable/cancellable inventory reservations."
    ),
    lifespan=lifespan,
)


def _safe_errors(errors):
    """``exc.errors()`` may carry raw exception objects in ``ctx``; make the
    details JSON-serializable so invalid input always gets a clean 400."""
    safe = []
    for err in errors:
        err = dict(err)
        ctx = err.get("ctx")
        if ctx:
            err["ctx"] = {
                k: (str(v) if isinstance(v, Exception) else v)
                for k, v in ctx.items()
            }
        safe.append(err)
    return safe


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={"error": "invalid_request", "detail": _safe_errors(exc.errors())},
    )


def _bad_request(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "invalid_request", "detail": detail},
    )


def _id_conflict() -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "idempotency_conflict",
            "detail": "X-Request-ID already frozen with different content",
        },
    )


@app.get("/health")
async def health():
    row = await app.state.db.fetchval("SELECT 1")
    return {"status": "ok" if row == 1 else "degraded"}


# --------------------------------------------------------------------------- #
# Stock batches (inventory)
# --------------------------------------------------------------------------- #


@app.post("/batches")
async def register_batch(req: BatchIn):
    created, batch = await insert_batch(
        app.state.db, req.id, req.concentration, req.initial_nl)
    if created:
        return JSONResponse(status_code=201, content=batch)
    if (batch["concentration"] == req.concentration
            and batch["initial_nl"] == req.initial_nl):
        # idempotent re-registration of the same batch
        return JSONResponse(status_code=200, content=batch)
    return JSONResponse(
        status_code=409,
        content={
            "error": "batch_conflict",
            "detail": f"batch {req.id} already registered with different "
                      "concentration or initial_nl",
            "batch": batch,
        },
    )


@app.get("/batches")
async def read_batches():
    return {"batches": await list_batches(app.state.db)}


@app.get("/batches/{batch_id}")
async def read_batch(batch_id: str):
    batch = await get_batch(app.state.db, batch_id)
    if batch is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found",
                     "detail": f"unknown batch {batch_id}"},
        )
    return batch


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #


@app.post("/plans")
async def create_plan(
    req: PlanRequest,
    request_id: str = Header(..., alias="X-Request-ID"),
):
    if not request_id or not request_id.strip():
        return _bad_request("X-Request-ID header is required")
    request_id = request_id.strip()

    try:
        cross_validate_references(req)
    except ValueError as exc:
        return _bad_request(str(exc))

    inventory_mode = req.batch_ids is not None
    batches = []
    if inventory_mode:
        batches = await get_batches(app.state.db, req.batch_ids)
        known = {b["id"] for b in batches}
        missing = [b for b in req.batch_ids if b not in known]
        if missing:
            return _bad_request(
                "unknown batch id(s): " + ", ".join(sorted(missing)))

    rhash = request_hash(req)
    frozen = await get_frozen(app.state.db, request_id)
    if frozen is not None:
        old_hash, status, payload = frozen
        if old_hash != rhash:
            return _id_conflict()
        code = 200 if status == "feasible" else 422
        return JSONResponse(status_code=code, content=payload)

    prob = build_problem(req, batches if inventory_mode else None)
    result = solve(prob)

    if result.ok:
        plan = relabel_plan(result.plan, container_ids(req))
        if inventory_mode:
            return await _freeze_inventory_plan(request_id, rhash, plan)
        payload = {"status": "feasible", "request_id": request_id, "plan": plan}
        await freeze(app.state.db, request_id, rhash, "feasible", payload)
        # A concurrent request with the same id may have inserted first.
        # Re-read the authoritative frozen row to honor conflict semantics.
        frozen = await get_frozen(app.state.db, request_id)
        if frozen is not None and frozen[0] != rhash:
            return _id_conflict()
        return JSONResponse(status_code=201, content=payload)

    if inventory_mode:
        # Inventory failures are NOT frozen: availability is transient, a
        # later retry with the same id may succeed.  Nothing is reserved.
        payload = {
            "status": "infeasible",
            "mode": "inventory",
            "request_id": request_id,
            "blocked": result.failure.blocked,
            "reason": result.failure.reason,
            "constraints": result.failure.detail,
            "inventory": {b["id"]: b for b in batches},
        }
        return JSONResponse(status_code=422, content=payload)

    payload = {
        "status": "infeasible",
        "request_id": request_id,
        "blocked": result.failure.blocked,
        "reason": result.failure.reason,
        "constraints": result.failure.detail,
    }
    await freeze(app.state.db, request_id, rhash, "infeasible", payload)
    frozen = await get_frozen(app.state.db, request_id)
    if frozen is not None and frozen[0] != rhash:
        return _id_conflict()
    return JSONResponse(status_code=422, content=payload)


async def _freeze_inventory_plan(request_id: str, rhash: str,
                                 plan: dict) -> JSONResponse:
    """Reserve the plan's per-batch draws and freeze the creation result."""
    usage = stock_usage(plan)
    reservations = [
        {"batch_id": batch_id, "reserved_nl": amount}
        for batch_id, amount in sorted(usage.items())
    ]
    payload = {
        "status": "feasible",
        "mode": "inventory",
        "request_id": request_id,
        "state": "reserved",
        "plan": plan,
        "reservations": reservations,
    }
    try:
        created = await freeze_inventory_plan(
            app.state.db, request_id, rhash, payload, usage)
    except InsufficientInventory as exc:
        # Lost a race against concurrent reservations; nothing was held.
        return JSONResponse(
            status_code=422,
            content={
                "status": "infeasible",
                "mode": "inventory",
                "request_id": request_id,
                "reason": "insufficient_inventory",
                "shortage": exc.shortages,
                "detail": "available inventory changed concurrently; "
                          "nothing was reserved",
            },
        )
    if not created:
        # A concurrent request froze this id first; it holds the reservation.
        frozen = await get_frozen(app.state.db, request_id)
        if frozen is not None and frozen[0] != rhash:
            return _id_conflict()
        if frozen is not None:
            return JSONResponse(status_code=200, content=frozen[2])
        return _id_conflict()
    return JSONResponse(status_code=201, content=payload)


@app.get("/plans/{request_id}")
async def read_plan(request_id: str):
    frozen = await get_frozen(app.state.db, request_id)
    if frozen is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found",
                     "detail": f"no frozen plan for request {request_id}"},
        )
    _, status, payload = frozen
    code = 200 if status == "feasible" else 422
    return JSONResponse(status_code=code, content=payload)


# --------------------------------------------------------------------------- #
# Reservation lifecycle: confirm / cancel
# --------------------------------------------------------------------------- #


async def _no_reservation_response(request_id: str) -> JSONResponse:
    frozen = await get_frozen(app.state.db, request_id)
    if frozen is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found",
                     "detail": f"no plan for request {request_id}"},
        )
    return JSONResponse(
        status_code=400,
        content={"error": "not_reservable",
                 "detail": f"plan {request_id} holds no inventory reservation"},
    )


@app.post("/plans/{request_id}/confirm")
async def confirm_plan(request_id: str):
    state, reservations = await confirm_reservation(app.state.db, request_id)
    if state is None:
        return await _no_reservation_response(request_id)
    if state == "cancelled":
        return JSONResponse(
            status_code=409,
            content={"error": "state_conflict",
                     "detail": "reservation was cancelled and cannot be "
                               "confirmed",
                     "state": "cancelled"},
        )
    # Idempotent: confirming an already-confirmed plan yields the same state.
    return JSONResponse(
        status_code=200,
        content={
            "request_id": request_id,
            "state": "confirmed",
            "reservations": reservations,
            "detail": "reservation confirmed; volumes counted as used",
        },
    )


@app.post("/plans/{request_id}/cancel")
async def cancel_plan(request_id: str):
    state, reservations = await cancel_reservation(app.state.db, request_id)
    if state is None:
        return await _no_reservation_response(request_id)
    if state == "confirmed":
        return JSONResponse(
            status_code=409,
            content={"error": "state_conflict",
                     "detail": "reservation already confirmed (counted as "
                               "used) and cannot be cancelled",
                     "state": "confirmed"},
        )
    # Idempotent: cancelling an already-cancelled plan releases nothing more.
    return JSONResponse(
        status_code=200,
        content={
            "request_id": request_id,
            "state": "cancelled",
            "reservations": reservations,
            "detail": "reservation cancelled; volumes released back to "
                      "available",
        },
    )


@app.get("/plans/{request_id}/reservation")
async def read_reservation(request_id: str):
    state, reservations = await get_reservation_state(app.state.db, request_id)
    if state is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found",
                     "detail": f"no inventory reservation for request "
                               f"{request_id}"},
        )
    return {"request_id": request_id, "state": state,
            "reservations": reservations}
