# SunnyboticsOS V0 — Integration Handoff
### OS Core → Machine Layer

> **TL;DR:** The OS Core (FastAPI + SQLite + Dashboard) and the machine layer (ROS 2) are developed as separate components. This doc explains exactly how to connect them on WSL and run a full end-to-end test.

---

## What Each Layer Does

```
┌──────────────────────────────────────────────────────┐
│  OS CORE LAYER — runs on Windows (or anywhere)       │
│                                                      │
│  os_core/main.py      — FastAPI on port 9000         │
│  os_core/database.py  — SQLite persistence           │
│  dashboard/app.py     — Streamlit UI on port 8501    │
│                                                      │
│  Receives pushes FROM your adapter.                  │
│  Exposes pull APIs TO the dashboard.                 │
└──────────────────────┬───────────────────────────────┘
                       │  HTTP  (both directions)
┌──────────────────────▼───────────────────────────────┐
│  MACHINE LAYER — runs on WSL (needs ROS 2)           │
│                                                      │
│  sunnybotics_adapter  — REST↔ROS2 bridge (port 8001) │
│  sunnybotics_machines — rover_01 + rover_02 (sim)    │
│  sunnybotics_cmi      — MachineState.msg + Mission.action│
└──────────────────────────────────────────────────────┘
```

---

## What the OS Core Layer Provides — File by File

### `os_core/main.py` — The OS Core (FastAPI)
The brain of SunnyboticsOS. This is the server your adapter talks to.

It has **two sets of endpoints:**

**Push endpoints** — your `os_client.py` calls these automatically:
```
POST  /api/v0/machines/register        ← called when adapter starts, registers each machine
PATCH /api/v0/machines/{id}/status     ← called every 1 Hz heartbeat, updates machine state
POST  /api/v0/missions/{id}/report     ← called on every feedback step + final result
```

**Pull endpoints** — the dashboard + operators use these:
```
POST  /api/v0/missions                 ← create + dispatch a mission (OS sends to adapter)
GET   /api/v0/missions                 ← list all missions
GET   /api/v0/missions/{id}            ← single mission status
GET   /api/v0/missions/{id}/events     ← full audit log
GET   /api/v0/machines                 ← registered machines
GET   /health                          ← liveness check
```

> **Key thing for you:** your `os_client.py` already calls the right endpoints with the right body shapes. You do NOT need to change anything in your code. Just point `--os-url` at wherever I'm running.

---

### `os_core/database.py` — SQLite Persistence
Stores everything permanently so the dashboard has history:
- `machines` table — one row per machine, updated on every heartbeat
- `missions` table — one row per mission, full lifecycle from PENDING → COMPLETED/EXCEPTION
- `mission_events` table — append-only audit log (every state change)

---

### `dashboard/app.py` — Streamlit Real-Time UI
- **Fleet panel** — shows all registered machines with live state badges and battery %
- **Mission log** — all missions with progress bars, filterable by state
- **Dispatch panel** (sidebar) — create new missions and watch them execute in real-time
- **Auto-refreshes every 3 seconds**

---

### `sim_adapter/main.py` — Windows Test Double (you can ignore this)
A pure Python mock of your adapter — no ROS 2. I use this to test on Windows without WSL. You have the real thing, so you don't need this. It's there for reference only.

---

## How the Push Flow Works

When your adapter starts and connects to `--os-url http://<os-core-ip>:9000`:

```
1. rover_01 starts publishing MachineState.msg at 1 Hz
2. adapter discovers rover_01 → calls os_client.register_machine(descriptor)
   → POST /api/v0/machines/register  →  OS Core stores it in SQLite
   → Dashboard shows rover_01: AVAILABLE 🟢

3. Every 1 Hz heartbeat:
   → PATCH /api/v0/machines/rover_01/status  →  OS Core updates battery, position, state

4. Someone dispatches a mission (from dashboard or curl):
   → OS Core calls POST http://localhost:8001/api/v0/missions
   → Your adapter selects a machine, sends Mission.action goal to rover_01
   → rover_01 starts: state = RUNNING

5. rover_01 sends feedback every step:
   → os_client.report_mission(mission_id, "RUNNING", detail, progress_percent)
   → POST /api/v0/missions/{id}/report  →  OS Core updates progress in SQLite
   → Dashboard shows progress bar filling up

6. rover_01 finishes:
   → os_client.report_mission(mission_id, "COMPLETED", detail, 100)
   → POST /api/v0/missions/{id}/report  →  OS Core marks mission COMPLETED
   → Dashboard shows ✓ COMPLETED
```

