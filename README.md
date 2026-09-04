# SunnyboticsOS V0

**Hardware-agnostic operating and orchestration layer for robotic work in energy infrastructure.**

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![ROS 2](https://img.shields.io/badge/ROS%202-Jazzy-22314E.svg?logo=ros)](https://docs.ros.org/en/jazzy/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)

---

## Overview

A solar site is not serviced by a single class of machine. Cleaning rovers,
inspection units, and equipment that has not been built yet all operate on the
same asset, and each one arrives with its own driver, its own message set, and
its own definition of "busy."

The common response is to write orchestration once per robot model. That
approach does not scale. Fleet logic gets copied and diverges, adding a machine
type means editing the scheduler, and the dispatcher ends up encoding the wiring
of every unit it commands.

SunnyboticsOS inverts the dependency. The OS requests a *capability*, never a
specific machine. A mission asks for something that can perform `CLEANING` in
sector A. It does not name a robot, know how many exist, or hold any assumption
about what they are made of. Machines describe themselves through a single
shared contract, the Common Machine Interface (CMI), and any unit that speaks
that contract becomes dispatchable as soon as it appears on the network.

The operational consequence is measurable: starting a third machine is enough to
make it appear in `GET /machines` and become eligible for work. No restart, no
configuration file, no code change. The adapter scans for topics matching
`/machines/<id>/state` once per second and subscribes to whatever it finds. No
component in the system holds a static list of machines.

---

## Scope of this release

V0 validates the contract and the orchestration path. The table below states
which components run production logic and which are stubbed.

| Component | Status | Notes |
|---|---|---|
| OS Core (FastAPI + SQLite) | Real | Mission lifecycle, persistence, audit log |
| CMI contract (`MachineState.msg`, `Mission.action`) | Real | Generated through `rosidl`, versioned |
| ROS 2 action loop | Real | `ActionServer` and `ActionClient`, goals, feedback, cancellation |
| DDS transport | Real | Standard ROS 2 middleware |
| Machine adapter (REST to ROS 2) | Real | Dynamic discovery, capability dispatch |
| Streamlit dashboard | Real | Live fleet and mission views |
| Physical behavior of `rover_01` and `rover_02` | Simulated | Node stubs, no hardware in the loop |

The orchestration path and the ROS 2 loop execute the same way they would
against hardware. What is stubbed is the pair of machines behind the action
server. The machine layer documents the simulation boundary fault by fault in
[What is simulated, and what is not](machine-layer/README.md#what-is-simulated-and-what-is-not).

---

## Architecture

```
┌─────────────────────────────────────────┐
│  SunnyboticsOS Core          port 9000  │
│  FastAPI + SQLite                       │
│  • Machine registry and heartbeats      │
│  • Mission lifecycle and audit log      │
│  • Read API for dashboard and operators │
└────────────────────┬────────────────────┘
                     │  HTTP  (push + pull)
┌────────────────────▼────────────────────┐
│  Machine Adapter             port 8001  │
│  FastAPI + rclpy                        │
│  • Discovers machines by topic scan     │
│  • Selects a machine by capability      │
│  • Dispatches ROS 2 Action goals        │
│  • Pushes state back to the OS Core     │
└────────────────────┬────────────────────┘
                     │  ROS 2 Actions + MachineState.msg  (DDS)
┌────────────────────▼────────────────────┐
│  Machine Nodes                          │
│  rover_01  cleaning_rover   [cleaning]  │
│  rover_02  inspection_rover [inspection]│
│  rclpy · SIMULATED                      │
└─────────────────────────────────────────┘
```

| Service | Port |
|---|---|
| OS Core | `9000` |
| Machine Adapter | `8001` |
| Dashboard | `8501` |

### Dispatch sequence

1. An operator posts a capability requirement to `POST /api/v0/missions`.
2. The OS Core persists the mission as `PENDING` before contacting the adapter,
   so the dashboard reflects the request even if dispatch fails, then forwards
   it downstream.
3. The adapter selects a machine in two stages. Tier 1 narrows the fleet to
   units advertising the requested capability. Tier 2 narrows that set to units
   currently `AVAILABLE`. When either stage returns empty, the rejection
   identifies which one did, so "no machine provides this capability" is never
   reported as "every machine is busy."
4. The adapter sends a `Mission` action goal. The receiving node re-checks the
   capability and its own availability instead of trusting the dispatcher.
5. Feedback propagates upward while the mission runs. The OS Core writes every
   transition to `mission_events`.
6. The mission terminates as `COMPLETED` or `EXCEPTION`. No path ends without a
   recorded outcome.

```
PENDING --> ASSIGNED --> RUNNING --> COMPLETED
                                \--> EXCEPTION
```

---

## Common Machine Interface

Two definitions form the contract:

```
machine-layer/src/sunnybotics_cmi/
├── msg/MachineState.msg     # published by every machine at 1 Hz
└── action/Mission.action    # goal, feedback and result for one unit of work
```

The design decisions behind them are documented here because they constrain
every machine type added later. This section is the summary; the field-level
reference lives in [The contract](machine-layer/README.md#the-contract) and the
full rationale in [Design decisions and why](machine-layer/README.md#design-decisions-and-why).

**A mission is modeled as an action rather than a service or a topic.** Missions
have the lifetime characteristics of an action: accepted or rejected up front,
long-running, reporting progress while active, cancellable, and resolving to
exactly one terminal outcome. A service call would block for the full duration
of the mission. A topic would provide neither acknowledgment nor result.

**`state` and `health` are separate fields.** A machine can be `RUNNING` while
`DEGRADED`, or `OK` while idle. Merging the two fields would erase the
distinction between a unit that is not working and one that is not well.

**`connection_status` is independent of both.** Without it, a machine that has
gone silent continues to report its last known state, and a silent unit reading
`AVAILABLE` would receive dispatches that no one is listening for.

**Capabilities are free-form strings rather than an enumeration.** Introducing a
capability must not require a schema change, a rebuild, or a coordinated
redeploy across the fleet. The dispatcher matches on `capabilities` and does not
read `machine_type` at all.

**`interface_version` and `seq` travel in the envelope.** The version field
allows the OS to communicate with a partially upgraded fleet rather than
requiring a synchronized cutover. The monotonic `seq` counter makes dropped
messages detectable, which no timestamp can establish on its own.

**`has_battery` and `has_location` guard their dependent fields.** A
mains-powered unit reports `has_battery=false` instead of a misleading `0.0`,
and coordinates of `0,0` are never ambiguous between a machine sitting on the
origin and one with no position fix.

**Type-specific payloads travel as JSON strings** (`parameters_json`,
`result_json`). New machine types introduce new keys inside those payloads
instead of new fields in the contract, which keeps the interface stable as the
fleet diversifies. For a worked example of onboarding a new machine type, see
[Adding a new machine type](machine-layer/README.md#adding-a-new-machine-type).

---

## Quick start

### Prerequisites

| Requirement | Version | Required for |
|---|---|---|
| Python | 3.10 or later | OS Core, adapter, dashboard |
| ROS 2 | Jazzy (Humble and Iron should also work) | Machine layer |
| Operating system | Linux or WSL 2 | ROS 2 dependency |

The OS Core and the dashboard run on any platform with Python, including native
Windows. Only the machine layer requires ROS 2. See
[Running without ROS 2](#running-without-ros-2) for environments where it is not
installed.

### Single command

```bash
./run.sh
```

The script builds the machine layer on first run, creates a virtual environment,
installs dependencies, starts all four processes, and waits for the machines to
register. `Ctrl-C` shuts the full stack down together. From a second terminal,
`./run.sh stop` and `./run.sh status` operate on the running stack. Logs are
written to `logs/`.

The steps below are what the script automates, for anyone who prefers one
terminal per process.

### Manual startup

**1. OS Core**

```bash
pip install -r os_core/requirements.txt
pip install streamlit

python -m uvicorn os_core.main:app --host 0.0.0.0 --port 9000
```

Verify at <http://localhost:9000/health>.

**2. Machine layer** (requires ROS 2)

```bash
cd machine-layer
colcon build --symlink-install
source install/setup.bash

# Terminal A: machine nodes
ros2 launch sunnybotics_machines machines.launch.py

# Terminal B: adapter, pointed at the OS Core
python -m sunnybotics_adapter.main --os-url http://localhost:9000
```

**3. Dashboard**

```bash
streamlit run dashboard/app.py
```

Open <http://localhost:8501>.

### Dispatching a mission

```bash
curl -X POST http://localhost:9000/api/v0/missions \
  -H "Content-Type: application/json" \
  -d '{
        "capability_required": "CLEANING",
        "objective": "clean sector A",
        "parameters": {"simulate_failure": false}
      }'
```

The request specifies a capability, not a machine.

---

## Running without ROS 2

The machine layer includes a stub that mirrors the OS Core API, which allows the
adapter and the HTTP contract to be exercised on a host with no ROS 2
installation:

```bash
# In place of the machine layer
python machine-layer/tools/os_stub.py --port 9000

# Then the adapter, standalone
python -m sunnybotics_adapter.main
```

---

## OS Core API

### Push endpoints (adapter to OS)

| Method | Endpoint | Called when |
|---|---|---|
| `POST` | `/api/v0/machines/register` | A machine is discovered |
| `PATCH` | `/api/v0/machines/{id}/status` | Every heartbeat, approximately 1 Hz |
| `POST` | `/api/v0/missions/{id}/report` | Mission feedback or result |

### Pull endpoints (dashboard and operators to OS)

| Method | Endpoint | Returns |
|---|---|---|
| `POST` | `/api/v0/missions` | Creates and dispatches a mission |
| `GET` | `/api/v0/missions` | All missions |
| `GET` | `/api/v0/missions/{id}` | A single mission |
| `GET` | `/api/v0/missions/{id}/events` | Audit log for one mission |
| `GET` | `/api/v0/machines` | Registered machines |
| `GET` | `/api/v0/machines/{id}` | A single machine |
| `GET` | `/health` | Liveness check |

FastAPI serves interactive documentation at <http://localhost:9000/docs>. The
adapter exposes its own REST surface on port 8001, documented in
[REST API reference](machine-layer/README.md#rest-api-reference).

---

## Data model

SQLite, three tables, indexed on the columns the dashboard filters by.

| Table | Contents |
|---|---|
| `machines` | Identity, capabilities, state, health, battery, location, last seen |
| `missions` | Capability required, objective, state, assignment, progress, result |
| `mission_events` | Append-only audit log of every state transition |

`mission_events` records the old state, new state, machine, progress and
timestamp for each transition, which reduces post-hoc analysis of a failed
mission to a query rather than a log-reading exercise.

---

## Concurrency model

`rclpy` spins on a background thread while FastAPI serves on the main thread.
Every attribute reachable from both is guarded by a lock, and the FastAPI
endpoints are synchronous so that they can block on it. Contributors extending
the adapter should preserve that boundary. Making the endpoints asynchronous
without revisiting the locking would introduce races that surface only under
concurrent dispatch.

---

## Project structure

```
sunnybotics-os-v0/
├── os_core/                          # OS Core, FastAPI + SQLite
│   ├── main.py                       #   All OS endpoints
│   ├── database.py                   #   Schema, queries, audit log
│   └── requirements.txt
│
├── machine-layer/                    # ROS 2 workspace
│   ├── src/
│   │   ├── sunnybotics_cmi/          #   The contract: .msg and .action
│   │   ├── sunnybotics_adapter/      #   REST to ROS 2 bridge
│   │   └── sunnybotics_machines/     #   Simulated machine nodes
│   ├── demo/
│   └── tools/os_stub.py              #   Test double for the OS Core API
│
├── dashboard/app.py                  # Streamlit fleet and mission views
├── run.sh                            # Full-stack bring-up
├── SunnyboticsOS_V0_Architecture_Report.md
└── INTEGRATION_HANDOFF.md
```

---

## Roadmap

| Stage | Scope |
|---|---|
| V0 (current) | Contract, orchestration and ROS 2 loop validated against simulated machines |
| V1 | Hardware integration with deployed cleaning units, telemetry from production motor drivers |
| V2 | Multi-machine scheduling policy beyond first-available selection |

Validating the contract in simulation ahead of hardware integration was a
deliberate sequencing decision. Revising `MachineState.msg` once a deployed
fleet depends on it is expensive. Revising it now costs nothing.

---

## Documentation

| Document | Contents |
|---|---|
| [Architecture report](SunnyboticsOS_V0_Architecture_Report.md) | Full system design and rationale |
| [Integration guide](INTEGRATION_HANDOFF.md) | Bringing the machine layer up against the OS Core |
| [Machine layer reference](machine-layer/README.md) | ROS 2 packages in detail |

---

## Team

Built and maintained by Sunnybotics.

| Engineer | Area |
|---|---|
| Santiago Puentes | Engineering lead |
| Avinash Maharoliya | OS Core and platform |
| Abdel | Robotics, ROS 2 machine layer |

## License

Apache License 2.0. See [LICENSE](LICENSE).

---

*SunnyboticsOS V0 · Sunnybotics · September 2026*
