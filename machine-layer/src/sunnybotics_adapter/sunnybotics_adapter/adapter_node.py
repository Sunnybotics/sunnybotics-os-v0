# Copyright 2026 Sunnybotics.
# Licensed under the Apache License, Version 2.0.
"""The ROS 2 half of the adapter.

This node is the only thing in the system that talks to machines. It keeps the
latest MachineState for every machine it can hear, dispatches Mission goals, and
tracks what happens to them. It holds no opinion about HTTP -- ``rest_api.py``
calls the plain methods here and turns the answers into responses.

Two things are worth pointing out.

**Discovery is dynamic.** Nothing here has a list of machines in it. The node
scans for topics matching ``/machines/<id>/state`` once a second and subscribes
to whatever it finds. Starting a third machine is enough to make it appear in
``GET /machines``; no restart, no config file, no code change. That is this
component's central claim, expressed as about fifteen lines of code.

**The thread boundary is real.** ``rclpy`` spins on a background thread while
FastAPI serves on the main one, so every attribute reachable from both is
guarded by ``self._lock``, and the FastAPI endpoints are deliberately synchronous
so they may block on it. See ``rest_api.py`` for the other side of that.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

from action_msgs.msg import GoalStatus
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.node import Node

from sunnybotics_cmi.action import Mission
from sunnybotics_cmi.msg import MachineState

from sunnybotics_adapter.translate import (
    machine_state_to_dict,
    machine_state_to_os_descriptor,
    mission_result_to_dict,
)

#: Topics that look like a machine heartbeat.
MACHINE_STATE_TOPIC = re.compile(r"^/machines/([^/]+)/state$")

#: The fully-qualified type a heartbeat topic must carry to count.
MACHINE_STATE_TYPE = "sunnybotics_cmi/msg/MachineState"

# Mission states as the OS sees them. The first four deliberately reuse the
# MachineState vocabulary, so "RUNNING" means the same thing in both views.
MISSION_ASSIGNED = "ASSIGNED"
MISSION_RUNNING = "RUNNING"
MISSION_COMPLETED = "COMPLETED"
MISSION_EXCEPTION = "EXCEPTION"
# These two have no machine equivalent: they describe what happened to the
# request, not to the machine.
MISSION_REJECTED = "REJECTED"
MISSION_CANCELED = "CANCELED"


@dataclass
class MachineRecord:
    """The latest thing a machine said, and when it said it."""

    machine_id: str
    msg: MachineState
    #: ``time.monotonic()``, not wall clock: staleness is an elapsed-time
    #: question and must survive the clock being stepped.
    last_seen: float
    #: Whether the OS was already told this machine is stale. Without this a
    #: machine that stops publishing entirely -- a flat battery, a killed
    #: process -- would never be re-reported: patch_status only ever fires
    #: from _on_machine_state, and a silent machine sends no more messages to
    #: trigger it. The OS would then show its last-known state forever.
    reported_stale: bool = False


@dataclass
class MissionRecord:
    """Everything the adapter knows about one dispatched mission."""

    mission_id: str
    assigned_machine_id: str
    state: str = MISSION_ASSIGNED
    progress_percent: int = 0
    status_message: str = ""
    result: dict[str, Any] | None = None
    goal_handle: Any = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Exactly the six keys GET /missions/{id} promises."""
        return {
            "mission_id": self.mission_id,
            "assigned_machine_id": self.assigned_machine_id,
            "state": self.state,
            "progress_percent": self.progress_percent,
            "status_message": self.status_message,
            "result": self.result,
        }