---

## Step-by-Step: Running the Full Stack on Your WSL

### Prerequisites
- ROS 2 Humble or Iron installed in WSL
- Python packages: `fastapi uvicorn httpx` (already in your adapter dependencies)
- The repo cloned: `git clone https://github.com/Sunnybotics/sunnybotics-os-v0`

---

### Step 1 — Build the ROS 2 packages

```bash
cd sunnyboticsosV0/machine-layer
colcon build --symlink-install
source install/setup.bash
```

---

### Step 2 — Start the machine nodes (Terminal A in WSL)

```bash
# From machine-layer/
ros2 launch sunnybotics_machines machines.launch.py
```

Expected output:
```
[rover_01]: [SIMULATED MACHINE] rover_01 (cleaning_rover) capabilities=['CLEANING']
[rover_01]:   state  -> /machines/rover_01/state @ 1 Hz
[rover_01]:   action -> /machines/rover_01/execute_mission
[rover_02]: [SIMULATED MACHINE] rover_02 (inspection_rover) capabilities=['INSPECTION']
```

---

### Step 3 — Start the adapter (Terminal B in WSL)

**Option A — Standalone (no OS Core needed yet, for your own testing):**
```bash
python -m sunnybotics_adapter.main
```
Adapter runs at `http://localhost:8001`. Test it:
```bash
curl http://localhost:8001/api/v0/machines
```

**Option B — With OS Core push enabled (the real integration test):**
```bash
# Replace <os-core-ip> with the OS Core host IP (or 172.x.x.x WSL gateway IP if same machine)
python -m sunnybotics_adapter.main --os-url http://<os-core-ip>:9000
```

Expected output when OS push is enabled:
```
OS client -> http://<os-core-ip>:9000/api/v0 (push mode enabled)
adapter up -- watching for /machines/<id>/state, no machine list configured anywhere
discovered machine 'rover_01' on /machines/rover_01/state
'rover_01' registered with the OS (201)
discovered machine 'rover_02' on /machines/rover_02/state
'rover_02' registered with the OS (201)
```

---

### Step 4 — Verify machines appeared on OS Core

```bash
curl http://<os-core-ip>:9000/api/v0/machines
```

Expected:
```json
{
  "machines": [
    {"machine_id": "rover_01", "state": "AVAILABLE", "battery_percent": 99.9, ...},
    {"machine_id": "rover_02", "state": "AVAILABLE", "battery_percent": 99.9, ...}
  ],
  "total": 2
}
```

---

### Step 5 — Dispatch a mission (from WSL or Windows)

```bash
# Dispatch a CLEANING mission
curl -X POST http://<os-core-ip>:9000/api/v0/missions \
  -H "Content-Type: application/json" \
  -d '{"capability_required":"CLEANING","objective":"clean row 3","parameters":{"target_x":15.0,"target_y":10.0,"simulate_failure":false}}'
```

Expected response (201):
```json
{
  "mission_id": "msn-abc12345",
  "assigned_machine_id": "rover_01",
  "state": "ASSIGNED",
  "capability_required": "CLEANING",
  "objective": "clean row 3"
}
```

---

### Step 6 — Watch mission progress

```bash
# Poll mission state
curl http://<os-core-ip>:9000/api/v0/missions/msn-abc12345
```

You should see `state` change:
```
ASSIGNED → RUNNING → (progress_percent: 10, 20, 30...) → COMPLETED
```

---

### Step 7 — Test failure path

```bash
curl -X POST http://<os-core-ip>:9000/api/v0/missions \
  -H "Content-Type: application/json" \
  -d '{"capability_required":"CLEANING","objective":"clean row 5","parameters":{"simulate_failure":true}}'
```

