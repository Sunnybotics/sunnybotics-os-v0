#!/usr/bin/env python3
# Copyright 2026 Sunnybotics.
# Licensed under the Apache License, Version 2.0.
"""End-to-end demo of the SunnyboticsOS V0 machine layer.

Run the fleet and the adapter first, then:

    python3 demo/demo.py

This script speaks only HTTP. It imports no ROS packages and does not need the
workspace sourced -- which is the point. It stands exactly where the OS /
Mission Engine stands, so anything it can do from here, that consumer can do
too.

Four scenarios, each asserting something specific:

  1. GOLDEN RUN        a CLEANING mission runs to COMPLETED
  2. FAILURE PATH      the same mission with simulate_failure=true reaches
                       EXCEPTION with error_code OBSTACLE_DETECTED -- driven by
                       the request body, with no code edited between runs
  3. CAPABILITY ROUTING an INSPECTION mission lands on rover_02 without anyone
                       naming rover_02, proving matching rather than hardcoding
  4. NO MATCH          an unheard-of capability is refused with 409 instead of
                       being dispatched into a void

Exits non-zero if any scenario fails, so it works as a smoke test as well as a
demo.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

TIMEOUT_SEC = 10
POLL_INTERVAL_SEC = 0.4
MISSION_TIMEOUT_SEC = 60
AVAILABILITY_TIMEOUT_SEC = 30

TERMINAL_STATES = {"COMPLETED", "EXCEPTION", "REJECTED", "CANCELED"}

#: Matches the adapter and the OS-side API. Versioned in the path so a
#: breaking change can be served alongside the old one.
API = "/api/v0"


# --------------------------------------------------------------------------- #
# Tiny HTTP helpers (stdlib only, so this runs anywhere)
# --------------------------------------------------------------------------- #
def request(base: str, method: str, path: str, body: dict | None = None):
    """Return (status_code, parsed_body). HTTP errors are values, not exceptions."""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        f"{base}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"null")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")}


def get(base: str, path: str):
    return request(base, "GET", path)


def post(base: str, path: str, body: dict):
    return request(base, "POST", path, body)


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
def heading(text: str) -> None:
    print()
    print("=" * 78)
    print(f"  {text}")
    print("=" * 78)


def step(text: str) -> None:
    print(f"\n--- {text}")


def render_fleet(machines: list[dict]) -> None:
    if not machines:
        print("  (no machines -- is the fleet running?)")
        return

    header = (
        f"  {'MACHINE':<12} {'TYPE':<18} {'CAPABILITIES':<16} "
        f"{'STATE':<10} {'HEALTH':<9} {'LINK':<9} {'BATT':>6}  POSITION"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for machine in machines:
        battery = machine["battery_percent"]
        battery_text = f"{battery:.1f}%" if battery is not None else "n/a"
        if machine["has_location"]:
            position = (
                f"({machine['longitude']:.1f}, {machine['latitude']:.1f}) "
                f"{machine['frame_id']}"
            )
        else:
            position = "unknown"
        print(
            f"  {machine['machine_id']:<12} {machine['machine_type']:<18} "
            f"{','.join(machine['capabilities']):<16} {machine['state']:<10} "
            f"{machine['health']:<9} {machine['connection_status']:<9} "
            f"{battery_text:>6}  {position}"
        )
    print(f"\n  (all {len(machines)} are SIMULATED -- every object carries "
          f'"simulated": true)')


def render_outcome(name: str, passed: bool, detail: str) -> bool:
    print(f"\n  [{'PASS' if passed else 'FAIL'}] {name}: {detail}")
    return passed


# --------------------------------------------------------------------------- #
# Waiting
# --------------------------------------------------------------------------- #
def wait_for_adapter(base: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, body = get(base, "/")
            if status == 200:
                print(f"  adapter is up: {body['service']} v{body['api_version']}")
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    print(f"  could not reach the adapter at {base} within {timeout:.0f}s")
    return False


def wait_for_capability(base: str, capability: str, timeout: float = 30.0) -> bool:
    """Wait until some machine advertising `capability` is AVAILABLE."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, body = get(base, f"{API}/machines")
        for machine in body.get("machines", []):
            advertised = {c.lower() for c in machine["capabilities"]}
            if (
                capability.lower() in advertised
                and machine["state"] == "AVAILABLE"
                and machine["connection_status"] == "ONLINE"
            ):
                return True
        time.sleep(POLL_INTERVAL_SEC)
    return False


def wait_for_machine(base: str, machine_id: str, predicate, timeout: float) -> dict:
    """Wait for a machine's *published* state to satisfy `predicate`.

    Necessary because the machine heartbeat is 1 Hz while an action result comes
    back immediately. Right after a mission ends, the fleet view is still up to
    one tick behind -- so anything asserting on machine-side state has to wait
    for the next heartbeat rather than read the stale one. That lag is a
    property of a 1 Hz polling interface, not a bug, and it is exactly what the
    WebSocket feed described in the README would remove.
    """
    deadline = time.monotonic() + timeout
    machine = {}
    while time.monotonic() < deadline:
        _, machine = get(base, f"{API}/machines/{machine_id}")
        if predicate(machine):
            return machine
        time.sleep(0.2)
    return machine


