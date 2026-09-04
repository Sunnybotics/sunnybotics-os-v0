# Copyright 2026 Sunnybotics.
# Licensed under the Apache License, Version 2.0.
"""Adapter entry point: one process, two runtimes.

    ros2 run sunnybotics_adapter adapter
    ros2 run sunnybotics_adapter adapter --port 8080

``rclpy`` wants a synchronous spin loop and FastAPI wants an asyncio one, and
neither will host the other. So the ROS executor gets a daemon thread of its own
and uvicorn keeps the main thread. Everything shared between the two lives on
``AdapterNode`` behind a lock, and the HTTP handlers are synchronous so they are
allowed to block on it.

The ROS side is started first and given a moment to spin before HTTP opens, so
the first ``GET /machines`` after start-up returns the fleet rather than an
empty list that looks like an outage.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

import rclpy
import uvicorn
from rclpy.executors import MultiThreadedExecutor

from sunnybotics_adapter.adapter_node import AdapterNode
from sunnybotics_adapter.os_client import OSClient
from sunnybotics_adapter.rest_api import create_app

#: Long enough for discovery to run once and the first 1 Hz heartbeats to land.
DISCOVERY_GRACE_SEC = 1.5


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="sunnybotics-adapter",
        description="Bridge ROS 2 machines onto the SunnyboticsOS REST contract.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Interface to bind. Defaults to localhost because V0 has no "
            "authentication; do not widen this without adding some."
        ),
    )
    parser.add_argument("--port", type=int, default=8001, help="HTTP port.")
    parser.add_argument(
        "--os-url",
        default=None,
        help=(
            "Base URL of the SunnyboticsOS API, e.g. http://10.0.0.5:9000. "
            "When set, machines are registered with the OS and their state "
            "and mission progress are pushed to it. When omitted, the "
            "adapter only serves its own REST API and pushes nothing."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn log level.",
    )
    # Unknown args are handed to rclpy so --ros-args still works.
    return parser.parse_known_args(argv)


def main(argv: list[str] | None = None) -> int:
    args, ros_args = parse_args(list(sys.argv[1:] if argv is None else argv))

    rclpy.init(args=[sys.argv[0], *ros_args])

    os_client = None
    node = AdapterNode()
    if args.os_url:
        os_client = OSClient(args.os_url, node.get_logger())
        node.attach_os_client(os_client)
    else:
        node.get_logger().info(
            "no --os-url given: serving the local REST API only, "
            "pushing nothing"
        )

    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    spin_thread = threading.Thread(
        target=executor.spin, name="rclpy-spin", daemon=True
    )
    spin_thread.start()

    node.get_logger().info("rclpy spinning on a background thread")
    time.sleep(DISCOVERY_GRACE_SEC)
    node.get_logger().info(
        f"machines discovered before HTTP came up: {node.known_machine_ids()}"
    )
    node.get_logger().info(
        f"REST API on http://{args.host}:{args.port}  "
        f"(docs at http://{args.host}:{args.port}/docs)"
    )
    node.get_logger().warning(
        "V0: every machine behind this adapter is SIMULATED, and there is no "
        "authentication. Localhost only."
    )

    app = create_app(node)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    except KeyboardInterrupt:
        pass
    finally:
        if os_client is not None:
            os_client.shutdown()
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
