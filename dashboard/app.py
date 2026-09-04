"""
dashboard/app.py — SunnyboticsOS V0 Real-Time Dashboard

Streamlit UI that connects to the OS Core (port 9000) and shows:
  - Fleet status: all registered machines with live state
  - Mission log: all missions with current state + progress
  - Dispatch panel: create new missions and watch them execute in real-time

Run:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

import httpx
import streamlit as st

# ── Config ────────────────────────────────────────────────────────────────────
OS_URL = "http://localhost:9000"
API   = f"{OS_URL}/api/v0"
AUTO_REFRESH_SEC = 3

# ── Page setup ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="SunnyboticsOS",
    page_icon="☀️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Sunnybotics Dark Theme ────────────────────────────────────────────────────
st.markdown("""
<style>
/* ── base ── */
:root {
    --yellow:   #F5C518;
    --orange:   #FF7043;
    --bg:       #0D0D0D;
    --surface:  #1A1A1A;
    --border:   #2A2A2A;
    --text:     #E8E8E8;
    --muted:    #888;
    --green:    #4CAF50;
    --red:      #F44336;
    --blue:     #42A5F5;
}
html, body, .stApp { background-color: var(--bg) !important; color: var(--text) !important; }

/* ── top bar ── */
.top-bar {
    background: linear-gradient(90deg, #1A1A1A, #111);
    border-bottom: 2px solid var(--yellow);
    padding: 1rem 2rem;
    display: flex;
    align-items: center;
    gap: 1rem;
    margin-bottom: 1.5rem;
}
.top-bar h1 { margin:0; font-size:1.6rem; color: var(--yellow); font-weight:700; }
.top-bar .sub { color: var(--muted); font-size:0.85rem; }
.sim-badge {
    background: #2a1a00;
    border: 1px solid var(--orange);
    color: var(--orange);
    padding: 3px 10px;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
    margin-left: auto;
}

/* ── cards ── */
.card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 0.75rem;
}
.card-header {
    font-size: 0.75rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 0.5rem;
}

