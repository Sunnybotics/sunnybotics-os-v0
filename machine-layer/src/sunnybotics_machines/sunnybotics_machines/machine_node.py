# Copyright 2026 Sunnybotics.
# Licensed under the Apache License, Version 2.0.
"""One reusable simulated machine node for SunnyboticsOS V0.

    *** EVERY PHYSICAL QUANTITY PRODUCED BY THIS FILE IS SIMULATED. ***

There is no robot behind this node. Battery drain, position, health blips and
mission progress are all invented by the fake logic in ``_tick`` and
``_execute_mission``. None of it models how any real machine behaves, and none
of it should ever be read as telemetry.

What is *not* faked -- and is the thing that actually matters here -- is the
shape of the conversation:

  * a ``MachineState`` published on ``/machines/<id>/state`` at 1 Hz
  * a ``Mission`` action served at ``/machines/<id>/execute_mission``
  * the lifecycle those two agree on:
        AVAILABLE -> ASSIGNED -> RUNNING -> COMPLETED | EXCEPTION -> AVAILABLE

A real machine driver replaces this one file and nothing else in the system
changes. That is the property this whole component exists to provide.

Field behaviour is split three ways on purpose:

  STATIC           machine_id, machine_type, capabilities, operating_mode,
                   interface_version. Set once in __init__, never written again.
  WORKFLOW-DRIVEN  state, current_mission_id and the Result fields. These fall
                   out of the action server's own logic -- they are never set
                   by the simulation, because faking them separately would let
                   the published state disagree with what actually happened.
  SIMULATED        seq, battery_percent, health, location, connection_status,
                   battery thresholds, mission duration,
                   active_errors, feedback progress. Explicit fake logic, all
                   of it in this file, all of it clearly marked.
"""

from __future__ import annotations

import json
import math
import random
import threading
import time

import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter

from sunnybotics_cmi.action import Mission
from sunnybotics_cmi.msg import MachineState

#: Version of the CMI contract this node speaks. Matches sunnybotics_cmi.
INTERFACE_VERSION = "0.1.0"

#: State is published at 1 Hz. The adapter's staleness window is a multiple of
#: this, so changing it here means changing it there too.
PUBLISH_PERIOD_SEC = 1.0

# --- Simulation knobs --------------------------------------------------------
# SIMULATED. Percent of battery burned per 1 Hz tick.
BATTERY_DRAIN_RUNNING = 0.30
BATTERY_DRAIN_IDLE = 0.06
# SIMULATED. Below this the machine reports DEGRADED but keeps working, which is
# the entire point of health being a separate field from state.
BATTERY_DEGRADED_BELOW = 20.0
# SIMULATED. Below this the machine reports FAULT and raises BATTERY_CRITICAL.
# Still not dead: it can finish what it is already doing, but it is in trouble
# and an operator needs to know that without reading a battery percentage.
BATTERY_CRITICAL_BELOW = 5.0
# SIMULATED. A machine below this refuses new work. Deliberately separate from
# the DEGRADED threshold: DEGRADED means "carry on and tell someone", this one
# means "do not start something you cannot finish". A machine that accepts a
# mission it will die halfway through is worse than one that refuses it.
BATTERY_MISSION_MINIMUM = 10.0
# SIMULATED. A random FAULT is a blip, not a latch: it clears itself after this
# many ticks. A real fault would stay latched until an operator cleared it.
FAULT_BLIP_TICKS = 3
# SIMULATED. Movement closes this fraction of the remaining distance each tick,
# so the machine eases into the target instead of teleporting.
MOVE_FRACTION_PER_TICK = 0.25
ARRIVED_EPSILON = 0.05
# SIMULATED. How far into a mission an induced failure fires.
FAILURE_AT_PERCENT = 60
# SIMULATED. One progress step covers this much ground, so a mission lasts as
# long as its goal is far away. Every mission used to be a fixed ten steps,
# which made duration independent of distance -- and made progress look like it
# jumped, because a six-second mission sampled by a three-second poll only ever
# shows two values. The bounds keep a trivial goal reportable and stop a
# distant one from outlasting the patience of whoever is watching.
METRES_PER_MISSION_STEP = 1.0
MIN_MISSION_STEPS = 12
MAX_MISSION_STEPS = 45