Expected: mission reaches 60% progress then goes to `EXCEPTION` with `OBSTACLE_DETECTED`.

---

### Step 8 — Test Tier 1 filters (no available machine)

```bash
# Dispatch first mission (rover_01 now RUNNING)
curl -X POST http://<os-core-ip>:9000/api/v0/missions \
  -H "Content-Type: application/json" \
  -d '{"capability_required":"CLEANING","objective":"mission A"}'

# Immediately dispatch second CLEANING mission while rover_01 is still running
curl -X POST http://<os-core-ip>:9000/api/v0/missions \
  -H "Content-Type: application/json" \
  -d '{"capability_required":"CLEANING","objective":"mission B"}'
```

Expected: second dispatch returns `409` with:
```json
{"reason": "no_available_machine", "capability_required": "CLEANING"}
```

---

### Step 9 — Verify full audit log

```bash
curl http://<os-core-ip>:9000/api/v0/missions/msn-abc12345/events
```

Should show every state transition logged: CREATED → ASSIGNED → RUNNING (via reports) → COMPLETED.

---

## Network Setup (If Running on Same Machine)

If the OS Core runs on Windows and the adapter runs on WSL on the same laptop:

```bash
# Find WSL gateway IP (your Windows IP from WSL perspective)
ip route | grep default
# Usually: 172.17.x.x or 172.28.x.x

# Use that IP for --os-url
python -m sunnybotics_adapter.main --os-url http://172.17.x.1:9000
```

If OS Core is on a different machine on the same network:
```bash
python -m sunnybotics_adapter.main --os-url http://192.168.x.x:9000
```

---

## What Each Endpoint Does — Quick Reference

| Endpoint | Direction | Who Calls It | What It Does |
|---|---|---|---|
| `POST /api/v0/machines/register` | Adapter → OS | `os_client.register_machine()` | Machine appears in fleet view |
| `PATCH /api/v0/machines/{id}/status` | Adapter → OS | `os_client.patch_status()` every 1Hz | Live state updates |
| `POST /api/v0/missions/{id}/report` | Adapter → OS | `os_client.report_mission()` | Progress + completion |
| `POST /api/v0/missions` | Dashboard → OS → Adapter | Human/dashboard | Creates + dispatches mission |
| `GET /api/v0/machines` | Dashboard → OS | Dashboard auto-refresh | Show fleet |
| `GET /api/v0/missions` | Dashboard → OS | Dashboard auto-refresh | Show mission history |
| `GET /health` | Any | Monitoring | Liveness check |

---

## What to Check If Something Goes Wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| `register` calls fail with connection refused | OS Core not running | Start `uvicorn os_core.main:app --port 9000` |
| Machines appear but state never updates | Wrong `--os-url` IP | Check WSL gateway IP |
| `POST /api/v0/missions` returns 503 | OS Core can't reach adapter at port 8001 | OS Core calls `http://localhost:8001` — ensure adapter is running |
| Mission stuck at ASSIGNED | Adapter didn't receive dispatch from OS | Check OS Core log for `DISPATCH` line |
| 409 on every dispatch | All machines RUNNING or below battery floor | Wait for machine to return to AVAILABLE |

---

## Running the Dashboard

Run this on the OS Core host, on Windows (requires internet browser):

```
http://localhost:8501
```

You can also hit it from WSL browser if needed.

---

## Summary of the Integration Contract

```
Your os_client.py calls these 3 endpoints on my OS Core:

  POST   /api/v0/machines/register          ← on machine discovery
  PATCH  /api/v0/machines/{id}/status       ← every 1 Hz heartbeat
  POST   /api/v0/missions/{id}/report       ← on feedback/result

These 3 calls are already implemented in your code.
You don't need to change anything — just pass --os-url.

My OS Core calls this 1 endpoint on your adapter:

  POST   /api/v0/missions                   ← when a new mission is dispatched

This is already implemented in your rest_api.py.
```

---

*Sunnybotics · SunnyboticsOS V0 · September 2026*  
*Repo: https://github.com/Sunnybotics/sunnybotics-os-v0*
