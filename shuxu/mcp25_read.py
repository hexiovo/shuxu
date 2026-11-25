"""Full, paged MCP reads for a specifically authorized Book host."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from . import mcp24_access, mcp24_reads, mcp_files, mcp_workflows


_KINDS = ("all", "prompt", "inspiration", "clipping", "resume", "topic")
_PAGE_MAX = 100
_CHUNK_MAX = 65_536
_EXPORT_TTL = timedelta(hours=1)


def specs(tool, field, ident, direction, version, include_private):
    text = lambda size: field("string", minLength=1, maxLength=size)
    choice = lambda *values: field("string", enum=list(values))
    count = lambda low, high: field("integer", minimum=low, maximum=high)
    return [
        tool("get_full_record", "读取一条完整命题、材料或主题档案及其真实字段、关联和附件校验信息。", {
            "kind": choice("prompt", "inspiration", "clipping", "resume", "topic"), "id": ident,
        }, ("kind", "id")),
        tool("list_full_records", "按稳定的 kind、ID 顺序分页读取完整档案；游标绑定当前宿主、授权修订和数据库快照。", {
            "kind": choice(*_KINDS), "prompt_id": ident, "direction": direction, "topic": ident,
            "state": choice("want_to_write", "in_progress", "written"),
            "created_from": field("string", format="date-time"),
            "created_to": field("string", format="date-time"),
            "page_size": count(1, _PAGE_MAX), "cursor": text(16_384),
        }),
        tool("get_attachment_chunk", "按有界 base64 分块读取一张已保存原图，返回完整文件和分块校验值。", {
            "id": ident, "offset": count(0, 2**31 - 1), "max_bytes": count(1, _CHUNK_MAX),
        }, ("id", "offset", "max_bytes")),
        tool("export_full_library", "按指定范围导出完整 JSONL 档案和原图；返回按宿主授权修订绑定的一小时文件引用。", {
            "request_id": ident, "kind": choice(*_KINDS), "prompt_id": ident,
            "direction": direction, "topic": ident,
            "created_from": field("string", format="date-time"),
            "created_to": field("string", format="date-time"),
        }, ("request_id",)),
        tool("get_full_export_info", "核对完整导出文件的格式、大小、SHA-256、记录数和有效期。", {
            "export_id": ident,
        }, ("export_id",)),
        tool("read_full_export_chunk", "按字节偏移读取完整 JSONL 导出的有界 base64 分块。", {
            "export_id": ident, "offset": count(0, 2**31 - 1), "max_bytes": count(1, _CHUNK_MAX),
        }, ("export_id", "offset", "max_bytes")),
    ]


def schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS mcp25_full_exports (
        export_id TEXT PRIMARY KEY,
        host_id TEXT NOT NULL,
        profile TEXT NOT NULL,
        authorization_revision INTEGER NOT NULL,
        request_id TEXT NOT NULL,
        params_hash TEXT NOT NULL,
        file_name TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        record_count INTEGER NOT NULL,
        attachment_count INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        UNIQUE(host_id, profile, authorization_revision, request_id)
    )""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(mcp25_full_exports)")}
    if "block_hashes_json" not in columns:
        db.execute("ALTER TABLE mcp25_full_exports ADD COLUMN block_hashes_json TEXT NOT NULL DEFAULT '[]'")
    db.execute("CREATE INDEX IF NOT EXISTS mcp25_full_exports_expiry ON mcp25_full_exports(expires_at)")


def _require_full(actor):
    if not mcp24_access.has_full_access(actor):
        raise mcp_workflows.WorkflowError("forbidden", "完整档案和附件读取需要 tool:* 全权限宿主。")


def _iso(value):
    return value.isoformat(timespec="seconds")


def _utc_timestamp(value):
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")


def _params_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def export_filters(book, db, params, actor):
    _require_full(actor)
    filters = {"kind": params.get("kind", "all"), "prompt_id": params.get("prompt_id"),
               "direction": params.get("direction"), "topic": params.get("topic"),
               "created_from": _utc_timestamp(params.get("created_from")),
               "created_to": _utc_timestamp(params.get("created_to"))}
    if filters["created_from"] and filters["created_to"] and filters["created_from"] >= filters["created_to"]:
        raise mcp_workflows.WorkflowError("invalid_params", "created_from 必须早于 created_to。", {"fields": ["created_from", "created_to"]})
    _record_refs(book, db, {**filters, "state": None, "page_size": 1})
    return filters, _params_hash(filters)


def export_row_for_request(db, actor, request_id):
    return db.execute("""SELECT * FROM mcp25_full_exports WHERE host_id=? AND profile=?
        AND authorization_revision=? AND request_id=?""",
        (actor["host_id"], actor["profile"], actor["authorization_revision"], request_id)).fetchone()


def _export_path(book, row):
    return mcp_files._safe_file(mcp_files._folder(book, "mcp-full-exports"), row["file_name"])


def validate_export_artifact(book, row, now):
    if row["expires_at"] <= _iso(now):
        raise mcp_workflows.WorkflowError("expired", "完整导出引用已过期。")
    path = _export_path(book, row)
    if not path.is_file() or path.stat().st_size != row["size_bytes"] or mcp_files._hash_file(path) != row["sha256"]:
        raise mcp_workflows.WorkflowError("version_conflict", "完整导出文件校验失败。")
    return path


def validate_export_replay(book, db, actor, request_id, cached, now):
    row = validate_export_metadata(db, actor, request_id, cached, now)
    validate_export_artifact(book, row, now)
    return row


def validate_export_metadata(db, actor, request_id, cached, now):
    data = cached.get("data", {})
    row = export_row_for_request(db, actor, data.get("request_id", request_id))
    if row is None:
        raise mcp_workflows.WorkflowError("version_conflict", "完整导出回执没有对应的文件引用。")
    if (data.get("export_id"), data.get("sha256"), data.get("size_bytes"),
        data.get("record_count"), data.get("attachment_count"), data.get("expires_at")) != (
            row["export_id"], row["sha256"], row["size_bytes"], row["record_count"],
            row["attachment_count"], row["expires_at"]):
        raise mcp_workflows.WorkflowError("version_conflict", "完整导出回执与文件引用不一致。")
    if row["expires_at"] <= _iso(now):
        raise mcp_workflows.WorkflowError("expired", "完整导出引用已过期。")
    return row


def export_result(row, request_id, *, include_download=False):
    result = {"request_id": request_id, "export_id": row["export_id"], "format": "jsonl",
              "record_count": row["record_count"], "attachment_count": row["attachment_count"],
              "size_bytes": row["size_bytes"], "sha256": row["sha256"],
              "expires_at": row["expires_at"], "complete": True, "committed": True}
    if include_download:
        result["download"] = {"tool": "read_full_export_chunk", "max_bytes": _CHUNK_MAX}
    return result


def insert_export(db, actor, request_id, params_hash, result, now, export_id):
    db.execute("""INSERT INTO mcp25_full_exports(export_id,host_id,profile,authorization_revision,request_id,
        params_hash,file_name,sha256,size_bytes,record_count,attachment_count,created_at,expires_at,block_hashes_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (export_id, actor["host_id"], actor["profile"], actor["authorization_revision"], request_id,
         params_hash, f"full-{export_id}.jsonl", result["sha256"], result["size_bytes"], result["record_count"],
         result["attachment_count"], _iso(now), _iso(now + _EXPORT_TTL),
         json.dumps(result["block_hashes"], separators=(",", ":"))))
    return db.execute("SELECT * FROM mcp25_full_exports WHERE export_id = ?", (export_id,)).fetchone()