#: The frame local coordinates are expressed in. Not a geographic frame.
LOCAL_FRAME_ID = "site_A_local"

#: Accepted values for the sim_connection_status parameter.
CONNECTION_ONLINE = "online"
CONNECTION_OFFLINE = "offline"
CONNECTION_BROKEN = "connection_broken"
VALID_CONNECTION_VALUES = (CONNECTION_ONLINE, CONNECTION_OFFLINE, CONNECTION_BROKEN)


class SimulatedMachine(Node):
    """A fake machine that speaks the real Common Machine Interface.

    Instantiated once per machine. Everything that differs between a cleaning
    rover and an inspection rover arrives through the constructor, which is the
    concrete form of the claim that the OS never needs to know machine types.
    """

    def __init__(
        self,
        *,
        machine_id: str,
        machine_type: str,
        capabilities: list[str],
        start_x: float = 0.0,
        start_y: float = 0.0,
    ) -> None:
        super().__init__(machine_id)

        # ---- STATIC identity. Written here, never again. --------------------
        self._machine_id = machine_id
        self._machine_type = machine_type
        self._capabilities = list(capabilities)
        self._operating_mode = MachineState.AUTOMATIC
        self._interface_version = INTERFACE_VERSION

        # ---- Tunables, exposed as ROS parameters ----------------------------
        # Parameters rather than constants so a demo can change the simulation
        # without editing code, which is the same rule the failure path follows.
        self.declare_parameter("has_battery", True)
        self.declare_parameter("initial_battery_percent", 100.0)
        self.declare_parameter("fault_probability", 0.002)
        # 0 means "work it out from the distance to the goal". A positive
        # value pins the mission to that many steps.
        self.declare_parameter("mission_steps", 0)
        self.declare_parameter("mission_step_sec", 0.6)
        self.declare_parameter("terminal_state_hold_sec", 3.0)
        self.declare_parameter("start_x", float(start_x))
        self.declare_parameter("start_y", float(start_y))
        # The manual override for link simulation. Flip it at runtime with:
        #   ros2 param set /<machine_id> sim_connection_status offline
        self.declare_parameter("sim_connection_status", CONNECTION_ONLINE)
        self.add_on_set_parameters_callback(self._validate_parameters)

        # ---- Guards every mutable attribute below ---------------------------
        # The 1 Hz timer thread and the action execution thread both touch that
        # state, because the executor has to be multi-threaded: a blocking
        # mission must not be able to starve the heartbeat.
        self._lock = threading.RLock()

        # ---- WORKFLOW-DRIVEN. Only the action server writes these. -----------
        self._state = MachineState.AVAILABLE
        self._current_mission_id = ""
        self._mission_errors: list[tuple[str, int]] = []

        # ---- SIMULATED. Only the fake logic writes these. --------------------
        self._seq = 0
        self._has_battery = bool(self.get_parameter("has_battery").value)
        self._battery = float(self.get_parameter("initial_battery_percent").value)
        self._health = MachineState.OK
        self._fault_ticks_left = 0
        self._x = float(self.get_parameter("start_x").value)
        self._y = float(self.get_parameter("start_y").value)
        self._target: tuple[float, float] | None = None
        self._rng = random.Random()
        self._reset_timer: threading.Timer | None = None
        # SIMULATED. Latches once the battery reaches zero. A flat battery is
        # not a blip and does not clear itself.
        self._battery_dead = False
        self._death_announced = False
        # SIMULATED. True only while a transient sensor blip is active, so a
        # FAULT caused by the battery is not blamed on a sensor.
        self._sensor_glitch = False

        # ---- ROS interfaces --------------------------------------------------
        # Separate callback groups so a mission blocking for ten seconds inside
        # execute_callback cannot hold up the heartbeat. Without this the
        # machine would look frozen for exactly as long as it was busy, which
        # is the worst possible time to lose sight of it.
        self._timer_group = MutuallyExclusiveCallbackGroup()
        self._action_group = ReentrantCallbackGroup()

        self._state_pub = self.create_publisher(
            MachineState, f"/machines/{self._machine_id}/state", 10
        )
        self._timer = self.create_timer(
            PUBLISH_PERIOD_SEC, self._tick, callback_group=self._timer_group
        )
        self._action_server = ActionServer(
            self,
            Mission,
            f"/machines/{self._machine_id}/execute_mission",
            execute_callback=self._execute_mission,
            goal_callback=self._on_goal_request,
            cancel_callback=self._on_cancel_request,
            callback_group=self._action_group,
        )

        self.get_logger().info(
            f"[SIMULATED MACHINE] {self._machine_id} ({self._machine_type}) "
            f"capabilities={self._capabilities} "
            f"start=({self._x:.1f}, {self._y:.1f}) frame={LOCAL_FRAME_ID}"
        )
        self.get_logger().info(
            f"  state  -> /machines/{self._machine_id}/state "
            f"@ {1 / PUBLISH_PERIOD_SEC:.0f} Hz"
        )
        self.get_logger().info(
            f"  action -> /machines/{self._machine_id}/execute_mission"
        )

    # ------------------------------------------------------------------ #
    # Parameters
    # ------------------------------------------------------------------ #
    def _validate_parameters(self, params: list[Parameter]) -> SetParametersResult:
        """Reject nonsense before it reaches the simulation."""
        for param in params:
            if param.name == "sim_connection_status":
                if param.value not in VALID_CONNECTION_VALUES:
                    return SetParametersResult(
                        successful=False,
                        reason=(
                            "sim_connection_status must be one of "
                            f"{list(VALID_CONNECTION_VALUES)}, got '{param.value}'"
                        ),
                    )
                self.get_logger().warning(
                    f"[SIMULATED] link status for {self._machine_id} "
                    f"-> {param.value}"
                )
            elif param.name == "has_battery":
                with self._lock:
                    self._has_battery = bool(param.value)
        return SetParametersResult(successful=True)

    @property
    def _sim_connection(self) -> str:
        return str(self.get_parameter("sim_connection_status").value)

    # ------------------------------------------------------------------ #
    # 1 Hz heartbeat -- all SIMULATED behaviour lives here
    # ------------------------------------------------------------------ #
    def _tick(self) -> None:
        with self._lock:
            self._seq += 1
            running = self._state == MachineState.RUNNING

            # SIMULATED battery drain, faster under load.
            if self._has_battery:
                drain = BATTERY_DRAIN_RUNNING if running else BATTERY_DRAIN_IDLE
                self._battery = max(0.0, self._battery - drain)

            # SIMULATED movement: only drifts while actually executing.
            if running and self._target is not None:
                self._step_towards_target()

            self._update_health()
            msg = self._build_message()
            dead = self._battery_dead
            newly_dead = dead and not self._death_announced
            if newly_dead:
                self._death_announced = True

        # SIMULATED link failure. "connection_broken" means the radio died, so
        # nothing is transmitted at all and the adapter has to notice the
        # silence by itself. That is the case connection_status exists to
        # cover, and a machine that politely announces its own outage cannot
        # demonstrate it.
        if self._sim_connection == CONNECTION_BROKEN:
            return

        # SIMULATED flat battery. A machine with no power does not transmit, so
        # it sends one last message and then goes quiet. It is deliberately not
        # reported as CONNECTION_BROKEN: the radio is fine, the robot is not,
        # and the honest description of a dead machine is OFFLINE. Letting the
        # staleness window in the adapter notice the silence exercises the path
        # a real fleet uses, which a machine that politely announces its own
        # death cannot.
        if dead:
            if newly_dead:
                self.get_logger().error(
                    f"[SIMULATED] {self._machine_id} battery flat -- going "
                    "silent; the adapter should mark it OFFLINE"
                )
                self._state_pub.publish(msg)
            return

        self._state_pub.publish(msg)

    def _step_towards_target(self) -> None:
        """SIMULATED. Ease towards the mission target. Caller holds the lock."""
        target_x, target_y = self._target
        dx, dy = target_x - self._x, target_y - self._y
        if math.hypot(dx, dy) <= ARRIVED_EPSILON:
            self._x, self._y = target_x, target_y
            return
        self._x += dx * MOVE_FRACTION_PER_TICK
        self._y += dy * MOVE_FRACTION_PER_TICK

    def _update_health(self) -> None:
        """SIMULATED. Caller holds the lock.

        Health is computed independently of state: nothing in here reads or
        writes ``self._state``. A DEGRADED machine keeps running, which is
        exactly why the two are separate fields in the message.
        """
        # A flat battery outranks everything else and never clears.
        if self._has_battery and self._battery <= 0.0:
            self._battery_dead = True
            self._sensor_glitch = False
            self._health = MachineState.FAULT
            return

        if self._fault_ticks_left > 0:
            self._fault_ticks_left -= 1
            self._sensor_glitch = True
            self._health = MachineState.FAULT
            return

        self._sensor_glitch = False
        if self._has_battery and self._battery < BATTERY_CRITICAL_BELOW:
            self._health = MachineState.FAULT
        elif self._has_battery and self._battery < BATTERY_DEGRADED_BELOW:
            self._health = MachineState.DEGRADED
        else:
            self._health = MachineState.OK

        # SIMULATED. Small chance of a transient fault, so the OS has to cope
        # with health changing under it rather than only at mission boundaries.
        fault_probability = float(self.get_parameter("fault_probability").value)
        if self._rng.random() < fault_probability:
            self._fault_ticks_left = FAULT_BLIP_TICKS
            self._sensor_glitch = True
            self._health = MachineState.FAULT
            self.get_logger().warning(
                f"[SIMULATED] transient fault on {self._machine_id}"
            )

    def _build_message(self) -> MachineState:
        """Assemble the current state. Caller holds the lock."""
        msg = MachineState()

        # STATIC
        msg.interface_version = self._interface_version
        msg.machine_id = self._machine_id
        msg.machine_type = self._machine_type
        msg.capabilities = list(self._capabilities)
        msg.operating_mode = self._operating_mode

        # SIMULATED
        msg.seq = self._seq
        msg.has_battery = self._has_battery
        msg.battery_percent = float(self._battery) if self._has_battery else 0.0
        msg.health = self._health

        # SIMULATED position. `latitude` carries local Y and `longitude` carries
        # local X, both in metres, both in LOCAL_FRAME_ID. Reusing the
        # geographic fields for a local frame is safe precisely because frame_id
        # states which frame the numbers belong to -- a consumer that ignores
        # frame_id and plots these on a world map gets what it deserves.
        msg.has_location = True
        msg.frame_id = LOCAL_FRAME_ID
        msg.latitude = float(self._y)
        msg.longitude = float(self._x)

        connection = self._sim_connection
        msg.connection_status = (
            MachineState.ONLINE
            if connection == CONNECTION_ONLINE
            else MachineState.OFFLINE
        )

        # WORKFLOW-DRIVEN
        msg.state = self._state
        msg.current_mission_id = self._current_mission_id

        errors = list(self._mission_errors)
        # The battery speaks for itself in active_errors rather than only as a
        # percentage, so a consumer can surface why a machine is unhappy
        # without having to know the thresholds in this file.
        if self._has_battery:
            if self._battery <= 0.0:
                errors.append(("BATTERY_DEPLETED", MachineState.SEVERITY_FATAL))
            elif self._battery < BATTERY_CRITICAL_BELOW:
                errors.append(("BATTERY_CRITICAL", MachineState.SEVERITY_FATAL))
            elif self._battery < BATTERY_DEGRADED_BELOW:
                errors.append(("BATTERY_LOW", MachineState.SEVERITY_WARNING))
        if self._sensor_glitch:
            errors.append(("SIM_SENSOR_GLITCH", MachineState.SEVERITY_WARNING))
        msg.active_errors = [code for code, _ in errors]
        msg.active_error_severities = [severity for _, severity in errors]

        msg.stamp = self.get_clock().now().to_msg()
        return msg

    # ------------------------------------------------------------------ #
    # Mission action server -- WORKFLOW-DRIVEN state lives here
    # ------------------------------------------------------------------ #
    def _on_goal_request(self, goal_request: Mission.Goal) -> GoalResponse:
        """Accept or refuse a mission. The only place ASSIGNED is ever set."""
        mission_id = goal_request.mission_id or "(no id)"

        if not _capability_matches(
            goal_request.capability_required, self._capabilities
        ):
            # The dispatcher already matched on capability. Re-checking here
            # means a buggy dispatcher cannot make this machine attempt work it
            # has no business attempting.
            self.get_logger().warning(
                f"rejecting {mission_id}: needs "
                f"'{goal_request.capability_required}', this machine has "
                f"{self._capabilities}"
            )
            return GoalResponse.REJECT

        if self._sim_connection != CONNECTION_ONLINE:
            self.get_logger().warning(
                f"rejecting {mission_id}: [SIMULATED] link is "
                f"'{self._sim_connection}'"
            )
            return GoalResponse.REJECT

        with self._lock:
            if self._state != MachineState.AVAILABLE:
                self.get_logger().warning(
                    f"rejecting {mission_id}: machine is "
                    f"{_state_name(self._state)}, not AVAILABLE"
                )
                return GoalResponse.REJECT

            # Checked here as well as in the dispatcher, for the same reason
            # capability is: the machine is the last word on whether it can do
            # the work, and a buggy dispatcher must not be able to send a
            # nearly flat machine out on a job.
            if self._has_battery and self._battery < BATTERY_MISSION_MINIMUM:
                self.get_logger().warning(
                    f"rejecting {mission_id}: battery at {self._battery:.1f}%, "
                    f"under the {BATTERY_MISSION_MINIMUM:.0f}% minimum for new "
                    "work"
                )
                return GoalResponse.REJECT

            # A new mission supersedes the cool-down after the previous one.
            self._cancel_reset_timer_locked()
            self._state = MachineState.ASSIGNED
            self._current_mission_id = goal_request.mission_id
            self._mission_errors = []

        self.get_logger().info(
            f"accepted {mission_id} "
            f"(capability={goal_request.capability_required}): "
            f"{goal_request.objective}"
        )
        return GoalResponse.ACCEPT

    def _on_cancel_request(self, goal_handle) -> CancelResponse:
        self.get_logger().info(
            f"cancel requested for {goal_handle.request.mission_id}"
        )
        return CancelResponse.ACCEPT

    def _execute_mission(self, goal_handle) -> Mission.Result:
        """Run one mission through to a terminal outcome.

        Progress and duration are SIMULATED. The state transitions and the
        Result are not -- those are the real contract, and the adapter reads
        them exactly as it would read them from a real machine.
        """
        goal = goal_handle.request
        parameters = self._parse_parameters(goal.parameters_json, goal.mission_id)

        # The failure path is driven entirely by the incoming request. Nothing
        # in this file is edited between a golden run and a failing one, which
        # is the only way a demonstrated failure path means anything.
        simulate_failure = bool(parameters.get("simulate_failure", False))
        target = self._resolve_target(parameters, goal.mission_id)

        with self._lock:
            self._state = MachineState.RUNNING
            self._target = target

        self.get_logger().info(
            f"running {goal.mission_id} -> target=({target[0]:.1f}, "
            f"{target[1]:.1f}) simulate_failure={simulate_failure}"
        )

        steps = self._steps_for(target)
        step_sec = float(self.get_parameter("mission_step_sec").value)
        started_at = time.time()

        for step in range(1, steps + 1):
            if goal_handle.is_cancel_requested:
                goal_handle.canceled()
                return self._finish(
                    goal.mission_id,
                    MachineState.EXCEPTION,
                    Mission.Result.RESULT_EXCEPTION,
                    error_code="MISSION_CANCELLED",
                    message=f"{goal.mission_id} cancelled by the OS",
                    result_json={
                        "cancelled_at_percent": (step - 1) * 100 // steps,
                        "simulated": True,
                    },
                )

            progress = step * 100 // steps

            with self._lock:
                battery_dead = self._battery_dead
                battery_now = self._battery
            if battery_dead:
                self.get_logger().error(
                    f"[SIMULATED] {goal.mission_id} aborting at {progress}%: "
                    "battery flat"
                )
                goal_handle.abort()
                return self._finish(
                    goal.mission_id,
                    MachineState.EXCEPTION,
                    Mission.Result.RESULT_EXCEPTION,
                    error_code="BATTERY_DEPLETED",
                    message=(
                        f"{self._machine_id} ran out of battery at "
                        f"{progress}% of {goal.mission_id}"
                    ),
                    result_json={
                        "failed_at_percent": progress,
                        "battery_percent": round(battery_now, 1),
                        "simulated": True,
                        "elapsed_sec": round(time.time() - started_at, 2),
                    },
                    errors=[("BATTERY_DEPLETED", MachineState.SEVERITY_FATAL)],
                )

            if simulate_failure and progress >= FAILURE_AT_PERCENT:
                self.get_logger().error(
                    f"[SIMULATED FAILURE] {goal.mission_id} aborting at "
                    f"{progress}%: OBSTACLE_DETECTED"
                )
                goal_handle.abort()
                return self._finish(
                    goal.mission_id,
                    MachineState.EXCEPTION,
                    Mission.Result.RESULT_EXCEPTION,
                    error_code="OBSTACLE_DETECTED",
                    message=(
                        f"Simulated obstacle blocked {self._machine_id} at "
                        f"{progress}% of {goal.mission_id}"
                    ),
                    result_json={
                        "failed_at_percent": progress,
                        "simulated": True,
                        "elapsed_sec": round(time.time() - started_at, 2),
                    },
                    errors=[("OBSTACLE_DETECTED", MachineState.SEVERITY_FATAL)],
                )

            feedback = Mission.Feedback()
            feedback.progress_percent = progress
            feedback.status_message = (
                f"{goal.objective or 'mission'} -- {progress}% "
                f"(step {step}/{steps})"
            )
            goal_handle.publish_feedback(feedback)
            time.sleep(step_sec)

        goal_handle.succeed()
        with self._lock:
            final_x, final_y = self._x, self._y
        return self._finish(
            goal.mission_id,
            MachineState.COMPLETED,
            Mission.Result.RESULT_COMPLETED,
            error_code="",
            message=(
                f"{self._machine_id} completed {goal.mission_id}: "
                f"{goal.objective or 'mission'}"
            ),
            result_json={
                "simulated": True,
                "elapsed_sec": round(time.time() - started_at, 2),
                "final_position": {
                    "frame_id": LOCAL_FRAME_ID,
                    "x": round(final_x, 3),
                    "y": round(final_y, 3),
                },
            },
        )

    def _steps_for(self, target: tuple[float, float]) -> int:
        """SIMULATED. How many progress steps this mission is worth.

        Derived from the distance still to travel, so a distant goal takes
        longer and reports progress more often than a near one. The old fixed
        count made every mission the same length regardless of its target.

        ``mission_steps`` still wins when set to a positive value, so a test
        can pin a mission to a known length instead of a known distance.
        """
        override = int(self.get_parameter("mission_steps").value)
        if override > 0:
            return override

        with self._lock:
            distance = math.hypot(target[0] - self._x, target[1] - self._y)
        steps = round(distance / METRES_PER_MISSION_STEP)
        return max(MIN_MISSION_STEPS, min(MAX_MISSION_STEPS, steps))

    def _finish(
        self,
        mission_id: str,
        machine_state: int,
        result_state: int,
        *,
        error_code: str,
        message: str,
        result_json: dict,
        errors: list[tuple[str, int]] | None = None,
    ) -> Mission.Result:
        """Land in a terminal state and schedule the return to AVAILABLE."""
        with self._lock:
            self._state = machine_state
            self._mission_errors = list(errors or [])
            self._target = None
            self._schedule_reset_locked()

        self.get_logger().info(
            f"{mission_id} -> {_state_name(machine_state)}: {message}"
        )

        result = Mission.Result()
        result.result_state = result_state
        result.error_code = error_code
        result.message = message
        result.result_json = json.dumps(result_json)
        return result

    def _schedule_reset_locked(self) -> None:
        """Hold the terminal state briefly, then become AVAILABLE again.

        The machine has to linger in COMPLETED/EXCEPTION long enough for a
        polling OS to actually observe it; snapping straight back to AVAILABLE
        would make the terminal state invisible over a 1 Hz feed. The mission's
        own outcome does not depend on this -- the adapter keeps that from the
        action Result -- which is precisely why the machine is free to move on.
        """
        self._cancel_reset_timer_locked()
        hold = float(self.get_parameter("terminal_state_hold_sec").value)
        self._reset_timer = threading.Timer(hold, self._reset_to_available)
        self._reset_timer.daemon = True
        self._reset_timer.start()

    def _cancel_reset_timer_locked(self) -> None:
        if self._reset_timer is not None:
            self._reset_timer.cancel()
            self._reset_timer = None

    def _reset_to_available(self) -> None:
        with self._lock:
            self._reset_timer = None
            # A new mission may have been accepted during the hold.
            if self._state not in (MachineState.COMPLETED, MachineState.EXCEPTION):
                return
            self._state = MachineState.AVAILABLE
            self._current_mission_id = ""
            self._mission_errors = []
        self.get_logger().info(f"{self._machine_id} -> AVAILABLE")

    # ------------------------------------------------------------------ #
    # Goal parameter helpers
    # ------------------------------------------------------------------ #
    def _parse_parameters(self, parameters_json: str, mission_id: str) -> dict:
        """Decode parameters_json, tolerating an empty or malformed payload."""
        if not parameters_json.strip():
            return {}
        try:
            parsed = json.loads(parameters_json)
        except json.JSONDecodeError as exc:
            self.get_logger().warning(
                f"{mission_id}: parameters_json is not valid JSON ({exc}); "
                f"treating it as empty"
            )
            return {}
        if not isinstance(parsed, dict):
            self.get_logger().warning(
                f"{mission_id}: parameters_json must be a JSON object, got "
                f"{type(parsed).__name__}; treating it as empty"
            )
            return {}
        return parsed

    def _resolve_target(
        self, parameters: dict, mission_id: str
    ) -> tuple[float, float]:
        """SIMULATED. Where this mission should drag the machine to.

        Accepts ``{"target_x": .., "target_y": ..}`` or
        ``{"target": {"x": .., "y": ..}}``. Given neither, invents a target
        seeded from the mission id, so the movement simulation still shows
        something and the same mission id always produces the same path.
        """
        target_x = parameters.get("target_x")
        target_y = parameters.get("target_y")

        nested = parameters.get("target")
        if isinstance(nested, dict):
            target_x = nested.get("x", target_x)
            target_y = nested.get("y", target_y)

        if target_x is None or target_y is None:
            rng = random.Random(mission_id or self._machine_id)
            target_x = round(rng.uniform(-30.0, 30.0), 2)
            target_y = round(rng.uniform(-30.0, 30.0), 2)

        try:
            return float(target_x), float(target_y)
        except (TypeError, ValueError):
            self.get_logger().warning(
                f"{mission_id}: target coordinates are not numeric; staying put"
            )
            with self._lock:
                return self._x, self._y

    # ------------------------------------------------------------------ #
    # Teardown
    # ------------------------------------------------------------------ #
    def shutdown(self) -> None:
        with self._lock:
            self._cancel_reset_timer_locked()


