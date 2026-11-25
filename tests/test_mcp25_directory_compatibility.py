from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from shuxu import mcp24_access, mcp24_contract, mcp_server, mcp_workflows
from shuxu.book import BookService, TOOL_SPECS


RUN_EVIDENCE = Path("H:/AIcode/HexiPlan/runs/shuxu-full-mcp-20261125-01a20bc6/evidence")
SNAPSHOTS = {
    "api-catalog-batch-i-v1.json": "7c4c684d6827fd4a390cab85c2d9028f77ef9f4f4870b7617cbd98503439587d",
    "api-catalog-batch-ii-v1.json": "5c7e411bcbd0689161aa24572d4ac2b1e49b8eec56d0f6830a05acaf0dae9e2f",
}


def _snapshot(name):
    content = (RUN_EVIDENCE / name).read_bytes()
    if hashlib.sha256(content).hexdigest() != SNAPSHOTS[name]:
        raise AssertionError(f"Unexpected RUN evidence hash for {name}")
    return json.loads(content.decode("utf-8"))


class DirectoryCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.batch_i = _snapshot("api-catalog-batch-i-v1.json")
        cls.batch_ii = _snapshot("api-catalog-batch-ii-v1.json")

    def test_saved_catalogs_show_real_additions_field_changes_and_enum_expansion(self):
        old = mcp24_contract._baseline_directory(self.batch_i)["tools"]
        current = mcp24_contract._baseline_directory(self.batch_ii)["tools"]
        added = set(current) - set(old)
        removed = set(old) - set(current)
        changed_schema = {
            name for name in set(old) & set(current)
            if old[name]["inputSchema"] != current[name]["inputSchema"]
        }
        changed_description = {
            name for name in set(old) & set(current)
            if old[name].get("description") != current[name].get("description")
        }
        self.assertEqual(len(old), 96)
        self.assertEqual(len(current), 116)
        self.assertEqual(len(added), 20)
        self.assertFalse(removed)
        self.assertEqual(len(changed_schema), 8)
        self.assertEqual(changed_description, {"narrow_prompt", "guide_thought", "next_prompt"})

        reason_changes = mcp24_contract._schema_changes(
            old["set_prompt_state"]["inputSchema"], current["set_prompt_state"]["inputSchema"]
        )
        reason = next(item for item in reason_changes if item["path"] == "inputSchema/properties/reason")
        self.assertEqual(reason["change"], "added")
        self.assertEqual(reason["impact"], "compatible")
        enum_changes = mcp24_contract._schema_changes(
            old["search_indexed_library"]["inputSchema"], current["search_indexed_library"]["inputSchema"]
        )
        kinds = next(item for item in enum_changes if item["path"] == "inputSchema/properties/kinds/items/enum")
        self.assertEqual(kinds["added_values"], ["relation"])
        self.assertEqual(kinds["impact"], "compatible")

        old_pick = old["pick_prompt"]["inputSchema"]
        current_pick = current["pick_prompt"]["inputSchema"]
        live_pick = next(item["inputSchema"] for item in TOOL_SPECS if item["name"] == "pick_prompt")
        self.assertEqual(old_pick, current_pick)
        self.assertEqual(current_pick, live_pick)

    def test_public_comparison_uses_full_current_catalog_and_pages_differences(self):
        result = mcp24_contract._compare_mcp_directories(
            None, {"baseline": self.batch_ii, "limit": 1, "offset": 0}, mcp24_access.local_actor()
        )
        self.assertEqual(result["baseline"]["source"], "tool_catalog")
        self.assertEqual(result["baseline"]["tool_count"], 116)
        self.assertTrue(result["current"]["complete"])
        self.assertGreater(result["current"]["tool_count"], 100)
        self.assertEqual(result["limit"], 1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["next_offset"], 1)
        self.assertGreaterEqual(result["counts"]["added"], 1)
        self.assertIn("compare_mcp_directories", {item["name"] for item in TOOL_SPECS})
        self.assertIn("compare_mcp_directories", mcp24_contract.HANDLERS)
        stdio = asyncio.run(mcp_server.list_tools(None, None))
        self.assertEqual({item.name for item in stdio.tools}, {item["name"] for item in TOOL_SPECS})
        self.assertIn("compare_mcp_directories", {item.name for item in stdio.tools})
        self.assertEqual(result["changes"][0]["tool"], "compare_mcp_directories")
        self.assertEqual(result["changes"][0]["migration"]["missing_required_fields"], ["baseline"])

    def test_same_host_directory_reports_actual_access_changes(self):
        actor = {
            "host_id": "named-directory-test", "profile": "operator",
            "scopes": ["tool:compare_mcp_directories"], "authorization_revision": 2,
        }
        old_prompt = next(item for item in TOOL_SPECS if item["name"] == "get_prompt")
        baseline = {
            "directory_id": "prior-directory-id", "directory_version": "1.4.0",
            "directory_schema_version": mcp24_contract.DIRECTORY_SCHEMA_VERSION,
            "protocol_version": mcp24_contract.PROTOCOL_VERSION, "service_version": "1.4.0",
            "identity": {"host_id": actor["host_id"], "profile": actor["profile"], "authorization_revision": 1},
            "capabilities": {"tool_count": 1}, "tools": [mcp24_contract._tool_entry(old_prompt, mcp24_contract._write_names())],
            "total_count": 1, "count": 1, "offset": 0, "complete": True,
        }
        result = mcp24_contract._compare_mcp_directories(None, {"baseline": baseline}, actor)
        self.assertFalse(result["compatible"])
        self.assertTrue(result["permission_delta"]["comparable"])
        self.assertEqual(result["permission_delta"]["newly_visible"], ["compare_mcp_directories"])
        self.assertEqual(result["permission_delta"]["no_longer_visible"], ["get_prompt"])
        self.assertEqual(result["permission_delta"]["currently_denied"], ["get_prompt"])
        self.assertIn("access_lost", {item["change"] for item in result["changes"]})

    def test_registered_tool_dispatches_a_full_snapshot_in_the_standard_envelope(self):
        with tempfile.TemporaryDirectory(prefix="shuxu-directory-compare-") as temp_dir:
            book = BookService(Path(temp_dir) / "book.sqlite3")
            result = book.dispatch(
                "compare_mcp_directories", {"baseline": self.batch_ii, "limit": 1, "offset": 0},
                channel="mcp", actor=mcp24_access.local_actor(),
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["baseline"]["tool_count"], 116)
        self.assertIn("directory_id", result["data"]["current"])
        self.assertIn("permission_delta", result["data"])

    def test_incomplete_baseline_is_rejected_and_old_directory_call_shape_remains(self):
        incomplete = dict(self.batch_i)
        incomplete["tools"] = incomplete["tools"][:-1]
        with self.assertRaises(mcp_workflows.WorkflowError) as raised:
            mcp24_contract._compare_mcp_directories(
                None, {"baseline": incomplete}, mcp24_access.local_actor()
            )
        self.assertEqual(raised.exception.code, "baseline_incomplete")

        directory = mcp24_contract._directory(None, {"limit": 1, "offset": 0}, mcp24_access.legacy_actor())
        with self.assertRaises(mcp_workflows.WorkflowError) as raised:
            mcp24_contract._compare_mcp_directories(
                None, {"baseline": directory}, mcp24_access.local_actor()
            )
        self.assertEqual(raised.exception.code, "baseline_incomplete")
        self.assertEqual(set(directory), {
            "identity", "full_access", "directory_id", "directory_version", "directory_schema_version",
            "capabilities", "categories", "tools", "total_count", "count", "offset", "complete", "next_offset",
        })
        self.assertEqual(directory["count"], 1)
        self.assertEqual(directory["offset"], 0)
        self.assertFalse(directory["complete"])


if __name__ == "__main__":
    unittest.main()
