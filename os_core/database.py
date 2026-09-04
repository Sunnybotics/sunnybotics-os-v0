"""
os_core/database.py
SQLite persistence for the SunnyboticsOS Core.

Tables:
  machines      — one row per registered machine, updated on every heartbeat
  missions      — one row per dispatched mission, full lifecycle
  mission_events — append-only audit log

WAL mode keeps reads fast while missions are being written.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent / "sunnybotics_os.db"

_local = threading.local()


def get_connection() -> sqlite3.Connection:
    """Return a per-thread connection with WAL and row_factory set."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call multiple times."""
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS machines (
            machine_id          TEXT PRIMARY KEY,
            machine_type        TEXT,
            capabilities        TEXT,           -- JSON array string
            state               TEXT DEFAULT 'AVAILABLE',
            health_connected    INTEGER DEFAULT 1,
            health_battery_pct  REAL,
            health_faults       TEXT DEFAULT '[]',  -- JSON array
            location_x          REAL,
            location_y          REAL,
            location_frame      TEXT,
            current_mission_id  TEXT,
            registered_at       TEXT NOT NULL,
            last_seen_at        TEXT NOT NULL,
            last_mission_at     TEXT
        );

        CREATE TABLE IF NOT EXISTS missions (
            mission_id              TEXT PRIMARY KEY,
            capability_required     TEXT NOT NULL,
            objective               TEXT,
            parameters_json         TEXT,
            state                   TEXT NOT NULL DEFAULT 'PENDING',
            assigned_machine_id     TEXT,
            progress_percent        INTEGER DEFAULT 0,
            status_message          TEXT,
            result_state            TEXT,
            error_code              TEXT,
            result_json             TEXT,
            created_at              TEXT NOT NULL,
            updated_at              TEXT NOT NULL,
            completed_at            TEXT,
            FOREIGN KEY(assigned_machine_id) REFERENCES machines(machine_id)
        );

        CREATE TABLE IF NOT EXISTS mission_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id      TEXT NOT NULL,
            event_type      TEXT NOT NULL,
            old_state       TEXT,
            new_state       TEXT,
            machine_id      TEXT,
            progress        INTEGER,
            note            TEXT,
            ts              TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_machines_state ON machines(state);
        CREATE INDEX IF NOT EXISTS idx_missions_state ON missions(state);
        CREATE INDEX IF NOT EXISTS idx_missions_machine ON missions(assigned_machine_id);
        CREATE INDEX IF NOT EXISTS idx_events_mission ON mission_events(mission_id);
    """)
    conn.commit()


# ── Machines ──────────────────────────────────────────────────────────────────

def upsert_machine(conn: sqlite3.Connection, m: dict) -> None:
    conn.execute("""
        INSERT INTO machines
            (machine_id, machine_type, capabilities, state,
             health_connected, health_battery_pct, health_faults,
             location_x, location_y, location_frame,
             current_mission_id, registered_at, last_seen_at)
        VALUES
            (:machine_id, :machine_type, :capabilities, :state,
             :health_connected, :health_battery_pct, :health_faults,
             :location_x, :location_y, :location_frame,
             :current_mission_id, :registered_at, :last_seen_at)
        ON CONFLICT(machine_id) DO UPDATE SET
            machine_type        = excluded.machine_type,
            capabilities        = excluded.capabilities,
            state               = excluded.state,
            health_connected    = excluded.health_connected,
            health_battery_pct  = excluded.health_battery_pct,
            health_faults       = excluded.health_faults,
            location_x          = excluded.location_x,
            location_y          = excluded.location_y,
            location_frame      = excluded.location_frame,
            current_mission_id  = excluded.current_mission_id,
            last_seen_at        = excluded.last_seen_at
    """, m)
    conn.commit()


def patch_machine_status(conn: sqlite3.Connection, machine_id: str, patch: dict, now: str) -> bool:
    """Apply a partial update (heartbeat PATCH). Returns False if machine unknown."""
    row = conn.execute("SELECT machine_id FROM machines WHERE machine_id=?", (machine_id,)).fetchone()
    if not row:
        return False

    sets = ["last_seen_at = :now"]
    params: dict = {"machine_id": machine_id, "now": now}

    if "state" in patch:
        sets.append("state = :state")
        params["state"] = patch["state"]
    if "current_mission_id" in patch:
        sets.append("current_mission_id = :current_mission_id")
        params["current_mission_id"] = patch["current_mission_id"]
    if "health" in patch:
        h = patch["health"]
        sets.append("health_connected = :health_connected")
        sets.append("health_faults = :health_faults")
        params["health_connected"] = int(h.get("connected", True))
        params["health_faults"] = str(h.get("faults", []))
        if "battery_pct" in h:
            sets.append("health_battery_pct = :health_battery_pct")
            params["health_battery_pct"] = h["battery_pct"]
    if "location" in patch and patch["location"]:
        loc = patch["location"]
        sets.append("location_x = :location_x")
        sets.append("location_y = :location_y")
        sets.append("location_frame = :location_frame")
        params["location_x"] = loc.get("x")
        params["location_y"] = loc.get("y")
        params["location_frame"] = loc.get("frame_id")

    conn.execute(f"UPDATE machines SET {', '.join(sets)} WHERE machine_id=:machine_id", params)
    conn.commit()
    return True


def get_all_machines(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM machines ORDER BY machine_id").fetchall()
    return [dict(r) for r in rows]


def get_machine(conn: sqlite3.Connection, machine_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM machines WHERE machine_id=?", (machine_id,)).fetchone()
    return dict(row) if row else None


# ── Missions ──────────────────────────────────────────────────────────────────

def insert_mission(conn: sqlite3.Connection, m: dict) -> None:
    conn.execute("""
        INSERT INTO missions
            (mission_id, capability_required, objective, parameters_json,
             state, assigned_machine_id, created_at, updated_at)
        VALUES
            (:mission_id, :capability_required, :objective, :parameters_json,
             :state, :assigned_machine_id, :created_at, :updated_at)
    """, m)
    conn.commit()


def update_mission(conn: sqlite3.Connection, mission_id: str, patch: dict) -> None:
    if not patch:
        return
    sets = [f"{k} = :{k}" for k in patch]
    patch["mission_id"] = mission_id
    conn.execute(
        f"UPDATE missions SET {', '.join(sets)} WHERE mission_id = :mission_id",
        patch,
    )
    conn.commit()


def get_all_missions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM missions ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_mission(conn: sqlite3.Connection, mission_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
    return dict(row) if row else None


def get_active_mission_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT mission_id FROM missions WHERE state IN ('ASSIGNED','RUNNING')"
    ).fetchall()
    return [r[0] for r in rows]


# ── Events ────────────────────────────────────────────────────────────────────

def add_mission_event(
    conn: sqlite3.Connection,
    mission_id: str,
    event_type: str,
    *,
    old_state: str = None,
    new_state: str = None,
    machine_id: str = None,
    progress: int = None,
    note: str = None,
    ts: str,
) -> None:
    conn.execute("""
        INSERT INTO mission_events
            (mission_id, event_type, old_state, new_state, machine_id, progress, note, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (mission_id, event_type, old_state, new_state, machine_id, progress, note, ts))
    conn.commit()


def get_mission_events(conn: sqlite3.Connection, mission_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM mission_events WHERE mission_id=? ORDER BY id",
        (mission_id,)
    ).fetchall()
    return [dict(r) for r in rows]
