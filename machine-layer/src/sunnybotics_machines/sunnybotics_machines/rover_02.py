# Copyright 2026 Sunnybotics.
# Licensed under the Apache License, Version 2.0.
"""SIMULATED inspection rover. The second instance of the same node class.

Note what is *not* here: no type-specific message, no type-specific topic, no
branch anywhere in the OS that tests for an inspection rover. A different
machine type is a different set of constructor arguments.

It is worth noticing that this file and rover_01.py differ only in their
values. The two machines share a chassis class in the fleet's imagination and
a node class in ours, but the OS cannot tell either way -- it matches on
capabilities and never reads machine_type at all.
"""

from sunnybotics_machines.machine_node import run_machine


def main(args=None) -> None:
    run_machine(
        machine_id="rover_02",
        machine_type="inspection_rover",
        capabilities=["inspection"],
        # SIMULATED starting position, metres in the site_A_local frame.
        start_x=40.0,
        start_y=-12.0,
        args=args,
    )


if __name__ == "__main__":
    main()
