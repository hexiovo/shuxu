from __future__ import annotations

import unittest

from shuxu.mcp25_host_acceptance import _verify_named_write_evidence, _verify_write_evidence


class HostAcceptanceEvidenceTests(unittest.TestCase):
    def test_real_receipt_and_increased_version_are_both_required(self):
        result = {
            "status": "ok",
            "data": {"id": "synthetic-prompt", "version": 2},
            "envelope": {"receipt": {"committed": True}},
        }
        self.assertTrue(_verify_write_evidence(result, True, "synthetic-prompt", 1, 2))
        self.assertFalse(_verify_write_evidence(result, False, "synthetic-prompt", 1, 2))
        self.assertFalse(_verify_write_evidence(result, True, "synthetic-prompt", 2, 2))

    def test_fake_success_without_receipt_or_version_growth_is_rejected(self):
        fake_success = {
            "status": "ok",
            "data": {"id": "synthetic-prompt", "version": 2},
            "envelope": {"receipt": {"committed": True}},
        }
        self.assertFalse(_verify_write_evidence(
            fake_success, False, "synthetic-prompt", 2, 2,
        ))

    def test_named_receipt_uses_public_request_id_and_matches_sqlite_version(self):
        request_id = "named-state-req"
        prompt_id = "synthetic-prompt"
        result = {
            "status": "ok",
            "data": {"id": prompt_id, "version": 3},
            "envelope": {"receipt": {
                "request_id": request_id, "action": "set_prompt_state", "state": "committed", "committed": True,
            }},
        }
        status = {"status": "ok", "data": {
            "request_id": request_id, "action": "set_prompt_state", "state": "committed", "committed": True,
        }}
        sqlite_evidence = {
            "receipt": {"action": "set_prompt_state"},
            "prompt": {"id": prompt_id, "version": 3, "state": "in_progress"},
            "public_request_key_absent": True,
        }
        self.assertTrue(_verify_named_write_evidence(
            result, status, sqlite_evidence, request_id, prompt_id, 2, 3,
        ))
        self.assertFalse(_verify_named_write_evidence(
            result, status, {**sqlite_evidence, "public_request_key_absent": False},
            request_id, prompt_id, 2, 3,
        ))
        self.assertFalse(_verify_named_write_evidence(
            result, status, sqlite_evidence, "another-request", prompt_id, 2, 3,
        ))


if __name__ == "__main__":
    unittest.main()
