from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shuxu import mcp24_access, mcp24_ops
from shuxu.book import BookService, TOOL_SPECS


class ServiceRestartOperationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mcp25-ops-")
        self.root = Path(self.temp.name)
        self.book = BookService(self.root / "shuxu.sqlite3")
        self.actor = mcp24_access.legacy_actor()
        self.book._service_restart_context = {
            "instance_id": "old-instance", "pid": 123, "port": 17831,
            "service_version": "1.4.0",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_restart_and_retry_keep_one_durable_operation_id(self) -> None:
        operation_id = "maintenance-retry-same-id"
        accepted = self.book.dispatch(
            "restart_service",
            {"request_id": "restart-request-1", "operation_id": operation_id, "timeout_seconds": 5},
            channel="mcp", actor=self.actor,
        )
        self.assertEqual(accepted["status"], "ok")
        self.assertEqual(accepted["data"]["status"], "accepted")
        store = mcp24_ops.ServiceMaintenanceStore(self.root / "mcp24-operations.sqlite3")
        self.assertEqual(store.get(self.actor, operation_id)["attempt"], 1)

        store.update(
            mcp24_ops._actor_domain(self.actor), operation_id, status="failed",
            retryable=True, error={"code": "drain_timeout", "message": "synthetic blocker"},
        )
        self.book._dispatch_scheduler.finish_maintenance(operation_id)
        self.book._service_restart_context["instance_id"] = "second-old-instance"
        retried = self.book.dispatch(
            "retry_service_restart",
            {"request_id": "restart-request-2", "operation_id": operation_id, "timeout_seconds": 9},
            channel="mcp", actor=self.actor,
        )
        self.assertEqual(retried["status"], "ok")
        self.assertEqual(retried["data"]["operation_id"], operation_id)
        self.assertEqual(retried["data"]["attempt"], 2)
        state = store.get(self.actor, operation_id)
        self.assertEqual(state["status"], "accepted")
        self.assertEqual(state["attempt"], 2)
        self.assertEqual(state["request_id"], "restart-request-2")
        self.assertEqual(len(state["attempts"]), 2)

        queried = self.book.dispatch(
            "get_service_restart", {"operation_id": operation_id}, channel="mcp", actor=self.actor,
        )
        self.assertEqual(queried["data"]["operation_id"], operation_id)
        self.assertEqual(queried["data"]["attempt"], 2)

    def test_tools_publish_the_persistent_restart_contract(self) -> None:
        by_name = {tool["name"]: tool for tool in TOOL_SPECS}
        for name in ("restart_service", "get_service_restart", "retry_service_restart"):
            self.assertIn(name, by_name)
        restart = by_name["restart_service"]["inputSchema"]
        self.assertEqual(restart["required"], ["request_id", "operation_id"])
        self.assertEqual(
            set(restart["properties"]), {"request_id", "operation_id", "timeout_seconds"},
        )
        self.assertEqual(restart["properties"]["timeout_seconds"]["minimum"], 1)
        self.assertEqual(restart["properties"]["timeout_seconds"]["maximum"], 3600)
        self.assertEqual(
            by_name["get_service_restart"]["inputSchema"]["required"], ["operation_id"],
        )
        self.assertEqual(
            by_name["retry_service_restart"]["inputSchema"]["required"], ["request_id", "operation_id"],
        )


if __name__ == "__main__":
    unittest.main()