def _capability_matches(required: str, advertised: list[str]) -> bool:
    """Compare capabilities case-insensitively.

    A capability is a bare string on both sides of the interface. If the two
    sides disagree on case there is no error to catch -- the machine is simply
    never chosen, which is a far worse failure than a loud one. Folding case
    here means the convention is a preference rather than a trap.
    """
    return required.strip().lower() in {c.strip().lower() for c in advertised}


def _state_name(state: int) -> str:
    """Readable name for a MachineState state constant. Logs only."""
    names = {
        MachineState.AVAILABLE: "AVAILABLE",
        MachineState.ASSIGNED: "ASSIGNED",
        MachineState.RUNNING: "RUNNING",
        MachineState.COMPLETED: "COMPLETED",
        MachineState.EXCEPTION: "EXCEPTION",
    }
    return names.get(state, f"UNKNOWN({state})")


def run_machine(
    *,
    machine_id: str,
    machine_type: str,
    capabilities: list[str],
    start_x: float = 0.0,
    start_y: float = 0.0,
    args=None,
) -> None:
    """Spin up one simulated machine and run it until interrupted."""
    rclpy.init(args=args)
    node = SimulatedMachine(
        machine_id=machine_id,
        machine_type=machine_type,
        capabilities=capabilities,
        start_x=start_x,
        start_y=start_y,
    )
    # Multi-threaded on purpose: _execute_mission blocks for the length of the
    # mission, and a single-threaded executor would stall the 1 Hz heartbeat
    # behind it -- making a busy machine indistinguishable from a dead one.
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.shutdown()
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