def _record_refs(book, db, filters):
    kinds = ("prompt", "inspiration", "clipping", "resume", "topic") if filters["kind"] == "all" else (filters["kind"],)
    topic_id = None
    if filters.get("topic"):
        topic = book._resolve_topic(db, filters["topic"])
        if topic is None:
            raise mcp_workflows.WorkflowError("not_found", "没有找到指定主题。", {"topic": filters["topic"]})
        topic_id = topic["id"]
    refs = []
    for kind in kinds:
        if kind == "topic":
            if filters.get("prompt_id") or filters.get("state") or filters.get("created_from") or filters.get("created_to"):
                continue
            clauses, values = [], []
            if filters.get("direction"):
                clauses.append("direction = ?")
                values.append(filters["direction"])
            if topic_id:
                clauses.append("id = ?")
                values.append(topic_id)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            refs.extend(("topic", row[0]) for row in db.execute("SELECT id FROM topics" + where, values))
            continue
        table = "prompts" if kind == "prompt" else "materials"
        columns = (("id", "direction", "topic_id", "state", "created_at") if kind == "prompt"
                   else ("id", "collection", "direction", "topic_id", "prompt_id", "created_at"))
        clauses, values = [], []
        if kind != "prompt":
            clauses.append("collection = ?")
            values.append(kind)
        if filters.get("direction"):
            clauses.append("direction = ?")
            values.append(filters["direction"])
        if topic_id:
            clauses.append("topic_id = ?")
            values.append(topic_id)
        if filters.get("prompt_id"):
            clauses.append("id = ?" if kind == "prompt" else "prompt_id = ?")
            values.append(filters["prompt_id"])
        if filters.get("state"):
            if kind != "prompt":
                continue
            clauses.append("state = ?")
            values.append(filters["state"])
        if filters.get("created_from"):
            clauses.append("created_at >= ?")
            values.append(filters["created_from"])
        if filters.get("created_to"):
            clauses.append("created_at < ?")
            values.append(filters["created_to"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        for row in db.execute(f"SELECT {','.join(columns)} FROM {table}{where}", values):
            if kind == "prompt" or row["collection"] == kind:
                refs.append((kind, row["id"]))
    refs.sort(key=lambda item: (item[0], item[1]))
    return refs


def _attachment_info(db, material_id):
    row = db.execute("SELECT content_type, content FROM inspiration_images WHERE material_id = ?", (material_id,)).fetchone()
    if row is None:
        return None
    content = row["content"]
    return {"content_type": row["content_type"], "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(), "chunk_tool": "get_attachment_chunk"}


def _record(book, db, kind, identifier, actor, now):
    if kind == "prompt":
        row = book._find_prompt_for_actor(db, identifier, "mcp", actor, {})
        from . import mcp25_relations

        result = {"kind": kind, "id": row["id"], "record": dict(row),
                  "content_provenance": {"text": "prompt_record", "origin": row["origin"]}}
        result["related_materials"] = [dict(item) for item in db.execute(
            "SELECT id, collection, version, updated_at FROM materials WHERE prompt_id = ? ORDER BY collection, id",
            (identifier,))]
        result["claim_sources"] = [dict(item) for item in db.execute(
            "SELECT prompt_id, text, clipping_id FROM mcp_claim_sources WHERE prompt_id = ? ORDER BY rowid",
            (identifier,))] if _table_exists(db, "mcp_claim_sources") else []
        result["continuation_cards"] = [dict(item) for item in db.execute(
            "SELECT id, collection, version, updated_at FROM materials WHERE prompt_id = ? AND collection = 'resume' ORDER BY id",
            (identifier,))]
        result["entity_relations"] = mcp25_relations.read_entity_relations(
            book, db, "prompt", identifier, "mcp", actor, limit=100, offset=0,
        )
        result["claim_evidence"] = mcp25_relations.read_prompt_evidence(
            book, db, identifier, "mcp", actor, limit=100, offset=0,
        )
        project_ids = mcp24_reads.related_project_ids(db, [("prompt", identifier)])
        result["topic_project_progress"] = [
            {"project_id": project_id,
             "progress": mcp25_relations.read_project_progress(
                 book, db, project_id, "mcp", actor, limit=50, offset=0,
             )}
            for project_id in project_ids
        ]
        return result
    if kind == "topic":
        row = db.execute("SELECT * FROM topics WHERE id = ?", (identifier,)).fetchone()
        if row is None:
            raise mcp_workflows.WorkflowError("not_found", "没有找到指定主题。", {"id": identifier})
        aliases = db.execute("SELECT aliases_json, version FROM mcp_read_topic_aliases WHERE topic_id = ?", (identifier,)).fetchone() if _table_exists(db, "mcp_read_topic_aliases") else None
        return {"kind": kind, "id": identifier, "record": dict(row),
                "aliases": json.loads(aliases["aliases_json"]) if aliases else [],
                "aliases_version": aliases["version"] if aliases else 0,
                "child_topics": [item[0] for item in db.execute("SELECT id FROM topics WHERE parent_id = ? ORDER BY id", (identifier,))],
                "prompt_ids": [item[0] for item in db.execute("SELECT id FROM prompts WHERE topic_id = ? ORDER BY id", (identifier,))],
                "material_ids": [item[0] for item in db.execute("SELECT id FROM materials WHERE topic_id = ? ORDER BY id", (identifier,))]}
    row = db.execute("SELECT * FROM materials WHERE id = ? AND collection = ?", (identifier, kind)).fetchone()
    if row is None:
        raise mcp_workflows.WorkflowError("not_found", "没有找到指定材料。", {"id": identifier, "kind": kind})
    if row["prompt_id"]:
        book._find_prompt_for_actor(db, row["prompt_id"], "mcp", actor, {})
    record = dict(row)
    record["content"] = json.loads(record.pop("data_json"))
    if kind == "clipping" and _table_exists(db, "mcp_claim_sources"):
        record["claim_references"] = [dict(item) for item in db.execute(
            "SELECT prompt_id, text FROM mcp_claim_sources WHERE clipping_id = ? ORDER BY rowid", (identifier,))]
    from . import mcp25_relations

    record_relations = mcp25_relations.read_entity_relations(
        book, db, kind, identifier, "mcp", actor, limit=100, offset=0,
    )
    project_ids = mcp24_reads.related_project_ids(db, [(kind, identifier)])
    return {"kind": kind, "id": identifier, "record": record,
            "attachment": _attachment_info(db, identifier), "entity_relations": record_relations,
            "topic_project_progress": [
                {"project_id": project_id,
                 "progress": mcp25_relations.read_project_progress(
                     book, db, project_id, "mcp", actor, limit=50, offset=0,
                 )}
                for project_id in project_ids
            ]}


def _table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def get_full_record(book, db, params, channel, validate, now, actor):
    _require_full(actor)
    return _record(book, db, params["kind"], params["id"], actor, now)


def list_full_records(book, db, params, channel, validate, now, actor):
    _require_full(actor)
    if "cursor" in params:
        if len(params) != 1:
            raise mcp_workflows.WorkflowError("invalid_params", "续页请求只传 cursor。")
        cursor = mcp24_reads._decode_cursor(db, params["cursor"])
        mcp24_reads._cursor_actor_check(cursor, actor)
        if cursor["purpose"] != "full_records" or cursor["expires_at"] <= mcp24_reads._cursor_time(now):
            raise mcp_workflows.WorkflowError("cursor_expired", "档案游标已过期，请使用原筛选重新读取。", {"retry_with_original_query": True})
        filters = cursor["filters"]
        if cursor["filter_hash"] != mcp24_reads._filter_hash(filters):
            raise mcp_workflows.WorkflowError("cursor_invalid", "档案游标范围无效，请使用原筛选重新读取。", {"retry_with_original_query": True})
        state = db.execute("SELECT revision, unsafe_revision, generation FROM mcp_read_state WHERE singleton = 1").fetchone()
        if (cursor["snapshot_revision"], cursor["unsafe_revision"], cursor["generation"]) != (state["revision"], state["unsafe_revision"], state["generation"]):
            raise mcp_workflows.WorkflowError("cursor_stale", "书序内容已变化，旧快照不能安全续页。", {"retry_with_original_query": True})
        offset = cursor["offset"]
    else:
        if params.get("kind", "all") not in _KINDS:
            raise mcp_workflows.WorkflowError("invalid_params", "档案 kind 无效。", {"field": "kind"})
        filters = {"kind": params.get("kind", "all"), "prompt_id": params.get("prompt_id"),
                   "direction": params.get("direction"), "topic": params.get("topic"),
                   "state": params.get("state"), "created_from": params.get("created_from"),
                   "created_to": params.get("created_to"), "page_size": params.get("page_size", 20)}
        filters["created_from"] = _utc_timestamp(filters["created_from"])
        filters["created_to"] = _utc_timestamp(filters["created_to"])
        if filters["created_from"] and filters["created_to"] and filters["created_from"] >= filters["created_to"]:
            raise mcp_workflows.WorkflowError("invalid_params", "created_from 必须早于 created_to。", {"fields": ["created_from", "created_to"]})
        state = db.execute("SELECT revision, unsafe_revision, generation FROM mcp_read_state WHERE singleton = 1").fetchone()
        offset = 0
    refs = _record_refs(book, db, filters)
    size = filters["page_size"]
    chosen = refs[offset:offset + size]
    items = [_record(book, db, kind, identifier, actor, now) for kind, identifier in chosen]
    end = offset + len(items)
    complete = end >= len(refs)
    next_cursor = None
    if not complete:
        next_cursor = mcp24_reads._sign_cursor(db, {
            "v": 1, "purpose": "full_records", "host_id": actor["host_id"], "profile": actor["profile"],
            "authorization_revision": actor["authorization_revision"], "scope_hash": mcp24_reads._scope_hash(actor),
            "filters": filters, "filter_hash": mcp24_reads._filter_hash(filters), "offset": end,
            "snapshot_revision": state["revision"], "unsafe_revision": state["unsafe_revision"],
            "generation": state["generation"], "expires_at": mcp24_reads._cursor_time(now) + 900,
        })
    return {"items": items, "total": len(refs), "record_count": len(refs),
            "current_range": {"start": offset + 1 if items else 0, "end": end},
            "page_size": size, "complete": complete, "next_cursor": next_cursor,
            "snapshot_version": state["revision"], "snapshot_revision": state["revision"],
            "sort": ["kind", "id"], "filters": filters}


def get_attachment_chunk(book, db, params, channel, validate, now, actor):
    _require_full(actor)
    material = db.execute("SELECT id, collection, prompt_id FROM materials WHERE id = ? AND collection = 'inspiration'", (params["id"],)).fetchone()
    if material is None:
        raise mcp_workflows.WorkflowError("not_found", "没有找到这张原图。", {"id": params["id"]})
    if material["prompt_id"]:
        book._find_prompt_for_actor(db, material["prompt_id"], "mcp", actor, {})
    row = db.execute("SELECT content_type, content FROM inspiration_images WHERE material_id = ?", (params["id"],)).fetchone()
    if row is None:
        raise mcp_workflows.WorkflowError("not_found", "该灵感没有已保存原图。", {"id": params["id"]})
    content = row["content"]
    size = len(content)
    offset = params["offset"]
    if offset > size:
        raise mcp_workflows.WorkflowError("invalid_params", "offset 超出图片大小。", {"size_bytes": size})
    chunk = content[offset:offset + min(params["max_bytes"], _CHUNK_MAX)]
    next_offset = offset + len(chunk)
    return {"id": params["id"], "content_type": row["content_type"], "size_bytes": size,
            "sha256": hashlib.sha256(content).hexdigest(), "offset": offset,
            "chunk_base64": base64.b64encode(chunk).decode("ascii"),
            "chunk_sha256": hashlib.sha256(chunk).hexdigest(), "next_offset": next_offset,
            "eof": next_offset >= size}


def export_full_library(book, db, params, channel, validate, now, actor):
    filters, digest = export_filters(book, db, params, actor)
    previous = export_row_for_request(db, actor, params["request_id"])
    if previous:
        if previous["params_hash"] != digest:
            raise mcp_workflows.WorkflowError("idempotency_conflict", "request_id 已用于不同的完整导出范围。")
        validate_export_artifact(book, previous, now)
        return export_result(previous, params["request_id"])
    export_id = uuid.uuid4().hex
    result = mcp_files.create_full_export(book, db, filters, actor, now, export_id)
    final_path = mcp_files._safe_file(mcp_files._folder(book, "mcp-full-exports"), f"full-{export_id}.jsonl")
    try:
        result["path"].replace(final_path)
        previous = insert_export(db, actor, params["request_id"], digest, result, now, export_id)
    except BaseException:
        final_path.unlink(missing_ok=True)
        result["path"].unlink(missing_ok=True)
        raise
    return export_result(previous, params["request_id"], include_download=True)


def get_full_export_info(book, db, params, channel, validate, now, actor):
    _require_full(actor)
    row = db.execute("""SELECT * FROM mcp25_full_exports WHERE export_id=? AND host_id=? AND profile=?
        AND authorization_revision=?""", (params["export_id"], actor["host_id"], actor["profile"], actor["authorization_revision"])).fetchone()
    if row is None:
        raise mcp_workflows.WorkflowError("not_found", "当前宿主没有这份完整导出引用。")
    validate_export_artifact(book, row, now)
    return {"export_id": row["export_id"], "format": "jsonl", "size_bytes": row["size_bytes"],
            "sha256": row["sha256"], "record_count": row["record_count"],
            "attachment_count": row["attachment_count"], "expires_at": row["expires_at"],
            "max_chunk_bytes": _CHUNK_MAX}


def read_full_export_chunk(book, db, params, channel, validate, now, actor):
    _require_full(actor)
    row = db.execute("""SELECT * FROM mcp25_full_exports WHERE export_id=? AND host_id=? AND profile=?
        AND authorization_revision=?""", (params["export_id"], actor["host_id"], actor["profile"], actor["authorization_revision"])).fetchone()
    if row is None:
        raise mcp_workflows.WorkflowError("not_found", "当前宿主没有这份完整导出引用。")
    if row["expires_at"] <= _iso(now):
        raise mcp_workflows.WorkflowError("expired", "完整导出引用已过期。")
    path = _export_path(book, row)
    if not path.is_file() or path.stat().st_size != row["size_bytes"]:
        raise mcp_workflows.WorkflowError("version_conflict", "完整导出文件校验失败。")
    offset = params["offset"]
    if offset > row["size_bytes"]:
        raise mcp_workflows.WorkflowError("invalid_params", "offset 超出导出大小。", {"size_bytes": row["size_bytes"]})
    block_hashes = json.loads(row["block_hashes_json"])
    block_size = mcp_files._FULL_EXPORT_BLOCK_SIZE
    expected_block_count = (row["size_bytes"] + block_size - 1) // block_size
    if not block_hashes:
        full_sha, block_hashes, size_bytes = mcp_files._hash_file_blocks(path)
        if full_sha != row["sha256"] or size_bytes != row["size_bytes"]:
            raise mcp_workflows.WorkflowError("version_conflict", "完整导出文件校验失败。")
        cache = book._connect()
        try:
            cache.execute("BEGIN IMMEDIATE")
            cache.execute("""UPDATE mcp25_full_exports SET block_hashes_json=? WHERE export_id=? AND host_id=?
                AND profile=? AND authorization_revision=? AND block_hashes_json='[]'""",
                (json.dumps(block_hashes, separators=(",", ":")), row["export_id"], actor["host_id"],
                 actor["profile"], actor["authorization_revision"]))
            cache.commit()
        except BaseException:
            cache.rollback()
            raise
        finally:
            cache.close()
    if len(block_hashes) != expected_block_count:
        raise mcp_workflows.WorkflowError("version_conflict", "完整导出文件校验失败。")
    chunk_size = min(params["max_bytes"], _CHUNK_MAX, row["size_bytes"] - offset)
    if chunk_size:
        first_block = offset // block_size
        last_block = (offset + chunk_size - 1) // block_size
        first_offset = first_block * block_size
        blocks = []
        with path.open("rb") as stream:
            stream.seek(first_offset)
            for block_index in range(first_block, last_block + 1):
                expected_size = min(block_size, row["size_bytes"] - block_index * block_size)
                block = stream.read(expected_size)
                if len(block) != expected_size or hashlib.sha256(block).hexdigest() != block_hashes[block_index]:
                    raise mcp_workflows.WorkflowError("version_conflict", "完整导出文件校验失败。")
                blocks.append(block)
        combined = b"".join(blocks)
        start = offset - first_offset
        chunk = combined[start:start + chunk_size]
    else:
        chunk = b""
    next_offset = offset + len(chunk)
    return {"export_id": row["export_id"], "offset": offset, "size_bytes": row["size_bytes"],
            "sha256": row["sha256"], "chunk_base64": base64.b64encode(chunk).decode("ascii"),
            "chunk_sha256": hashlib.sha256(chunk).hexdigest(), "next_offset": next_offset,
            "eof": next_offset >= row["size_bytes"]}


HANDLERS = {
    "get_full_record": get_full_record,
    "list_full_records": list_full_records,
    "get_attachment_chunk": get_attachment_chunk,
    "export_full_library": export_full_library,
    "get_full_export_info": get_full_export_info,
    "read_full_export_chunk": read_full_export_chunk,
}
WRITE_ACTIONS = frozenset({"export_full_library"})


def handle(book, db, action, params, channel, validate, now, actor):
    return HANDLERS[action](book, db, params, channel, validate, now, actor)
