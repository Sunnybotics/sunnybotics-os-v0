# SunnyboticsOS v0 — Technical Architecture & Sprint Report

> **Author:** Sunnybotics  
> **Date:** September 2026  
> **Sprint:** Task 1 — First Operating Layer  
> **Status:** 🔨 Active — Integrated & Verified (ROS 2 Jazzy + OS Core)

---

## Table of Contents

1. [What We Are Building & Why](#1-what-we-are-building--why)
2. [Adopted Architecture & Realized Decisions](#2-adopted-architecture--realized-decisions)
3. [3-Layer Architecture](#3-3-layer-architecture)
4. [Full End-to-End Data Flow](#4-full-end-to-end-data-flow)
5. [Common Machine Interface (CMI)](#5-common-machine-interface-cmi)
6. [Mission State Machine](#6-mission-state-machine)
7. [Tiered Machine Selection & Dynamic Physics](#7-tiered-machine-selection--dynamic-physics)
8. [OS Core & Dashboard](#8-os-core--dashboard)
9. [ROS 2 Layer (Adapter + Machine Nodes)](#9-ros-2-layer-adapter--machine-nodes)
10. [Adapter REST API Contract](#10-adapter-rest-api-contract)
11. [OS Core REST API Contract](#11-os-core-rest-api-contract)
12. [One-Command Pipeline (run.sh)](#12-one-command-pipeline-runsh)
13. [Integration Test Checklist](#13-integration-test-checklist)
14. [Technical Roadmap](#14-technical-roadmap)

---

## 1. What We Are Building & Why

Sunnybotics today has real robots performing paid work at solar sites. The goal is **not** to keep building individual robots independently.

We are building **SunnyboticsOS** — an operating and intelligence layer for robotic work in energy infrastructure.

### The Evolution

```
Human operates machine
    ↓
SunnyboticsOS connects and understands the machine
    ↓
Human assigns work instead of controlling movements
    ↓
OS orchestrates missions
    ↓
Robots make more local autonomous decisions
    ↓
OS coordinates multiple machines and workflows
    ↓
System learns from operations → preventive & predictive decisions
```

### The Fundamental Principle

```
Different machines. One common interface. One operating layer. Mission in. Execution state back.
```

---

## 2. Adopted Architecture & Realized Decisions

| Aspect | Initial Conception | Implemented & Working Architecture |
|---|---|---|
| OS-to-Robot Comms | Combined inside one process | **Two decoupled processes:** OS Core (:9000) ↔ Adapter (:8001) |
| Protocol Boundary | Direct ROS in OS | **HTTP REST / JSON** over TCP (OS uses zero ROS libraries) |
| Robot Comms | Topic pub/sub | **ROS 2 Actions** (`Mission.action` with goal, feedback, result) |
| State Delivery | OS polling machine topics | **Outbound Push:** Adapter pushes `register`, `status`, and `report` to OS |
| Machine Selection | Simple capability check | **Two-stage Filtering:** Adapter checks Tier 1 (online, mode, available) + Tier 2 (<10% battery floor) |
| Mission ID Lifecycle | Independent generated IDs | **Unified `mission_id`:** OS generates ID, passes in dispatch, Adapter/Robot mirrors it end-to-end |
| ROS Distribution | Humble / Iron | **ROS 2 Jazzy** (Ubuntu 24.04 compatible) |
| Machine Fleet | Generic stubs | **`rover_01`** (`CLEANING`) and **`rover_02`** (`INSPECTION`) |

---

## 3. 3-Layer Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                          LAYER 1 — SunnyboticsOS Core                        │
│                                                                              │
│   ┌─────────────────┐     ┌──────────────────────┐     ┌─────────────────┐ │
│   │  Dashboard UI   │────▶│   OS Core Server     │────▶│  SQLite DB      │ │
│   │  (Streamlit)    │◀────│  (FastAPI service)    │◀────│  (machines,     │ │
│   │  port 8501      │     │  port 9000           │     │   missions,     │ │
│   └─────────────────┘     └──────────┬───────────┘     │   events)       │ │
│         Owner: Sunnybotics            │                 └─────────────────┘ │
│         Tech: FastAPI + SQLite        │                                      │
└──────────────────────────────────────┼──────────────────────────────────────┘
                                        │
                          Common Machine Interface (CMI)
                          HTTP REST (JSON over TCP)
                                        │
┌──────────────────────────────────────┼──────────────────────────────────────┐
│                     LAYER 2 — ROS 2 / REST Adapter                          │
│                                      ▼                                       │
│   ┌─────────────────────────────────────────────────────────────────────┐  │
│   │  Adapter FastAPI (port 8001)                                         │  │
│   │  • Listens for ROS 2 heartbeats via DDS                              │  │
│   │  • Pushes machine register/heartbeats to OS Core (:9000)             │  │
│   │  • Receives POST /api/v0/missions from OS Core                       │  │
│   │  • Filters capable & available machines (battery >= 10%)             │  │
│   │  • Dispatches ROS 2 Action Goal to target machine                    │  │
│   │  • Streams ROS feedback/result reports back to OS Core               │  │
│   └─────────────────────────────┬───────────────────────────────────────┘  │
│         Owner: Sunnybotics       │ ROS 2 (DDS)                              │
│         Tech: FastAPI + rclpy    │ ROS 2 Jazzy                              │
└──────────────────────────────────┼───────────────────────────────────────────┘
                                    │
                         ROS 2 Actions + MachineState.msg
                                    │
┌───────────────────────────────────┼───────────────────────────────────────────┐
│                        LAYER 3 — Machine Nodes                                │
│                                   │                                            │
│   ┌──────────────────────┐        │        ┌──────────────────────┐           │
│   │  rover_01            │◀───────┤        │  rover_02            │           │
│   │  Type: cleaning      │        └───────▶│  Type: inspection    │           │
│   │  Cap:  [CLEANING]    │                 │  Cap:  [INSPECTION]  │           │
│   │  [SIMULATED]         │                 │  [SIMULATED]         │           │
│   └──────────────────────┘                 └──────────────────────┘           │
│         Owner: Sunnybotics                                                     │
│         Tech:  rclpy + ROS 2 Action Server                                    │
└────────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Full End-to-End Data Flow

```
Step 1: Fleet Discovery
        rover_01 / rover_02 publish MachineState.msg at 1 Hz
        → Adapter discovers rovers on ROS 2 DDS
        → Adapter calls: POST http://localhost:9000/api/v0/machines/register
        → OS Core saves to SQLite; Dashboard displays live rovers with battery and AVAILABLE badge

Step 2: Mission Creation
        Operator / Dashboard submits mission:
        POST /api/v0/missions { "capability_required": "CLEANING", "objective": "clean aisle 3" }
        → OS Core generates unique mission_id (e.g., msn-a1b2c3d4)
        → OS Core creates mission row (PENDING) in SQLite

Step 3: Dispatch Forwarding
        OS Core forwards request to Adapter:
        POST http://localhost:8001/api/v0/missions
        { "mission_id": "msn-a1b2c3d4", "capability_required": "CLEANING", "objective": "clean aisle 3" }

Step 4: Machine Selection & Action Goal
        Adapter validates capability, availability, and battery (>=10%)
        Matches rover_01
        Sends ROS 2 Action Goal on /machines/rover_01/execute_mission with mission_id

Step 5: Execution & Distance Scaling
        rover_01 accepts goal → transitions to RUNNING
        Calculates steps based on target distance (e.g. 10 to 60 steps)
        Streams ROS 2 Action Feedback (10%, 20%, ... 100%)
        Adapter catches feedback and pushes:
        POST http://localhost:9000/api/v0/missions/msn-a1b2c3d4/report
        { "status": "RUNNING", "progress_percent": 40, "detail": "clean aisle 3 -- 40%" }
        Dashboard progress bar fills up dynamically

Step 6: Completion
        rover_01 finishes → returns RESULT_COMPLETED
        Adapter pushes terminal report:
        POST http://localhost:9000/api/v0/missions/msn-a1b2c3d4/report
        { "status": "COMPLETED", "progress_percent": 100 }
        OS Core updates SQLite: state = COMPLETED
        Dashboard displays green COMPLETED badge

Step 7: Cool-down & Reset
        rover_01 holds COMPLETED for 3 seconds, then resets to AVAILABLE for next job
```

---

## 5. Common Machine Interface (CMI)

### 5.1 MachineState.msg (Published at 1 Hz)

```
string interface_version
uint32 seq
string machine_id
string machine_type
string[] capabilities
uint8 state                 # AVAILABLE=0, ASSIGNED=1, RUNNING=2, COMPLETED=3, EXCEPTION=4
uint8 health                # OK=0, DEGRADED=1, FAULT=2
uint8 operating_mode        # MANUAL=0, TELEOP=1, SEMI_AUTO=2, AUTOMATIC=3
uint8 connection_status     # ONLINE=0, OFFLINE=1, CONNECTION_BROKEN=2
bool has_battery
float32 battery_percent
bool has_location
string frame_id
float64 latitude
float64 longitude
string current_mission_id
string[] active_errors
uint8[] active_error_severities
builtin_interfaces/Time stamp
```

### 5.2 Mission.action (Dispatched over ROS 2)

```
# Goal
string mission_id
string capability_required
string objective
string parameters_json
---
# Result
uint8 RESULT_COMPLETED = 0
uint8 RESULT_EXCEPTION = 1
uint8 result_state
string error_code
string message
string result_json
---
# Feedback
uint8 progress_percent
string status_message
```

---

## 6. Mission State Machine

```
   [ PENDING ] ── (OS creates mission)
        │
        ▼
   [ ASSIGNED ] ── (Adapter selects machine & sends Action Goal)
        │
        ▼
   [ RUNNING ] ── (Machine executes, sends feedback 0→100%)
        │
   ┌────┴─────────────────────────────┐
   ▼                                  ▼
[ COMPLETED ]                   [ EXCEPTION ]
(All steps succeeded)           • OBSTACLE_DETECTED
                                • BATTERY_DEPLETED
                                • Adapter/Machine refusal (<10% battery)
```

---

## 7. Tiered Machine Selection & Dynamic Physics

### Hard Eligibility & Feasibility (Layer 2 - Adapter)
1. **Capability Matching:** Case-insensitive string matching (`"CLEANING"` / `"INSPECTION"`).
2. **Connectivity:** Must report `connection_status == ONLINE` and not be stale (>5s heartbeat).
3. **Availability:** State must be `AVAILABLE`.
4. **Autonomous Mode:** `operating_mode == AUTOMATIC`.
5. **Battery Floor (10% Minimum):**
   - If `battery_percent < 10.0%`, Adapter refuses dispatch (`409 Conflict`) with reason `battery under minimum`.
   - Rover also internally rejects goal if battery < 10%.

### Depletion & Dying Signal
- If a rover's battery reaches `0.0%` during mission execution:
  - Aborts goal with `BATTERY_DEPLETED`.
  - Emits dying heartbeat: `health = FAULT`, `active_errors = ["BATTERY_DEPLETED"]`.
  - Turns off radio (`sim_connection_status = offline`).
  - Dashboard displays: `💀 0% (DEAD)` and `⚠️ BATTERY_DEPLETED`.

### Distance-Scaled Mission Duration
- Execution steps scale dynamically with distance:
  $$\text{steps} = \text{clamp}(10, 60, 10 + \lfloor\text{distance} / 5\rfloor)$$
- Farther goals take proportionally longer, creating realistic feedback timing.

---

## 8. OS Core & Dashboard

### Tech Stack
- **FastAPI** (Async Python 3.9+)
- **SQLite** with WAL mode (`sunnybotics_os.db`)
- **Streamlit** (Live dark-theme fleet monitoring & mission dispatch)

### Files
```
os_core/
├── main.py          # FastAPI application (Push ingestion + Pull dispatch APIs)
├── database.py      # SQLite operations for machines, missions, audit events
└── requirements.txt # Dependencies (fastapi, uvicorn, httpx, pydantic, streamlit)

dashboard/
└── app.py           # Real-time Streamlit dashboard
```

---

## 9. ROS 2 Layer (Adapter + Machine Nodes)

### Tech Stack
- **ROS 2 Jazzy** / Python 3.12 / Ubuntu 24.04
- **colcon build** workspace

### Files
```
machine-layer/
├── src/
│   ├── sunnybotics_cmi/        # MachineState.msg, Mission.action
│   ├── sunnybotics_adapter/    # Adapter node (REST API + ROS Action client + OS client)
│   └── sunnybotics_machines/   # Simulated rovers (rover_01, rover_02)
└── demo/demo.py                # 4-scenario end-to-end test script
```

---

## 10. Adapter REST API Contract (Port 8001)

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v0/machines` | Returns snapshot of all active machines |
| `GET` | `/api/v0/machines/{id}` | Returns single machine details |
| `POST` | `/api/v0/missions` | Dispatches mission by capability (accepts `mission_id`) |
| `GET` | `/api/v0/missions/{id}` | Returns terminal or in-flight mission state |
| `GET` | `/` | Adapter service info & machine counts |

---

## 11. OS Core REST API Contract (Port 9000)

### Push Endpoints (Called by Adapter)
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v0/machines/register` | Registers machine on first discovery |
| `PATCH` | `/api/v0/machines/{id}/status` | 1 Hz heartbeat update (battery, state, location) |
| `POST` | `/api/v0/missions/{id}/report` | In-flight progress or completion/exception report |

### Pull Endpoints (Called by Dashboard / Operators)
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v0/missions` | Creates mission & forwards to Adapter with `mission_id` |
| `GET` | `/api/v0/missions` | Lists all missions with latest state |
| `GET` | `/api/v0/missions/{id}` | Single mission detail |
| `GET` | `/api/v0/missions/{id}/events` | Chronological audit log of mission state transitions |
| `GET` | `/api/v0/machines` | Lists all registered fleet machines |
| `GET` | `/health` | System status & registered machine count |

---

## 12. One-Command Pipeline (run.sh)

Sunnybotics provides an automated runner script for the full stack:

```bash
# Start all 4 processes (OS Core, Machines, Adapter, Dashboard)
./run.sh

# Check running ports
./run.sh status

# Cleanly stop everything
./run.sh stop
```

---

## 13. Integration Test Checklist

| Scenario | Trigger | Expected Outcome | Verified |
|---|---|---|:---:|
| **Golden Run** | Dispatch `CLEANING` mission | `rover_01` assigned, progress 0→100%, status `COMPLETED` | ✅ |
| **Capability Routing** | Dispatch `INSPECTION` mission | `rover_02` assigned without specifying robot name | ✅ |
| **Simulated Failure** | Dispatch with `simulate_failure: true` | Progress hits 60% → aborts with `OBSTACLE_DETECTED` | ✅ |
| **Low Battery Refusal** | Rover battery < 10% | Adapter returns `409` conflict (`battery under minimum`) | ✅ |
| **Battery Depletion** | Rover battery hits 0% mid-run | Rover aborts with `BATTERY_DEPLETED`, dies, link marked broken | ✅ |
| **Dynamic Speed** | Set distant target coordinates | Execution steps increase proportionally with distance | ✅ |

---

## 14. Technical Roadmap

```
Phase 1 (Done): CMI Contract & Simulated Fleet
Phase 2 (Current): SunnyboticsOS v0 — Dispatch, Push/Pull Telemetry, Live Dashboard
Phase 3 (Next): Connect Real Hardware (JC600L solar cleaning rover via RS-232 / LTE)
Phase 4 (Future): Supervised Autonomy (human-in-the-loop overrides & inspection-to-cleaning chains)
Phase 5 (Future): Multi-Robot Fleet Coordination & Autonomous Solar Site
```

---

*SunnyboticsOS Sprint 1 · September 2026*  
*Sunnybotics*
