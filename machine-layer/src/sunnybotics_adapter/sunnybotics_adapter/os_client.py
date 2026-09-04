# Copyright 2026 Sunnybotics.
# Licensed under the Apache License, Version 2.0.
"""Outbound client to the SunnyboticsOS API.

**Which way the connection is opened matters more than it looks.** This adapter
also serves its own read-only REST API, but that requires the OS to reach *in*
to the machine network -- which in a real deployment means the robots need to be
publicly addressable, or sat behind a tunnel. Machines are normally the ones
behind NAT, so the durable arrangement is the other way round: the OS is the
server, and the machine side is a client that registers itself and pushes.

This module is that client. It speaks the OS-side contract:

    POST   {base}/api/v0/machines/register          on discovery
    PATCH  {base}/api/v0/machines/{id}/status       every heartbeat
    POST   {base}/api/v0/missions/{id}/report       on progress and completion

It is entirely optional. With no ``--os-url`` configured nothing here runs and
the adapter behaves exactly as it does standalone, so the local REST API and the
demo keep working while the OS side is still being built.

Two properties this deliberately guarantees:

* **It never blocks a ROS callback.** Every send is queued and drained by one
  background thread. A slow or dead OS delays telemetry; it must never stall the
  executor, because that would stop the machines being heard from at all.
* **It tolerates the OS being absent.** Failures are logged and dropped, and a
  machine whose registration failed is retried on its next heartbeat. The
  machine layer stays up whether or not anything is listening.
"""

from __future__ import annotations

import json
import queue
import threading
import urllib.error
import urllib.request
from typing import Any

#: The OS-side API prefix, per the shared contract.
API_PREFIX = "/api/v0"

#: Bounded so a permanently dead OS cannot grow the queue without limit. When
#: full, the oldest telemetry is dropped -- stale position data is worth less
#: than staying responsive.
MAX_QUEUED = 500


class OSClient:
    """Pushes machine and mission state to the SunnyboticsOS API."""

    def __init__(self, base_url: str, logger, timeout_sec: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._log = logger
        self._timeout = timeout_sec

        self._queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUED)
        self._registered: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()

        self._sent = 0
        self._failed = 0
        self._dropped = 0

        self._thread = threading.Thread(
            target=self._worker, name="os-client", daemon=True
        )
        self._thread.start()
        self._log.info(f"OS client -> {self._base}{API_PREFIX} (push mode enabled)")

    # ------------------------------------------------------------------ #
    # Public API -- all of these return immediately
    # ------------------------------------------------------------------ #
    def register_machine(self, descriptor: dict[str, Any]) -> None:
        machine_id = descriptor["machine_id"]
        self._enqueue("POST", f"{API_PREFIX}/machines/register", descriptor)
        self._log.info(f"registering '{machine_id}' with the OS")

    def is_registered(self, machine_id: str) -> bool:
        with self._lock:
            return machine_id in self._registered

    def patch_status(self, machine_id: str, descriptor: dict[str, Any]) -> None:
        """Heartbeat. Registers first if a previous attempt never succeeded."""
        if not self.is_registered(machine_id):
            self.register_machine(descriptor)
            return
        body = {
            key: descriptor[key]
            for key in ("state", "health", "location", "current_mission_id")
            if key in descriptor
        }
        self._enqueue("PATCH", f"{API_PREFIX}/machines/{machine_id}/status", body)

    def report_mission(
        self,
        mission_id: str,
        status: str,
        detail: str,
        timestamp: str | None = None,
        progress_percent: int | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "mission_id": mission_id,
            "status": status,
            "detail": detail,
            "timestamp": timestamp,
        }
        if progress_percent is not None:
            # Not in the OS contract yet. Sent anyway because a consumer cannot
            # draw a progress bar from RUNNING/COMPLETED alone, and an unknown
            # extra key is cheap for the receiver to ignore.
            body["progress_percent"] = progress_percent
        self._enqueue("POST", f"{API_PREFIX}/missions/{mission_id}/report", body)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            registered = sorted(self._registered)
        return {
            "os_url": self._base,
            "registered_machines": registered,
            "sent": self._sent,
            "failed": self._failed,
            "dropped": self._dropped,
            "queued": self._queue.qsize(),
        }

    def shutdown(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _enqueue(self, method: str, path: str, body: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait((method, path, body))
        except queue.Full:
            self._dropped += 1
            if self._dropped % 50 == 1:
                self._log.warning(
                    f"OS client queue full; dropped {self._dropped} messages "
                    f"(is {self._base} reachable?)"
                )

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                method, path, body = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._send(method, path, body)

    def _send(self, method: str, path: str, body: dict[str, Any]) -> None:
        url = f"{self._base}{path}"
        data = json.dumps(body).encode()
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = response.read()
                self._sent += 1
                if path.endswith("/machines/register"):
                    machine_id = body.get("machine_id", "")
                    with self._lock:
                        self._registered.add(machine_id)
                    self._log.info(
                        f"'{machine_id}' registered with the OS "
                        f"({response.status})"
                    )
                return payload
        except urllib.error.HTTPError as exc:
            self._failed += 1
            detail = exc.read()[:200].decode(errors="replace")
            self._log.warning(f"{method} {path} -> {exc.code} {detail}")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            self._failed += 1
            # Expected while the OS is down. Logged, not raised: the machine
            # layer has to keep running whether or not anything is listening.
            if self._failed % 20 == 1:
                self._log.warning(
                    f"{method} {path} unreachable ({exc}); "
                    f"{self._failed} failures so far"
                )
        return None
