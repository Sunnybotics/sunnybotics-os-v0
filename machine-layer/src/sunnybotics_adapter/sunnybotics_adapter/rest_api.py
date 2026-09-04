# Copyright 2026 Sunnybotics.
# Licensed under the Apache License, Version 2.0.
"""The HTTP half of the adapter: the surface the OS / Mission Engine talks to.

Every endpoint here is a thin shell over one method on ``AdapterNode``. There is
no ROS vocabulary above this line and no HTTP vocabulary below it, which is what
makes the machine layer replaceable without the OS noticing.

**Why these handlers are ``def`` and not ``async def``.** ``rclpy`` is
synchronous and spins on its own thread; reading fleet state means taking a
``threading.Lock``, and dispatching a mission means blocking on an action
server for up to a few seconds. Declaring the handlers synchronous makes
FastAPI run them in its worker threadpool, where blocking is correct and cheap.
Written as ``async def`` the same code would block the event loop and stall
every other request in the process -- the classic way this bridge goes wrong.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from sunnybotics_adapter.adapter_node import AdapterNode

#: Version of the REST contract, independent of the CMI interface_version.
API_VERSION = "0.1.0"

#: Path prefix, matching the OS-side API. Versioning in the path means a
#: breaking change can be served alongside the old one instead of
#: requiring both sides to cut over at the same moment.
API_PREFIX = "/api/v0"


class MissionRequest(BaseModel):
    """Body of ``POST /missions``.

    The OS asks for a *capability*, never for a machine. That is the whole
    hardware-agnostic claim reduced to one field: the Mission Engine never
    learns that rover_01 exists.
    """

    capability_required: str = Field(
        ..., min_length=1, description='e.g. "cleaning" or "inspection"'
    )
    mission_id: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Optional. The id the caller has already issued for this mission. "
            "Supplied, it is used as-is and every report comes back under it, "
            "so the mission has one identity end to end. Omitted, the adapter "
            "generates one."
        ),
    )
    objective: str = Field(
        default="", description='Human-readable intent, e.g. "clean aisle 3"'
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Machine-type-specific parameters, forwarded verbatim as the "
            "goal's parameters_json. Recognised by the V0 simulated machines: "
            "simulate_failure (bool), target_x / target_y (float)."
        ),
    )


def create_app(node: AdapterNode) -> FastAPI:
    """Build the FastAPI app around a live adapter node."""
    app = FastAPI(
        title="SunnyboticsOS Machine Adapter",
        version=API_VERSION,
        description=(
            "Bridges ROS 2 machines onto REST/JSON for the SunnyboticsOS "
            "Mission Engine. V0: every machine behind this adapter is "
            "SIMULATED, and each machine object says so in its `simulated` "
            "field. No authentication -- bind to localhost only."
        ),
    )

    @app.get("/", tags=["service"])
    def service_info() -> dict[str, Any]:
        """What this process is, for anyone who hits the port by hand."""
        return {
            "service": "sunnybotics-machine-adapter",
            "api_version": API_VERSION,
            "simulated_fleet": True,
            "machines_known": node.known_machine_ids(),
            "missions_tracked": node.mission_count(),
            "endpoints": [
                f"GET  {API_PREFIX}/machines",
                f"GET  {API_PREFIX}/machines/{{machine_id}}",
                f"POST {API_PREFIX}/missions",
                f"GET  {API_PREFIX}/missions/{{mission_id}}",
            ],
            "os_push": node.os_push_stats(),
            "not_implemented_in_v0": ["WS /ws/machines (polling only for now)"],
        }

    @app.get(f"{API_PREFIX}/machines", tags=["machines"])
    def list_machines() -> dict[str, Any]:
        """Every machine the adapter can currently hear.

        Populated purely by topic discovery, so a machine started a second ago
        appears here without anything being restarted or reconfigured.
        """
        return {"machines": node.machines_snapshot()}

    @app.get(API_PREFIX + "/machines/{machine_id}", tags=["machines"])
    def get_machine(machine_id: str) -> dict[str, Any]:
        machine = node.machine_snapshot(machine_id)
        if machine is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "reason": "unknown_machine",
                    "machine_id": machine_id,
                    "known_machines": node.known_machine_ids(),
                },
            )
        return machine

    @app.post(f"{API_PREFIX}/missions", status_code=201, tags=["missions"])
    def create_mission(request: MissionRequest) -> dict[str, Any]:
        """Match a capable machine and dispatch a mission to it.

        201 with the assignment, or 409 when nothing can take the work -- either
        because no machine advertises the capability at all, or because every
        machine that does is busy or unreachable. The `reason` field says which,
        since those two call for very different responses from the OS.

        A caller that already has a mission id should send it as `mission_id`.
        It is echoed back and every subsequent report uses it, which removes the
        need for the caller to map between its own id and one invented here.
        """
        status, body = node.dispatch_mission(
            capability_required=request.capability_required,
            objective=request.objective,
            parameters=request.parameters,
            mission_id=request.mission_id,
        )
        if status != 201:
            raise HTTPException(status_code=status, detail=body)
        return body

    @app.get(API_PREFIX + "/missions/{mission_id}", tags=["missions"])
    def get_mission(mission_id: str) -> dict[str, Any]:
        """Current state of one dispatched mission.

        Outlives the machine's own view of it: the machine returns to AVAILABLE
        a few seconds after finishing, but the mission keeps its terminal state
        and result here forever.
        """
        mission = node.mission_snapshot(mission_id)
        if mission is None:
            raise HTTPException(
                status_code=404,
                detail={"reason": "unknown_mission", "mission_id": mission_id},
            )
        return mission

    return app
