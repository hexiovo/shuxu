from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from shuxu import mcp24_access, mcp24_admin, seed
from shuxu.book import BookService


class SharedPromptHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shuxu-shared-picker-")
        self.db_path = Path(self.temp.name) / "book.sqlite3"
        self.book = BookService(self.db_path)
        mcp24_admin.create_host(
            self.db_path, host_id="picker-full-test", bearer="P" * 40,
            profile="operator", scopes=["tool:*"],
        )
        mcp24_admin.create_host(
            self.db_path, host_id="picker-reader-test", bearer="R" * 40,
            profile="reader", scopes=["tool:check_prompt"],
        )
        self.full_actor = mcp24_access.authenticate(self.book, "picker-full-test", "P" * 40)
        self.reader_actor = mcp24_access.authenticate(self.book, "picker-reader-test", "R" * 40)
        self.direction = seed.DIRECTIONS[0][0]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db = self.book._connect()
        for identifier, text, visibility in (
            ("shared-near-prompt", "How do I set boundaries during a hard conversation?", "shared"),
            ("private-near-prompt", "A private phrase that only the full host can match", "private"),
        ):
            db.execute(
                """INSERT INTO prompts(id,text,direction,topic_id,depth,parent_id,state,snoozed_until,
                    visibility,origin,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, text, self.direction, None, "medium", None, None, None,
                 visibility, "web", 1, now, now),
            )
        db.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_similarity_rows_share_threshold_and_respect_actor_visibility(self):
        db = self.book._connect()
        shared = self.book._similar_prompt_rows(
            db, "How do I set boundaries during a hard conversation?", "mcp", actor=self.reader_actor,
        )
        full = self.book._similar_prompt_rows(
            db, "A private phrase that only the full host can match", "mcp", actor=self.full_actor,
        )
        hidden = self.book._similar_prompt_rows(
            db, "A private phrase that only the full host can match", "mcp", actor=self.reader_actor,
        )
        db.close()
        self.assertEqual(shared[0]["row"]["id"], "shared-near-prompt")
        self.assertEqual(shared[0]["similarity"], 1.0)
        self.assertTrue(shared[0]["duplicate"])
        self.assertEqual(full[0]["row"]["id"], "private-near-prompt")
        self.assertEqual(hidden, [])

    def test_check_prompt_uses_the_same_actor_visible_candidate_pool(self):
        params = {"text": "A private phrase that only the full host can match"}
        full = self.book.dispatch("check_prompt", params, channel="mcp", actor=self.full_actor)
        reader = self.book.dispatch("check_prompt", params, channel="mcp", actor=self.reader_actor)
        self.assertTrue(full["data"]["duplicate"])
        self.assertFalse(reader["data"]["duplicate"])
        self.assertFalse(reader["data"]["similar"])

    def test_prompt_state_events_commit_with_public_request_ids_and_versions(self):
        saved = self.book.dispatch(
            "save_prompt",
            {"request_id": "save-event-1", "text": "A synthetic event prompt", "direction": self.direction},
            channel="mcp", actor=self.full_actor,
        )
        prompt_id = saved["data"]["id"]
        changed = self.book.dispatch(
            "set_prompt_state",
            {"request_id": "state-event-1", "prompt_id": prompt_id,
             "state": "in_progress", "expected_version": 1, "reason": "用户明确要继续写这道题。"},
            channel="mcp", actor=self.full_actor,
        )
        self.assertEqual(changed["data"]["version"], 2)

        db = self.book._connect()
        events = [dict(row) for row in db.execute(
            "SELECT * FROM mcp25_prompt_events WHERE prompt_id=? ORDER BY sequence", (prompt_id,),
        )]
        db.close()
        self.assertEqual([event["event_type"] for event in events], ["created", "state_changed"])
        self.assertEqual([event["request_id_public"] for event in events], ["save-event-1", "state-event-1"])
        self.assertEqual([(event["from_version"], event["to_version"]) for event in events], [(None, 1), (1, 2)])
        self.assertIsNone(events[0]["reason"])
        self.assertEqual(events[1]["reason"], "用户明确要继续写这道题。")
        self.assertEqual(events[0]["request_id_internal"], mcp24_access.namespace_id(self.full_actor, "request", "save-event-1"))

    def test_narrow_prompt_uses_session_depth_and_explicit_depth_precedence(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db = self.book._connect()
        for identifier, text, depth, parent_id in (
            ("narrow-parent", "A synthetic parent prompt", "medium", None),
            ("narrow-light", "A synthetic light child prompt", "light", "narrow-parent"),
            ("narrow-deep", "A synthetic deep child prompt", "deep", "narrow-parent"),
        ):
            db.execute(
                """INSERT INTO prompts(id,text,direction,topic_id,depth,parent_id,state,snoozed_until,
                    visibility,origin,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, text, self.direction, None, depth, parent_id, None, None,
                 "shared", "web", 1, now, now),
            )
        db.close()
        self.book.dispatch(
            "set_session_conditions",
            {"request_id": "narrow-session-1", "conversation_id": "narrow-session",
             "expected_version": 0, "conditions": {"depth": "light"}},
            channel="mcp", actor=self.full_actor,
        )

        inherited = self.book.dispatch(
            "narrow_prompt", {"prompt_id": "narrow-parent", "conversation_id": "narrow-session"},
            channel="mcp", actor=self.full_actor,
        )
        explicit = self.book.dispatch(
            "narrow_prompt", {"prompt_id": "narrow-parent", "conversation_id": "narrow-session", "depth": "deep"},
            channel="mcp", actor=self.full_actor,
        )
        self.assertEqual(inherited["data"]["prompt"]["id"], "narrow-light")
        self.assertEqual(inherited["data"]["effective_conditions"]["condition_sources"]["depth"]["source"], "session")
        self.assertEqual(explicit["data"]["prompt"]["id"], "narrow-deep")
        self.assertEqual(explicit["data"]["effective_conditions"]["condition_sources"]["depth"]["source"], "call")
        self.assertEqual(explicit["data"]["effective_conditions"]["conflicts"], ["depth"])


if __name__ == "__main__":
    unittest.main()
