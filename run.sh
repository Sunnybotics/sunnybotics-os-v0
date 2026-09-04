#!/usr/bin/env bash
# Bring the whole SunnyboticsOS V0 stack up, or take it down again.
#
#   ./run.sh          start everything and follow the logs
#   ./run.sh stop     stop everything this script started
#   ./run.sh status   show what is running and on which port
#
# Four processes have to be alive at once for a mission to travel end to end:
#
#   os_core     :9000   Mission Engine -- accepts missions, keeps the record
#   machines     ROS    the SIMULATED rovers
#   adapter     :8001   translates between ROS and the Mission Engine
#   dashboard   :8501   Streamlit fleet view
#
# `source /opt/ros/.../setup.bash` cannot run under `set -u` -- the ROS setup
# scripts read unset variables -- so this file deliberately does not use it.
set -eo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGS="$ROOT/logs"
PIDFILE="$LOGS/run.pids"

VENV="$ROOT/venv"
OS_PORT=9000
ADAPTER_PORT=8001
DASHBOARD_PORT=8501

# Localhost-only discovery, so two people running this on the same network do
# not discover each other's rovers.
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-42}"
export ROS_AUTOMATIC_DISCOVERY_RANGE="${ROS_AUTOMATIC_DISCOVERY_RANGE:-LOCALHOST}"

say() { printf '  %s\n' "$1"; }
die() { printf 'error: %s\n' "$1" >&2; exit 1; }


# --------------------------------------------------------------------------- #
# stop / status
# --------------------------------------------------------------------------- #
stop_all() {
    echo "Stopping..."
    if [ -f "$PIDFILE" ]; then
        while read -r pid name; do
            if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
                # The process group, because `ros2 launch` and `streamlit` both
                # fork children that outlive the parent otherwise.
                kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
                say "stopped $name (pid $pid)"
            fi
        done < "$PIDFILE"
        rm -f "$PIDFILE"
    fi
    # Anything left from an earlier run that predates the pid file.
    pkill -f 'os_core.main'            2>/dev/null || true
    pkill -f 'dashboard/app.py'        2>/dev/null || true
    pkill -f 'machines.launch.py'      2>/dev/null || true
    pkill -f 'sunnybotics_machines'    2>/dev/null || true
    pkill -f 'sunnybotics_adapter'     2>/dev/null || true
    sleep 1
    say "all stopped"
}

show_status() {
    for entry in "OS Core:$OS_PORT:/health" "Adapter:$ADAPTER_PORT:/" "Dashboard:$DASHBOARD_PORT:/"; do
        name="${entry%%:*}"
        rest="${entry#*:}"
        port="${rest%%:*}"
        path="${rest#*:}"
        if curl -sf -o /dev/null --max-time 2 "http://127.0.0.1:$port$path" 2>/dev/null; then
            say "$name  :$port  up"
        else
            say "$name  :$port  down"
        fi
    done
}

case "${1:-start}" in
    stop)   stop_all; exit 0 ;;
    status) show_status; exit 0 ;;
    start)  ;;
    *)      die "unknown command '$1'; expected start, stop or status" ;;
esac


# --------------------------------------------------------------------------- #
# preflight
# --------------------------------------------------------------------------- #
echo "SunnyboticsOS V0 -- ALL MACHINES SIMULATED"
echo

ROS_SETUP=""
for candidate in /opt/ros/jazzy /opt/ros/iron /opt/ros/humble; do
    if [ -f "$candidate/setup.bash" ]; then
        ROS_SETUP="$candidate/setup.bash"
        break
    fi
done
[ -n "$ROS_SETUP" ] || die "no ROS 2 installation found under /opt/ros"

mkdir -p "$LOGS"
stop_all
echo

# The Mission Engine and the dashboard are plain Python and share one venv.
# Ubuntu 24.04 refuses `pip install` into the system interpreter, so the venv
# is not optional here.
if [ ! -x "$VENV/bin/python" ]; then
    say "creating venv"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
fi
say "installing OS Core requirements"
"$VENV/bin/pip" install --quiet -r "$ROOT/os_core/requirements.txt"
"$VENV/bin/pip" install --quiet streamlit

# shellcheck disable=SC1090
source "$ROS_SETUP"
say "ROS 2 from $(dirname "$ROS_SETUP")"

if [ ! -f "$ROOT/machine-layer/install/setup.bash" ]; then
    say "building the machine layer (first run)"
    ( cd "$ROOT/machine-layer" && colcon build --symlink-install > "$LOGS/colcon.log" 2>&1 ) \
        || { tail -30 "$LOGS/colcon.log"; die "colcon build failed; see logs/colcon.log"; }
fi
# shellcheck disable=SC1091
source "$ROOT/machine-layer/install/setup.bash"
say "machine layer sourced"
echo


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #
: > "$PIDFILE"

# setsid so each one leads its own process group and `stop` can take its
# children down with it.
spawn() {
    local name="$1"; shift
    setsid "$@" > "$LOGS/$name.log" 2>&1 &
    echo "$! $name" >> "$PIDFILE"
    say "started $name (pid $!) -> logs/$name.log"
}

cd "$ROOT"
spawn os_core "$VENV/bin/python" -m uvicorn os_core.main:app \
    --host 0.0.0.0 --port "$OS_PORT"

# Wait for the Mission Engine before the adapter, which registers against it.
for _ in $(seq 30); do
    curl -sf -o /dev/null --max-time 1 "http://127.0.0.1:$OS_PORT/health" && break
    sleep 0.5
done

spawn machines ros2 launch sunnybotics_machines machines.launch.py
sleep 4
spawn adapter ros2 run sunnybotics_adapter adapter \
    --os-url "http://localhost:$OS_PORT" --port "$ADAPTER_PORT"
spawn dashboard "$VENV/bin/python" -m streamlit run dashboard/app.py \
    --server.port "$DASHBOARD_PORT" --server.headless true

echo
say "waiting for the rovers to register"
for _ in $(seq 40); do
    count="$(curl -sf --max-time 1 "http://127.0.0.1:$OS_PORT/health" 2>/dev/null \
             | grep -o '"machines_registered": *[0-9]*' | grep -o '[0-9]*$' || true)"
    [ "${count:-0}" -gt 0 ] && break
    sleep 0.5
done
echo

echo "Ready."
show_status
say "registered machines: ${count:-0}"
echo
say "dashboard   http://localhost:$DASHBOARD_PORT"
say "mission API http://localhost:$OS_PORT/api/v0/missions"
say "stop with   ./run.sh stop   (or Ctrl-C here)"
echo

# Ctrl-C on the follow below should bring the stack down, not orphan it.
trap 'echo; stop_all; exit 0' INT TERM
tail -f "$LOGS"/os_core.log "$LOGS"/adapter.log "$LOGS"/machines.log
