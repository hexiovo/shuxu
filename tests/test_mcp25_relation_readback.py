from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from shuxu import mcp24_access, mcp24_admin
from shuxu.book import BookService


class RelationReadbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shuxu-relation-readback-")
        self.db_path = Path(self.temp.name) / "book.sqlite3"
        self.book = BookService(self.db_path)
        mcp24_admin.create_host(
            self.db_path, host_id="relations-full-test", bearer="P" * 40,
            profile="operator", scopes=["tool:*"],
        )
        mcp24_admin.create_host(
            self.db_path, host_id="relations-reader-test", bearer="R" * 40,
            profile="reader", scopes=["tool:read_entity_relations", "tool:search_indexed_library",
                                       "prompt:relation-prompt"],
        )
        self.full_actor = mcp24_access.authenticate(self.book, "relations-full-test", "P" * 40)
        self.reader_actor = mcp24_access.authenticate(self.book, "relations-reader-test", "R" * 40)
        self.direction = "social_topics"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db = self.book._connect()
        db.execute(
            """INSERT INTO prompts(id,text,direction,topic_id,depth,parent_id,state,snoozed_until,
                visibility,origin,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("relation-prompt", "A synthetic prompt about evidence", self.direction, None, "medium", None,
             None, None, "shared", "web", 1, now, now),
        )
        for identifier, title in (("relation-clip-a", "Alpha source"), ("relation-clip-b", "Beta source")):
            db.execute(
                """INSERT INTO materials(id,collection,direction,topic_id,prompt_id,data_json,version,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (identifier, "clipping", self.direction, None, None,
                 json.dumps({"title": title, "url": "https://example.invalid/" + identifier,
                             "published_on": "2026-01-01", "source": "synthetic", "note": ""}),
                 1, now, now),
            )
        db.close()

    def tearDown(self):
        self.temp.cleanup()

    def _save_relations(self, request_id, expected_prompt_version, descriptions, expected_relation_version=None):
        materials = []
        for index, (identifier, description) in enumerate(descriptions):
            material = {"kind": "clipping", "id": identifier, "expected_version": 1,
                        "selection": "full", "role": "source", "position": index,
                        "description": description}
            if expected_relation_version is not None and index == 0:
                material["expected_relation_version"] = expected_relation_version
            materials.append(material)
        return self.book.dispatch(
            "save_prompt_material_relations",
            {"request_id": request_id, "prompt_id": "relation-prompt",
             "expected_version": expected_prompt_version, "relation_type": "comparison",
             "materials": materials},
            channel="mcp", actor=self.full_actor,
        )

    def test_full_record_context_and_index_use_typed_relation_helpers(self):
        self._save_relations("relation-save-1", 1, [
            ("relation-clip-a", "alpha evidence phrase"),
            ("relation-clip-b", "beta evidence phrase"),
        ])
        db = self.book._connect()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute("INSERT INTO mcp_topic_projects VALUES(?,?,?,?,?)",
                   ("relation-project", "Synthetic relation project",
                    json.dumps([{"kind": "prompt", "id": "relation-prompt"}]), 1, now))
        from shuxu import mcp24_reads
        mcp24_reads.sync_changed(db)
        db.commit()
        db.close()
        relations = self.book.dispatch(
            "read_entity_relations", {"kind": "prompt", "object_id": "relation-prompt", "limit": 1},
            channel="mcp", actor=self.full_actor,
        )
        self.assertEqual(relations["data"]["total"], 2)
        self.assertIsNotNone(relations["data"]["next_offset"])

        record = self.book.dispatch(
            "get_full_record", {"kind": "prompt", "id": "relation-prompt"},
            channel="mcp", actor=self.full_actor,
        )
        self.assertEqual(record["data"]["entity_relations"]["total"], 2)
        self.assertEqual(record["data"]["entity_relations"]["items"][0]["relation_type"], "comparison")
        self.assertEqual(record["data"]["topic_project_progress"][0]["project_id"], "relation-project")

        context = self.book.dispatch(
            "get_thought_context", {"prompt_id": "relation-prompt"},
            channel="mcp", actor=self.full_actor,
        )
        self.assertEqual(context["data"]["detail"], "full")
        self.assertEqual(context["data"]["entity_relations"]["total"], 2)
        self.assertEqual(context["data"]["topic_project_progress"][0]["project_id"], "relation-project")

        search = self.book.dispatch(
            "search_indexed_library", {"query": "comparison", "kinds": ["relation"], "page_size": 1},
            channel="mcp", actor=self.full_actor,
        )
        self.assertTrue(search["data"]["has_more"])
        stale_cursor = search["data"]["cursor"]
        db = self.book._connect()
        state_before = db.execute(
            "SELECT revision,unsafe_revision FROM mcp_read_state WHERE singleton=1",
        ).fetchone()
        db.close()
        self._save_relations("relation-save-2", 2, [
            ("relation-clip-a", "alpha evidence revised phrase"),
        ], expected_relation_version=1)
        db = self.book._connect()
        state_after = db.execute("SELECT revision,unsafe_revision FROM mcp_read_state WHERE singleton=1").fetchone()
        db.close()
        self.assertGreater(state_after["revision"], state_before["revision"])
        self.assertGreater(state_after["unsafe_revision"], state_before["unsafe_revision"])
        stale = self.book.dispatch(
            "search_indexed_library", {"cursor": stale_cursor}, channel="mcp", actor=self.full_actor,
        )
        self.assertEqual(stale["error"]["code"], "cursor_stale")

    def test_limited_reader_gets_only_existing_scoped_relation_read(self):
        self._save_relations("relation-save-1", 1, [
            ("relation-clip-a", "alpha confidential phrase"),
        ])
        relation_read = self.book.dispatch(
            "read_entity_relations", {"kind": "prompt", "object_id": "relation-prompt"},
            channel="mcp", actor=self.reader_actor,
        )
        self.assertEqual(relation_read["data"]["items"][0]["description"], "alpha confidential phrase")

        search = self.book.dispatch(
            "search_indexed_library", {"query": "confidential", "kinds": ["relation"]},
            channel="mcp", actor=self.reader_actor,
        )
        self.assertTrue(search["data"]["items"])
        self.assertNotIn("documents", search["data"]["items"][0])
        self.assertTrue(search["data"]["items"][0]["content_requires_authorization"])

        forbidden = self.book.dispatch(
            "get_full_record", {"kind": "prompt", "id": "relation-prompt"},
            channel="mcp", actor=self.reader_actor,
        )
        self.assertEqual(forbidden["error"]["code"], "forbidden")

    def test_legacy_claim_source_changes_refresh_relation_search_docs(self):
        from shuxu import mcp24_reads

        db = self.book._connect()
        db.execute("INSERT INTO mcp_claim_sources VALUES(?,?,?)",
                   ("relation-prompt", "legacy source phrase", "relation-clip-a"))
        mcp24_reads.sync_changed(db)
        db.commit()
        db.close()
        search_before = self.book.dispatch(
            "search_indexed_library", {"query": "legacy source phrase", "kinds": ["relation"]},
            channel="mcp", actor=self.full_actor,
        )
        self.assertTrue(search_before["data"]["items"])
        db = self.book._connect()
        db.execute("DELETE FROM mcp_claim_sources WHERE prompt_id=? AND text=? AND clipping_id=?",
                   ("relation-prompt", "legacy source phrase", "relation-clip-a"))
        mcp24_reads.sync_changed(db)
        db.commit()
        db.close()
        search = self.book.dispatch(
            "search_indexed_library", {"query": "legacy source phrase", "kinds": ["relation"]},
            channel="mcp", actor=self.full_actor,
        )
        self.assertEqual(search["data"]["items"], [])


if __name__ == "__main__":
    unittest.main()
