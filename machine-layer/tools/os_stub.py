#!/usr/bin/env python3
# Copyright 2026 Sunnybotics.
# Licensed under the Apache License, Version 2.0.
"""A stand-in for the SunnyboticsOS API, for testing the outbound push.

    python3 tools/os_stub.py --port 9000

This is a **test double, not an implementation**. It accepts the three calls the
adapter makes, records them, and answers with the documented success shapes. It
has no database, no Mission Engine and no matching logic.

It exists so the push direction can be exercised before the real OS is built,
and so the receiving end of the contract is written down as running code rather
than only as prose. Anything building the real server can point this repository
at it instead and expect the same three calls in the same order.

    POST   /api/v0/machines/register
    PATCH  /api/v0/machines/{machine_id}/status
    POST   /api/v0/missions/{mission_id}/report

Plus one endpoint that is *not* part of the contract, for assertions:

    GET    /_captured        everything received so far
"""

from __future__ import annotations

import argparse
import threading
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI

app = FastAPI(title="SunnyboticsOS API (stub)", version="0.1.0")

_lock = threading.Lock()
_events: list[dict[str, Any]] = []
_machines: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _record(kind: str, **fields: Any) -> None:
    with _lock:
        _events.append({"kind": kind, "at": _now(), **fields})


@app.post("/api/v0/machines/register", status_code=201)
def register(body: dict[str, Any]) -> dict[str, Any]:
    machine_id = body.get("machine_id", "")
    with _lock:
        _machines[machine_id] = body
    _record("register", machine_id=machine_id, body=body)
    print(
        f"[stub] register  {machine_id:<14} "
        f"type={body.get('machine_type')} caps={body.get('capabilities')}",
        flush=True,
    )
    return {
        "machine_id": machine_id,
        "state": body.get("state", "AVAILABLE"),
        "registered_at": _now(),
    }


@app.patch("/api/v0/machines/{machine_id}/status")
def patch_status(machine_id: str, body: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        known = machine_id in _machines
        if known:
            _machines[machine_id].update(body)
    _record("status", machine_id=machine_id, body=body, known=known)
    return {"machine_id": machine_id, "state": body.get("state")}


@app.post("/api/v0/missions/{mission_id}/report")
def report(mission_id: str, body: dict[str, Any]) -> dict[str, Any]:
    _record("report", mission_id=mission_id, body=body)
    print(
        f"[stub] report    {mission_id:<16} {body.get('status'):<10} "
        f"{body.get('progress_percent')}%  {body.get('detail', '')[:60]}",
        flush=True,
    )
    return {"mission_id": mission_id, "status": body.get("status")}


@app.get("/_captured")
def captured() -> dict[str, Any]:
    """Not part of the contract. Exists so a test can assert on what arrived."""
    with _lock:
        return {
            "machines": sorted(_machines),
            "counts": {
                kind: sum(1 for e in _events if e["kind"] == kind)
                for kind in ("register", "status", "report")
            },
            "events": list(_events),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    print(f"[stub] SunnyboticsOS API stub on http://{args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
