# SunnyboticsOS V0

**Hardware-agnostic operating and intelligence layer for robotic work in energy infrastructure.**

> ⚡ **ALL machines in this V0 are SIMULATED.** The OS Core and CMI contract are real software.

---

## Architecture

```
┌────────────────────────────────────┐
│  SunnyboticsOS Core  (port 9000)   │  ← Avinash
│  FastAPI + SQLite                   │
│  • Receives machine registrations   │
│  • Tracks mission state             │
│  • Dashboard data API               │
└────────────────┬───────────────────┘
                 │  HTTP (push + pull)
┌────────────────▼───────────────────┐
│  Machine Adapter   (port 8001)      │  ← Abdel
│  FastAPI + rclpy                    │
│  • Tier 1+2 machine filtering       │
│  • Dispatches ROS 2 Action goals    │
│  • Pushes state to OS Core          │
└────────────────┬───────────────────┘
                 │  ROS 2 Actions + MachineState.msg
┌────────────────▼───────────────────┐
│  Machine Nodes  (rover_01, rover_02)│  ← Abdel
│  rclpy + [SIMULATED]                │
└────────────────────────────────────┘
```

---

## Quick Start

### Fastest: one command

```bash
./run.sh
```

Builds the machine layer on first run, creates a venv, installs everything,
starts all four processes, and waits for the machines to register. Ctrl-C
brings the whole stack down together. `./run.sh stop` and `./run.sh status`
work from a separate terminal. Logs land in `logs/`.

The steps below are what that script automates, spelled out for anyone who
wants a terminal per process instead.

### 1 — Install OS Core dependencies

```bash
cd <repo-root>
pip install -r os_core/requirements.txt
pip install streamlit   # for the dashboard
```

### 2 — Start the OS Core (Avinash's layer)

```bash
python -m uvicorn os_core.main:app --host 0.0.0.0 --port 9000
```

Check: http://localhost:9000/health

### 3 — Start Abdel's Adapter + Machine Nodes (separate terminal, needs ROS 2; developed on Jazzy, Humble/Iron should also work)

```bash
cd machine-layer
colcon build --symlink-install
source install/setup.bash

# Terminal A — machine nodes
ros2 launch sunnybotics_machines machines.launch.py

# Terminal B — adapter (pointing at the OS Core)
python -m sunnybotics_adapter.main --os-url http://localhost:9000
```

### 4 — Dashboard (separate terminal)

```bash
streamlit run dashboard/app.py
```

Open: http://localhost:8501

---

## Testing without ROS 2

Point the adapter at the OS stub and test the push endpoints:

```bash
# Instead of step 3, run Abdel's os_stub.py which mirrors the OS Core API
python machine-layer/tools/os_stub.py --port 9000

# Then run the adapter standalone
python -m sunnybotics_adapter.main  # no --os-url needed
```

Or hit the OS Core directly:

```bash
# Dispatch a mission
curl -X POST http://localhost:9000/api/v0/missions \
  -H "Content-Type: application/json" \
  -d '{"capability_required":"CLEANING","objective":"clean sector A","parameters":{"simulate_failure":false}}'

# List machines
curl http://localhost:9000/api/v0/machines

# List missions
curl http://localhost:9000/api/v0/missions
```

---

## OS Core API Contract

### Push (Adapter → OS)

| Method | Endpoint | Called by |
|--------|----------|-----------|
| `POST` | `/api/v0/machines/register` | Adapter on machine discovery |
| `PATCH` | `/api/v0/machines/{id}/status` | Adapter every heartbeat (~1 Hz) |
| `POST` | `/api/v0/missions/{id}/report` | Adapter on feedback/result |

### Pull (Dashboard / Operators → OS)

| Method | Endpoint | Returns |
|--------|----------|---------|
| `POST` | `/api/v0/missions` | Create + dispatch a mission |
| `GET` | `/api/v0/missions` | All missions |
| `GET` | `/api/v0/missions/{id}` | Single mission |
| `GET` | `/api/v0/missions/{id}/events` | Audit log |
| `GET` | `/api/v0/machines` | Registered machines |
| `GET` | `/api/v0/machines/{id}` | Single machine |
| `GET` | `/health` | Liveness check |

---

## Common Machine Interface

```
MachineState.msg  →  published by each machine at 1 Hz
Mission.action    →  goal sent by adapter, feedback + result from machine
```

See `machine-layer/src/sunnybotics_cmi/` for the full definitions.

---

## Project Structure

```
sunnyboticsosV0/
├── os_core/
│   ├── main.py          # FastAPI — all OS endpoints
│   ├── database.py      # SQLite — machines, missions, events
│   ├── requirements.txt
│   └── __init__.py
│
├── dashboard/
│   └── app.py           # Streamlit real-time dashboard
│
├── machine-layer/       # Abdel's ROS 2 layer
│   ├── src/
│   │   ├── sunnybotics_cmi/        # MachineState.msg + Mission.action
│   │   ├── sunnybotics_adapter/    # REST↔ROS2 bridge
│   │   └── sunnybotics_machines/   # Simulated machine nodes
│   └── tools/
│       └── os_stub.py   # Test double for the OS Core API
│
└── README.md
```

---

*SunnyboticsOS V0 · September 2026 · Avinash Maharoliya + Abdel*
