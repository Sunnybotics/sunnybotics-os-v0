# Copyright 2026 Sunnybotics.
# Licensed under the Apache License, Version 2.0.
"""The ROS 2 <-> REST/JSON border.

Everything the OS sees crosses through this module. On the ROS side there are
enum integers, parallel arrays and ``builtin_interfaces/Time``. On the OS side
there is snake_case JSON with readable strings and ISO 8601 timestamps, because
``"state": 2`` in a log line tells an operator nothing and ``"state": "RUNNING"``
tells them everything.

The lookup tables below are keyed off the *generated constants* rather than
hardcoded integers. Renumber a constant in MachineState.msg and this module
follows automatically; rename one and the import fails loudly at start-up
instead of quietly emitting the wrong string forever.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sunnybotics_cmi.action import Mission
from sunnybotics_cmi.msg import MachineState

STATE_TO_STRING = {
    MachineState.AVAILABLE: "AVAILABLE",
    MachineState.ASSIGNED: "ASSIGNED",
    MachineState.RUNNING: "RUNNING",
    MachineState.COMPLETED: "COMPLETED",
    MachineState.EXCEPTION: "EXCEPTION",
}

HEALTH_TO_STRING = {
    MachineState.OK: "OK",
    MachineState.DEGRADED: "DEGRADED",
    MachineState.FAULT: "FAULT",
}

OPERATING_MODE_TO_STRING = {
    MachineState.MANUAL: "MANUAL",
    MachineState.TELEOP: "TELEOP",
    MachineState.SEMI_AUTO: "SEMI_AUTO",
    MachineState.AUTOMATIC: "AUTOMATIC",
}

CONNECTION_TO_STRING = {
    MachineState.ONLINE: "ONLINE",
    MachineState.OFFLINE: "OFFLINE",
    MachineState.CONNECTION_BROKEN: "CONNECTION_BROKEN",
}

SEVERITY_TO_STRING = {
    MachineState.SEVERITY_WARNING: "WARNING",
    MachineState.SEVERITY_FATAL: "FATAL",
}

# The action Result carries its own enum. The RESULT_ prefix is dropped on the
# way out: in JSON the surrounding key already says this is a result, and
# "COMPLETED" then matches the mission state vocabulary exactly.
RESULT_STATE_TO_STRING = {
    Mission.Result.RESULT_COMPLETED: "COMPLETED",
    Mission.Result.RESULT_EXCEPTION: "EXCEPTION",
}


def _name_of(table: dict[int, str], value: int, kind: str) -> str:
    """Look up an enum name, surfacing unknown values instead of hiding them.

    A machine speaking a newer interface_version can legitimately send a value
    this adapter has never heard of. Returning a loud placeholder keeps the
    endpoint answering while making the mismatch obvious in the response.
    """
    return table.get(value, f"UNKNOWN_{kind}_{value}")


def ros_time_to_iso8601(stamp) -> str | None:
    """``builtin_interfaces/Time`` -> ISO 8601 UTC, or None if unset.

    A zero stamp means the publisher never filled it in. That is worth
    distinguishing from 1970-01-01, which is what a naive conversion produces.
    """
    if stamp.sec == 0 and stamp.nanosec == 0:
        return None
    moment = datetime.fromtimestamp(
        stamp.sec + stamp.nanosec / 1e9, tz=timezone.utc
    )
    return moment.isoformat().replace("+00:00", "Z")


def parse_json_object(raw: str) -> Any:
    """Decode a JSON string field, degrading to the raw string if malformed.

    ``result_json`` arrives as a string over the wire. Handing the OS a parsed
    object saves it a second decode; handing it the raw string when the decode
    fails is better than dropping information a human might still read.
    """
    if not raw or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"unparsed": raw}


def machine_state_to_dict(
    msg: MachineState,
    *,
    seconds_since_last_message: float,
    stale: bool,
) -> dict[str, Any]:
    """One MachineState -> the JSON object the OS consumes.

    ``stale`` is decided by the adapter, not the machine: a machine whose link
    has genuinely died cannot send a message saying so. When the heartbeat stops
    arriving the adapter overrides connection_status to CONNECTION_BROKEN, which
    is exactly the case that field exists for. Every other field is passed
    through as last heard, so the OS can still see where the machine was and
    what it was doing when it went quiet.
    """
    connection = _name_of(CONNECTION_TO_STRING, msg.connection_status, "CONNECTION")
    if stale:
        connection = "CONNECTION_BROKEN"

    return {
        "interface_version": msg.interface_version,
        "seq": int(msg.seq),
        "machine_id": msg.machine_id,
        "machine_type": msg.machine_type,
        "capabilities": list(msg.capabilities),
        "state": _name_of(STATE_TO_STRING, msg.state, "STATE"),
        "health": _name_of(HEALTH_TO_STRING, msg.health, "HEALTH"),
        "operating_mode": _name_of(
            OPERATING_MODE_TO_STRING, msg.operating_mode, "MODE"
        ),
        "connection_status": connection,
        # The has_* guards are honoured rather than passed through: emitting
        # null for a machine that has no battery is unambiguous, where 0.0
        # reads as a flat one.
        "has_battery": bool(msg.has_battery),
        "battery_percent": (
            round(float(msg.battery_percent), 2) if msg.has_battery else None
        ),
        "has_location": bool(msg.has_location),
        "frame_id": msg.frame_id if msg.has_location else None,
        "latitude": round(float(msg.latitude), 4) if msg.has_location else None,
        "longitude": round(float(msg.longitude), 4) if msg.has_location else None,
        "current_mission_id": msg.current_mission_id or None,
        # Kept as the two parallel arrays the message defines, rather than
        # zipped into objects: the consumer maps this 1:1 onto the .msg, and
        # a helpful reshaping here would be one more thing to keep in sync.
        "active_errors": list(msg.active_errors),
        "active_error_severities": [
            _name_of(SEVERITY_TO_STRING, severity, "SEVERITY")
            for severity in msg.active_error_severities
        ],
        "stamp": ros_time_to_iso8601(msg.stamp),
        "seconds_since_last_message": round(float(seconds_since_last_message), 2),
        # V0 has no real machines behind this adapter, so this is a constant.
        # When a real driver appears it has to become source-derived -- a field
        # that says "simulated" for a real robot is worse than no field at all.
        "simulated": True,
    }


def mission_result_to_dict(result: Mission.Result) -> dict[str, Any]:
    """One Mission.Result -> JSON."""
    return {
        "result_state": _name_of(
            RESULT_STATE_TO_STRING, result.result_state, "RESULT"
        ),
        "error_code": result.error_code or None,
        "message": result.message,
        "result_json": parse_json_object(result.result_json),
    }


# --------------------------------------------------------------------------- #
# The OS-side contract
# --------------------------------------------------------------------------- #
# The OS models a machine with a smaller vocabulary than MachineState carries.
# Translating down to it is lossy in two specific ways, both recorded here so
# the loss is visible rather than discovered later:
#
#   1. The OS folds connectivity into `state` as OFFLINE. A machine that is
#      RUNNING and then loses its link has to be reported as OFFLINE, which
#      overwrites the fact that a mission is still executing somewhere.
#   2. The OS has no terminal machine state. COMPLETED and EXCEPTION live on
#      the mission, so a machine that has just finished is reported AVAILABLE
#      and the outcome is carried by the mission report instead.
#
# Both are survivable because the mission report carries the outcome, but the
# first one is worth raising: `state` and connectivity answer different
# questions and a single field cannot hold both answers at once.

OS_STATE_AVAILABLE = "AVAILABLE"
OS_STATE_ASSIGNED = "ASSIGNED"
OS_STATE_RUNNING = "RUNNING"
OS_STATE_OFFLINE = "OFFLINE"

_OS_STATE_FROM_MACHINE = {
    MachineState.AVAILABLE: OS_STATE_AVAILABLE,
    MachineState.ASSIGNED: OS_STATE_ASSIGNED,
    MachineState.RUNNING: OS_STATE_RUNNING,
    # No OS equivalent -- the machine is free again as far as the OS cares.
    MachineState.COMPLETED: OS_STATE_AVAILABLE,
    MachineState.EXCEPTION: OS_STATE_AVAILABLE,
}


def machine_state_to_os_descriptor(
    msg: MachineState, *, stale: bool
) -> dict[str, Any]:
    """One MachineState -> the OS Machine Descriptor.

    ``stale`` is the adapter's own judgement that the heartbeat has stopped.
    The machine cannot report this about itself, so if it is not applied here
    an unreachable machine keeps looking idle and keeps being given work.
    """
    reachable = (msg.connection_status == MachineState.ONLINE) and not stale
    state = (
        OS_STATE_OFFLINE
        if not reachable
        else _OS_STATE_FROM_MACHINE.get(msg.state, OS_STATE_AVAILABLE)
    )

    faults = list(msg.active_errors)
    if stale:
        # Surfaced as a fault as well as a state, because OFFLINE alone does
        # not say whether the machine stopped or merely stopped being heard.
        faults.append("NO_HEARTBEAT")

    # `status` is the machine own OK / DEGRADED / FAULT verdict. Without it
    # the only health signal crossing this boundary was a battery percentage,
    # which left a consumer to re-derive thresholds this layer already knows --
    # and left a machine that is DEGRADED for any other reason invisible.
    health: dict[str, Any] = {
        "connected": reachable,
        "status": _name_of(HEALTH_TO_STRING, msg.health, "HEALTH"),
        "faults": faults,
    }
    if msg.has_battery:
        health["battery_pct"] = round(float(msg.battery_percent), 1)

    location = None
    if msg.has_location:
        # The OS models position as plain site-local x/y. `longitude` carries
        # local X and `latitude` local Y, per this repository's frame note.
        location = {
            "x": round(float(msg.longitude), 4),
            "y": round(float(msg.latitude), 4),
            "frame_id": msg.frame_id,
        }

    return {
        "machine_id": msg.machine_id,
        "machine_type": msg.machine_type,
        "capabilities": list(msg.capabilities),
        "state": state,
        "health": health,
        "location": location,
        "current_mission_id": msg.current_mission_id or None,
    }
