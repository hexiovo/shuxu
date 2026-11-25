from __future__ import annotations

import unittest
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from shuxu import mcp24_access, mcp24_admin, mcp_server, seed
from shuxu.book import BookService, TOOL_SPECS


class MCPStdioCatalogTests(unittest.TestCase):
    def test_complete_instruction_bundle_and_full_tool_catalog_are_discoverable(self):
        project_root = Path(mcp_server.__file__).resolve().parents[1]
        prompt_root = project_root / "config" / "mcp-prompts"
        filenames = (
            "01-host-role.txt", "02-tool-routing.txt", "03-materials-and-full-text.txt",
            "04-admin-operations.txt", "05-timeouts-and-recovery.txt", "06-host-acceptance.txt",
        )
        expected = "\n\n".join((prompt_root / name).read_text(encoding="utf-8") for name in filenames)
        self.assertEqual(mcp_server.HOST_INSTRUCTIONS, expected)

        expected_names = {item["name"] for item in TOOL_SPECS}
        actual_names = {item.name for item in mcp_server.TOOLS}
        self.assertEqual(actual_names, expected_names)
        for name in ("get_full_record", "list_full_records", "get_attachment_chunk", "export_full_library",
                     "get_full_export_info", "read_full_export_chunk", "correct_prompt_status"):
            self.assertIn(name, actual_names)

    def test_write_annotations_include_full_export(self):
        item = next(tool for tool in mcp_server.TOOLS if tool.name == "export_full_library")
        self.assertFalse(item.annotations.read_only_hint)
        self.assertTrue(item.annotations.idempotent_hint)

    def test_plan_previous_keyword_uses_conversation_history_before_current_prompt(self):
        with tempfile.TemporaryDirectory(prefix="shuxu-plan-reference-") as temp_dir:
            db_path = Path(temp_dir) / "book.sqlite3"
            book = BookService(db_path)
            mcp24_admin.create_host(
                db_path, host_id="plan-reference-test", bearer="R" * 40,
                profile="operator", scopes=["tool:*"],
            )
            actor = mcp24_access.authenticate(book, "plan-reference-test", "R" * 40)
            conversation_id = mcp24_access.namespace_id(actor, "conversation", "session-1")
            direction = seed.DIRECTIONS[0][0]
            db = book._connect()
            prompts = (
                ("historical-relations", "如何处理亲密关系中的界限", "2026-11-24T12:00:00+00:00"),
                ("current-boundaries", "如何规划时间边界", "2026-11-25T12:00:00+00:00"),
            )
            for prompt_id, text, created_at in prompts:
                db.execute(
                    """INSERT INTO prompts(id,text,direction,topic_id,depth,parent_id,state,snoozed_until,
                        visibility,origin,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (prompt_id, text, direction, None, "light", None, "in_progress", None,
                     "shared", "mcp", 1, created_at, created_at),
                )
            db.execute(
                "INSERT INTO mcp_conversation_entries VALUES(?,?,?,?,?,?)",
                ("history-entry", conversation_id, "historical-relations", "bind", "{}", prompts[0][2]),
            )
            db.execute(
                "INSERT INTO mcp_conversation_entries VALUES(?,?,?,?,?,?)",
                ("current-entry", conversation_id, "current-boundaries", "bind", "{}", prompts[1][2]),
            )
            db.close()

            result = book.dispatch(
                "plan_mcp_calls",
                {"intent": "get_prompt", "conversation_id": "session-1", "reference": "上次", "keywords": ["关系"]},
                channel="mcp", actor=actor,
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["data"]["calls"][0]["arguments"]["prompt_id"], "historical-relations")

    def test_pending_checkpoint_staging_checks_privacy_before_persisting(self):
        with tempfile.TemporaryDirectory(prefix="shuxu-pending-privacy-") as temp_dir:
            db_path = Path(temp_dir) / "book.sqlite3"
            book = BookService(db_path)
            mcp24_admin.create_host(
                db_path, host_id="pending-privacy-test", bearer="P" * 40,
                profile="operator", scopes=["tool:*"],
            )
            actor = mcp24_access.authenticate(book, "pending-privacy-test", "P" * 40)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            db = book._connect()
            db.execute(
                """INSERT INTO prompts(id,text,direction,topic_id,depth,parent_id,state,snoozed_until,
                    visibility,origin,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("private-consult-prompt", "Synthetic reflection prompt", "consultation_reflection", None,
                 "light", None, "in_progress", None, "private", "mcp", 1, now, now),
            )
            db.close()

            staged = book.dispatch(
                "continue_pending_call",
                {"request_id": "safe-pending", "tool": "save_thought_checkpoint",
                 "arguments": {"cue": "ordinary synthetic cue"}},
                channel="mcp", actor=actor,
            )
            self.assertEqual(staged["status"], "ok")
            pending_id = staged["data"]["pending_id"]

            rejected_update = book.dispatch(
                "continue_pending_call",
                {"request_id": "private-pending-update", "pending_id": pending_id,
                 "expected_version": 1, "arguments": {"unanswered_question": "姓名是张三"}},
                channel="mcp", actor=actor,
            )
            self.assertEqual(rejected_update["error"]["code"], "privacy_scope_required")
            self.assertNotIn("张三", json.dumps(rejected_update, ensure_ascii=False))

            rejected_private_prompt = book.dispatch(
                "continue_pending_call",
                {"request_id": "private-prompt-pending", "tool": "save_thought_checkpoint",
                 "arguments": {"prompt_id": "private-consult-prompt", "cue": "姓名是李四"}},
                channel="mcp", actor=actor,
            )
            self.assertEqual(rejected_private_prompt["error"]["code"], "privacy_rejected")
            self.assertNotIn("李四", json.dumps(rejected_private_prompt, ensure_ascii=False))

            db = book._connect()
            row = db.execute("SELECT arguments_json,version FROM mcp24_pending_calls WHERE pending_id=?", (pending_id,)).fetchone()
            count = db.execute("SELECT count(*) FROM mcp24_pending_calls").fetchone()[0]
            db.close()
            self.assertEqual(json.loads(row["arguments_json"]), {"cue": "ordinary synthetic cue"})
            self.assertEqual(row["version"], 1)
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