def poll_mission(base: str, mission_id: str, machine_id: str) -> dict:
    """Follow a mission to a terminal state, narrating as it goes."""
    deadline = time.monotonic() + MISSION_TIMEOUT_SEC
    last_line = None

    while time.monotonic() < deadline:
        _, mission = get(base, f"{API}/missions/{mission_id}")
        _, machine = get(base, f"{API}/machines/{machine_id}")

        position = ""
        if machine.get("has_location"):
            position = (
                f"  pos=({machine['longitude']:6.1f}, {machine['latitude']:6.1f})"
            )
        battery = (
            f"  batt={machine['battery_percent']:.2f}%"
            if machine.get("battery_percent") is not None
            else ""
        )
        line = (
            f"    {mission['state']:<10} {mission['progress_percent']:>3}%  "
            f"machine={machine['state']:<10} health={machine['health']:<9}"
            f"{battery}{position}  {mission['status_message']}"
        )
        if line != last_line:
            print(line)
            last_line = line

        if mission["state"] in TERMINAL_STATES:
            return mission
        time.sleep(POLL_INTERVAL_SEC)

    print(f"    mission {mission_id} did not finish within {MISSION_TIMEOUT_SEC}s")
    return mission


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #
def scenario_golden_run(base: str) -> bool:
    heading("SCENARIO 1 -- GOLDEN RUN: a CLEANING mission that succeeds")
    step(f"POST {API}/missions  {{capability_required: cleaning}}")

    status, body = post(
        base,
        f"{API}/missions",
        {
            "capability_required": "cleaning",
            "objective": "Clean aisle 3",
            "parameters": {"target_x": 18.0, "target_y": 9.0},
        },
    )
    print(f"    -> {status} {json.dumps(body)}")
    if status != 201:
        return render_outcome("golden run", False, f"expected 201, got {status}")

    print(
        f"\n    the OS asked for a capability and got back "
        f"'{body['assigned_machine_id']}'. It never named a machine."
    )
    step(f"polling GET {API}/missions/{body['mission_id']}")
    mission = poll_mission(base, body["mission_id"], body["assigned_machine_id"])

    print(f"\n    final result: {json.dumps(mission['result'], indent=6)}")
    return render_outcome(
        "golden run",
        mission["state"] == "COMPLETED" and mission["progress_percent"] == 100,
        f"mission reached {mission['state']} at {mission['progress_percent']}%",
    )


def scenario_failure_path(base: str) -> bool:
    heading("SCENARIO 2 -- FAILURE PATH: the same mission, simulate_failure=true")
    print(
        "\n  Nothing was edited between scenario 1 and this one. The only\n"
        "  difference is one key in the request body, which is the only way a\n"
        "  demonstrated failure path is worth anything."
    )

    step("waiting for a CLEANING machine to become AVAILABLE again")
    if not wait_for_capability(base, "cleaning", AVAILABILITY_TIMEOUT_SEC):
        return render_outcome(
            "failure path", False, "no CLEANING machine became available"
        )
    print("    a CLEANING machine is free")

    step(f"POST {API}/missions  {{..., parameters: {{simulate_failure: true}}}}")
    status, body = post(
        base,
        f"{API}/missions",
        {
            "capability_required": "cleaning",
            "objective": "Clean aisle 4 (will hit a simulated obstacle)",
            "parameters": {
                "simulate_failure": True,
                "target_x": -12.0,
                "target_y": 22.0,
            },
        },
    )
    print(f"    -> {status} {json.dumps(body)}")
    if status != 201:
        return render_outcome("failure path", False, f"expected 201, got {status}")

    step(f"polling GET {API}/missions/{body['mission_id']}")
    mission = poll_mission(base, body["mission_id"], body["assigned_machine_id"])

    print(f"\n    final result: {json.dumps(mission['result'], indent=6)}")

    step(f"GET {API}/machines/{body['assigned_machine_id']}  (the error surfaces on the machine too)")
    print("    waiting one heartbeat -- the fleet view is 1 Hz, the action")
    print("    result was immediate, so the machine is briefly a tick behind")
    machine = wait_for_machine(
        base,
        body["assigned_machine_id"],
        lambda m: bool(m["active_errors"]) or m["state"] == "EXCEPTION",
        timeout=5.0,
    )
    print(f"    state={machine['state']}  active_errors={machine['active_errors']}")
    print(f"    severities={machine['active_error_severities']}")

    result = mission.get("result") or {}
    passed = (
        mission["state"] == "EXCEPTION"
        and result.get("error_code") == "OBSTACLE_DETECTED"
        # The failure must be visible on the machine as well, not just in the
        # mission record. An OS that could only see it in one place would be
        # blind to a machine that failed outside a mission.
        and "OBSTACLE_DETECTED" in machine["active_errors"]
        and "FATAL" in machine["active_error_severities"]
    )
    return render_outcome(
        "failure path",
        passed,
        f"mission reached {mission['state']} with "
        f"error_code={result.get('error_code')}; machine reported "
        f"{machine['state']} / {machine['active_errors']}",
    )