def _utc_now_iso() -> str:
    """Wall-clock UTC in ISO 8601, for the OS report timestamp."""
    return (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def wait_for_future(future, timeout_sec: float):
    """Block until an rclpy future resolves, without spinning.

    The executor is already spinning this node on another thread, so calling
    ``spin_until_future_complete`` here would mean two spinners on one node.
    A done-callback plus an Event gets the same result and stays out of the
    executor's way. rclpy fires the callback immediately if the future has
    already resolved, so there is no race between the two lines.
    """
    done = threading.Event()
    future.add_done_callback(lambda _f: done.set())
    if not done.wait(timeout_sec):
        return None
    return future.result()


class AdapterNode(Node):
    """Holds fleet state, dispatches missions, tracks their outcomes."""

    def __init__(self, os_client=None) -> None:
        super().__init__("sunnybotics_adapter")

        # Optional. When absent the adapter is purely a server and
        # nothing is pushed anywhere; see os_client.py for why the
        # push direction exists at all.
        self._os = os_client

        # Five missed heartbeats before a machine is called unreachable. Long
        # enough that a scheduling hiccup is not mistaken for a dead radio.
        self.declare_parameter("stale_after_sec", 5.0)
        self.declare_parameter("discovery_period_sec", 1.0)
        self.declare_parameter("action_server_timeout_sec", 5.0)
        # Machines below this are not offered work. Kept a fraction under the
        # threshold the machines enforce themselves, so the dispatcher stops
        # offering just before the machine starts refusing rather than the
        # other way round.
        self.declare_parameter("min_battery_percent", 10.0)

        self._lock = threading.RLock()
        self._machines: dict[str, MachineRecord] = {}
        self._missions: dict[str, MissionRecord] = {}
        # Not `_subscriptions`: rclpy.node.Node already owns an attribute by
        # that name, and create_subscription() appends to it. Shadowing it with
        # a dict makes the first subscription blow up inside the executor.
        self._machine_subs: dict[str, Any] = {}
        self._action_clients: dict[str, ActionClient] = {}

        # Reentrant so a mission dispatch can wait on an action server while
        # heartbeats keep being processed on other threads.
        self._callback_group = ReentrantCallbackGroup()

        self._discovery_timer = self.create_timer(
            float(self.get_parameter("discovery_period_sec").value),
            self._discover_machines,
            callback_group=self._callback_group,
        )

        self.get_logger().info(
            "adapter up -- watching for /machines/<id>/state, "
            "no machine list configured anywhere"
        )

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    def _discover_machines(self) -> None:
        """Subscribe to new heartbeats, and tell the OS about newly-silent ones.

        Two unrelated jobs share one timer deliberately: both are periodic
        fleet housekeeping, and a machine that goes fully silent needs the
        same cadence of attention as one that just appeared.
        """
        self._report_newly_stale_machines()
        for topic_name, type_names in self.get_topic_names_and_types():
            match = MACHINE_STATE_TOPIC.match(topic_name)
            if match is None or MACHINE_STATE_TYPE not in type_names:
                continue

            machine_id = match.group(1)
            with self._lock:
                if machine_id in self._machine_subs:
                    continue
                self._machine_subs[machine_id] = self.create_subscription(
                    MachineState,
                    topic_name,
                    lambda msg, mid=machine_id: self._on_machine_state(mid, msg),
                    10,
                    callback_group=self._callback_group,
                )
                # Created here, on an executor thread, rather than lazily from
                # an HTTP worker: entity creation races the executor's wait set,
                # and this way it only ever happens on one thread.
                self._action_clients[machine_id] = ActionClient(
                    self,
                    Mission,
                    f"/machines/{machine_id}/execute_mission",
                    callback_group=self._callback_group,
                )
            self.get_logger().info(
                f"discovered machine '{machine_id}' on {topic_name}"
            )

    def _report_newly_stale_machines(self) -> None:
        """Push OFFLINE to the OS for any machine that just crossed stale.

        ``_select_machine`` already excludes a stale machine from dispatch by
        checking ``last_seen`` live, so no mission can ever reach it -- that
        safety property holds with or without this method. What this adds is
        visibility: without it, a machine that stops transmitting keeps
        showing its last reported state (state, battery, everything) forever,
        because nothing else ever sends the OS a fresher picture.
        """
        if self._os is None:
            return
        stale_after = float(self.get_parameter("stale_after_sec").value)
        now = time.monotonic()
        with self._lock:
            newly_stale = [
                record for record in self._machines.values()
                if not record.reported_stale
                and (now - record.last_seen) > stale_after
            ]
            for record in newly_stale:
                record.reported_stale = True
        for record in newly_stale:
            self.get_logger().warning(
                f"'{record.machine_id}' has not been heard from in "
                f"{now - record.last_seen:.1f}s; reporting OFFLINE"
            )
            self._os.patch_status(
                record.machine_id,
                machine_state_to_os_descriptor(record.msg, stale=True),
            )

    def _on_machine_state(self, topic_machine_id: str, msg: MachineState) -> None:
        """Store the latest heartbeat."""
        machine_id = msg.machine_id or topic_machine_id
        if msg.machine_id and msg.machine_id != topic_machine_id:
            # Worth a warning rather than a silent preference: it means a
            # machine is publishing under someone else's topic, and every
            # mission routed by topic name would go to the wrong place.
            self.get_logger().warning(
                f"machine on /machines/{topic_machine_id}/state identifies "
                f"itself as '{msg.machine_id}'; trusting the message"
            )

        with self._lock:
            existing = self._machines.get(machine_id)
            self._machines[machine_id] = MachineRecord(
                machine_id=machine_id,
                msg=msg,
                last_seen=time.monotonic(),
                reported_stale=False,
            )
            came_back = existing is not None and existing.reported_stale

        if came_back:
            self.get_logger().info(f"'{machine_id}' is transmitting again")

        if self._os is not None:
            # A message just arrived, so by definition this one is not stale.
            # Registration happens on the first heartbeat rather than on topic
            # discovery, because only a heartbeat carries the descriptor the
            # OS needs to register.
            self._os.patch_status(
                machine_id, machine_state_to_os_descriptor(msg, stale=False)
            )

    # ------------------------------------------------------------------ #
    # Fleet views
    # ------------------------------------------------------------------ #
    def _snapshot_locked(self, record: MachineRecord, now: float) -> dict[str, Any]:
        age = now - record.last_seen
        stale = age > float(self.get_parameter("stale_after_sec").value)
        return machine_state_to_dict(
            record.msg, seconds_since_last_message=age, stale=stale
        )

    def machines_snapshot(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            return [
                self._snapshot_locked(record, now)
                for record in sorted(
                    self._machines.values(), key=lambda r: r.machine_id
                )
            ]

    def machine_snapshot(self, machine_id: str) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._lock:
            record = self._machines.get(machine_id)
            if record is None:
                return None
            return self._snapshot_locked(record, now)

    def known_machine_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._machines)

    # ------------------------------------------------------------------ #
    # Outbound push to the OS (optional)
    # ------------------------------------------------------------------ #
    def attach_os_client(self, os_client) -> None:
        """Start pushing machine and mission state to the OS.

        Attached after construction so the node itself has no opinion about
        whether anything is listening, and so its logger can be handed to the
        client rather than the client inventing its own.
        """
        self._os = os_client

    def os_push_stats(self) -> dict[str, Any] | None:
        """What the outbound client has managed to deliver, or None if off."""
        return self._os.stats() if self._os is not None else None

    # ------------------------------------------------------------------ #
    # Mission dispatch
    # ------------------------------------------------------------------ #
    def _select_machine(self, capability_required: str) -> tuple[str | None, dict]:
        """Pick a machine for this capability. Caller must hold the lock.

        V0 policy: any machine that advertises the capability, is AVAILABLE,
        reports ONLINE, is not stale, and has enough battery to be worth
        sending. Ties break on battery, so repeated missions spread across a
        fleet instead of hammering machine one.

        Note what the matching does *not* consult: machine_type. A third
        machine type advertising CLEANING would be picked up here with no
        change to this function.
        """
        now = time.monotonic()
        stale_after = float(self.get_parameter("stale_after_sec").value)

        capable: list[str] = []
        candidates: list[tuple[float, str]] = []
        rejections: dict[str, str] = {}

        wanted = capability_required.strip().lower()
        for record in self._machines.values():
            msg = record.msg
            # Case-folded: a capability is a bare string on both sides of the
            # interface, and a case mismatch would silently match nothing
            # rather than raising anything.
            if wanted not in {c.strip().lower() for c in msg.capabilities}:
                continue
            capable.append(record.machine_id)

            if now - record.last_seen > stale_after:
                rejections[record.machine_id] = "CONNECTION_BROKEN (no heartbeat)"
                continue
            if msg.connection_status != MachineState.ONLINE:
                rejections[record.machine_id] = "not ONLINE"
                continue
            if msg.state != MachineState.AVAILABLE:
                rejections[record.machine_id] = "not AVAILABLE"
                continue
            # The machine refuses this itself as well. Filtering here turns
            # what would be a dispatch followed by a rejection into an honest
            # 409 with a reason, which is the difference between the OS being
            # told "nothing can take this" and being told nothing at all.
            minimum = float(self.get_parameter("min_battery_percent").value)
            if msg.has_battery and float(msg.battery_percent) < minimum:
                rejections[record.machine_id] = (
                    f"battery {float(msg.battery_percent):.1f}% is under the "
                    f"{minimum:.0f}% minimum"
                )
                continue

            # A mains-powered machine has no battery constraint at all, so it
            # sorts ahead of every battery-powered one rather than behind them
            # on a 0.0 that does not mean what it looks like.
            battery = (
                float(msg.battery_percent) if msg.has_battery else float("inf")
            )
            candidates.append((battery, record.machine_id))

        diagnostics = {
            "capability_required": capability_required,
            "machines_with_capability": sorted(capable),
            "why_not_selected": rejections,
        }

        if not candidates:
            return None, diagnostics

        # Highest battery first, machine_id as a stable tiebreak.
        candidates.sort(key=lambda pair: (-pair[0], pair[1]))
        return candidates[0][1], diagnostics

    def dispatch_mission(
        self,
        capability_required: str,
        objective: str,
        parameters: dict[str, Any] | None,
        mission_id: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Match a machine, send it a goal, start tracking. Returns (status, body).

        ``mission_id`` lets the caller supply the id it has already issued, so a
        mission has one identity end to end instead of one per layer. Without
        it the caller has to map between its own id and the one generated here,
        and reports arrive under an id it does not recognise.
        """
        if mission_id:
            # A caller-supplied id must be unique here too, or a second
            # dispatch would silently overwrite the first mission's record and
            # its reports would be attributed to the wrong work.
            with self._lock:
                if mission_id in self._missions:
                    existing = self._missions[mission_id]
                    return 409, {
                        "reason": "mission_id_already_used",
                        "mission_id": mission_id,
                        "existing_state": existing.state,
                        "assigned_machine_id": existing.assigned_machine_id,
                    }
        else:
            mission_id = f"m-{uuid.uuid4().hex[:12]}"

        with self._lock:
            machine_id, diagnostics = self._select_machine(capability_required)
            if machine_id is None:
                reason = (
                    "no_machine_with_capability"
                    if not diagnostics["machines_with_capability"]
                    else "no_available_machine"
                )
                self.get_logger().warning(
                    f"cannot dispatch '{capability_required}': {reason} "
                    f"{diagnostics}"
                )
                return 409, {"reason": reason, **diagnostics}
            client = self._action_clients.get(machine_id)

        if client is None:
            # Heard the heartbeat but never built a client: only reachable if
            # discovery is mid-flight.
            return 503, {
                "reason": "action_client_not_ready",
                "assigned_machine_id": machine_id,
            }

        timeout = float(self.get_parameter("action_server_timeout_sec").value)
        if not client.wait_for_server(timeout_sec=timeout):
            self.get_logger().error(
                f"{machine_id} publishes state but its mission action server "
                f"did not answer within {timeout}s"
            )
            return 503, {
                "reason": "action_server_unreachable",
                "assigned_machine_id": machine_id,
            }

        goal = Mission.Goal()
        goal.mission_id = mission_id
        goal.capability_required = capability_required
        goal.objective = objective
        goal.parameters_json = json.dumps(parameters or {})

        record = MissionRecord(
            mission_id=mission_id,
            assigned_machine_id=machine_id,
            status_message=f"dispatched to {machine_id}",
        )
        with self._lock:
            self._missions[mission_id] = record

        send_future = client.send_goal_async(
            goal,
            feedback_callback=lambda fb, mid=mission_id: self._on_feedback(mid, fb),
        )
        goal_handle = wait_for_future(send_future, timeout)

        if goal_handle is None:
            self._fail_mission(
                mission_id,
                MISSION_EXCEPTION,
                f"{machine_id} did not acknowledge the goal within {timeout}s",
                error_code="DISPATCH_TIMEOUT",
            )
            return 504, {
                "reason": "goal_not_acknowledged",
                "mission_id": mission_id,
                "assigned_machine_id": machine_id,
            }

        if not goal_handle.accepted:
            # The machine refused. It is entitled to: it knows things the
            # dispatcher does not, and it re-checks capability and availability
            # itself. A refusal here is the interface working, not failing.
            self._fail_mission(
                mission_id,
                MISSION_REJECTED,
                f"{machine_id} rejected the mission",
                error_code="GOAL_REJECTED",
            )
            return 409, {
                "reason": "machine_rejected_goal",
                "mission_id": mission_id,
                "assigned_machine_id": machine_id,
            }

        with self._lock:
            record.goal_handle = goal_handle

        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(
            lambda fut, mid=mission_id: self._on_result(mid, fut)
        )

        self.get_logger().info(
            f"{mission_id} -> {machine_id} ({capability_required}): {objective}"
        )
        return 201, {
            "mission_id": mission_id,
            "assigned_machine_id": machine_id,
            "state": MISSION_ASSIGNED,
        }

    # ------------------------------------------------------------------ #
    # Mission tracking
    # ------------------------------------------------------------------ #
    def _on_feedback(self, mission_id: str, feedback_msg) -> None:
        feedback = feedback_msg.feedback
        with self._lock:
            record = self._missions.get(mission_id)
            if record is None:
                return
            # First feedback is the moment the machine actually started work,
            # which is the only honest point to call the mission RUNNING.
            if record.state == MISSION_ASSIGNED:
                record.state = MISSION_RUNNING
            record.progress_percent = int(feedback.progress_percent)
            record.status_message = feedback.status_message

        if self._os is not None:
            self._os.report_mission(
                mission_id,
                status="RUNNING",
                detail=feedback.status_message,
                timestamp=_utc_now_iso(),
                progress_percent=int(feedback.progress_percent),
            )

    def _on_result(self, mission_id: str, future) -> None:
        response = future.result()
        status = response.status
        result = response.result

        if status == GoalStatus.STATUS_CANCELED:
            state = MISSION_CANCELED
        elif (
            status == GoalStatus.STATUS_SUCCEEDED
            and result.result_state == Mission.Result.RESULT_COMPLETED
        ):
            state = MISSION_COMPLETED
        else:
            # Covers STATUS_ABORTED and the case of a server that succeeds the
            # goal while reporting RESULT_EXCEPTION. The result field wins:
            # the machine's own verdict on its work outranks the transport's.
            state = MISSION_EXCEPTION

        with self._lock:
            record = self._missions.get(mission_id)
            if record is None:
                return
            record.state = state
            record.result = mission_result_to_dict(result)
            record.status_message = result.message
            if state == MISSION_COMPLETED:
                record.progress_percent = 100
            record.goal_handle = None

        self.get_logger().info(f"{mission_id} finished: {state} ({result.message})")

        if self._os is not None:
            # The OS vocabulary for a report is RUNNING / COMPLETED / EXCEPTION,
            # so anything that is not a clean completion is reported as an
            # exception with the reason in `detail`.
            self._os.report_mission(
                mission_id,
                status="COMPLETED" if state == MISSION_COMPLETED else "EXCEPTION",
                detail=result.message or state,
                timestamp=_utc_now_iso(),
                progress_percent=100 if state == MISSION_COMPLETED else None,
            )

    def _fail_mission(
        self, mission_id: str, state: str, message: str, *, error_code: str
    ) -> None:
        """Record a failure that happened before the machine ever ran anything."""
        with self._lock:
            record = self._missions.get(mission_id)
            if record is None:
                return
            record.state = state
            record.status_message = message
            record.result = {
                "result_state": "EXCEPTION",
                "error_code": error_code,
                "message": message,
                "result_json": None,
            }
        self.get_logger().error(f"{mission_id}: {message}")

    def mission_snapshot(self, mission_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._missions.get(mission_id)
            return record.to_dict() if record else None

    def mission_count(self) -> int:
        with self._lock:
            return len(self._missions)
