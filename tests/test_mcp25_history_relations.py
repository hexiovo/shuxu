from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from shuxu import mcp24_access, mcp24_admin, mcp25_history, seed
from shuxu.book import BookService, TOOL_SPECS


class PromptHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shuxu-prompt-history-")
        self.db_path = Path(self.temp.name) / "book.sqlite3"
        self.book = BookService(self.db_path)
        mcp24_admin.create_host(
            self.db_path, host_id="history-full-test", bearer="H" * 40,
            profile="operator", scopes=["tool:*"],
        )
        mcp24_admin.create_host(
            self.db_path, host_id="history-reader-test", bearer="R" * 40,
            profile="reader", scopes=["tool:get_prompt_timeline"],
        )
        self.full_actor = mcp24_access.authenticate(self.book, "history-full-test", "H" * 40)
        self.reader_actor = mcp24_access.authenticate(self.book, "history-reader-test", "R" * 40)
        self.direction = seed.DIRECTIONS[0][0]

    def tearDown(self):
        self.temp.cleanup()

    def call(self, action, params, actor=None):
        return self.book.dispatch(action, params, channel="mcp", actor=actor or self.full_actor)

    def save_prompt(self, request_id, *, direction=None):
        result = self.call("save_prompt", {
            "request_id": request_id,
            "text": "Synthetic prompt used to verify event history.",
            "direction": direction or self.direction,
        })
        self.assertEqual(result["status"], "ok")
        return result["data"]["id"]

    def test_timeline_review_reason_and_visibility(self):
        spec = next(item for item in TOOL_SPECS if item["name"] == "correct_prompt_status")
        self.assertIn("reason", spec["inputSchema"]["properties"])
        self.assertNotIn("reason", spec["inputSchema"].get("required", []))

        prompt_id = self.save_prompt("history-save")
        changed = self.call("set_prompt_state", {
            "request_id": "history-complete", "prompt_id": prompt_id,
            "expected_version": 1, "state": "written",
            "reason": "用户确认已经完成本题。",
        })
        self.assertEqual(changed["status"], "ok")

        full = self.call("get_prompt_timeline", {"prompt_ids": [prompt_id]})
        self.assertEqual(full["status"], "ok")
        events = full["data"]["events"]
        self.assertEqual([event["event_type"] for event in events], ["created", "state_changed"])
        self.assertEqual(events[1]["reason"], "用户确认已经完成本题。")
        self.assertEqual(events[1]["source"]["request_id"], "history-complete")
        self.assertFalse(full["data"]["coverage"][0]["older_changes_reconstructible"])

        limited = self.call("get_prompt_timeline", {"prompt_ids": [prompt_id]}, self.reader_actor)
        limited_event = limited["data"]["events"][1]
        self.assertIsNone(limited_event["reason"])
        self.assertIsNone(limited_event["source"]["request_id"])

        local_date = datetime.fromisoformat(events[1]["occurred_at"].replace("Z", "+00:00")) \
            .astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        review = self.call("review_prompt_events", {
            "periods": [{"from_date": local_date, "through_date": local_date}],
            "timezone": "Asia/Shanghai", "event_types": ["state_changed"],
        })
        period = review["data"]["periods"][0]
        self.assertEqual(period["counts"]["completed_events"], 1)
        self.assertEqual(period["counts"]["completed_prompts_currently_written"], 1)
        self.assertEqual(period["records"][0]["reason"], "用户确认已经完成本题。")

    def test_conversation_selected_correction_never_returns_internal_request_id(self):
        prompt_id = self.save_prompt("correction-history-save")
        missing_internal_id = mcp24_access.namespace_id(
            self.full_actor, "request", "unknown-private-source",
        )
        missing = self.call("correct_prompt_status", {
            "request_id": "correction-missing-source", "conversation_id": "correction-session",
            "target_request_id": "unknown-private-source", "state": "want_to_write",
        })
        self.assertEqual(missing["error"]["code"], "not_found")
        self.assertNotIn(missing_internal_id, json.dumps(missing, ensure_ascii=False))

        changed = self.call("apply_actions", {
            "request_id": "correction-source-state", "conversation_id": "source-session",
            "actions": [{"tool": "set_prompt_state", "arguments": {
                "prompt_id": prompt_id, "expected_version": 1, "state": "written",
            }}],
        })
        self.assertEqual(changed["status"], "ok")

        internal_source_id = mcp24_access.namespace_id(
            self.full_actor, "request", "correction-source-state",
        )
        db = self.book._connect()
        db.execute(
            "DELETE FROM mcp25_prompt_events WHERE request_id_internal = ?",
            (internal_source_id,),
        )
        db.close()

        correction = self.call("correct_prompt_status", {
            "request_id": "correction-from-session", "conversation_id": "correction-session",
            "source_conversation_id": "source-session", "state": "want_to_write",
            "reason": "恢复到尚未完成状态。",
        })
        self.assertEqual(correction["status"], "ok")
        serialized = json.dumps(correction["data"], ensure_ascii=False)
        self.assertNotIn(internal_source_id, serialized)
        self.assertIsNone(correction["data"]["target_request_id"])
        self.assertIsNone(correction["data"]["original_receipt"]["request_id"])

    def test_review_uncovered_interval_stays_inside_requested_period(self):
        review = self.call("review_prompt_events", {
            "periods": [{"from_date": "2000-01-01", "through_date": "2000-01-02"}],
            "timezone": "Asia/Shanghai",
        })
        period = review["data"]["periods"][0]
        self.assertFalse(period["coverage"]["covers_entire_period"])
        self.assertEqual(period["coverage"]["uncovered_interval"], {
            "from": period["from"],
            "through_exclusive": period["through_exclusive"],
        })

    def test_existing_event_table_gets_nullable_reason_column_without_backfill(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE mcp25_prompt_events(
            sequence INTEGER, prompt_id TEXT, occurred_at TEXT, direction TEXT, topic_id TEXT,
            event_type TEXT, request_id_internal TEXT)""")
        db.execute("INSERT INTO mcp25_prompt_events VALUES(1,'old-prompt','2020-01-01',NULL,NULL,'state_changed','old-request')")
        mcp25_history.schema(db)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(mcp25_prompt_events)")}
        self.assertIn("reason", columns)
        self.assertIsNone(db.execute("SELECT reason FROM mcp25_prompt_events").fetchone()[0])
        db.close()

    def test_new_prompt_relations_are_ordered_and_version_bound(self):
        inspiration = self.call("add_inspiration", {
            "request_id": "relation-inspiration", "kind": "thought",
            "text": "Synthetic first source", "direction": self.direction,
        })
        clipping = self.call("save_clipping", {
            "request_id": "relation-clipping", "title": "Synthetic second source",
            "url": "https://example.test/relation", "published_on": "2026-11-20",
            "source": "Synthetic publication", "direction": self.direction,
        })
        self.assertEqual(inspiration["status"], "ok")
        self.assertEqual(clipping["status"], "ok")
        inspiration_id, clipping_id = inspiration["data"]["id"], clipping["data"]["id"]

        saved = self.call("save_prompt_material_relations", {
            "request_id": "relation-new-prompt", "relation_type": "comparison",
            "prompt": {"text": "Compare two synthetic source choices.", "direction": self.direction},
            "materials": [
                {"kind": "inspiration", "id": inspiration_id, "expected_version": 1,
                 "selection": "full", "role": "source", "position": 0},
                {"kind": "clipping", "id": clipping_id, "expected_version": 1,
                 "selection": "full", "role": "counterexample", "position": 1},
            ],
        })
        self.assertEqual(saved["status"], "ok")
        self.assertTrue(saved["data"]["prompt_created"])
        self.assertEqual(saved["data"]["version"], 2)
        prompt_id = saved["data"]["prompt_id"]

        timeline = self.call("get_prompt_timeline", {"prompt_ids": [prompt_id]})["data"]["events"]
        self.assertEqual([(event["event_type"], event["action"]) for event in timeline],
                         [("created", "save_prompt_material_relations")])
        self.assertEqual(timeline[0]["after"]["version"], 2)

        prompt_relations = self.call("read_entity_relations", {
            "kind": "prompt", "object_id": prompt_id,
        })["data"]["items"]
        self.assertEqual([item["position"] for item in prompt_relations], [0, 1])
        self.assertEqual([item["target"]["id"] for item in prompt_relations],
                         [inspiration_id, clipping_id])
        self.assertEqual(prompt_relations[1]["source_version"], 1)
        material_relations = self.call("read_entity_relations", {
            "kind": "clipping", "object_id": clipping_id,
        })["data"]["items"]
        self.assertEqual(material_relations[0]["direction"], "incoming")
        self.assertEqual(material_relations[0]["source"]["id"], prompt_id)

        mcp24_admin.create_host(
            self.db_path, host_id="relation-reader-test", bearer="Q" * 40,
            profile="reader", scopes=["tool:read_entity_relations"],
        )
        reader = mcp24_access.authenticate(self.book, "relation-reader-test", "Q" * 40)
        denied = self.call("read_entity_relations", {"kind": "prompt", "object_id": prompt_id}, reader)
        self.assertEqual(denied["error"]["code"], "not_found")

        updated = self.call("update_clipping", {
            "request_id": "relation-clipping-update", "id": clipping_id,
            "expected_version": 1, "title": "Updated synthetic source",
        })
        self.assertEqual(updated["status"], "ok")
        stale_draft = self.call("draft_prompt_from_materials", {
            "relation_type": "comparison",
            "materials": [
                {"kind": "inspiration", "id": inspiration_id, "selection": "full", "expected_version": 1},
                {"kind": "clipping", "id": clipping_id, "selection": "full", "expected_version": 1},
            ],
        })
        self.assertEqual(stale_draft["error"]["code"], "version_conflict")

        relation_id = prompt_relations[0]["relation_id"]
        stale_remove = self.call("remove_prompt_material_relation", {
            "request_id": "relation-remove-stale", "relation_id": relation_id,
            "expected_version": 2,
        })
        self.assertEqual(stale_remove["error"]["code"], "version_conflict")
        removed = self.call("remove_prompt_material_relation", {
            "request_id": "relation-remove", "relation_id": relation_id,
            "expected_version": 1,
        })
        self.assertTrue(removed["data"]["removed"])


if __name__ == "__main__":
    unittest.main()
