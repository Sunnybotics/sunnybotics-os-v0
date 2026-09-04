# Copyright 2026 Sunnybotics.
# Licensed under the Apache License, Version 2.0.
"""Bring up the whole SIMULATED fleet at once.

    ros2 launch sunnybotics_machines machines.launch.py

Adding a third machine here needs no change anywhere else in the system: the
adapter discovers whatever is publishing on /machines/*/state.
"""

from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    return LaunchDescription(
        [
            Node(
                package="sunnybotics_machines",
                executable="rover_01",
                name="rover_01",
                output="screen",
                emulate_tty=True,
            ),
            Node(
                package="sunnybotics_machines",
                executable="rover_02",
                name="rover_02",
                output="screen",
                emulate_tty=True,
            ),
        ]
    )
