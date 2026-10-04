"""PostgreSQL persistence for frozen plans (idempotency support)."""
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
"""


async def connect(dsn: Optional[str] = None) -> asyncpg.Connection:
    dsn = dsn or os.environ.get(
        "DATABASE_URL",
        "postgresql://planner:planner@db:5432/planner",
    )
    last_err = None
    for attempt in range(60):
        try:
            conn = await asyncpg.connect(dsn)
            break
        except _CONNECT_ERRORS as exc:  # database not ready yet
            last_err = exc
            await asyncio.sleep(1)
    else:
        raise RuntimeError(f"cannot reach database: {last_err}")
    await conn.execute(SCHEMA)
    return conn


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
