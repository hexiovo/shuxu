from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shuxu import mcp24_access, mcp24_admin, privacy_guard
from shuxu.book import BookService


class ClaimEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shuxu-claim-evidence-")
        self.db_path = Path(self.temp.name) / "book.sqlite3"
        self.book = BookService(self.db_path)
        mcp24_admin.create_host(
            self.db_path, host_id="claims-full-test", bearer="C" * 40,
            profile="operator", scopes=["tool:*"],
        )
        self.actor = mcp24_access.authenticate(self.book, "claims-full-test", "C" * 40)

    def tearDown(self):
        self.temp.cleanup()

    def call(self, action, params):
        return self.book.dispatch(action, params, channel="mcp", actor=self.actor)

    def test_evidence_versions_and_legacy_links_remain_unverified(self):
        direction = "consultation_reflection"
        prompt = self.call("save_prompt", {
            "request_id": "claim-prompt", "text": "Synthetic prompt for evidence linking.",
            "direction": direction,
        })
        self.assertEqual(prompt["status"], "ok")
        prompt_id = prompt["data"]["id"]
        clipping = self.call("save_clipping", {
            "request_id": "claim-clipping", "title": "Synthetic source title",
            "url": "https://example.test/source", "published_on": "2026-11-20",
            "source": "Synthetic publication", "note": "Synthetic source note",
            "direction": direction,
        })
        self.assertEqual(clipping["status"], "ok")
        clipping_id = clipping["data"]["id"]

        sensitive = self.call("write_claim_evidence", {
            "request_id": "claim-sensitive-evidence", "prompt_id": prompt_id,
            "expected_prompt_version": 1,
            "claims": [{"text": "Synthetic claim", "evidence": [{
                "clipping_id": clipping_id, "material_version": 1,
                "field": "title", "support_type": "supports",
                "location": "section 2", "excerpt": "来访者电话是13800138000",
            }]}],
        })
        self.assertEqual(sensitive["error"]["code"], "privacy_rejected")
        self.assertEqual(self.call("read_prompt_evidence", {"prompt_id": prompt_id})["data"]["total"], 0)

        written = self.call("write_claim_evidence", {
            "request_id": "claim-evidence", "prompt_id": prompt_id,
            "expected_prompt_version": 1,
            "claims": [{"text": "Synthetic claim", "evidence": [{
                "clipping_id": clipping_id, "material_version": 1,
                "field": "title", "support_type": "supports",
                "location": "section 2", "excerpt": "Synthetic excerpt",
                "conclusion": "Supports the stated claim",
            }]}],
        })
        self.assertEqual(written["status"], "ok")

        evidence = self.call("read_prompt_evidence", {"prompt_id": prompt_id})["data"]
        claim = next(item for item in evidence["claims"] if item["text"] == "Synthetic claim")
        self.assertEqual(claim["version"], 1)
        self.assertEqual(claim["items"][0]["support_type"], "supports")
        self.assertEqual(claim["items"][0]["material"]["version"], 1)
        self.assertEqual(claim["items"][0]["source_version"], 1)
        self.assertEqual(claim["items"][0]["field"], "title")
        self.assertEqual(claim["items"][0]["excerpt"], "Synthetic excerpt")
        self.assertIsNotNone(claim["items"][0]["checked_at"])

        db = self.book._connect()
        db.execute(
            "INSERT INTO mcp_claim_sources(prompt_id,text,clipping_id) VALUES (?,?,?)",
            (prompt_id, "Synthetic legacy claim", clipping_id),
        )
        db.close()
        evidence = self.call("read_prompt_evidence", {"prompt_id": prompt_id})["data"]
        legacy = next(item for item in evidence["claims"] if item["text"] == "Synthetic legacy claim")
        self.assertTrue(legacy["legacy"])
        self.assertEqual(legacy["items"][0]["support_type"], "unverified")
        self.assertIsNone(legacy["items"][0]["checked_at"])

        state = self.call("set_prompt_state", {
            "request_id": "claim-prompt-state", "prompt_id": prompt_id,
            "expected_version": 2, "state": "in_progress",
        })
        self.assertEqual(state["status"], "ok")
        conflict = self.call("write_claim_evidence", {
            "request_id": "claim-evidence-stale", "prompt_id": prompt_id,
            "expected_prompt_version": 2,
            "claims": [{"text": "Another synthetic claim", "evidence": []}],
        })
        self.assertEqual(conflict["error"]["code"], "version_conflict")
        evidence = self.call("read_prompt_evidence", {"prompt_id": prompt_id})["data"]
        self.assertFalse(any(item["text"] == "Another synthetic claim" for item in evidence["claims"]))

    def test_sensitive_question_preference_reason_uses_async_privacy_gate(self):
        direct = self.call("set_question_preference", {
            "request_id": "sensitive-preference",
            "expected_version": 1, "mode": "evidence", "scope": "baseline", "strength": 1,
            "direction": "consultation_reflection", "reason": "来访者电话是13800138000",
        })
        self.assertEqual(direct["error"]["code"], "privacy_rejected", direct)

        db = self.book._connect()
        try:
            with self.assertRaises(privacy_guard.PrivacyViolation) as caught:
                privacy_guard.check_action(db, "submit_async_job", {
                    "action": "set_question_preference",
                    "arguments": {
                        "direction": "consultation_reflection",
                        "reason": "来访者电话是13800138000",
                    },
                })
            self.assertEqual(caught.exception.code, "privacy_rejected")
            privacy_guard.check_action(db, "set_question_preference", {"reason": "保持提问简短"})
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
