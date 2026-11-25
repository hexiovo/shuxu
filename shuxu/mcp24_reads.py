"""Permission-scoped context reads and a local incremental Chinese search index."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import unicodedata
import uuid

from . import materials, mcp24_access

_INDEX_VERSION = 3
_PAGE_MAX = 50
_PRIVATE_RESULT_MAX = 12
_CURSOR_TTL = 15 * 60
_SEARCH_KINDS = ("prompt", "inspiration", "clipping", "resume", "topic", "checkpoint", "claim_source", "topic_project", "relation")
WRITE_ACTIONS = {"set_topic_aliases"}


def specs(tool, field, ident, direction, version, include_private):
    text = lambda size: field("string", minLength=1, maxLength=size)
    choice = lambda *values: field("string", enum=list(values))
    count = lambda low, high: field("integer", minimum=low, maximum=high)

    return [
        tool("get_thought_context", "一次读取当前会话或指定题的公开题文、写作状态、续接卡和素材引用及有效偏好；可选简要或详细，始终遵守逐项私密授权。", {
            "conversation_id": ident, "prompt_id": ident,
            "detail": choice("summary", "detail", "full"),
            "budget": field("integer", minimum=256, maximum=6000, description="题文文本预算，单位为字符。"),
            "page_size": count(1, 50),
            "cursor": field("string", minLength=1, maxLength=16384),
        }),
        tool("search_indexed_library", "在本机增量中文索引中检索题文、摘句、标题和主题；说明真实命中字段与原词、主题别名或近似表述。游标续页绑定宿主、范围、授权和索引快照。", {
            "query": text(160), "cursor": field("string", minLength=1, maxLength=16384),
            "direction": direction, "topic": ident,
            "kinds": field("array", items=choice(*_SEARCH_KINDS), minItems=1, maxItems=len(_SEARCH_KINDS)),
            "page_size": count(1, _PAGE_MAX),
        }),
        tool("set_topic_aliases", "为一条现有主题设置用户明确给出的别名；空列表清除。只接受具名授权，不自动生成同义词。", {
            "request_id": ident, "topic_id": ident,
            "aliases": field("array", items=text(80), minItems=0, maxItems=32),
        }, ("request_id", "topic_id", "aliases")),
    ]


def schema(db):
    had_state = _table_exists(db, "mcp_read_state")
    had_docs = _table_exists(db, "mcp_read_docs")
    had_aliases = _table_exists(db, "mcp_read_topic_aliases")
    db.execute("""CREATE TABLE IF NOT EXISTS mcp_read_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        revision INTEGER NOT NULL,
        unsafe_revision INTEGER NOT NULL,
        index_version INTEGER NOT NULL,
        generation TEXT NOT NULL,
        cursor_secret BLOB NOT NULL
    )""")
    db.execute("""INSERT OR IGNORE INTO mcp_read_state
        (singleton, revision, unsafe_revision, index_version, generation, cursor_secret)
        VALUES (1, 0, 0, ?, ?, ?)""",
        (_INDEX_VERSION, uuid.uuid4().hex, secrets.token_bytes(32)))
    db.execute("""CREATE TABLE IF NOT EXISTS mcp_read_dirty (
        kind TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        PRIMARY KEY(kind, entity_id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS mcp_read_docs (
        doc_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        field TEXT NOT NULL,
        text TEXT NOT NULL,
        direction TEXT,
        topic_id TEXT,
        visibility TEXT NOT NULL,
        version INTEGER,
        updated_at TEXT NOT NULL,
        indexed_revision INTEGER NOT NULL
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS mcp_read_docs_entity
        ON mcp_read_docs(kind, entity_id)""")
    db.execute("""CREATE INDEX IF NOT EXISTS mcp_read_docs_scope
        ON mcp_read_docs(direction, topic_id, indexed_revision)""")
    db.execute("""CREATE TABLE IF NOT EXISTS mcp_read_terms (
        doc_id TEXT NOT NULL,
        term TEXT NOT NULL,
        PRIMARY KEY(doc_id, term)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS mcp_read_terms_lookup ON mcp_read_terms(term, doc_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS mcp_read_topic_aliases (
        topic_id TEXT PRIMARY KEY REFERENCES topics(id) ON DELETE CASCADE,
        aliases_json TEXT NOT NULL,
        version INTEGER NOT NULL
    )""")

    for kind, table in (("prompt", "prompts"), ("material", "materials"), ("topic", "topics")):
        _install_source_triggers(db, kind, table)
    _install_alias_triggers(db)
    _install_extended_triggers(db)

    if not had_state or not had_docs or not had_aliases:
        backfill(db)
    else:
        row = db.execute("SELECT index_version FROM mcp_read_state WHERE singleton = 1").fetchone()
        if row["index_version"] != _INDEX_VERSION:
            backfill(db)


def _table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def related_project_ids(db, entities):
    if not _table_exists(db, "mcp_topic_projects"):
        return []
    wanted = {(kind, identifier) for kind, identifier in entities}
    return [row["id"] for row in db.execute("SELECT id,items_json FROM mcp_topic_projects ORDER BY updated_at DESC,id")
            if any((item["kind"], item["id"]) in wanted for item in json.loads(row["items_json"]))]


def _install_source_triggers(db, kind, table):
    prefix = f"mcp_read_{table}"
    for operation, timing, id_exprs, unsafe in (
        ("insert", "INSERT", ("NEW.id",), 0),
        ("update", "UPDATE", ("OLD.id", "NEW.id"), 1),
        ("delete", "DELETE", ("OLD.id",), 1),
    ):
        name = f"{prefix}_{operation}"
        increments = "revision = revision + 1"
        if unsafe:
            increments += ", unsafe_revision = unsafe_revision + 1"
        values = "\n".join(
            f"INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('{kind}', {expr});"
            for expr in id_exprs
        )
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS {name} AFTER {timing} ON {table}
            BEGIN
                UPDATE mcp_read_state SET {increments} WHERE singleton = 1;
                {values}
            END""")


def _install_alias_triggers(db):
    for operation, timing, topic_expr, unsafe in (
        ("insert", "INSERT", "NEW.topic_id", 0),
        ("update", "UPDATE", "NEW.topic_id", 1),
        ("delete", "DELETE", "OLD.topic_id", 1),
    ):
        increments = "revision = revision + 1"
        if unsafe:
            increments += ", unsafe_revision = unsafe_revision + 1"
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS mcp_read_alias_{operation}
            AFTER {timing} ON mcp_read_topic_aliases
            BEGIN
                UPDATE mcp_read_state SET {increments} WHERE singleton = 1;
                INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('topic', {topic_expr});
            END""")


def _install_extended_triggers(db):
    for table, kind, key in (("mcp_thought_checkpoints", "checkpoint", "id"),
                             ("mcp_claim_sources", "claim_source", "prompt_id"),
                             ("mcp_topic_projects", "topic_project", "id")):
        if not _table_exists(db, table):
            continue
        for operation, timing, expressions, unsafe in (
            ("insert", "INSERT", (f"NEW.{key}",), 0),
            ("update", "UPDATE", (f"OLD.{key}", f"NEW.{key}"), 1),
            ("delete", "DELETE", (f"OLD.{key}",), 1),
        ):
            increments = "revision = revision + 1"
            if unsafe:
                increments += ", unsafe_revision = unsafe_revision + 1"
            dirty = "\n".join(
                f"INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('{kind}', {expression});"
                for expression in expressions
            )
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS mcp_read_{table}_{operation}
                AFTER {timing} ON {table}
                BEGIN
                    UPDATE mcp_read_state SET {increments} WHERE singleton = 1;
                    {dirty}
                END""")


def _doc_id(kind, entity_id, field_name, text=""):
    raw = json.dumps((kind, entity_id, field_name, text), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _terms(value):
    normalized = _normalize(value)
    return {normalized[index:index + 2] for index in range(len(normalized) - 1) if normalized[index:index + 2].strip()}


def _store_doc(db, *, kind, entity_id, field_name, text, direction, topic_id, visibility, version, updated_at, indexed_revision):
    if not text.strip():
        return
    doc_id = _doc_id(kind, entity_id, field_name, text)
    db.execute("""INSERT OR REPLACE INTO mcp_read_docs
        (doc_id, kind, entity_id, field, text, direction, topic_id, visibility, version, updated_at, indexed_revision)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (doc_id, kind, entity_id, field_name, text, direction, topic_id, visibility, version, updated_at, indexed_revision))
    terms = _terms(text)
    db.executemany("INSERT OR IGNORE INTO mcp_read_terms(doc_id, term) VALUES (?, ?)", ((doc_id, term) for term in terms))


def _clear_entity_docs(db, kind, entity_id):
    doc_ids = [row[0] for row in db.execute("SELECT doc_id FROM mcp_read_docs WHERE kind = ? AND entity_id = ?", (kind, entity_id))]
    if doc_ids:
        db.executemany("DELETE FROM mcp_read_terms WHERE doc_id = ?", ((doc_id,) for doc_id in doc_ids))
        db.executemany("DELETE FROM mcp_read_docs WHERE doc_id = ?", ((doc_id,) for doc_id in doc_ids))


def _index_prompt(db, row, revision):
    _store_doc(db, kind="prompt", entity_id=row["id"], field_name="prompts.text", text=row["text"],
               direction=row["direction"], topic_id=row["topic_id"], visibility=row["visibility"],
               version=row["version"], updated_at=row["updated_at"], indexed_revision=revision)


def _index_material(db, row, revision):
    payload = json.loads(row["data_json"])
    if row["collection"] == "inspiration":
        values = tuple((f"materials.data_json.{field_name}", payload.get(field_name, ""))
                       for field_name in ("text", "source", "url"))
    elif row["collection"] == "clipping":
        values = tuple((f"materials.data_json.{field_name}", payload.get(field_name, ""))
                       for field_name in ("title", "url", "published_on", "source", "note"))
        values += tuple(("materials.data_json.annotations.note", item.get("note", ""))
                        for item in payload.get("annotations", []))
    elif row["collection"] == "resume":
        values = tuple((f"materials.data_json.{field_name}", payload.get(field_name, ""))
                       for field_name in ("title", "note", "question"))
    else:
        values = ()
    for field_name, text in values:
        _store_doc(db, kind=row["collection"], entity_id=row["id"], field_name=field_name, text=text,
                   direction=row["direction"], topic_id=row["topic_id"], visibility="private",
                   version=row["version"], updated_at=row["updated_at"], indexed_revision=revision)


def _index_topic(db, row, revision):
    _store_doc(db, kind="topic", entity_id=row["id"], field_name="topics.name", text=row["name"],
               direction=row["direction"], topic_id=row["id"], visibility="metadata", version=None,
               updated_at="", indexed_revision=revision)
    aliases = db.execute("SELECT aliases_json FROM mcp_read_topic_aliases WHERE topic_id = ?", (row["id"],)).fetchone()
    if aliases:
        for alias in json.loads(aliases["aliases_json"]):
            _store_doc(db, kind="topic", entity_id=row["id"], field_name="topics.aliases", text=alias,
                       direction=row["direction"], topic_id=row["id"], visibility="metadata", version=None,
                       updated_at="", indexed_revision=revision)


def _index_checkpoint(db, row, revision):
    for field_name in ("prompt_text", "cue", "unanswered_question", "progress"):
        _store_doc(db, kind="checkpoint", entity_id=row["id"], field_name=f"mcp_thought_checkpoints.{field_name}",
                   text=row[field_name], direction=row["direction"], topic_id=row["topic_id"],
                   visibility="private", version=row["prompt_version"], updated_at=row["created_at"],
                   indexed_revision=revision)


def _index_claim_sources(db, prompt_id, revision):
    prompt = db.execute("SELECT direction,topic_id,version FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
    if prompt is None or not _table_exists(db, "mcp_claim_sources"):
        return
    for row in db.execute("SELECT text FROM mcp_claim_sources WHERE prompt_id = ? ORDER BY text", (prompt_id,)):
        _store_doc(db, kind="claim_source", entity_id=prompt_id, field_name="mcp_claim_sources.text",
                   text=row["text"], direction=prompt["direction"], topic_id=prompt["topic_id"],
                   visibility="private", version=prompt["version"], updated_at="", indexed_revision=revision)


def _index_topic_project(db, row, revision):
    _store_doc(db, kind="topic_project", entity_id=row["id"], field_name="mcp_topic_projects.title",
               text=row["title"], direction=None, topic_id=None, visibility="private", version=row["version"],
               updated_at=row["updated_at"], indexed_revision=revision)


def _index_legacy_claim_relations(db, revision):
    from . import mcp25_relations

    docs = [doc for prompt in db.execute("SELECT DISTINCT prompt_id FROM mcp_claim_sources ORDER BY prompt_id")
            for doc in mcp25_relations.searchable_relation_docs(db, "prompt", prompt["prompt_id"])
            if doc["field"] == "mcp_claim_sources.text"]
    old_ids = [row[0] for row in db.execute(
        "SELECT doc_id FROM mcp_read_docs WHERE kind='relation' AND field='mcp_claim_sources.text'")]
    if old_ids:
        db.executemany("DELETE FROM mcp_read_terms WHERE doc_id=?", ((identifier,) for identifier in old_ids))
        db.executemany("DELETE FROM mcp_read_docs WHERE doc_id=?", ((identifier,) for identifier in old_ids))
    for doc in docs:
        _store_doc(db, kind="relation", entity_id=doc["relation_id"], field_name=doc["field"], text=doc["text"],
                   direction=doc["direction"], topic_id=doc["topic_id"], visibility=doc["visibility"],
                   version=doc["version"], updated_at=doc["updated_at"], indexed_revision=revision)


def _reindex_entity(db, kind, entity_id, revision):
    if kind == "relation":
        _clear_entity_docs(db, kind, entity_id)
        from . import mcp25_relations

        for row in mcp25_relations.searchable_relation_docs(db, kind, entity_id):
            _store_doc(db, kind=kind, entity_id=entity_id, field_name=row["field"], text=row["text"],
                       direction=row["direction"], topic_id=row["topic_id"], visibility=row["visibility"],
                       version=row["version"], updated_at=row["updated_at"], indexed_revision=revision)
        return
    if kind in {"checkpoint", "claim_source", "topic_project"}:
        _clear_entity_docs(db, kind, entity_id)
        if kind == "checkpoint":
            row = db.execute("SELECT * FROM mcp_thought_checkpoints WHERE id = ?", (entity_id,)).fetchone()
            if row:
                _index_checkpoint(db, row, revision)
        elif kind == "claim_source":
            _index_claim_sources(db, entity_id, revision)
            _index_legacy_claim_relations(db, revision)
        elif _table_exists(db, "mcp_topic_projects"):
            row = db.execute("SELECT * FROM mcp_topic_projects WHERE id = ?", (entity_id,)).fetchone()
            if row:
                _index_topic_project(db, row, revision)
        return
    index_kind = {"prompt": "prompt", "material": None, "topic": "topic"}[kind]
    if index_kind is None:
        for material_kind in ("inspiration", "clipping", "resume"):
            _clear_entity_docs(db, material_kind, entity_id)
        row = db.execute("SELECT * FROM materials WHERE id = ?", (entity_id,)).fetchone()
        if row:
            _index_material(db, row, revision)
        return
    _clear_entity_docs(db, index_kind, entity_id)
    table = {"prompt": "prompts", "topic": "topics"}[kind]
    row = db.execute(f"SELECT * FROM {table} WHERE id = ?", (entity_id,)).fetchone()
    if row:
        if kind == "prompt":
            _index_prompt(db, row, revision)
        else:
            _index_topic(db, row, revision)


def sync_changed(db):
    state = db.execute("SELECT revision FROM mcp_read_state WHERE singleton = 1").fetchone()
    revision = state["revision"]
    dirty = db.execute("SELECT kind, entity_id FROM mcp_read_dirty ORDER BY kind, entity_id").fetchall()
    for row in dirty:
        _reindex_entity(db, row["kind"], row["entity_id"], revision)
    if dirty:
        db.execute("DELETE FROM mcp_read_dirty")


def backfill(db):
    if not _table_exists(db, "mcp_read_state") or not _table_exists(db, "mcp_read_docs"):
        schema(db)
        return
    db.execute("DELETE FROM mcp_read_terms")
    db.execute("DELETE FROM mcp_read_docs")
    db.execute("DELETE FROM mcp_read_dirty")
    db.execute("""UPDATE mcp_read_state SET revision = revision + 1,
        unsafe_revision = unsafe_revision + 1, index_version = ?, generation = ?
        WHERE singleton = 1""", (_INDEX_VERSION, uuid.uuid4().hex))
    revision = db.execute("SELECT revision FROM mcp_read_state WHERE singleton = 1").fetchone()[0]
    for row in db.execute("SELECT * FROM prompts"):
        _index_prompt(db, row, revision)
    for row in db.execute("SELECT * FROM materials"):
        _index_material(db, row, revision)
    for row in db.execute("SELECT * FROM topics"):
        _index_topic(db, row, revision)
    if _table_exists(db, "mcp_thought_checkpoints"):
        for row in db.execute("SELECT * FROM mcp_thought_checkpoints"):
            _index_checkpoint(db, row, revision)
    if _table_exists(db, "mcp_claim_sources"):
        for row in db.execute("SELECT DISTINCT prompt_id FROM mcp_claim_sources"):
            _index_claim_sources(db, row["prompt_id"], revision)
    if _table_exists(db, "mcp_topic_projects"):
        for row in db.execute("SELECT * FROM mcp_topic_projects"):
            _index_topic_project(db, row, revision)
    if _table_exists(db, "mcp_read_extension_versions"):
        db.execute("DELETE FROM mcp_read_extension_versions WHERE name='relations'")


def ensure_relation_index(db):
    db.execute("CREATE TABLE IF NOT EXISTS mcp_read_extension_versions (name TEXT PRIMARY KEY, version INTEGER NOT NULL)")
    row = db.execute("SELECT version FROM mcp_read_extension_versions WHERE name='relations'").fetchone()
    if row is not None and row["version"] == 1:
        return
    from . import mcp25_relations

    docs = [doc for prompt in db.execute("SELECT id FROM prompts ORDER BY id")
            for doc in mcp25_relations.searchable_relation_docs(db, "prompt", prompt["id"])]
    existing = _table_exists(db, "mcp_read_docs") and db.execute(
        "SELECT 1 FROM mcp_read_docs WHERE kind='relation' LIMIT 1").fetchone() is not None
    db.execute("DELETE FROM mcp_read_terms WHERE doc_id IN (SELECT doc_id FROM mcp_read_docs WHERE kind='relation')")
    db.execute("DELETE FROM mcp_read_docs WHERE kind='relation'")
    revision = db.execute("SELECT revision FROM mcp_read_state WHERE singleton=1").fetchone()[0]
    if docs or existing:
        db.execute("UPDATE mcp_read_state SET revision=revision+1,unsafe_revision=unsafe_revision+1,generation=? WHERE singleton=1",
                   (uuid.uuid4().hex,))
        revision += 1
    for doc in docs:
        _store_doc(db, kind="relation", entity_id=doc["relation_id"], field_name=doc["field"], text=doc["text"],
                   direction=doc["direction"], topic_id=doc["topic_id"], visibility=doc["visibility"],
                   version=doc["version"], updated_at=doc["updated_at"], indexed_revision=revision)
    db.execute("INSERT INTO mcp_read_extension_versions(name,version) VALUES('relations',1) "
               "ON CONFLICT(name) DO UPDATE SET version=excluded.version")


def _workflow_error(code, message, **details):
    from .mcp_workflows import WorkflowError

    raise WorkflowError(code, message, details or None)


def _allowed(actor, action, *, continuing_cursor=False):
    from . import mcp24_access

    if not mcp24_access.allows(actor, action):
        code = "cursor_forbidden" if continuing_cursor else "forbidden"
        _workflow_error(code, "当前宿主没有继续此查询的具名授权。" if continuing_cursor else "当前宿主没有调用此工具的具名授权。",
                        tool=action, retry_with_new_query=continuing_cursor)


def _prompt_for_context(book, db, prompt_id, channel, actor):
    return book._find_prompt_for_actor(db, prompt_id, channel, actor, {})


def _effective_preferences(book, db, prompt, channel):
    if not prompt:
        return None
    preferences = book._get_preferences(db, {}, channel)["data"]
    return {
        "version": preferences["version"],
        "direction": next(item for item in preferences["directions"] if item["id"] == prompt["direction"]),
        "topic": next((item for item in preferences["topics"] if item["id"] == prompt["topic_id"]), None),
    }


def get_thought_context(book, db, params, channel, validate, now, actor):
    _allowed(actor, "get_thought_context")
    full_access = mcp24_access.has_full_access(actor)
    if "cursor" in params:
        if len(params) != 1:
            _workflow_error("invalid_params", "续页只传 cursor。")
        payload = _decode_cursor(db, params["cursor"])
        _cursor_actor_check(payload, actor)
        if payload["purpose"] != "thought_context":
            _workflow_error("cursor_invalid", "游标不是完整思考上下文的续页凭据。", retry_with_original_query=True)
        if payload["expires_at"] <= _cursor_time(now):
            _workflow_error("cursor_expired", "游标已过期，请重新读取题目上下文。", retry_with_original_query=True)
        filters = payload["filters"]
        if payload["filter_hash"] != _filter_hash(filters):
            _workflow_error("cursor_invalid", "游标范围无效，请重新读取题目上下文。", retry_with_original_query=True)
        state = db.execute("SELECT revision, unsafe_revision, generation FROM mcp_read_state WHERE singleton = 1").fetchone()
        if (state["revision"] != payload["snapshot_revision"] or state["unsafe_revision"] != payload["unsafe_revision"]
                or state["generation"] != payload["generation"]):
            _workflow_error("cursor_stale", "题目、材料或索引已更正，无法安全续页；请重新读取。", retry_with_original_query=True)
        prompt_id, detail, budget, page_size, offset = (
            filters["prompt_id"], "full", filters["budget"], filters["page_size"], payload["offset"]
        )
        snapshot_revision = payload["snapshot_revision"]
    else:
        has_conversation = "conversation_id" in params
        has_prompt = "prompt_id" in params
        if has_conversation == has_prompt:
            _workflow_error("invalid_params", "请提供 conversation_id 或 prompt_id 中的一项。")
        prompt_id = params.get("prompt_id")
        if has_conversation:
            entry = db.execute("""SELECT prompt_id FROM mcp_conversation_entries
                WHERE conversation_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (params["conversation_id"],)).fetchone()
            prompt_id = entry["prompt_id"] if entry else None
        detail = params.get("detail", "full" if full_access else "summary")
        if detail == "full" and not full_access:
            _workflow_error("forbidden", "完整题目上下文需要 full-access 宿主。")
        budget = params.get("budget", 1200 if detail == "summary" else 4000)
        page_size = params.get("page_size", 20)
        offset = 0
        state = db.execute("SELECT revision, unsafe_revision, generation FROM mcp_read_state WHERE singleton = 1").fetchone()
        snapshot_revision = state["revision"]
    prompt = _prompt_for_context(book, db, prompt_id, channel, actor) if prompt_id else None
    related, resumes, full_related = [], [], []
    entity_relations = {"items": [], "total": 0, "next_offset": None}
    prompt_evidence = {"claims": [], "total": 0, "next_offset": None}
    topic_project_progress = []
    if prompt:
        rows = db.execute("""SELECT * FROM materials WHERE prompt_id = ?
            ORDER BY updated_at DESC, id""", (prompt["id"],)).fetchall()
        for row in rows:
            item = materials._view(db, row, content=detail == "full", channel=channel)
            item["kind"] = row["collection"]
            if detail == "full":
                item["prompt_id"] = row["prompt_id"]
                image = db.execute("SELECT content_type, content FROM inspiration_images WHERE material_id = ?", (row["id"],)).fetchone() if row["collection"] == "inspiration" else None
                if image:
                    item["attachment"] = {"content_type": image["content_type"], "size_bytes": len(image["content"]),
                                          "sha256": hashlib.sha256(image["content"]).hexdigest(), "read_tool": "get_attachment_chunk"}
            (resumes if row["collection"] == "resume" else related).append(item)
        linked = {item["id"] for item in related if item["kind"] == "clipping"}
        for row in db.execute("SELECT DISTINCT clipping_id FROM mcp_claim_sources WHERE prompt_id = ?", (prompt["id"],)):
            if row["clipping_id"] not in linked:
                material = db.execute("SELECT * FROM materials WHERE id = ? AND collection = 'clipping'", (row["clipping_id"],)).fetchone()
                if material:
                    item = materials._view(db, material, content=detail == "full", channel=channel)
                    item["kind"] = "clipping"
                    if detail == "full":
                        item["prompt_id"] = material["prompt_id"]
                    related.append(item)
        if detail == "full":
            for checkpoint in db.execute("""SELECT id, source_conversation_id_public, prompt_id, prompt_version,
                prompt_text, direction, topic_id, cue, unanswered_question, progress, guide_step, created_at
                FROM mcp_thought_checkpoints WHERE prompt_id = ? ORDER BY created_at DESC, id""", (prompt["id"],)):
                full_related.append({"kind": "checkpoint", **dict(checkpoint),
                                     "source_conversation_id": checkpoint["source_conversation_id_public"]})
            for claim in db.execute("SELECT text, clipping_id FROM mcp_claim_sources WHERE prompt_id = ? ORDER BY text, clipping_id", (prompt["id"],)):
                claim_id = hashlib.sha256(f"{prompt['id']}\0{claim['text']}\0{claim['clipping_id']}".encode("utf-8")).hexdigest()[:32]
                full_related.append({"kind": "claim_source", "id": claim_id, "prompt_id": prompt["id"],
                                     "claim": claim["text"], "clipping_id": claim["clipping_id"]})
            for project in db.execute("SELECT * FROM mcp_topic_projects ORDER BY updated_at DESC, id"):
                items = json.loads(project["items_json"])
                if any(item.get("kind") == "prompt" and item.get("id") == prompt["id"] for item in items):
                    full_related.append({"kind": "topic_project", **dict(project), "items": items})
            from . import mcp25_relations

            entity_relations = mcp25_relations.read_entity_relations(
                book, db, "prompt", prompt["id"], channel, actor, limit=page_size, offset=0,
            )
            prompt_evidence = mcp25_relations.read_prompt_evidence(
                book, db, prompt["id"], channel, actor, limit=page_size, offset=0,
            )
            project_ids = related_project_ids(db, [("prompt", prompt["id"]),
                                                    *((row["collection"], row["id"]) for row in rows)])
            topic_project_progress = [
                {"project_id": project_id,
                 "progress": mcp25_relations.read_project_progress(
                     book, db, project_id, channel, actor, limit=page_size, offset=0,
                 )}
                for project_id in project_ids
            ]
        for collection in (resumes, related):
            collection.sort(key=lambda item: (item.get("updated_at", ""), item["id"]), reverse=True)

    prompt_view = None
    used = 0
    truncated = False
    if prompt:
        from .book import BookService, _prompt_dict

        if detail == "full":
            prompt_view = _prompt_dict(prompt, now)
        elif channel == "mcp":
            prompt_view = BookService._prompt_view(prompt, now, channel, include_state=True)
        else:
            prompt_view = _prompt_dict(prompt, now)
        text_budget = len(prompt_view["text"]) if detail == "full" else min(budget, 1000 if detail == "detail" else 180)
        prompt_view["text"] = prompt_view["text"][:text_budget]
        used = len(prompt_view["text"])
        truncated = used < len(prompt["text"])
    max_items = page_size if detail == "full" else max(1, min(24, budget // 120))
    resume_count, material_count = len(resumes), len(related)
    all_related = sorted([*resumes, *related, *full_related],
                         key=lambda item: (item.get("updated_at", item.get("created_at", "")), item["id"]), reverse=True)
    if detail == "full":
        page = all_related[offset:offset + max_items]
        resumes = [item for item in page if item["kind"] == "resume"]
        related = [item for item in page if item["kind"] in {"inspiration", "clipping"}]
        full_related = [item for item in page if item["kind"] in {"checkpoint", "claim_source", "topic_project"}]
    else:
        resumes, related = resumes[:max_items], related[:max_items]
    truncated = truncated or resume_count > len(resumes) or material_count > len(related)
    result = {
        "detail": detail,
        "authorization": {"host_id": actor["host_id"], "profile": actor["profile"],
                          "revision": actor["authorization_revision"], "tool": "get_thought_context"},
        "prompt": prompt_view,
        "writing_status": ({"state": prompt_view.get("state"), "snoozed_until": prompt_view.get("snoozed_until")}
                           if prompt_view and "state" in prompt_view else None),
        "resume_cards": resumes,
        "materials": related,
        "effective_preferences": _effective_preferences(book, db, prompt, channel),
        "budget": {"unit": "characters", "limit": used if detail == "full" else budget,
                   "used_prompt_text": used, "mode": "complete" if detail == "full" else "bounded"},
        "truncated": False if detail == "full" else truncated,
        "private_content_included": detail == "full",
    }
    if detail == "full":
        end = offset + len(page)
        complete = end >= len(all_related)
        result.update({"related_records": page,
                       "checkpoints": [item for item in full_related if item["kind"] == "checkpoint"],
                       "claim_sources": [item for item in full_related if item["kind"] == "claim_source"],
                       "topic_projects": [item for item in full_related if item["kind"] == "topic_project"],
                       "total_related": len(all_related), "page_range": {"start": offset + 1 if page else 0,
                       "end": end, "total": len(all_related)}, "snapshot_version": snapshot_revision,
                       "complete": complete, "next_cursor": None})
        result.update({"entity_relations": entity_relations,
                       "prompt_evidence": prompt_evidence,
                       "topic_project_progress": topic_project_progress})
        if not complete:
            filters = {"prompt_id": prompt["id"], "budget": budget, "page_size": page_size}
            result["next_cursor"] = _sign_cursor(db, {
                "v": 1, "purpose": "thought_context", "host_id": actor["host_id"],
                "profile": actor["profile"], "authorization_revision": actor["authorization_revision"],
                "scope_hash": _scope_hash(actor), "filters": filters,
                "filter_hash": _filter_hash(filters), "snapshot_revision": snapshot_revision,
                "unsafe_revision": state["unsafe_revision"], "generation": state["generation"],
                "expires_at": _cursor_time(now) + _CURSOR_TTL, "offset": end,
            })
    return result


def _query_terms(db, query, snapshot_revision):
    terms = sorted(_terms(query))
    minimum = max(1, math.ceil(len(terms) * 0.6))
    marks = ",".join("?" for _ in terms)
    return db.execute(f"""SELECT d.* FROM mcp_read_docs AS d
        JOIN mcp_read_terms AS t ON t.doc_id = d.doc_id
        WHERE d.indexed_revision <= ? AND t.term IN ({marks})
        GROUP BY d.doc_id HAVING COUNT(DISTINCT t.term) >= ?""",
        (snapshot_revision, *terms, minimum)).fetchall()


def _match(query, text, field_name):
    normalized_query, normalized_text = _normalize(query), _normalize(text)
    if normalized_query in normalized_text or (len(normalized_query.split()) > 1 and all(term in normalized_text for term in normalized_query.split())):
        if field_name == "topics.aliases":
            return 3, "topic_alias"
        return 3, "original_word"
    query_terms = _terms(normalized_query)
    if len(normalized_query) >= 4 and query_terms:
        overlap = len(query_terms & _terms(normalized_text)) / len(query_terms)
        if overlap >= 0.6:
            return 1, "approximate"
    return 0, None


def _scope_hash(actor):
    raw = json.dumps({"host_id": actor["host_id"], "profile": actor["profile"], "scopes": sorted(actor["scopes"])},
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sign_cursor(db, payload):
    secret = db.execute("SELECT cursor_secret FROM mcp_read_state WHERE singleton = 1").fetchone()[0]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(secret, raw, hashlib.sha256).digest()
    return body + "." + base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def _decode_cursor(db, token):
    try:
        body, signature = token.split(".", 1)
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except (ValueError, TypeError):
        _workflow_error("cursor_invalid", "游标格式无效，请使用原查询重新搜索。", retry_with_original_query=True)
    secret = db.execute("SELECT cursor_secret FROM mcp_read_state WHERE singleton = 1").fetchone()[0]
    expected = hmac.new(secret, raw, hashlib.sha256).digest()
    if not hmac.compare_digest(actual, expected):
        _workflow_error("cursor_invalid", "游标签名无效，请使用原查询重新搜索。", retry_with_original_query=True)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        _workflow_error("cursor_invalid", "游标内容无效，请使用原查询重新搜索。", retry_with_original_query=True)


def _cursor_actor_check(payload, actor):
    if (payload["host_id"] != actor["host_id"] or payload["profile"] != actor["profile"]
            or payload["authorization_revision"] != actor["authorization_revision"]
            or payload["scope_hash"] != _scope_hash(actor)):
        _workflow_error("cursor_forbidden", "游标不属于当前宿主或授权范围，请按当前授权重新搜索。", retry_with_new_query=True)


def _filter_hash(filters):
    raw = json.dumps(filters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cursor_time(now):
    return int(now.timestamp())


def _add_hit(hits, doc, match_type, field_name, query_term, score=None):
    key = (doc["kind"], doc["entity_id"])
    score = score if score is not None else {"original_word": 3, "topic_alias": 2, "approximate": 1}[match_type]
    hit = hits.get(key)
    if hit is None:
        hit = {
            "kind": doc["kind"], "id": doc["entity_id"], "topic_id": doc["topic_id"],
            "direction": doc["direction"], "visibility": doc["visibility"],
            "updated_at": doc["updated_at"], "score": score, "explanations": [],
        }
        hits[key] = hit
    elif score > hit["score"]:
        hit["score"] = score
        hit["updated_at"] = doc["updated_at"]
    explanation = {"field": field_name, "match_type": match_type, "query_term": query_term}
    if explanation not in hit["explanations"]:
        hit["explanations"].append(explanation)


def _hit_private(hit):
    return hit["visibility"] == "private" or hit["kind"] in (
        "inspiration", "clipping", "resume", "checkpoint", "claim_source", "topic_project", "relation",
    )


def _scope_hit(hit, filters):
    return (hit["kind"] in filters["kinds"]
            and (filters["direction"] is None or hit["direction"] == filters["direction"])
            and (filters["topic_id"] is None or hit["topic_id"] == filters["topic_id"]))


def _after(hit, last):
    if last is None:
        return True
    score, updated_at, kind, identifier = last
    if hit["score"] != score:
        return hit["score"] < score
    if hit["updated_at"] != updated_at:
        return hit["updated_at"] < updated_at
    return (hit["kind"], hit["id"]) > (kind, identifier)


def _item_view(book, db, hit, channel, now, actor=None):
    full_access = mcp24_access.has_full_access(actor)
    if hit["kind"] == "prompt":
        row = db.execute("SELECT * FROM prompts WHERE id = ?", (hit["id"],)).fetchone()
        if row is None:
            return None
        if row["visibility"] == "private" and channel == "mcp" and not full_access:
            item = {key: row[key] for key in ("id", "direction", "topic_id", "version", "updated_at")}
            item["kind"] = "prompt"
            item["content_requires_authorization"] = True
            return item
        from .book import BookService, _prompt_dict

        item = _prompt_dict(row, now) if full_access else BookService._prompt_view(row, now, channel, include_state=True)
        if not full_access:
            item["text"] = item["text"][:180]
        item["kind"] = "prompt"
        return item
    if hit["kind"] == "topic":
        row = db.execute("SELECT id, direction, name, parent_id, active FROM topics WHERE id = ?", (hit["id"],)).fetchone()
        if row is None:
            return None
        alias_row = db.execute("SELECT version, aliases_json FROM mcp_read_topic_aliases WHERE topic_id = ?", (hit["id"],)).fetchone()
        return {"kind": "topic", **dict(row), "alias_count": len(json.loads(alias_row["aliases_json"])) if alias_row else 0,
                "alias_version": alias_row["version"] if alias_row else 0}
    if hit["kind"] == "checkpoint":
        row = db.execute("""SELECT id,source_conversation_id_public,prompt_id,prompt_version,prompt_text,
            direction,topic_id,cue,unanswered_question,progress,guide_step,created_at
            FROM mcp_thought_checkpoints WHERE id = ?""", (hit["id"],)).fetchone()
        if row is None:
            return None
        if not full_access:
            return {"kind": hit["kind"], "id": row["id"], "prompt_id": row["prompt_id"],
                    "created_at": row["created_at"], "content_requires_authorization": True}
        item = dict(row)
        item["source_conversation_id"] = item.pop("source_conversation_id_public")
        item["kind"] = hit["kind"]
        return item
    if hit["kind"] == "claim_source":
        prompt_id = hit["id"]
        rows = db.execute("SELECT text,clipping_id FROM mcp_claim_sources WHERE prompt_id = ? ORDER BY text,clipping_id", (prompt_id,)).fetchall()
        if not rows:
            return None
        if not full_access:
            return {"kind": hit["kind"], "prompt_id": prompt_id,
                    "count": len(rows), "content_requires_authorization": True}
        return {"kind": hit["kind"], "prompt_id": prompt_id, "claims": [dict(row) for row in rows]}
    if hit["kind"] == "topic_project":
        row = db.execute("SELECT id,title,items_json,version,updated_at FROM mcp_topic_projects WHERE id = ?", (hit["id"],)).fetchone()
        if row is None:
            return None
        if not full_access:
            return {"kind": hit["kind"], "id": row["id"], "version": row["version"],
                    "updated_at": row["updated_at"], "content_requires_authorization": True}
        return {"kind": hit["kind"], "id": row["id"], "title": row["title"],
                "items": json.loads(row["items_json"]), "version": row["version"], "updated_at": row["updated_at"]}
    if hit["kind"] == "relation":
        if not full_access:
            return {"kind": hit["kind"], "id": hit["id"], "content_requires_authorization": True,
                    "read_tool": "read_entity_relations"}
        from . import mcp25_relations

        return {"kind": hit["kind"], "id": hit["id"],
                "documents": mcp25_relations.searchable_relation_docs(db, "relation", hit["id"]),
                "read_tool": "read_entity_relations"}
    row = db.execute("SELECT * FROM materials WHERE id = ? AND collection = ?", (hit["id"], hit["kind"])).fetchone()
    if row is None:
        return None
    item = materials._view(db, row, content=full_access, channel=channel)
    item["kind"] = hit["kind"]
    if full_access:
        item["prompt_id"] = row["prompt_id"]
        if hit["kind"] == "inspiration":
            image = db.execute("SELECT content_type,content FROM inspiration_images WHERE material_id = ?", (row["id"],)).fetchone()
            if image:
                item["attachment"] = {"content_type": image["content_type"], "size_bytes": len(image["content"]),
                                      "sha256": hashlib.sha256(image["content"]).hexdigest(), "read_tool": "get_attachment_chunk"}
    else:
        item["content_requires_authorization"] = True
    return item


def search_indexed_library(book, db, params, channel, validate, now, actor):
    _allowed(actor, "search_indexed_library", continuing_cursor="cursor" in params)
    _ensure_read_tables(db)
    full_access = mcp24_access.has_full_access(actor)
    if "cursor" in params:
        if len(params) != 1:
            _workflow_error("invalid_params", "续页只传 cursor。")
        payload = _decode_cursor(db, params["cursor"])
        _cursor_actor_check(payload, actor)
        if payload["filter_hash"] != _filter_hash(payload["filters"]):
            _workflow_error("cursor_invalid", "游标范围无效，请使用原查询重新搜索。", retry_with_original_query=True)
        if payload["expires_at"] <= _cursor_time(now):
            _workflow_error("cursor_expired", "游标已过期，请使用原查询重新搜索。", retry_with_original_query=True)
        state = db.execute("SELECT revision, unsafe_revision, generation FROM mcp_read_state WHERE singleton = 1").fetchone()
        if payload["generation"] != state["generation"] or payload["unsafe_revision"] != state["unsafe_revision"]:
            _workflow_error("cursor_stale", "资料已更正或删除，无法安全续页；请使用原查询重新搜索。", retry_with_original_query=True)
        filters = payload["filters"]
        snapshot_revision = payload["snapshot_revision"]
        last = payload["last"]
        private_seen = payload["private_seen"]
        returned_count = payload.get("returned_count", 0)
        page_size = filters["page_size"]
    else:
        if "query" not in params:
            _workflow_error("invalid_params", "新查询需要 query，续页需要 cursor。")
        query = _normalize(params["query"].strip())
        if len(query) < 2:
            _workflow_error("invalid_params", "关键词至少需要两个字符。", fields=["query"])
        kinds = sorted(set(params.get("kinds", _SEARCH_KINDS)))
        topic_id = None
        if "topic" in params:
            topic = book._resolve_topic(db, params["topic"])
            if topic is None:
                _workflow_error("not_found", "没有找到指定主题。", topic=params["topic"])
            topic_id = topic["id"]
        page_size = params.get("page_size", _PAGE_MAX)
        filters = {"query": query, "direction": params.get("direction"), "topic_id": topic_id,
                   "kinds": kinds, "page_size": page_size}
        state = db.execute("SELECT revision, unsafe_revision, generation FROM mcp_read_state WHERE singleton = 1").fetchone()
        snapshot_revision = state["revision"]
        last = None
        private_seen = 0
        returned_count = 0

    query = filters["query"]
    docs = _query_terms(db, query, snapshot_revision)
    hits = {}
    topic_matches = []
    for doc in docs:
        score, match_type = _match(query, doc["text"], doc["field"])
        if not score:
            continue
        _add_hit(hits, doc, match_type, doc["field"], query, score)
        if doc["kind"] == "topic":
            topic_matches.append((doc, match_type, score))
    for topic_doc, match_type, score in topic_matches:
        linked = db.execute("""SELECT * FROM mcp_read_docs WHERE topic_id = ? AND kind != 'topic'
            AND indexed_revision <= ?""", (topic_doc["entity_id"], snapshot_revision)).fetchall()
        linked_type = "topic_alias" if match_type in ("topic_alias", "original_word") else "approximate"
        for doc in linked:
            if doc["kind"] not in filters["kinds"]:
                continue
            _add_hit(hits, doc, linked_type, topic_doc["field"], query, min(score, 2))

    all_candidates = [hit for hit in hits.values() if _scope_hit(hit, filters)]
    all_candidates.sort(key=lambda hit: (hit["kind"], hit["id"]))
    all_candidates.sort(key=lambda hit: hit["updated_at"], reverse=True)
    all_candidates.sort(key=lambda hit: hit["score"], reverse=True)
    candidates = [hit for hit in all_candidates if _after(hit, last)]

    items, private_count = [], private_seen
    for hit in candidates:
        if _hit_private(hit) and not full_access:
            if private_count >= _PRIVATE_RESULT_MAX:
                continue
            private_count += 1
        item = _item_view(book, db, hit, channel, now, actor)
        if item is None:
            continue
        item["match_explanations"] = hit["explanations"]
        items.append((hit, item))
        if len(items) >= page_size:
            break

    has_more = False
    private_limited = False
    if items:
        last_hit = items[-1][0]
        for hit in candidates:
            if not _after(hit, [last_hit["score"], last_hit["updated_at"], last_hit["kind"], last_hit["id"]]):
                continue
            if _hit_private(hit) and not full_access:
                if private_count >= _PRIVATE_RESULT_MAX:
                    private_limited = True
                    continue
            has_more = True
            break
    else:
        private_limited = not full_access and private_seen >= _PRIVATE_RESULT_MAX and any(_hit_private(hit) for hit in candidates)

    next_cursor = None
    if has_more:
        last_hit = items[-1][0]
        next_payload = {
            "v": 1, "host_id": actor["host_id"], "profile": actor["profile"],
            "authorization_revision": actor["authorization_revision"], "scope_hash": _scope_hash(actor),
            "filters": filters, "filter_hash": _filter_hash(filters), "snapshot_revision": snapshot_revision,
            "unsafe_revision": state["unsafe_revision"], "generation": state["generation"],
            "expires_at": _cursor_time(now) + _CURSOR_TTL,
            "last": [last_hit["score"], last_hit["updated_at"], last_hit["kind"], last_hit["id"]],
            "private_seen": private_count,
            "returned_count": returned_count + len(items),
        }
        next_cursor = _sign_cursor(db, next_payload)
    result = {"items": [item for _, item in items], "has_more": has_more, "cursor": next_cursor,
              "snapshot_revision": snapshot_revision, "private_results_limited": private_limited}
    if full_access:
        end = returned_count + len(items)
        result.update({"total_count": len(all_candidates), "page_range": {
            "start": returned_count + 1 if items else 0, "end": end, "total": len(all_candidates)},
            "complete": not has_more, "next_cursor": next_cursor})
    return result


def _ensure_read_tables(db):
    if not _table_exists(db, "mcp_read_state") or not _table_exists(db, "mcp_read_docs"):
        schema(db)


def set_topic_aliases(book, db, params, channel, validate, now, actor):
    _allowed(actor, "set_topic_aliases")
    topic = db.execute("SELECT id, name FROM topics WHERE id = ?", (params["topic_id"],)).fetchone()
    if topic is None:
        _workflow_error("not_found", "没有找到指定主题。", topic_id=params["topic_id"])
    aliases = [unicodedata.normalize("NFKC", alias).strip() for alias in params["aliases"]]
    normalized = [_normalize(alias) for alias in aliases]
    if any(not alias for alias in normalized) or len(set(normalized)) != len(normalized):
        _workflow_error("invalid_params", "别名不能为空，且同一主题内不能重复。", fields=["aliases"])
    current = db.execute("SELECT aliases_json, version FROM mcp_read_topic_aliases WHERE topic_id = ?", (topic["id"],)).fetchone()
    previous = json.loads(current["aliases_json"]) if current else []
    if aliases == previous:
        return {"request_id": params["request_id"], "committed": True, "topic_id": topic["id"],
                "aliases": aliases, "version": current["version"] if current else 0, "changed": False}
    version = current["version"] + 1 if current else 1
    db.execute("""INSERT INTO mcp_read_topic_aliases(topic_id, aliases_json, version) VALUES (?, ?, ?)
        ON CONFLICT(topic_id) DO UPDATE SET aliases_json = excluded.aliases_json, version = excluded.version""",
        (topic["id"], json.dumps(aliases, ensure_ascii=False), version))
    sync_changed(db)
    return {"request_id": params["request_id"], "committed": True, "topic_id": topic["id"],
            "aliases": aliases, "version": version, "changed": True}


HANDLERS = {
    "get_thought_context": get_thought_context,
    "search_indexed_library": search_indexed_library,
    "set_topic_aliases": set_topic_aliases,
}


def handle(book, db, action, params, channel, validate, now, actor):
    handler = HANDLERS.get(action)
    if handler is None:
        _workflow_error("not_found", "未找到 MCP24 读取工具。", action=action)
    return handler(book, db, params, channel, validate, now, actor)
