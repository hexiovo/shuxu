from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from shuxu import mcp24_access, mcp25_history, seed
from shuxu.book import BookService, TOOL_SPECS


class PromptCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.book = BookService(":memory:")
        self.actor = mcp24_access.legacy_actor()
        self.direction = seed.DIRECTIONS[0][0]

    def tearDown(self):
        self.book._memory_anchor.close()

    def call(self, action, params, *, channel="mcp", actor=None):
        return self.book.dispatch(action, params, channel=channel, actor=actor or self.actor)

    def save_prompt(self, request_id, text, *, state=None, channel="mcp", actor=None):
        params = {"request_id": request_id, "text": text, "direction": self.direction}
        if state is not None:
            params["state"] = state
        result = self.call("save_prompt", params, channel=channel, actor=actor)
        self.assertEqual(result["status"], "ok")
        return result["data"]["id"]

    def prompt_row(self, prompt_id):
        db = self.book._connect()
        try:
            return dict(db.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone())
        finally:
            db.close()

    def undo_count(self, request_id):
        db = self.book._connect()
        try:
            return db.execute("SELECT count(*) FROM mcp_undo_log WHERE request_id = ?", (request_id,)).fetchone()[0]
        finally:
            db.close()

    def test_schema_and_combined_correction_are_idempotent_and_undoable(self):
        spec = next(item for item in TOOL_SPECS if item["name"] == "correct_prompt_status")
        self.assertEqual(set(spec["inputSchema"]["properties"]), {
            "request_id", "conversation_id", "source_conversation_id", "target_request_id",
            "prompt_id", "state", "snoozed_until", "reason",
        })

        prompt_id = self.save_prompt("original-save", "synthetic status correction", state="written")
        snoozed_until = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(timespec="seconds")
        params = {
            "request_id": "correction-1", "conversation_id": "correction-session",
            "target_request_id": "original-save", "state": "want_to_write",
            "snoozed_until": snoozed_until,
        }
        first = self.call("correct_prompt_status", params)

        self.assertEqual(first["status"], "ok")
        self.assertTrue(first["envelope"]["receipt"]["committed"])
        data = first["data"]
        self.assertEqual(data["before"], {"version": 1, "state": "written", "snoozed_until": None})
        self.assertEqual(data["after"]["version"], 3)
        self.assertEqual(data["after"]["state"], "want_to_write")
        self.assertEqual(data["after"]["snoozed_until"], snoozed_until)
        self.assertEqual(data["original_receipt"]["request_id"], "original-save")
        self.assertEqual(data["original_receipt"]["action"], "save_prompt")
        self.assertEqual(data["original_receipt"]["data"]["state"], "written")
        self.assertEqual(data["correction_receipt"]["request_id"], "correction-1")
        self.assertTrue(data["undo_available"])
        self.assertEqual(self.prompt_row(prompt_id)["version"], 3)
        self.assertEqual(self.undo_count("correction-1"), 1)

        replay = self.call("correct_prompt_status", params)
        self.assertEqual(replay["data"], data)
        self.assertEqual(self.prompt_row(prompt_id)["version"], 3)
        self.assertEqual(self.undo_count("correction-1"), 1)

        undone = self.call("undo_action", {
            "request_id": "undo-correction", "conversation_id": "correction-session",
            "target_request_id": "correction-1",
        })
        self.assertEqual(undone["status"], "ok")
        row = self.prompt_row(prompt_id)
        self.assertEqual(row["state"], "written")
        self.assertIsNone(row["snoozed_until"])
        self.assertEqual(row["version"], 4)

    def test_latest_batch_with_multiple_prompts_asks_then_corrects_selected_prompt(self):
        batch = self.call("apply_actions", {
            "request_id": "source-batch", "conversation_id": "source-session",
            "actions": [
                {"tool": "save_prompt", "arguments": {
                    "text": "synthetic first prompt", "direction": self.direction, "state": "written",
                }},
                {"tool": "save_prompt", "arguments": {
                    "text": "synthetic second prompt", "direction": self.direction, "state": "written",
                }},
            ],
        })
        self.assertEqual(batch["status"], "ok")
        first_id = batch["data"]["steps"][0]["data"]["id"]
        second_id = batch["data"]["steps"][1]["data"]["id"]

        ambiguous = self.call("correct_prompt_status", {
            "request_id": "correction-ambiguous", "conversation_id": "correction-session",
            "source_conversation_id": "source-session", "state": "want_to_write",
        })
        self.assertEqual(ambiguous["error"]["code"], "needs_clarification")
        self.assertEqual({item["prompt_id"] for item in ambiguous["error"]["details"]["candidates"]},
                         {first_id, second_id})
        self.assertEqual(self.prompt_row(first_id)["state"], "written")
        self.assertEqual(self.prompt_row(second_id)["state"], "written")

        selected = self.call("correct_prompt_status", {
            "request_id": "correction-selected", "conversation_id": "correction-session",
            "source_conversation_id": "source-session", "prompt_id": first_id,
            "state": "want_to_write",
        })
        self.assertEqual(selected["status"], "ok")
        self.assertEqual(selected["data"]["prompt_id"], first_id)
        self.assertEqual(self.prompt_row(first_id)["state"], "want_to_write")
        self.assertEqual(self.prompt_row(second_id)["state"], "written")

    def test_stale_source_version_conflicts_without_overwrite(self):
        prompt_id = self.save_prompt("conflict-save", "synthetic conflict prompt", state="want_to_write")
        source = self.call("set_prompt_state", {
            "request_id": "source-state", "prompt_id": prompt_id,
            "expected_version": 1, "state": "written",
        })
        self.assertEqual(source["status"], "ok")
        changed_elsewhere = self.call("set_prompt_state", {
            "request_id": "later-state", "prompt_id": prompt_id,
            "expected_version": 2, "state": "in_progress",
        })
        self.assertEqual(changed_elsewhere["status"], "ok")

        correction = self.call("correct_prompt_status", {
            "request_id": "stale-correction", "conversation_id": "correction-session",
            "target_request_id": "source-state", "state": "want_to_write",
        })
        self.assertEqual(correction["error"]["code"], "version_conflict")
        details = correction["error"]["details"]
        self.assertEqual(details["expected_version"], 2)
        self.assertEqual(details["actual_version"], 3)
        self.assertEqual(details["current"]["state"], "in_progress")
        self.assertEqual(self.prompt_row(prompt_id)["state"], "in_progress")
        self.assertEqual(self.undo_count("stale-correction"), 0)

    def test_invalid_snooze_rolls_back_prior_state_step(self):
        prompt_id = self.save_prompt("rollback-save", "synthetic rollback prompt", state="written")
        expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        result = self.call("correct_prompt_status", {
            "request_id": "rollback-correction", "conversation_id": "correction-session",
            "target_request_id": "rollback-save", "state": "want_to_write",
            "snoozed_until": expired,
        })
        self.assertEqual(result["error"]["code"], "invalid_params")
        row = self.prompt_row(prompt_id)
        self.assertEqual(row["state"], "written")
        self.assertEqual(row["version"], 1)
        self.assertIsNone(row["snoozed_until"])
        self.assertEqual(self.undo_count("rollback-correction"), 0)

    def test_full_actor_can_correct_private_prompt(self):
        private_id = self.save_prompt(
            "private-save", "synthetic private prompt", state="want_to_write", channel="local",
            actor=mcp24_access.local_actor(),
        )
        state = self.call("set_prompt_state", {
            "request_id": "private-source", "prompt_id": private_id,
            "expected_version": 1, "state": "written",
        })
        self.assertEqual(state["status"], "ok")
        correction = self.call("correct_prompt_status", {
            "request_id": "private-correction", "conversation_id": "private-session",
            "target_request_id": "private-source", "state": "want_to_write",
        })
        self.assertEqual(correction["status"], "ok")
        self.assertEqual(self.prompt_row(private_id)["state"], "want_to_write")


if __name__ == "__main__":
    unittest.main()
