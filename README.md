# SunnyboticsOS V0

**A hardware-agnostic operating and orchestration layer for robotic work in energy infrastructure.**

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![ROS 2](https://img.shields.io/badge/ROS%202-Jazzy-22314E.svg?logo=ros)](https://docs.ros.org/en/jazzy/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)

<!-- TODO: replace with a 10-15s screen capture of the dashboard dispatching a
     mission and both rovers reporting state. This is the single highest-value
     addition to this file. -->
<!-- ![SunnyboticsOS dashboard](docs/media/dashboard.gif) -->

---

## The problem

A solar farm is not cleaned by one kind of machine. It is cleaned by rovers of
different models, inspected by others, and serviced by equipment that does not
exist yet. Every one of them speaks its own dialect: a different driver, a
different message set, a different idea of what "busy" means.

The usual answer is to write orchestration once per robot model. That does not
scale. Fleet logic gets copied and diverges, adding a machine type means editing
the scheduler, and the dispatcher ends up knowing the wiring of every unit it
commands.

## The approach

SunnyboticsOS inverts the dependency. **The OS asks for a capability, never for a
machine.**

A mission says *"something that can CLEAN, for sector A"*. It does not name a
robot, know how many exist, or care what any of them are made of. Machines
describe themselves through one shared contract — the **Common Machine Interface
(CMI)** — and anything that can speak it is dispatchable the moment it appears on
the network.

The practical consequence, and this V0's central claim:

> Starting a third machine is enough to make it appear in `GET /machines` and
> become eligible for work. **No restart, no configuration file, no code change.**

The adapter scans for topics matching `/machines/<id>/state` once per second and
subscribes to whatever it finds. Nothing in the system holds a list of machines.

---

## What is real and what is simulated

This is a V0. Being precise about the boundary matters more than overselling it.

| Component | Status | Notes |
|---|---|---|
| OS Core (FastAPI + SQLite) | **Real** | Mission lifecycle, persistence, audit log |
| CMI contract (`MachineState.msg`, `Mission.action`) | **Real** | Generated via `rosidl`, versioned |
| ROS 2 action loop | **Real** | `ActionServer` / `ActionClient`, goals, feedback, cancellation |
| DDS transport | **Real** | Standard ROS 2 middleware |
| Machine adapter (REST ↔ ROS 2) | **Real** | Dynamic discovery, capability dispatch |
| Streamlit dashboard | **Real** | Live fleet and mission views |
| Physical behaviour of `rover_01` / `rover_02` | **Simulated** | Node stubs; no hardware in the loop |

The orchestration and the ROS 2 loop run exactly as they would against hardware.
What is stubbed is the two machines at the far end of the action server.

---

## Architecture

```
┌─────────────────────────────────────────┐
│  SunnyboticsOS Core          port 9000  │
│  FastAPI + SQLite                       │
│  • Machine registry and heartbeats      │
│  • Mission lifecycle and audit log      │
│  • Read API for dashboard / operators   │
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

### The path of one mission

1. An operator posts a capability requirement to `POST /api/v0/missions`.
2. The OS Core persists it as `PENDING` — so the dashboard shows it immediately,
   before anything can go wrong — and forwards it to the adapter.
3. The adapter selects a machine. **Tier 1** narrows to machines advertising the
   capability; **Tier 2** narrows to those currently `AVAILABLE`. If either tier
   comes back empty, the rejection says which one did, so "no such machine" is
   never confused with "all of them are busy".
4. The adapter sends a `Mission` action goal. The machine node re-checks the
   capability and its own availability rather than trusting the dispatcher.
5. Feedback flows back as the mission runs; the OS Core records every transition
   in `mission_events`.
6. The mission ends `COMPLETED` or `EXCEPTION`. Never silently.

```
PENDING ──▶ ASSIGNED ──▶ RUNNING ──▶ COMPLETED
                                 └──▶ EXCEPTION
```

---

## The Common Machine Interface

The CMI is the reason this project exists. Two files define it:

```
machine-layer/src/sunnybotics_cmi/
├── msg/MachineState.msg     # published by every machine at 1 Hz
└── action/Mission.action    # goal, feedback and result for one unit of work
```

The design decisions inside them are deliberate and worth stating explicitly.

**A mission is an action, not a service or a topic.** A mission already has the
shape of an action: it is accepted or rejected up front, it runs long, it reports
progress, it can be cancelled, and it ends in exactly one terminal outcome. A
service call would block for the mission's entire duration. A topic would give no
acknowledgement and no result.

**`state` and `health` are separate fields.** A machine can be `RUNNING` while
`DEGRADED`, and perfectly `OK` while sitting idle. Collapsing the two would erase
the difference between *not working* and *not well*.

**`connection_status` is separate from both.** Without it, a machine that has gone
silent keeps reporting its last known state — and a silent machine reading
`AVAILABLE` would have missions dispatched into a void.

**Capabilities are free-form strings, not an enum.** Adding a capability must not
require a schema change, a rebuild, or a coordinated redeploy across the fleet.
The dispatcher matches on `capabilities` and never reads `machine_type` at all.

**`interface_version` and `seq` are in the envelope.** The version lets the OS
talk to a fleet that is only partly upgraded, instead of needing a flag-day
across every machine. The monotonic `seq` makes dropped messages detectable —
information no timestamp can recover.

**`has_battery` and `has_location` guard their own fields.** A mains-powered
machine reports `has_battery=false` rather than a misleading `0.0`, and `0,0` is
never ambiguous between *sitting on the origin* and *no idea where this is*.

**Type-specific data travels as JSON strings** (`parameters_json`,
`result_json`). This is the escape hatch that keeps the interface stable: a new
machine type invents new keys instead of new fields in the contract.

---

## Quick start

### Prerequisites

| Requirement | Version | Needed for |
|---|---|---|
| Python | 3.10+ | OS Core, adapter, dashboard |
| ROS 2 | Jazzy (Humble / Iron should also work) | Machine layer |
| OS | Linux or WSL 2 | ROS 2 requirement |

The OS Core and the dashboard run anywhere Python does, including native Windows.
Only the machine layer needs ROS 2. See [Running without ROS 2](#running-without-ros-2)
if you do not have it installed.

### One command

```bash
./run.sh
```

Builds the machine layer on first run, creates a virtualenv, installs
dependencies, starts all four processes, and waits for the machines to register.
`Ctrl-C` brings the whole stack down together. From another terminal,
`./run.sh stop` and `./run.sh status` work as expected. Logs land in `logs/`.

The steps below are what that script automates, for anyone who wants one terminal
per process.

### Manual startup

**1 — OS Core**

```bash
pip install -r os_core/requirements.txt
pip install streamlit

python -m uvicorn os_core.main:app --host 0.0.0.0 --port 9000
```

Verify: <http://localhost:9000/health>

**2 — Machine layer** *(requires ROS 2)*

```bash
cd machine-layer
colcon build --symlink-install
source install/setup.bash

# Terminal A — machine nodes
ros2 launch sunnybotics_machines machines.launch.py

# Terminal B — adapter, pointed at the OS Core
python -m sunnybotics_adapter.main --os-url http://localhost:9000
```

**3 — Dashboard**

```bash
streamlit run dashboard/app.py
```

Open: <http://localhost:8501>

### Dispatch a mission

```bash
curl -X POST http://localhost:9000/api/v0/missions \
  -H "Content-Type: application/json" \
  -d '{
        "capability_required": "CLEANING",
        "objective": "clean sector A",
        "parameters": {"simulate_failure": false}
      }'
