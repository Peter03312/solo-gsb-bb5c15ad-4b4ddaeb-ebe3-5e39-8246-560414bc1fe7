"""PostgreSQL persistence: frozen plans (idempotency) and stock-batch inventory.

Inventory model
---------------
* ``batches``         - registered stock batches; ``reserved_nl`` is held by
                        not-yet-confirmed plans, ``used_nl`` by confirmed ones.
                        Invariant: ``reserved_nl + used_nl <= initial_nl``.
* ``inventory_plans`` - lifecycle of a feasible inventory-mode plan:
                        reserved -> confirmed | cancelled.
* ``reservations``    - per (plan, batch) reserved amount in nl.

All multi-row mutations happen inside transactions guarded by atomic
conditional UPDATEs, so concurrent requests can never oversell a batch and
repeated confirm/cancel calls converge to one final state.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

import asyncpg

# Errors raised while the server is starting / accepting connections but the
# database / role is not ready yet.
_CONNECT_ERRORS = (OSError, asyncpg.PostgresError, ConnectionError)


SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    request_id      TEXT PRIMARY KEY,
    request_hash    TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('feasible', 'infeasible')),
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id        TEXT PRIMARY KEY,
    concentration   TEXT NOT NULL,
    initial_nl      BIGINT NOT NULL CHECK (initial_nl > 0),
    reserved_nl     BIGINT NOT NULL DEFAULT 0 CHECK (reserved_nl >= 0),
    used_nl         BIGINT NOT NULL DEFAULT 0 CHECK (used_nl >= 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (reserved_nl + used_nl <= initial_nl)
);

CREATE TABLE IF NOT EXISTS inventory_plans (
    request_id      TEXT PRIMARY KEY REFERENCES plans(request_id),
    state           TEXT NOT NULL CHECK (state IN ('reserved', 'confirmed', 'cancelled')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reservations (
    request_id      TEXT NOT NULL REFERENCES inventory_plans(request_id),
    batch_id        TEXT NOT NULL REFERENCES batches(batch_id),
    amount_nl       BIGINT NOT NULL CHECK (amount_nl > 0),
    PRIMARY KEY (request_id, batch_id)
);
"""


class InsufficientInventory(Exception):
    """A batch could not cover the required reservation (concurrent change).

    ``shortages`` maps batch_id -> {"required_nl": int, "available_nl": int}.
    """

    def __init__(self, shortages: dict):
        super().__init__(f"insufficient inventory: {shortages}")
        self.shortages = shortages


async def connect(dsn: Optional[str] = None) -> asyncpg.Pool:
    """Create the connection pool and ensure the schema exists.

    A pool (not a single connection) is required so that concurrent requests
    can run independent transactions for atomic inventory reservation.
    ``asyncpg.Pool`` exposes the same ``fetchrow``/``fetchval``/``execute``
    coroutines used by the simple helpers below.
    """
    dsn = dsn or os.environ.get(
        "DATABASE_URL",
        "postgresql://planner:planner@db:5432/planner",
    )
    last_err = None
    for attempt in range(60):
        try:
            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
            break
        except _CONNECT_ERRORS as exc:  # database not ready yet
            last_err = exc
            await asyncio.sleep(1)
    else:
        raise RuntimeError(f"cannot reach database: {last_err}")
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
    return pool


# --------------------------------------------------------------------------- #
# Frozen plans (idempotency)
# --------------------------------------------------------------------------- #


async def get_frozen(conn, request_id: str):
    """Return (hash, status, payload dict) or None."""
    row = await conn.fetchrow(
        "SELECT request_hash, status, payload FROM plans WHERE request_id = $1",
        request_id,
    )
    if row is None:
        return None
    return row["request_hash"], row["status"], json.loads(row["payload"])


async def freeze(conn, request_id: str, request_hash: str,
                 status: str, payload: dict) -> None:
    await conn.execute(
        """
        INSERT INTO plans (request_id, request_hash, status, payload)
        VALUES ($1, $2, $3, $4::jsonb)
        ON CONFLICT (request_id) DO NOTHING
        """,
        request_id, request_hash, status, json.dumps(payload),
    )


# --------------------------------------------------------------------------- #
# Stock batches
# --------------------------------------------------------------------------- #


def _batch_dict(row) -> dict:
    return {
        "id": row["batch_id"],
        "concentration": row["concentration"],
        "initial_nl": row["initial_nl"],
        "reserved_nl": row["reserved_nl"],
        "used_nl": row["used_nl"],
        "available_nl": row["initial_nl"] - row["reserved_nl"] - row["used_nl"],
    }


async def insert_batch(db, batch_id: str, concentration: str, initial_nl: int):
    """Register a batch.  Returns (created, batch); on conflict ``created``
    is False and ``batch`` is the authoritative existing row."""
    row = await db.fetchrow(
        """
        INSERT INTO batches (batch_id, concentration, initial_nl)
        VALUES ($1, $2, $3)
        ON CONFLICT (batch_id) DO NOTHING
        RETURNING batch_id, concentration, initial_nl, reserved_nl, used_nl
        """,
        batch_id, concentration, initial_nl,
    )
    if row is not None:
        return True, _batch_dict(row)
    return False, await get_batch(db, batch_id)


async def get_batch(db, batch_id: str):
    row = await db.fetchrow(
        """
        SELECT batch_id, concentration, initial_nl, reserved_nl, used_nl
        FROM batches WHERE batch_id = $1
        """,
        batch_id,
    )
    return _batch_dict(row) if row is not None else None