def scenario_capability_routing(base: str) -> bool:
    heading("SCENARIO 3 -- CAPABILITY ROUTING: INSPECTION must find rover_02")
    print(
        "\n  The request below does not contain the string 'rover_02'. Nor\n"
        "  does the adapter. Both machines are rovers, so machine_type cannot\n"
        "  be doing the work either. The only thing connecting this mission\n"
        "  to that machine is a capability string both sides agree on."
    )

    step("waiting for an INSPECTION machine to be AVAILABLE")
    if not wait_for_capability(base, "inspection", AVAILABILITY_TIMEOUT_SEC):
        return render_outcome(
            "capability routing", False, "no INSPECTION machine became available"
        )

    step(f"POST {API}/missions  {{capability_required: inspection}}")
    status, body = post(
        base,
        f"{API}/missions",
        {
            "capability_required": "inspection",
            "objective": "Inspect the aisle 7 racking",
            "parameters": {"target_x": 55.0, "target_y": 4.0},
        },
    )
    print(f"    -> {status} {json.dumps(body)}")
    if status != 201:
        return render_outcome(
            "capability routing", False, f"expected 201, got {status}"
        )

    assigned = body["assigned_machine_id"]
    step(f"polling GET {API}/missions/{body['mission_id']}")
    mission = poll_mission(base, body["mission_id"], assigned)

    _, machine = get(base, f"{API}/machines/{assigned}")
    print(
        f"\n    routed to '{assigned}' (machine_type={machine['machine_type']}, "
        f"capabilities={machine['capabilities']})"
    )
    return render_outcome(
        "capability routing",
        mission["state"] == "COMPLETED" and "inspection" in machine["capabilities"],
        f"INSPECTION went to {assigned} and reached {mission['state']}",
    )


def scenario_no_match(base: str) -> bool:
    heading("SCENARIO 4 -- NO MATCH: an unheard-of capability must be refused")
    print(
        "\n  A fleet that silently accepts work nobody can do is worse than one\n"
        "  that refuses it. WELDING is not advertised by any machine."
    )

    step(f"POST {API}/missions  {{capability_required: welding}}")
    status, body = post(
        base,
        f"{API}/missions",
        {"capability_required": "welding", "objective": "Weld the frame"},
    )
    print(f"    -> {status} {json.dumps(body, indent=6)}")

    detail = body.get("detail", {}) if isinstance(body, dict) else {}
    return render_outcome(
        "no match",
        status == 409 and detail.get("reason") == "no_machine_with_capability",
        f"got {status} with reason={detail.get('reason')}",
    )


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    base = f"http://{args.host}:{args.port}"

    heading("SunnyboticsOS V0 -- machine layer end-to-end demo")
    print(f"\n  adapter: {base}")
    print("  every machine in this demo is SIMULATED\n")

    step("waiting for the adapter")
    if not wait_for_adapter(base):
        print("\n  Start the stack first:")
        print("    ros2 launch sunnybotics_machines machines.launch.py")
        print("    ros2 run sunnybotics_adapter adapter")
        return 1

    heading(f"FLEET -- GET {API}/machines")
    print(
        "\n  Nothing configured this list. The adapter discovered these by\n"
        "  watching for /machines/<id>/state topics.\n"
    )
    _, body = get(base, f"{API}/machines")
    render_fleet(body.get("machines", []))

    if not body.get("machines"):
        print("\n  Start the fleet first:")
        print("    ros2 launch sunnybotics_machines machines.launch.py")
        return 1

    results = {
        "1. golden run": scenario_golden_run(base),
        "2. failure path": scenario_failure_path(base),
        "3. capability routing": scenario_capability_routing(base),
        "4. no match": scenario_no_match(base),
    }

    heading(f"FLEET AFTER THE RUN -- GET {API}/machines")
    print(
        "\n  Waiting for both machines to settle. Each holds its terminal"
        "\n  state for a few seconds so a polling OS can actually observe it,"
        "\n  then returns to AVAILABLE on its own.\n"
    )
    for machine_id in ("rover_01", "rover_02"):
        wait_for_machine(
            base, machine_id, lambda m: m["state"] == "AVAILABLE", timeout=15.0
        )
    _, body = get(base, f"{API}/machines")
    render_fleet(body.get("machines", []))

    heading("SUMMARY")
    print()
    for name, passed in results.items():
        print(f"  [{'PASS' if passed else 'FAIL'}]  {name}")

    everything_passed = all(results.values())
    print()
    if everything_passed:
        print(
            "  The whole chain held: capability in, machine matched, mission\n"
            "  dispatched over a ROS 2 action, execution state back out over\n"
            "  REST -- including the failure path, driven entirely by the\n"
            "  request body."
        )
    else:
        print("  Something did not hold. See the FAIL lines above.")
    print()
    return 0 if everything_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
