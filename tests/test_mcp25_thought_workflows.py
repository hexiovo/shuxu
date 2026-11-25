from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shuxu import mcp25_thought, mcp_workflows, privacy_guard
from shuxu.book import BookService, TOOL_SPECS


class ThoughtWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shuxu-thought-workflows-")
        self.book = BookService(Path(self.temp.name) / "book.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def insert_prompt(self, identifier, text, *, direction="self_exploration", depth="light",
                      state=None, snoozed_until=None, parent_id=None, updated_at=None):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db = self.book._connect()
        db.execute(
            """INSERT INTO prompts(id,text,direction,topic_id,depth,parent_id,state,snoozed_until,
                visibility,origin,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (identifier, text, direction, None, depth, parent_id, state, snoozed_until,
             "shared", "web", 1, now, updated_at or now),
        )
        db.close()

    def dispatch_data(self, action, params):
        result = self.book.dispatch(action, params, channel="local")
        self.assertEqual(result["status"], "ok", result)
        return result["data"]

    def insert_checkpoint(self, identifier, prompt_id, *, source="prior-session", cue="A synthetic cue"):
        db = self.book._connect()
        prompt = db.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        created_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
        db.execute(
            """INSERT INTO mcp_thought_checkpoints
                (id,source_conversation_id,source_conversation_id_public,prompt_id,prompt_version,
                 prompt_text,direction,topic_id,cue,unanswered_question,progress,guide_step,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (identifier, source, source, prompt_id, prompt["version"], prompt["text"],
             prompt["direction"], prompt["topic_id"], cue, "A synthetic open question",
             "A synthetic progress note", 1, created_at),
        )
        db.close()

    def test_session_conditions_return_source_and_check_versions(self):
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
        saved = self.dispatch_data("set_session_conditions", {
            "request_id": "session-set-1", "conversation_id": "session-a", "expected_version": 0,
            "conditions": {"depth": "light", "avoid_days": 4}, "expires_at": expires_at,
        })
        current = self.dispatch_data("get_session_conditions", {"conversation_id": "session-a"})
        self.assertEqual(saved["version"], 1)
        self.assertEqual(current["conditions"], {"depth": "light", "avoid_days": 4})
        self.assertEqual(current["sources"]["depth"]["request_id"], "session-set-1")
        self.assertEqual(current["sources"]["depth"]["source"], "session")
        self.assertEqual(current["precedence"], ["call", "session", "preference"])

        conflict = self.book.dispatch("end_session_conditions", {
            "request_id": "session-end-conflict", "conversation_id": "session-a", "expected_version": 0,
        }, channel="local")
        self.assertEqual(conflict["error"]["code"], "version_conflict")
        absent_conflict = self.book.dispatch("end_session_conditions", {
            "request_id": "session-end-absent", "conversation_id": "missing-session", "expected_version": 1,
        }, channel="local")
        self.assertEqual(absent_conflict["error"]["code"], "version_conflict")

        ended = self.dispatch_data("end_session_conditions", {
            "request_id": "session-end-1", "conversation_id": "session-a", "expected_version": 1,
        })
        self.assertEqual(ended["version"], 2)
        self.assertEqual(ended["ended_fields"], ["depth", "avoid_days"])

        db = self.book._connect()
        db.execute(
            "INSERT INTO mcp25_session_conditions VALUES(?,?,?,?)",
            ("expired-session", 1, json.dumps({"depth": {
                "value": "deep", "set_at": expires_at,
                "expires_at": "2000-01-01T00:00:00+00:00", "source_request_id": "old-turn",
            }}), expires_at),
        )
        db.close()
        expired = self.dispatch_data("get_session_conditions", {"conversation_id": "expired-session"})
        self.assertEqual(expired["conditions"], {})
        self.assertEqual(expired["expired_fields"], ["depth"])

    def test_family_confirmation_and_separation_use_shared_similarity(self):
        self.insert_prompt("family-a", "How can I set a boundary in a difficult conversation?")
        self.insert_prompt("family-b", "How can I set a boundary in a difficult conversation?")
        similar = self.dispatch_data("get_prompt_family", {"prompt_id": "family-a"})
        self.assertEqual(similar["similar_candidate_count"], 1)
        self.assertTrue(similar["similar_candidates"][0]["duplicate"])

        confirmed = self.dispatch_data("set_prompt_family_relation", {
            "request_id": "family-confirm-1", "prompt_id": "family-a", "related_prompt_id": "family-b",
            "relation": "same_family", "expected_version": 0, "reason": "The prompts are the same task.",
        })
        self.assertEqual(confirmed["version"], 1)
        family = self.dispatch_data("get_prompt_family", {"prompt_id": "family-a"})
        self.assertEqual(set(family["family_ids"]), {"family-a", "family-b"})
        self.assertEqual(family["confirmed_relations"][0]["source"], "user_confirmed")

        separated = self.dispatch_data("set_prompt_family_relation", {
            "request_id": "family-separate-1", "prompt_id": "family-a", "related_prompt_id": "family-b",
            "relation": "separate", "expected_version": 1, "reason": "Keep these separate after review.",
        })
        self.assertEqual(separated["version"], 2)
        family = self.dispatch_data("get_prompt_family", {"prompt_id": "family-a"})
        self.assertEqual(family["family_ids"], ["family-a"])
        self.assertEqual(family["separated_relations"][0]["relation"], "separate")

    def test_unfinished_pick_filters_written_recent_and_future_work_and_lists_without_writing(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=2)).isoformat(timespec="seconds")
        future = (now + timedelta(days=1)).isoformat(timespec="seconds")
        past = (now - timedelta(days=1)).isoformat(timespec="seconds")
        self.insert_prompt("unfinished-checkpoint", "A saved thought to continue", state="in_progress", updated_at=old)
        self.insert_prompt("unfinished-recent", "A recently selected thought", state="in_progress", updated_at=old)
        self.insert_prompt("unfinished-want", "A thought marked to write", state="want_to_write", updated_at=old)
        self.insert_prompt("unfinished-due", "A due thought", snoozed_until=past, updated_at=old)
        self.insert_prompt("unfinished-new", "A new task", updated_at=old)
        self.insert_prompt("unfinished-future", "A future snoozed task", snoozed_until=future, updated_at=old)
        self.insert_prompt("unfinished-written", "A written task", state="written", updated_at=old)
        self.insert_prompt("unfinished-deep", "A deep task", state="in_progress", depth="deep", updated_at=old)
        self.insert_checkpoint("checkpoint-unfinished", "unfinished-checkpoint")
        self.insert_checkpoint("checkpoint-written", "unfinished-written")

        db = self.book._connect()
        mcp_workflows._decision(db, {"conversation_id": "prior-session"}, "unfinished-recent", "pick", {}, now)
        before_events = db.execute("SELECT count(*) FROM mcp_conversation_entries WHERE action='unfinished_pick'").fetchone()[0]
        db.close()
        picked = self.dispatch_data("pick_unfinished_prompt", {
            "request_id": "unfinished-pick-1", "conversation_id": "current-session",
            "minutes": 5, "avoid_days": 7, "limit": 1,
        })
        self.assertEqual(picked["prompt"]["id"], "unfinished-checkpoint")
        self.assertEqual(picked["decision"]["candidate_count"], 3)
        self.assertEqual(picked["decision"]["eligible_before_recent_count"], 4)
        self.assertEqual(picked["decision"]["recently_excluded_count"], 1)
        self.assertEqual(picked["decision"]["effective_conditions"]["sources"]["avoid_days"]["source"], "call")
        self.assertEqual(picked["decision"]["continuation_point"]["cue"], "A synthetic cue")
        self.assertEqual(picked["alternative_count"], 2)
        self.assertEqual(picked["alternatives"][0]["prompt"]["id"], "unfinished-want")
        self.assertTrue(picked["has_more"])

        first_page = self.dispatch_data("list_unfinished_prompts", {
            "conversation_id": "current-session", "minutes": 5, "avoid_days": 0, "limit": 1,
        })
        self.assertEqual(first_page["items"][0]["prompt"]["id"], "unfinished-checkpoint")
        self.assertFalse(first_page["complete"])
        self.assertEqual(first_page["total"], 4)
        self.assertEqual(first_page["items"][0]["continuation_point"]["cue"], "A synthetic cue")
        second_page = self.dispatch_data("list_unfinished_prompts", {
            "conversation_id": "current-session", "minutes": 5, "avoid_days": 0,
            "limit": 1, "offset": first_page["next_offset"],
        })
        self.assertEqual(second_page["offset"], 1)
        self.assertEqual(second_page["total"], first_page["total"])
        self.assertEqual(second_page["items"][0]["position"], 2)
        db = self.book._connect()
        after_events = db.execute("SELECT count(*) FROM mcp_conversation_entries WHERE action='unfinished_pick'").fetchone()[0]
        db.close()
        self.assertEqual(before_events + 1, after_events)

    def test_unfinished_empty_result_reports_only_in_scope_nonwritten_counts(self):
        now = datetime.now(timezone.utc)
        future = (now + timedelta(days=2)).isoformat(timespec="seconds")
        self.insert_prompt("empty-new", "A new synthetic prompt", direction="social_topics")
        self.insert_prompt("empty-future", "A future synthetic prompt", direction="social_topics", snoozed_until=future)
        self.insert_prompt("empty-written", "A written synthetic prompt", direction="social_topics", state="written")
        self.insert_checkpoint("empty-written-checkpoint", "empty-written")
        result = self.book.dispatch("pick_unfinished_prompt", {
            "request_id": "unfinished-empty-1", "conversation_id": "empty-session",
            "direction": "social_topics", "depth": "light", "avoid_days": 0,
        }, channel="local")
        self.assertEqual(result["error"]["code"], "no_candidate")
        details = result["error"]["details"]
        self.assertEqual(details["candidate_count"], 0)
        self.assertEqual(details["eligible_before_recent_count"], 0)
        self.assertEqual(details["new_prompt_count"], 4)
        self.assertEqual(details["future_snoozed_count"], 1)

    def test_question_modes_history_and_preference_expiry(self):
        self.insert_prompt("guide-prompt", "A synthetic reflection prompt")
        prefs = self.dispatch_data("get_question_preferences", {"direction": "self_exploration"})
        deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
        baseline = self.dispatch_data("set_question_preference", {
            "request_id": "pref-baseline-1", "expected_version": prefs["version"],
            "mode": "evidence", "scope": "baseline", "strength": 1.5,
            "reason": "Prefer concrete examples.",
        })
        recent = self.dispatch_data("set_question_preference", {
            "request_id": "pref-recent-1", "expected_version": baseline["version"],
            "mode": "evidence", "scope": "recent", "strength": 0.5,
            "expires_at": deadline, "reason": "Try evidence briefly.",
        })
        missing_expiry = self.book.dispatch("set_question_preference", {
            "request_id": "pref-no-expiry", "expected_version": recent["version"],
            "mode": "contrast", "scope": "recent", "strength": 1,
        }, channel="local")
        self.assertEqual(missing_expiry["error"]["code"], "invalid_params")

        db = self.book._connect()
        old_expiry = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "INSERT INTO mcp25_question_preferences VALUES(?,?,?,?,?,?,?)",
            ("self_exploration", "evidence", "recent", 0, old_expiry, "expired direction value", updated_at),
        )
        effective = mcp25_thought._question_preference_values(
            self.book, db, "self_exploration", datetime.now(timezone.utc),
        )[0]
        db.close()
        evidence = next(item for item in effective if item["mode"] == "evidence")
        self.assertEqual(evidence["strength"], 0.5)
        self.assertEqual(evidence["scope"], "recent")
        self.assertEqual(evidence["source"], "default")

        first = self.dispatch_data("guide_thought", {
            "request_id": "guide-1", "conversation_id": "guide-session", "prompt_id": "guide-prompt",
            "mode": "start", "question_mode": "counterexample", "focus": "A synthetic detail",
        })
        second = self.dispatch_data("guide_thought", {
            "request_id": "guide-2", "conversation_id": "guide-session", "prompt_id": "guide-prompt",
            "mode": "continue", "question_mode": "experience", "focus": "A second synthetic detail",
        })
        self.assertEqual(first["question_mode"], "counterexample")
        self.assertIn("改变", first["question"])
        self.assertEqual(second["question_mode"], "experience")
        self.assertNotEqual(first["question"], second["question"])
        history = self.dispatch_data("get_guide_history", {"prompt_id": "guide-prompt"})
        self.assertEqual([item["question_mode"] for item in reversed(history["items"])], ["counterexample", "experience"])
        self.assertTrue(all("answer" not in item for item in history["items"]))

    def test_preference_reason_and_staged_guide_focus_use_privacy_guard(self):
        prefs = self.dispatch_data("get_question_preferences", {})
        rejected = self.book.dispatch("set_question_preference", {
            "request_id": "pref-private-1", "expected_version": prefs["version"],
            "mode": "experience", "scope": "baseline", "strength": 1,
            "direction": "consultation_reflection", "reason": "case contact email: person@example.com",
        }, channel="local")
        self.assertEqual(rejected["error"]["code"], "privacy_rejected")
        after = self.dispatch_data("get_question_preferences", {})
        self.assertEqual(after["version"], prefs["version"])

        self.insert_prompt("consult-guide", "A synthetic consultation reflection prompt",
                           direction="consultation_reflection")
        db = self.book._connect()
        entries_before = db.execute("SELECT count(*) FROM mcp_thought_progress").fetchone()[0]
        with self.assertRaises(privacy_guard.PrivacyViolation):
            mcp_workflows.check_staged_action_privacy(
                self.book, db, "guide_thought",
                {"prompt_id": "consult-guide", "conversation_id": "pending-guide",
                 "focus": "Contact email person@example.com"},
                "local", None,
            )
        mcp_workflows.check_staged_action_privacy(
            self.book, db, "guide_thought", {"conversation_id": "pending-guide", "focus": "A general synthetic focus"},
            "local", None,
        )
        entries_after = db.execute("SELECT count(*) FROM mcp_thought_progress").fetchone()[0]
        db.close()
        self.assertEqual(entries_before, entries_after)

    def test_legacy_routes_remain_unique_and_new_tools_follow_old_intents(self):
        ids = [route[0] for route in mcp_workflows.ROUTES]
        self.assertEqual(len(ids), 20)
        self.assertEqual(len(set(ids)), len(ids))
        routes = {route[0]: route for route in mcp_workflows.ROUTES}
        self.assertEqual(routes["05"][2], ["pick_due_prompt"])
        self.assertEqual(routes["06"][2], ["guide_thought"])
        self.assertEqual(routes["07"][2], ["next_prompt"])
        self.assertEqual(routes["08"][2], ["next_prompt"])
        self.assertEqual(routes["09"][2], ["search_library", "draft_from_material"])
        self.assertIn("list_unfinished_prompts", {spec["name"] for spec in TOOL_SPECS})

        plan_05 = self.dispatch_data("plan_mcp_calls", {"scenario": "05"})
        plan_09 = self.dispatch_data("plan_mcp_calls", {"scenario": "09"})
        self.assertEqual([step["tool"] for step in plan_05["steps"]], ["pick_due_prompt"])
        self.assertEqual([step["tool"] for step in plan_09["steps"]], ["search_library", "draft_from_material"])


if __name__ == "__main__":
    unittest.main()