```

Note that the request names a capability, not a machine.

---

## Running without ROS 2

The machine layer ships a stub that mirrors the OS Core API, so the adapter and
the HTTP contract can be exercised on a machine with no ROS 2 installation:

```bash
# In place of the machine layer
python machine-layer/tools/os_stub.py --port 9000

# Then the adapter, standalone
python -m sunnybotics_adapter.main
```

---

## OS Core API

### Push — adapter to OS

| Method | Endpoint | Called when |
|---|---|---|
| `POST` | `/api/v0/machines/register` | A machine is discovered |
| `PATCH` | `/api/v0/machines/{id}/status` | Every heartbeat (~1 Hz) |
| `POST` | `/api/v0/missions/{id}/report` | Mission feedback or result |

### Pull — dashboard and operators to OS

| Method | Endpoint | Returns |
|---|---|---|
| `POST` | `/api/v0/missions` | Create and dispatch a mission |
| `GET` | `/api/v0/missions` | All missions |
| `GET` | `/api/v0/missions/{id}` | One mission |
| `GET` | `/api/v0/missions/{id}/events` | Audit log for a mission |
| `GET` | `/api/v0/machines` | Registered machines |
| `GET` | `/api/v0/machines/{id}` | One machine |
| `GET` | `/health` | Liveness |

Interactive documentation is served by FastAPI at <http://localhost:9000/docs>.

---

## Data model

SQLite, three tables, indexed on the columns the dashboard actually filters by.

| Table | Holds |
|---|---|
| `machines` | Identity, capabilities, state, health, battery, location, last seen |
| `missions` | Capability required, objective, state, assignment, progress, result |
| `mission_events` | Append-only audit log: every state transition, with timestamps |

`mission_events` is the reason a mission can always be explained after the fact.
Every transition is recorded with its old state, new state, machine and note —
so "why did this mission fail" is a query, not an investigation.

---

## Concurrency

`rclpy` spins on a background thread while FastAPI serves on the main one. Every
attribute reachable from both is guarded by a lock, and the FastAPI endpoints are
deliberately synchronous so that they may block on it. This is stated here
because it is the kind of boundary that silently breaks under load if a later
contributor assumes otherwise.

---

## Project structure

```
sunnybotics-os-v0/
├── os_core/                          # OS Core — FastAPI + SQLite
│   ├── main.py                       #   All OS endpoints
│   ├── database.py                   #   Schema, queries, audit log
│   └── requirements.txt
│
├── machine-layer/                    # ROS 2 workspace
│   ├── src/
│   │   ├── sunnybotics_cmi/          #   The contract: .msg + .action
│   │   ├── sunnybotics_adapter/      #   REST ↔ ROS 2 bridge
│   │   └── sunnybotics_machines/     #   Simulated machine nodes
│   ├── demo/
│   └── tools/os_stub.py              #   Test double for the OS Core API
│
├── dashboard/app.py                  # Streamlit fleet and mission views
├── run.sh                            # Full-stack bring-up
├── SunnyboticsOS_V0_Architecture_Report.md   # Full system design
└── INTEGRATION_HANDOFF.md                   # Machine-layer setup guide
```

---

## Roadmap

| Stage | Scope |
|---|---|
| **V0** — current | Contract, orchestration and ROS 2 loop validated against simulated machines |
| **V1** | Hardware integration with deployed cleaning units; telemetry from real motor drivers |
| **V2** | Multi-machine scheduling policy, richer dispatch than first-available |

The contract was deliberately validated in simulation before touching hardware.
Changing `MachineState.msg` after a fleet depends on it is expensive; changing it
now is free.

---

## Documentation

| Document | Contents |
|---|---|
| [Architecture report](SunnyboticsOS_V0_Architecture_Report.md) | Full system design and rationale |
| [Integration guide](INTEGRATION_HANDOFF.md) | Setting up the machine layer against the OS Core |
| [Machine layer reference](machine-layer/README.md) | ROS 2 packages in depth |

---

## Team

Built and maintained by **Sunnybotics**.

| | |
|---|---|
| Santiago Puentes | Engineering lead |
| Avinash Maharoliya | OS Core and platform |
| Abdel | Robotics, ROS 2 machine layer |

## License

Apache License 2.0. See [LICENSE](LICENSE).

---

*SunnyboticsOS V0 · Sunnybotics · September 2026*
