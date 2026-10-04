"""HTTP API for microplate dilution planning."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .db import connect, freeze, get_frozen
from .models import PlanRequest
from .planning import (
    build_problem,
    container_ids,
    cross_validate_references,
    relabel_plan,
    request_hash,
)
from .solver import solve


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await connect()
    yield
    await app.state.db.close()


app = FastAPI(
    title="Microplate Dilution Planner",
    version="1.0.0",
    description=(
        "Computes an exact, executable pipetting plan for microplate wells. "
        "Volumes are integer nanoliters; concentrations are rationals."
    ),
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={"error": "invalid_request", "detail": exc.errors()},
    )


@app.get("/health")
async def health():
    row = await app.state.db.fetchval("SELECT 1")
    return {"status": "ok" if row == 1 else "degraded"}


@app.post("/plans")
async def create_plan(
    req: PlanRequest,
    request_id: str = Header(..., alias="X-Request-ID"),
):
    if not request_id or not request_id.strip():
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request",
                     "detail": "X-Request-ID header is required"},
        )
    request_id = request_id.strip()

    try:
        cross_validate_references(req)
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "detail": str(exc)},
        )

    rhash = request_hash(req)
    frozen = await get_frozen(app.state.db, request_id)
    if frozen is not None:
        old_hash, status, payload = frozen
        if old_hash != rhash:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "idempotency_conflict",
                    "detail": (
                        "X-Request-ID already frozen with different content"),
                },
            )
        code = 200 if status == "feasible" else 422
        return JSONResponse(status_code=code, content=payload)

    prob = build_problem(req)
    result = solve(prob)

    if result.ok:
        plan = relabel_plan(result.plan, container_ids(req))
        payload = {"status": "feasible", "request_id": request_id, "plan": plan}
        await freeze(app.state.db, request_id, rhash, "feasible", payload)
        # A concurrent request with the same id may have inserted first.
        # Re-read the authoritative frozen row to honor conflict semantics.
        frozen = await get_frozen(app.state.db, request_id)
        if frozen is not None and frozen[0] != rhash:
            return JSONResponse(
                status_code=409,
                content={"error": "idempotency_conflict",
                         "detail": "X-Request-ID frozen concurrently with "
                                   "different content"},
            )
        return JSONResponse(status_code=201, content=payload)

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
        return JSONResponse(
            status_code=409,
            content={"error": "idempotency_conflict",
                     "detail": "X-Request-ID frozen concurrently with "
                               "different content"},
        )
    return JSONResponse(status_code=422, content=payload)


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
