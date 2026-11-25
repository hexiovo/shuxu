from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shuxu import mcp24_access, mcp24_admin, mcp24_contract, mcp_server, seed
from shuxu.book import BookService, TOOL_SPECS


class BatchTwoMCPToolCatalogTests(unittest.TestCase):
    def test_stdio_list_tools_exposes_registered_workflow_history_and_relation_actions(self):
        result = asyncio.run(mcp_server.list_tools(None, None))
        names = {item.name for item in result.tools}
        expected = {item["name"] for item in TOOL_SPECS}
        self.assertEqual(names, expected)
        self.assertTrue({
            "set_session_conditions", "get_prompt_family", "pick_unfinished_prompt",
            "get_prompt_timeline", "review_prompt_events", "read_entity_relations",
            "read_prompt_evidence", "get_topic_project_progress",
        }.issubset(names))
        self.assertEqual(mcp24_contract.SERVICE_VERSION, "1.4.0")

    def test_relation_writer_and_history_tools_have_mcp_annotations(self):
        tools = {item.name: item for item in mcp_server.TOOLS}
        self.assertFalse(tools["write_claim_evidence"].annotations.read_only_hint)
        self.assertTrue(tools["read_prompt_evidence"].annotations.read_only_hint)
        self.assertTrue(tools["get_prompt_timeline"].annotations.read_only_hint)


class NamedUndoNamespaceTests(unittest.TestCase):
    def test_named_request_receipt_can_be_undone_and_appears_in_timeline(self):
        with tempfile.TemporaryDirectory(prefix="shuxu-named-undo-") as temp_dir:
            db_path = Path(temp_dir) / "book.sqlite3"
            book = BookService(db_path)
            mcp24_admin.create_host(
                db_path, host_id="named-undo-test", bearer="N" * 40,
                profile="operator", scopes=["tool:*"],
            )
            actor = mcp24_access.authenticate(book, "named-undo-test", "N" * 40)
            now = datetime.now(timezone.utc)
            db = book._connect()
            db.execute(
                """INSERT INTO prompts(id,text,direction,topic_id,depth,parent_id,state,snoozed_until,
                    visibility,origin,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("named-undo-prompt", "Synthetic prompt for named undo", seed.DIRECTIONS[0][0], None,
                 "medium", None, "in_progress", None, "shared", "web", 1,
                 now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
            )
            db.close()

            snoozed = book.dispatch(
                "snooze_prompt",
                {"request_id": "public-snooze-1", "prompt_id": "named-undo-prompt",
                 "snoozed_until": (now + timedelta(days=2)).isoformat(timespec="seconds"),
                 "expected_version": 1},
                channel="mcp", actor=actor,
            )
            self.assertEqual(snoozed["status"], "ok")
            undone = book.dispatch(
                "undo_action",
                {"request_id": "public-undo-1", "target_request_id": "public-snooze-1"},
                channel="mcp", actor=actor,
            )
            self.assertEqual(undone["status"], "ok")
            self.assertEqual(undone["data"]["target_request_id"], "public-snooze-1")

            db = book._connect()
            prompt = db.execute("SELECT state,snoozed_until,version FROM prompts WHERE id='named-undo-prompt'").fetchone()
            db.close()
            self.assertEqual(prompt["state"], "in_progress")
            self.assertIsNone(prompt["snoozed_until"])
            self.assertEqual(prompt["version"], 3)

            timeline = book.dispatch(
                "get_prompt_timeline", {"prompt_ids": ["named-undo-prompt"]},
                channel="mcp", actor=actor,
            )
            self.assertEqual([event["event_type"] for event in timeline["data"]["events"]], ["snoozed", "undone"])
            self.assertEqual(timeline["data"]["events"][1]["source"]["target_request_id"], "public-snooze-1")
            self.assertEqual(timeline["data"]["events"][1]["source"]["request_id"], "public-undo-1")


if __name__ == "__main__":
    unittest.main()
