"""Convert validated HTTP models into the solver Problem and its answer back."""
from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from typing import Optional

from .models import PlanRequest
from .solver import Problem, Well


def _fraction_str(f: Fraction) -> str:
    return str(f)


def build_problem(req: PlanRequest, batches: Optional[list] = None) -> Problem:
    """Map a validated request to the solver Problem.

    ``batches`` is the list of registered batch rows (dicts with ``id``,
    ``concentration``, ``available_nl``) for inventory mode; when None the
    classic inline ``stocks`` of the request are used.
    """
    if batches is not None:
        stocks = {
            b["id"]: {"conc": Fraction(b["concentration"]),
                      "volume": b["available_nl"]}
            for b in batches
        }
    else:
        stocks = {
            s.id: {"conc": Fraction(s.concentration), "volume": s.available_nl}
            for s in req.stocks
        }
    wells = []
    for w in req.wells:
        cap = w.capacity_nl
        if cap is None:
            cap = req.default_well_capacity_nl
        wells.append(Well(
            id=w.id,
            volume=w.final_volume_nl,
            conc=Fraction(w.final_concentration),
            capacity=cap,
        ))
    containers = [c.capacity_nl for c in req.containers]
    return Problem(
        stocks=stocks,
        diluent_volume=req.diluent.available_nl,
        wells=wells,
        containers=containers,
        pip_min=req.pipette.min_nl,
        pip_max=req.pipette.max_nl,
        pip_step=req.pipette.step_nl,
        max_depth=req.limits.max_depth,
        max_search_nodes=req.limits.max_search_nodes,
        max_enumeration_units=req.limits.max_enumeration_units,
        max_factor=req.limits.max_factor,
    )


def container_ids(req: PlanRequest) -> list[str]:
    ids = []
    for i, c in enumerate(req.containers):
        ids.append(c.id if c.id is not None else f"C{i}")
    return ids


def relabel_plan(plan: dict, cids: list[str]) -> dict:
    """Map solver's C0/C1... container labels to user container ids."""
    for im in plan["intermediates"]:
        idx = int(im["container"][1:])
        im["container"] = cids[idx]
    for t in plan["transfers"]:
        for side in ("source", "destination"):
            lab = t[side]
            if lab.get("kind") == "intermediate" and lab.get("container"):
                idx = int(lab["container"][1:])
                lab["container"] = cids[idx]
    return plan


def cross_validate_references(req: PlanRequest) -> None:
    """Semantic validation that needs the whole request."""
    if (req.stocks is None) == (req.batch_ids is None):
        raise ValueError(
            "provide exactly one of 'stocks' (classic mode) or "
            "'batch_ids' (inventory mode)")
    if req.stocks is not None:
        stock_ids = [s.id for s in req.stocks]
        if len(set(stock_ids)) != len(stock_ids):
            raise ValueError("duplicate stock id")
    if req.batch_ids is not None:
        if len(set(req.batch_ids)) != len(req.batch_ids):
            raise ValueError("duplicate batch id")
    well_ids = [w.id for w in req.wells]
    if len(set(well_ids)) != len(well_ids):
        raise ValueError("duplicate well id")
    cids = [c.id for c in req.containers if c.id is not None]
    if len(cids) != len(set(cids)):
        raise ValueError("duplicate container id")
    p = req.pipette
    if p.min_nl > p.max_nl:
        raise ValueError("pipette.min_nl must be <= pipette.max_nl")
    if p.step_nl > (p.max_nl - 0):
        raise ValueError("pipette.step_nl must be <= pipette.max_nl")
    # there must be a step-aligned volume inside the range
    k0 = -((-p.min_nl) // p.step_nl)
    k1 = p.max_nl // p.step_nl
    if k0 > k1:
        raise ValueError(
            "no step-aligned transfer volume exists within pipette range")


def stock_usage(plan: dict) -> dict:
    """Total nl each stock (batch) actually gives up in the plan's transfers."""
    usage: dict = {}
    for t in plan["transfers"]:
        src = t["source"]
        if src.get("kind") == "stock":
            usage[src["id"]] = usage.get(src["id"], 0) + t["volume_nl"]
    return usage


def canonical_request(req: PlanRequest) -> bytes:
    """Stable canonical serialization used for idempotency conflict checks."""
    data = req.model_dump()
    # The two mode selectors are mutually exclusive; drop the absent one so
    # classic requests hash exactly as they did before inventory mode existed.
    if data.get("stocks") is None:
        data.pop("stocks", None)
    if data.get("batch_ids") is None:
        data.pop("batch_ids", None)

    def norm(x):
        if isinstance(x, Fraction):
            return {"__fraction__": str(x)}
        if isinstance(x, dict):
            return {k: norm(x[k]) for k in sorted(x)}
        if isinstance(x, list):
            return [norm(v) for v in x]
        return x

    return json.dumps(norm(data), sort_keys=True, separators=(",", ":")).encode()


def request_hash(req: PlanRequest) -> str:
    return hashlib.sha256(canonical_request(req)).hexdigest()
