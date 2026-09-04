# Copyright 2026 Sunnybotics.
# Licensed under the Apache License, Version 2.0.
"""SIMULATED cleaning rover. One of two instances of the same node class.

This file contains configuration and nothing else. That is the point: the
difference between one machine type and the next is data, not code.
"""

from sunnybotics_machines.machine_node import run_machine


def main(args=None) -> None:
    run_machine(
        machine_id="rover_01",
        machine_type="cleaning_rover",
        capabilities=["cleaning"],
        # SIMULATED starting position, metres in the site_A_local frame.
        start_x=2.0,
        start_y=3.0,
        args=args,
    )


if __name__ == "__main__":
    main()