async def get_batches(db, batch_ids) -> list:
    rows = await db.fetch(
        """
        SELECT batch_id, concentration, initial_nl, reserved_nl, used_nl
        FROM batches WHERE batch_id = ANY($1::text[])
        """,
        list(batch_ids),
    )
    return [_batch_dict(r) for r in rows]


async def list_batches(db) -> list:
    rows = await db.fetch(
        """
        SELECT batch_id, concentration, initial_nl, reserved_nl, used_nl
        FROM batches ORDER BY batch_id
        """
    )
    return [_batch_dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Inventory-mode plans: atomic reservation + confirm/cancel lifecycle
# --------------------------------------------------------------------------- #


async def freeze_inventory_plan(db, request_id: str, request_hash: str,
                                payload: dict, usage: dict) -> bool:
    """Freeze a feasible inventory-mode plan and reserve its stock draws.

    ``usage`` maps batch_id -> nl drawn from that batch by the plan.  The
    plan row, lifecycle row, reservation rows and the ``reserved_nl``
    increments commit in one transaction.

    Returns True when this call created the plan; False when another request
    froze the same ``request_id`` first (nothing was reserved).  Raises
    :class:`InsufficientInventory` when a batch cannot cover its draw -
    the whole transaction rolls back, so no inventory is held and the
    request id stays unused.
    """
    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO plans (request_id, request_hash, status, payload)
                VALUES ($1, $2, 'feasible', $3::jsonb)
                ON CONFLICT (request_id) DO NOTHING
                RETURNING request_id
                """,
                request_id, request_hash, json.dumps(payload),
            )
            if row is None:
                return False
            await conn.execute(
                "INSERT INTO inventory_plans (request_id, state) "
                "VALUES ($1, 'reserved')",
                request_id,
            )
            for batch_id in sorted(usage):
                amount = usage[batch_id]
                if amount <= 0:
                    continue
                # Atomic guard: the WHERE clause is re-evaluated against the
                # latest committed row after any lock wait, so concurrent
                # reservations can never oversell the batch.
                upd = await conn.fetchrow(
                    """
                    UPDATE batches
                    SET reserved_nl = reserved_nl + $2
                    WHERE batch_id = $1
                      AND initial_nl - used_nl - reserved_nl >= $2
                    RETURNING batch_id
                    """,
                    batch_id, amount,
                )
                if upd is None:
                    available = await conn.fetchval(
                        "SELECT initial_nl - used_nl - reserved_nl "
                        "FROM batches WHERE batch_id = $1",
                        batch_id,
                    )
                    raise InsufficientInventory({
                        batch_id: {"required_nl": amount,
                                   "available_nl": available},
                    })
                await conn.execute(
                    "INSERT INTO reservations (request_id, batch_id, amount_nl) "
                    "VALUES ($1, $2, $3)",
                    request_id, batch_id, amount,
                )
    return True


async def _reservation_rows(conn, request_id: str) -> list:
    rows = await conn.fetch(
        """
        SELECT batch_id, amount_nl FROM reservations
        WHERE request_id = $1 ORDER BY batch_id
        """,
        request_id,
    )
    return [{"batch_id": r["batch_id"], "amount_nl": r["amount_nl"]}
            for r in rows]


async def _transition(db, request_id: str, target: str):
    """Move a reservation from 'reserved' to ``target`` exactly once.

    Returns (state, reservations): the state after the call and the plan's
    reservation rows.  ``state`` is None when no inventory plan exists for
    the request id.  Concurrent / repeated calls converge: only the caller
    that flips 'reserved' -> ``target`` applies the inventory movement.
    """
    assert target in ("confirmed", "cancelled")
    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE inventory_plans
                SET state = $2, updated_at = now()
                WHERE request_id = $1 AND state = 'reserved'
                RETURNING request_id
                """,
                request_id, target,
            )
            if row is not None:
                reservations = await _reservation_rows(conn, request_id)
                for r in reservations:
                    if target == "confirmed":
                        # hold -> consumed: release the hold, count as used
                        await conn.execute(
                            """
                            UPDATE batches
                            SET reserved_nl = reserved_nl - $2,
                                used_nl = used_nl + $2
                            WHERE batch_id = $1
                            """,
                            r["batch_id"], r["amount_nl"],
                        )
                    else:
                        # hold -> released back to available, exactly once
                        await conn.execute(
                            """
                            UPDATE batches
                            SET reserved_nl = reserved_nl - $2
                            WHERE batch_id = $1
                            """,
                            r["batch_id"], r["amount_nl"],
                        )
                return target, reservations
            cur = await conn.fetchrow(
                "SELECT state FROM inventory_plans WHERE request_id = $1",
                request_id,
            )
            if cur is None:
                return None, None
            return cur["state"], await _reservation_rows(conn, request_id)


async def confirm_reservation(db, request_id: str):
    return await _transition(db, request_id, "confirmed")


async def cancel_reservation(db, request_id: str):
    return await _transition(db, request_id, "cancelled")


async def get_reservation_state(db, request_id: str):
    """Return (state, reservations) or (None, None) when unknown."""
    row = await db.fetchrow(
        "SELECT state FROM inventory_plans WHERE request_id = $1",
        request_id,
    )
    if row is None:
        return None, None
    return row["state"], await _reservation_rows(db, request_id)