/* ── state badges ── */
.badge {
    display: inline-block;
    padding: 3px 9px;
    border-radius: 4px;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.05em;
}
.b-AVAILABLE  { background:#1B3A1F; color:#4CAF50; border:1px solid #4CAF50; }
.b-RUNNING    { background:#1A2D3A; color:#42A5F5; border:1px solid #42A5F5; }
.b-ASSIGNED   { background:#2D2A10; color:#FFC107; border:1px solid #FFC107; }
.b-COMPLETED  { background:#0D2018; color:#66BB6A; border:1px solid #66BB6A; }
.b-EXCEPTION  { background:#3A1010; color:#EF5350; border:1px solid #EF5350; }
.b-PENDING    { background:#1E1E1E; color:#9E9E9E; border:1px solid #555; }
.b-OFFLINE    { background:#1E1E1E; color:#555;    border:1px solid #333; }

/* ── machine card ── */
.machine-card {
    background: #151515;
    border: 1px solid var(--border);
    border-left: 3px solid var(--yellow);
    border-radius: 6px;
    padding: 0.85rem 1rem;
    margin-bottom: 0.6rem;
}
.machine-id { font-weight: 700; font-size: 1rem; color: var(--text); }
.machine-meta { color: var(--muted); font-size: 0.78rem; margin-top: 2px; }
.machine-caps {
    font-size: 0.7rem;
    background: #222;
    border: 1px solid #333;
    border-radius: 4px;
    padding: 2px 6px;
    margin-right: 4px;
    color: var(--yellow);
}

/* ── mission row ── */
.mission-row {
    background: #151515;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.7rem 1rem;
    margin-bottom: 0.4rem;
    font-size: 0.82rem;
}
.mission-id { font-family: monospace; color: var(--muted); font-size: 0.75rem; }
.mission-objective { font-weight: 600; color: var(--text); }

/* ── progress bar ── */
.prog-wrap { background:#222; border-radius:4px; height:6px; margin-top:4px; }
.prog-fill  { background: var(--blue); border-radius:4px; height:6px; transition: width 0.5s; }
.prog-fill.done { background: var(--green); }
.prog-fill.err  { background: var(--red); }

/* ── stat box ── */
.stat-box {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1rem;
    text-align: center;
}
.stat-num { font-size: 2rem; font-weight: 700; color: var(--yellow); }
.stat-lbl { font-size: 0.75rem; color: var(--muted); margin-top:2px; }

/* ── sidebar ── */
section[data-testid="stSidebar"] {
    background: #111 !important;
    border-right: 1px solid var(--border);
}
.stButton button {
    background: var(--yellow) !important;
    color: #000 !important;
    font-weight: 700 !important;
    border: none !important;
    border-radius: 6px !important;
    padding: 0.5rem 1.2rem !important;
}
.stButton button:hover { background: #e6b800 !important; }
</style>
""", unsafe_allow_html=True)


# ── Data helpers ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=AUTO_REFRESH_SEC)
def fetch_machines() -> list[dict]:
    try:
        r = httpx.get(f"{API}/machines", timeout=3)
        return r.json().get("machines", [])
    except Exception:
        return []


@st.cache_data(ttl=AUTO_REFRESH_SEC)
def fetch_missions() -> list[dict]:
    try:
        r = httpx.get(f"{API}/missions", timeout=3)
        return r.json().get("missions", [])
    except Exception:
        return []


@st.cache_data(ttl=AUTO_REFRESH_SEC)
def fetch_health() -> dict:
    try:
        r = httpx.get(f"{OS_URL}/health", timeout=2)
        return r.json()
    except Exception:
        return {}


def _badge(state: str) -> str:
    return f'<span class="badge b-{state}">{state}</span>'


def _progress_bar(pct: int, state: str) -> str:
    cls = "done" if state == "COMPLETED" else ("err" if state == "EXCEPTION" else "")
    return (
        f'<div class="prog-wrap">'
        f'<div class="prog-fill {cls}" style="width:{pct}%"></div>'
        f'</div>'
        f'<div style="font-size:0.7rem;color:#888;margin-top:2px">{pct}%</div>'
    )


def _relative_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        delta = datetime.now(dt.tzinfo) - dt
        s = int(delta.total_seconds())
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s//60}m ago"
        return f"{s//3600}h ago"
    except Exception:
        return iso[:16]


def _caps_html(caps_json: str | list) -> str:
    if isinstance(caps_json, str):
        try:
            caps = json.loads(caps_json)
        except Exception:
            caps = [caps_json]
    else:
        caps = caps_json or []
    return "".join(f'<span class="machine-caps">{c}</span>' for c in caps)


def _battery_icon(pct: float | None) -> str:
    if pct is None:
        return "🔌"
    if pct <= 0:
        return "💀 0% (DEAD)"
    if pct < 10:
        return f"🚨 {pct:.0f}% (CRITICAL)"
    if pct <= 20:
        return f"🪫 {pct:.0f}%"
    if pct <= 60:
        return f"🔋 {pct:.0f}%"
    return f"🔋 {pct:.0f}%"


# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="top-bar">
  <div>
    <h1>☀️ SunnyboticsOS</h1>
    <div class="sub">Mission Engine · V0 · Real-time fleet view</div>
  </div>
  <div class="sim-badge">⚡ ALL MACHINES SIMULATED</div>
</div>
""", unsafe_allow_html=True)


# ── Sidebar — Dispatch Panel ──────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🚀 Dispatch Mission")

    capability = st.selectbox(
        "Capability required",
        ["CLEANING", "INSPECTION"],
        help="The OS asks by capability. The adapter picks the best available machine."
    )
    objective = st.text_input("Objective", placeholder="clean aisle 3")
    simulate_failure = st.checkbox("Simulate failure (OBSTACLE_DETECTED)")
    col_a, col_b = st.columns(2)
    target_x = col_a.number_input("Target X", value=10.0, step=1.0)
    target_y = col_b.number_input("Target Y", value=5.0, step=1.0)

    if st.button("Dispatch →", use_container_width=True):
        with st.spinner("Dispatching…"):
            try:
                r = httpx.post(f"{API}/missions", json={
                    "capability_required": capability,
                    "objective": objective or f"{capability.lower()} mission",
                    "parameters": {
                        "simulate_failure": simulate_failure,
                        "target_x": target_x,
                        "target_y": target_y,
                    },
                }, timeout=20)
                if r.status_code == 201:
                    body = r.json()
                    st.success(
                        f"✅ Dispatched!\n\n"
                        f"**{body.get('mission_id')}** → **{body.get('assigned_machine_id')}**"
                    )
                else:
                    detail = r.json()
                    reason = detail.get("reason", r.text)
                    st.error(f"❌ {r.status_code}: {reason}")
            except httpx.ConnectError:
                st.error(f"Cannot reach OS Core at {OS_URL}")

    st.markdown("---")
    st.markdown(f"**OS Core:** `{OS_URL}`")
    st.markdown(f"**Auto-refresh:** {AUTO_REFRESH_SEC}s")

    if st.button("🔄 Refresh now"):
        st.cache_data.clear()
        st.rerun()


# ── Main content ──────────────────────────────────────────────────────────────
health    = fetch_health()
machines  = fetch_machines()
missions  = fetch_missions()

os_ok = bool(health.get("status") == "ok")

# ── Summary stats ─────────────────────────────────────────────────────────────
c1, c2, c3, c4, c5 = st.columns(5)
c1.markdown(
    f'<div class="stat-box"><div class="stat-num">{"🟢" if os_ok else "🔴"}</div>'
    f'<div class="stat-lbl">OS Status</div></div>',
    unsafe_allow_html=True,
)
c2.markdown(
    f'<div class="stat-box"><div class="stat-num">{len(machines)}</div>'
    f'<div class="stat-lbl">Machines</div></div>',
    unsafe_allow_html=True,
)
c3.markdown(
    f'<div class="stat-box"><div class="stat-num">'
    f'{sum(1 for m in machines if m.get("state")=="AVAILABLE")}</div>'
    f'<div class="stat-lbl">Available</div></div>',
    unsafe_allow_html=True,
)
running_count = sum(1 for m in missions if m.get("state") in ("ASSIGNED", "RUNNING"))
c4.markdown(
    f'<div class="stat-box"><div class="stat-num" style="color:#42A5F5">{running_count}</div>'
    f'<div class="stat-lbl">Active Missions</div></div>',
    unsafe_allow_html=True,
)
completed_count = sum(1 for m in missions if m.get("state") == "COMPLETED")
c5.markdown(
    f'<div class="stat-box"><div class="stat-num" style="color:#4CAF50">{completed_count}</div>'
    f'<div class="stat-lbl">Completed</div></div>',
    unsafe_allow_html=True,
)

st.markdown("<br>", unsafe_allow_html=True)

# ── Two columns: Fleet + Missions ─────────────────────────────────────────────
left, right = st.columns([1, 1.6], gap="large")

# ── FLEET ─────────────────────────────────────────────────────────────────────
with left:
    st.markdown("### 🤖 Fleet")

    if not machines:
        st.markdown(
            '<div class="card"><div class="card-header">No machines registered yet</div>'
            '<p style="color:#555;font-size:0.85rem">Start the adapter to see machines appear here automatically.</p>'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        for m in machines:
            state    = m.get("state", "OFFLINE")
            caps_raw = m.get("capabilities", "[]")
            batt     = m.get("health_battery_pct")
            cur_msn  = m.get("current_mission_id") or ""
            last_seen = _relative_time(m.get("last_seen_at"))

            mission_line = (
                f'<div style="margin-top:4px;font-size:0.76rem;color:#888">'
                f'Mission: <span style="color:#42A5F5;font-family:monospace">{cur_msn}</span></div>'
                if cur_msn else ""
            )

            faults_raw = m.get("health_faults", "[]")
            try:
                faults = json.loads(faults_raw) if isinstance(faults_raw, str) else (faults_raw or [])
            except Exception:
                faults = []
            faults_line = (
                f'<div style="margin-top:4px;font-size:0.75rem;color:#EF5350;font-weight:600">'
                f'⚠️ {" · ".join(faults)}</div>'
                if faults else ""
            )

            st.markdown(f"""
<div class="machine-card">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
      <div class="machine-id">{m.get('machine_id')}</div>
      <div class="machine-meta">{m.get('machine_type','?')} · {_battery_icon(batt)} · seen {last_seen}</div>
      <div style="margin-top:5px">{_caps_html(caps_raw)}</div>
    </div>
    <div>{_badge(state)}</div>
  </div>
  {mission_line}
  {faults_line}
</div>
""", unsafe_allow_html=True)

# ── MISSIONS ──────────────────────────────────────────────────────────────────
with right:
    st.markdown("### 📋 Mission Log")

    # Filter tabs
    tab_all, tab_active, tab_done, tab_exc = st.tabs(["All", "Active", "Completed", "Exceptions"])

    def _render_missions(items: list[dict]) -> None:
        if not items:
            st.markdown('<div style="color:#555;font-size:0.85rem;padding:0.5rem">No missions here.</div>', unsafe_allow_html=True)
            return
        for m in items:
            state   = m.get("state", "PENDING")
            pct     = m.get("progress_percent", 0)
            machine = m.get("assigned_machine_id") or "—"
            obj     = m.get("objective") or m.get("capability_required", "")
            note    = m.get("status_message") or ""
            created = _relative_time(m.get("created_at"))

            note_html = (
                f'<div style="color:#aaa;font-size:0.73rem;margin-top:3px">{note[:80]}</div>'
                if note else ""
            )

            st.markdown(f"""
<div class="mission-row">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
      <div class="mission-id">{m.get('mission_id')}</div>
      <div class="mission-objective">{obj}</div>
      <div style="font-size:0.75rem;color:#888;margin-top:2px">
        → {machine} &nbsp;·&nbsp; {created}
      </div>
    </div>
    <div>{_badge(state)}</div>
  </div>
  {_progress_bar(pct, state)}
  {note_html}
</div>
""", unsafe_allow_html=True)

    with tab_all:
        _render_missions(missions)
    with tab_active:
        _render_missions([m for m in missions if m.get("state") in ("PENDING","ASSIGNED","RUNNING")])
    with tab_done:
        _render_missions([m for m in missions if m.get("state") == "COMPLETED"])
    with tab_exc:
        _render_missions([m for m in missions if m.get("state") == "EXCEPTION"])


# ── Auto-refresh footer ───────────────────────────────────────────────────────
st.markdown("---")
col_l, col_r = st.columns([3, 1])
col_l.markdown(
    f'<span style="color:#555;font-size:0.75rem">'
    f'☀️ SunnyboticsOS V0 · <b>ALL DATA FROM SIMULATED MACHINES</b> · '
    f'Auto-refresh every {AUTO_REFRESH_SEC}s</span>',
    unsafe_allow_html=True,
)
col_r.markdown(
    f'<span style="color:#555;font-size:0.75rem">🕐 {datetime.now().strftime("%H:%M:%S")}</span>',
    unsafe_allow_html=True,
)

# Auto-refresh
time.sleep(AUTO_REFRESH_SEC)
st.rerun()
