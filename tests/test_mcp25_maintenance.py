from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from contextlib import closing
from pathlib import Path

from shuxu.mcp24_contract import SERVICE_VERSION


class IsolatedMaintenanceRestartTest(unittest.TestCase):
    def test_drain_timeout_reopens_admission_then_same_operation_restarts_instance(self) -> None:
        run_tmp = Path(os.environ.get("HEXIPLAN_RUN_TMP", tempfile.gettempdir())).resolve()
        run_tmp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="mcp25-maintenance-", dir=run_tmp) as temp:
            root = Path(temp)
            data_dir = root / "data"
            runtime_dir = root / "runtime"
            data_dir.mkdir()
            runtime_dir.mkdir()
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            env = os.environ.copy()
            env.update({
                "SHUXU_DATA_DIR": str(data_dir),
                "SHUXU_RUNTIME_DIR": str(runtime_dir),
                "SHUXU_PORT": str(port),
            })
            script = Path(__file__).resolve().parents[1] / "shuxu" / "server.py"
            runtime_file = runtime_dir / "service.json"
            old_info = None
            new_info = None

            def invoke(command: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, "-X", "utf8", "-B", str(script), command],
                    cwd=script.parents[1], env=env, capture_output=True, text=True, timeout=timeout,
                )

            def read_info() -> dict | None:
                try:
                    return json.loads(runtime_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return None

            def call(info: dict, action: str, params: dict) -> dict:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/{action}",
                    data=json.dumps(params).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-Book-Bridge": info["bridge_token"]},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    return json.load(response)

            try:
                started = invoke("start")
                self.assertEqual(started.returncode, 0, started.stderr)
                old_info = read_info()
                self.assertIsNotNone(old_info)

                queued_job_id = "synthetic-queued-maintenance-blocker"
                with closing(sqlite3.connect(data_dir / "mcp24-operations.sqlite3")) as db:
                    now = "2026-11-25T00:00:00Z"
                    db.execute(
                        """INSERT INTO mcp24_async_jobs
                           (job_id, actor_domain, request_id, operation_request_id, action, arguments_json,
                            arguments_hash, status, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                        (queued_job_id, "isolated-test-actor", "queued-request", "queued-operation",
                         "create_backup", "{}", "synthetic", now, now),
                    )
                    db.commit()

                operation_id = "isolated-maintenance-operation-1"
                accepted = call(old_info, "restart_service", {
                    "request_id": "isolated-restart-request-1",
                    "operation_id": operation_id,
                    "timeout_seconds": 1,
                })
                self.assertEqual(accepted["status"], "ok")
                self.assertEqual(accepted["data"]["status"], "accepted")

                deadline = time.monotonic() + 12
                failed = None
                while time.monotonic() < deadline:
                    current = read_info()
                    if current and current["instance_id"] == old_info["instance_id"]:
                        try:
                            status = call(current, "get_service_restart", {"operation_id": operation_id})
                        except OSError:
                            time.sleep(0.1)
                            continue
                        data = status.get("data", {})
                        if data.get("status") == "failed":
                            failed = data
                            break
                    time.sleep(0.1)
                self.assertIsNotNone(failed, "drain timeout was not durably queryable")
                self.assertEqual(failed["error"]["code"], "drain_timeout")
                self.assertTrue(failed["retryable"])
                self.assertEqual(failed["active_work"]["jobs"][0]["job_id"], queued_job_id)

                resumed = call(old_info, "save_prompt", {})
                self.assertNotEqual(resumed.get("status"), "maintenance_in_progress")

                with closing(sqlite3.connect(data_dir / "mcp24-operations.sqlite3")) as db:
                    db.execute(
                        "UPDATE mcp24_async_jobs SET status = 'failed', error_code = 'synthetic_complete' WHERE job_id = ?",
                        (queued_job_id,),
                    )
                    db.commit()
                retried = call(old_info, "retry_service_restart", {
                    "request_id": "isolated-restart-request-2",
                    "operation_id": operation_id,
                    "timeout_seconds": 15,
                })
                self.assertEqual(retried["status"], "ok")
                self.assertEqual(retried["data"]["status"], "accepted")
                self.assertEqual(retried["data"]["attempt"], 2)

                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    candidate = read_info()
                    if candidate and candidate["instance_id"] != old_info["instance_id"]:
                        new_info = candidate
                        try:
                            state = call(new_info, "get_service_restart", {"operation_id": operation_id}).get("data", {})
                        except OSError:
                            time.sleep(0.1)
                            continue
                        if state.get("status") == "completed":
                            break
                        if state.get("status") == "failed":
                            self.fail(f"isolated restart failed: {state.get('error')}")
                    time.sleep(0.1)
                self.assertIsNotNone(new_info, "independent helper did not start a new service instance")
                self.assertNotEqual(new_info["instance_id"], old_info["instance_id"])
                self.assertEqual(new_info["service_version"], SERVICE_VERSION)
                self.assertEqual(new_info["restart_operation_id"], operation_id)
                self.assertEqual(state["status"], "completed")
                self.assertEqual(state["attempt"], 2)
                self.assertTrue(state["completion_receipt"]["business_receipt_persisted"])
                self.assertTrue(state["completion_receipt"]["health_verified"])
                self.assertTrue(state["completion_receipt"]["handshake_verified"])
                self.assertEqual(state["completion_receipt"]["old_instance"]["instance_id"], old_info["instance_id"])
                self.assertEqual(state["completion_receipt"]["new_instance"]["instance_id"], new_info["instance_id"])

                with closing(sqlite3.connect(data_dir / "shuxu.sqlite3")) as db:
                    receipts = db.execute(
                        "SELECT request_id, action FROM request_receipts WHERE request_id IN (?, ?) ORDER BY request_id",
                        ("isolated-restart-request-1", "isolated-restart-request-2"),
                    ).fetchall()
                self.assertEqual({row[1] for row in receipts}, {"restart_service", "retry_service_restart"})
            finally:
                current = read_info()
                if current is not None:
                    invoke("stop")
                    stop_deadline = time.monotonic() + 5
                    while runtime_file.exists() and time.monotonic() < stop_deadline:
                        time.sleep(0.1)
                    time.sleep(0.25)


if __name__ == "__main__":
    unittest.main()
