from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from shuxu import mcp24_access, mcp24_admin, mcp24_reads, seed
from shuxu.book import BookService


class FullReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shuxu-full-read-")
        self.db_path = Path(self.temp.name) / "book.sqlite3"
        self.book = BookService(self.db_path)
        self.direction = seed.DIRECTIONS[0][0]
        mcp24_admin.create_host(self.db_path, host_id="full-read-test", bearer="F" * 40,
                                profile="operator", scopes=["tool:*"])
        mcp24_admin.create_host(self.db_path, host_id="other-full-test", bearer="O" * 40,
                                profile="other", scopes=["tool:*"])
        mcp24_admin.create_host(self.db_path, host_id="limited-read-test", bearer="L" * 40,
                                profile="reader", scopes=["tool:get_full_record"])
        mcp24_admin.create_host(self.db_path, host_id="search-read-test", bearer="S" * 40,
                                profile="searcher", scopes=["tool:search_indexed_library"])
        self.actor = mcp24_access.authenticate(self.book, "full-read-test", "F" * 40)
        self.other_actor = mcp24_access.authenticate(self.book, "other-full-test", "O" * 40)
        self.limited_actor = mcp24_access.authenticate(self.book, "limited-read-test", "L" * 40)
        self.search_actor = mcp24_access.authenticate(self.book, "search-read-test", "S" * 40)
        self.image = b"\x89PNG\r\n\x1a\n" + b"synthetic-image-bytes" * 9000
        db = self.book._connect()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute("""INSERT INTO prompts(id,text,direction,topic_id,depth,parent_id,state,snoozed_until,
            visibility,origin,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("private-read-prompt", "private complete prompt text", self.direction, None, "deep", None,
             "in_progress", None, "private", "web", 4, now, now))
        db.execute("""INSERT INTO prompts(id,text,direction,topic_id,depth,parent_id,state,snoozed_until,
            visibility,origin,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("full-read-prompt-a", "complete prompt A", self.direction, None, "light", None,
             "in_progress", None, "shared", "mcp", 1, now, now))
        db.execute("""INSERT INTO prompts(id,text,direction,topic_id,depth,parent_id,state,snoozed_until,
            visibility,origin,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("full-read-prompt-b", "complete prompt B", self.direction, None, "medium", None,
             "in_progress", None, "shared", "mcp", 1, now, now))
        db.execute("""INSERT INTO materials(id,collection,direction,topic_id,prompt_id,data_json,version,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            ("full-read-image", "inspiration", self.direction, None, "private-read-prompt",
             json.dumps({"kind": "screenshot", "text": "full material text", "source": "synthetic", "url": ""}), 2, now, now))
        db.execute("INSERT INTO inspiration_images(material_id,content_type,content) VALUES(?,?,?)",
                   ("full-read-image", "image/png", self.image))
        db.execute("""INSERT INTO mcp_thought_checkpoints(id,source_conversation_id,source_conversation_id_public,
            prompt_id,prompt_version,prompt_text,direction,topic_id,cue,unanswered_question,progress,guide_step,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("checkpoint-read-1", "internal-conversation-key", "session-public-1", "private-read-prompt", 4,
             "private complete prompt text", self.direction, None, "saved cue", "unanswered question", "saved progress", 2, now))
        mcp24_reads.sync_changed(db)
        db.close()

    def tearDown(self):
        self.temp.cleanup()

    def call(self, action, params, actor=None):
        return self.book.dispatch(action, params, channel="mcp", actor=actor or self.actor)

    def test_full_actor_gets_private_full_record_while_scoped_actor_stays_limited(self):
        full = self.call("get_full_record", {"kind": "prompt", "id": "private-read-prompt"})
        self.assertEqual(full["status"], "ok")
        self.assertEqual(full["data"]["record"]["text"], "private complete prompt text")
        self.assertEqual(full["data"]["record"]["visibility"], "private")
        self.assertEqual(full["data"]["related_materials"][0]["id"], "full-read-image")

        limited = self.call("get_full_record", {"kind": "prompt", "id": "private-read-prompt"}, self.limited_actor)
        self.assertEqual(limited["error"]["code"], "forbidden")

        context = self.call("get_thought_context", {"prompt_id": "private-read-prompt", "detail": "full"})
        self.assertEqual(context["status"], "ok")
        self.assertEqual(context["data"]["prompt"]["text"], "private complete prompt text")
        self.assertEqual(context["data"]["materials"][0]["text"], "full material text")
        checkpoint = context["data"]["checkpoints"][0]
        self.assertEqual(checkpoint["source_conversation_id"], "session-public-1")
        self.assertNotIn("internal-conversation-key", json.dumps(context["data"], ensure_ascii=False))

    def test_full_pages_keep_stable_order_snapshot_and_actor_binding(self):
        first = self.call("list_full_records", {
            "kind": "prompt", "direction": self.direction, "state": "in_progress", "page_size": 1,
        })
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["data"]["total"], 3)
        self.assertEqual(first["data"]["current_range"], {"start": 1, "end": 1})
        self.assertFalse(first["data"]["complete"])
        cursor = first["data"]["next_cursor"]
        wrong_actor = self.call("list_full_records", {"cursor": cursor}, self.other_actor)
        self.assertEqual(wrong_actor["error"]["code"], "cursor_forbidden")

        next_page = self.call("list_full_records", {"cursor": cursor})
        self.assertEqual(next_page["data"]["current_range"], {"start": 2, "end": 2})
        self.assertEqual(next_page["data"]["items"][0]["id"], "full-read-prompt-b")

        db = self.book._connect()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute("""INSERT INTO prompts(id,text,direction,state,visibility,origin,version,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", ("late-prompt", "inserted after snapshot", self.direction,
            "in_progress", "shared", "mcp", 1, now, now))
        db.close()
        stale = self.call("list_full_records", {"cursor": next_page["data"]["next_cursor"]})
        self.assertEqual(stale["error"]["code"], "cursor_stale")

    def test_original_image_chunks_reassemble_and_full_export_hashes(self):
        chunks = []
        offset = 0
        while True:
            result = self.call("get_attachment_chunk", {"id": "full-read-image", "offset": offset, "max_bytes": 65536})
            self.assertEqual(result["status"], "ok")
            data = result["data"]
            self.assertEqual(data["sha256"], hashlib.sha256(self.image).hexdigest())
            chunk = base64.b64decode(data["chunk_base64"])
            self.assertEqual(data["chunk_sha256"], hashlib.sha256(chunk).hexdigest())
            chunks.append(chunk)
            offset = data["next_offset"]
            if data["eof"]:
                break
        self.assertEqual(b"".join(chunks), self.image)

        exported = self.call("export_full_library", {"request_id": "full-export-1", "kind": "all"})
        self.assertEqual(exported["status"], "ok")
        data = exported["data"]
        self.assertTrue(data["committed"])
        self.assertGreater(data["attachment_count"], 0)
        info = self.call("get_full_export_info", {"export_id": data["export_id"]})["data"]
        self.assertEqual(info["sha256"], data["sha256"])
        payload = bytearray()
        offset = 0
        while True:
            chunk_result = self.call("read_full_export_chunk", {
                "export_id": data["export_id"], "offset": offset, "max_bytes": 65536,
            })["data"]
            decoded = base64.b64decode(chunk_result["chunk_base64"])
            self.assertEqual(chunk_result["chunk_sha256"], hashlib.sha256(decoded).hexdigest())
            payload.extend(decoded)
            offset = chunk_result["next_offset"]
            if chunk_result["eof"]:
                break
        self.assertEqual(hashlib.sha256(payload).hexdigest(), data["sha256"])
        lines = [json.loads(line) for line in payload.decode("utf-8").splitlines()]
        checkpoint = next(item for item in lines if item.get("kind") == "checkpoint")
        self.assertNotIn("internal-conversation-key", json.dumps(checkpoint, ensure_ascii=False))
        self.assertEqual(checkpoint["data"]["source_conversation_id_public"], "session-public-1")
        image_record = next(item for item in lines if item.get("id") == "full-read-image")
        image_data = base64.b64decode(image_record["data"]["attachment"]["data_base64"])
        self.assertEqual(hashlib.sha256(image_data).hexdigest(), hashlib.sha256(self.image).hexdigest())

    def test_full_text_search_indexes_private_checkpoints_and_paginates_completely(self):
        params = {"query": "unanswered", "kinds": ["checkpoint"], "page_size": 1}
        full = self.call("search_indexed_library", params)
        self.assertEqual(full["status"], "ok")
        self.assertEqual(full["data"]["total_count"], 1)
        self.assertTrue(full["data"]["complete"])
        self.assertEqual(full["data"]["items"][0]["unanswered_question"], "unanswered question")
        self.assertEqual(full["data"]["items"][0]["source_conversation_id"], "session-public-1")
        self.assertNotIn("internal-conversation-key", json.dumps(full["data"], ensure_ascii=False))

        limited = self.call("search_indexed_library", params, self.search_actor)
        self.assertTrue(limited["data"]["items"][0]["content_requires_authorization"])
        self.assertNotIn("unanswered question", json.dumps(limited["data"], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
