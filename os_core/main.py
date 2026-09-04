"""
os_core/main.py  — SunnyboticsOS Core FastAPI application

Two sets of endpoints:

  PUSH (Abdel's adapter calls these):
    POST  /api/v0/machines/register           — machine announces itself
    PATCH /api/v0/machines/{id}/status        — heartbeat with partial state
    POST  /api/v0/missions/{id}/report        — mission progress / completion

  PULL (dashboard + operators call these):
    POST  /api/v0/missions                    — create + dispatch a mission
    GET   /api/v0/missions                    — list all missions
    GET   /api/v0/missions/{id}               — single mission
    GET   /api/v0/missions/{id}/events        — audit log
    GET   /api/v0/machines                    — registered machines
    GET   /api/v0/machines/{id}               — single machine
    GET   /health                             — liveness

Port: 9000  (Abdel's stub runs on 9000, so we match it)
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from typing import Optional, Dict, List

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from os_core.database import (
    init_db, get_connection,
    upsert_machine, patch_machine_status,
    get_all_machines, get_machine,
    insert_mission, update_mission,
    get_all_missions, get_mission, get_active_mission_ids,
    add_mission_event, get_mission_events,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("os_core")

# ── App ───────────────────────────────────────────────────────────────────────
API_PREFIX = "/api/v0"

app = FastAPI(
    title="SunnyboticsOS Core",
    version="0.1.0",
    description=(
        "The Mission Engine and persistence layer for SunnyboticsOS V0. "
        "Receives machine registrations and mission reports from the adapter (push), "
        "and exposes missions + machines to the dashboard and operators (pull)."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    init_db()
    log.info("SunnyboticsOS Core started — database ready")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _mission_id() -> str:
    return f"msn-{uuid.uuid4().hex[:10]}"


# ── Request / response models ─────────────────────────────────────────────────

class RegisterBody(BaseModel):
    machine_id:         str
    machine_type:       str
    capabilities:       List[str]
    state:              str = "AVAILABLE"
    health:             Dict[str, Any] = Field(default_factory=dict)
    location:           Optional[Dict[str, Any]] = None
    current_mission_id: Optional[str] = None


class StatusPatch(BaseModel):
    state:              Optional[str] = None
    health:             Optional[Dict[str, Any]] = None
    location:           Optional[Dict[str, Any]] = None
    current_mission_id: Optional[str] = None


class MissionReport(BaseModel):
    mission_id:         str
    status:             str          # "RUNNING" | "COMPLETED" | "EXCEPTION"
    detail:             str = ""
    timestamp:          Optional[str] = None
    progress_percent:   Optional[int] = None


class MissionRequest(BaseModel):
    """
    Create + dispatch a new mission.
    Maps 1:1 to the body the adapter's POST /api/v0/missions accepts,
    so the OS and adapter share the same vocabulary.
    """
    capability_required: str = Field(..., min_length=1)
    objective:           str = ""
    parameters:          Dict[str, Any] = Field(default_factory=dict)


# ── ADAPTER_URL: where the adapter (Abdel's) is running ──────────────────────
import os
ADAPTER_URL = os.environ.get("ADAPTER_URL", "http://localhost:8001")


# =============================================================================
# PUSH endpoints — Abdel's adapter calls these
# =============================================================================

@app.post(f"{API_PREFIX}/machines/register", status_code=201)
def register_machine(body: RegisterBody) -> dict[str, Any]:
    """
    Machine announces itself to the OS. Called by os_client.py on first heartbeat.
    """
    conn = get_connection()
    now = _now_iso()
    health = body.health or {}
    loc = body.location or {}

    machine_row = {
        "machine_id":           body.machine_id,
        "machine_type":         body.machine_type,
        "capabilities":         json.dumps(body.capabilities),
        "state":                body.state,
        "health_connected":     int(health.get("connected", True)),
        "health_battery_pct":   health.get("battery_pct"),
        "health_faults":        json.dumps(health.get("faults", [])),
        "location_x":           loc.get("x") if loc else None,
        "location_y":           loc.get("y") if loc else None,
        "location_frame":       loc.get("frame_id") if loc else None,
        "current_mission_id":   body.current_mission_id,
        "registered_at":        now,
        "last_seen_at":         now,
    }
    upsert_machine(conn, machine_row)
    log.info(
        "REGISTER  %-14s  type=%-18s  caps=%s",
        body.machine_id, body.machine_type, body.capabilities
    )
    return {
        "machine_id":    body.machine_id,
        "state":         body.state,
        "registered_at": now,
    }


@app.patch(f"{API_PREFIX}/machines/{{machine_id}}/status")
def patch_status(machine_id: str, body: StatusPatch) -> dict[str, Any]:
    """
    Heartbeat. Updates partial state of a registered machine.
    Called by os_client.py every 1 Hz (or whenever state changes).
    """
    conn = get_connection()
    now = _now_iso()
    patch = body.model_dump(exclude_none=True)
    ok = patch_machine_status(conn, machine_id, patch, now)
    if not ok:
        # Machine not registered yet — treat as a full registration with defaults
        register_machine(RegisterBody(
            machine_id=machine_id,
            machine_type="unknown",
            capabilities=[],
            state=patch.get("state", "AVAILABLE"),
            health=patch.get("health", {}),
            location=patch.get("location"),
            current_mission_id=patch.get("current_mission_id"),
        ))
    return {"machine_id": machine_id, "state": body.state}


@app.post(f"{API_PREFIX}/missions/{{mission_id}}/report")
def report_mission(mission_id: str, body: MissionReport) -> dict[str, Any]:
    """
    Mission progress update. Called by os_client.py on each feedback/result.
    status: "RUNNING" | "COMPLETED" | "EXCEPTION"
    """
    conn = get_connection()
    now = body.timestamp or _now_iso()

    mission = get_mission(conn, mission_id)
    if not mission:
        # Mission was created by the adapter, not via the OS. Insert a stub.
        log.warning("report for unknown mission %s — inserting stub", mission_id)
        insert_mission(conn, {
            "mission_id":           mission_id,
            "capability_required":  "UNKNOWN",
            "objective":            "",
            "parameters_json":      "{}",
            "state":                body.status,
            "assigned_machine_id":  None,
            "created_at":           now,
            "updated_at":           now,
        })
    else:
        old_state = mission["state"]
        patch: dict[str, Any] = {
            "state":           body.status,
            "status_message":  body.detail,
            "updated_at":      now,
        }
        if body.progress_percent is not None:
            patch["progress_percent"] = body.progress_percent
        if body.status in ("COMPLETED", "EXCEPTION"):
            patch["result_state"] = body.status
            patch["completed_at"] = now
        update_mission(conn, mission_id, patch)
        add_mission_event(
            conn, mission_id, "REPORT",
            old_state=old_state, new_state=body.status,
            progress=body.progress_percent,
            note=body.detail[:200] if body.detail else None,
            ts=now,
        )

    state_emoji = {"RUNNING": "▶", "COMPLETED": "✓", "EXCEPTION": "✗"}.get(body.status, "·")
    pct = f"  {body.progress_percent}%" if body.progress_percent is not None else ""
    log.info("REPORT    %-16s  %s %-10s%s  %s", mission_id, state_emoji, body.status, pct, body.detail[:60])

    return {"mission_id": mission_id, "status": body.status}


# =============================================================================
# PULL endpoints — dashboard + operators
# =============================================================================

@app.post(f"{API_PREFIX}/missions", status_code=201)
async def create_mission(body: MissionRequest) -> dict[str, Any]:
    """
    Create and dispatch a mission. The OS forwards the request to the adapter,
    which selects a machine using its own Tier 1+2 filters, then tracks the
    result here for persistence and the dashboard.
    """
    conn = get_connection()
    now = _now_iso()
    mission_id = _mission_id()

    # Persist PENDING first so the dashboard shows it immediately
    insert_mission(conn, {
        "mission_id":           mission_id,
        "capability_required":  body.capability_required,
        "objective":            body.objective,
        "parameters_json":      json.dumps(body.parameters),
        "state":                "PENDING",
        "assigned_machine_id":  None,
        "created_at":           now,
        "updated_at":           now,
    })
    add_mission_event(conn, mission_id, "CREATED", new_state="PENDING", ts=now)

    # Forward to the adapter — the adapter owns machine selection (Tier 1+2)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{ADAPTER_URL}{API_PREFIX}/missions",
                json={
                    "mission_id":          mission_id,
                    "capability_required": body.capability_required,
                    "objective":           body.objective,
                    "parameters":          body.parameters,
                },
            )
    except httpx.ConnectError:
        update_mission(conn, mission_id, {
            "state": "EXCEPTION",
            "status_message": f"Adapter not reachable at {ADAPTER_URL}",
            "updated_at": _now_iso(),
        })
        raise HTTPException(503, detail={
            "reason": "adapter_unreachable",
            "mission_id": mission_id,
            "adapter_url": ADAPTER_URL,
        })

    adapter_body = r.json()

    if r.status_code == 201:
        # Adapter accepted and dispatched
        assigned = adapter_body.get("assigned_machine_id")
        adapter_mission_id = adapter_body.get("mission_id", mission_id)

        update_mission(conn, mission_id, {
            "state":                "ASSIGNED",
            "assigned_machine_id":  assigned,
            "updated_at":           _now_iso(),
        })
        add_mission_event(
            conn, mission_id, "ASSIGNED",
            old_state="PENDING", new_state="ASSIGNED",
            machine_id=assigned,
            ts=_now_iso(),
        )
        log.info("DISPATCH  %-16s  → %-14s  %s", mission_id, assigned, body.objective)
        return {
            "mission_id":           mission_id,
            "adapter_mission_id":   adapter_mission_id,
            "capability_required":  body.capability_required,
            "objective":            body.objective,
            "state":                "ASSIGNED",
            "assigned_machine_id":  assigned,
            "created_at":           now,
        }
    else:
        # Adapter rejected (409 = no machine available)
        reason = adapter_body.get("reason", "unknown")
        update_mission(conn, mission_id, {
            "state":          "EXCEPTION",
            "status_message": reason,
            "updated_at":     _now_iso(),
        })
        add_mission_event(
            conn, mission_id, "EXCEPTION",
            old_state="PENDING", new_state="EXCEPTION",
            note=reason, ts=_now_iso(),
        )
        log.warning("REJECTED  %-16s  %s", mission_id, reason)
        raise HTTPException(
            status_code=r.status_code,
            detail={**adapter_body, "mission_id": mission_id},
        )


@app.get(f"{API_PREFIX}/missions")
def list_missions() -> dict[str, Any]:
    conn = get_connection()
    missions = get_all_missions(conn)
    return {"missions": missions, "total": len(missions)}


@app.get(f"{API_PREFIX}/missions/{{mission_id}}")
def get_one_mission(mission_id: str) -> dict[str, Any]:
    conn = get_connection()
    mission = get_mission(conn, mission_id)
    if not mission:
        raise HTTPException(404, detail={"reason": "unknown_mission", "mission_id": mission_id})
    return mission


@app.get(f"{API_PREFIX}/missions/{{mission_id}}/events")
def get_events(mission_id: str) -> dict[str, Any]:
    conn = get_connection()
    events = get_mission_events(conn, mission_id)
    return {"mission_id": mission_id, "events": events}


@app.get(f"{API_PREFIX}/machines")
def list_machines() -> dict[str, Any]:
    conn = get_connection()
    machines = get_all_machines(conn)
    return {"machines": machines, "total": len(machines)}


@app.get(f"{API_PREFIX}/machines/{{machine_id}}")
def get_one_machine(machine_id: str) -> dict[str, Any]:
    conn = get_connection()
    machine = get_machine(conn, machine_id)
    if not machine:
        raise HTTPException(404, detail={"reason": "unknown_machine", "machine_id": machine_id})
    return machine


@app.get("/health")
def health() -> dict[str, Any]:
    conn = get_connection()
    machines = get_all_machines(conn)
    missions = get_all_missions(conn)
    active = [m for m in missions if m["state"] in ("ASSIGNED", "RUNNING")]
    return {
        "status": "ok",
        "machines_registered": len(machines),
        "missions_total":      len(missions),
        "missions_active":     len(active),
        "adapter_url":         ADAPTER_URL,
    }


# ── Dev entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("os_core.main:app", host="0.0.0.0", port=9000, reload=True)
