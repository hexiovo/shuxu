"""Typed material relations, claim evidence, and complete topic progress."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid

from . import materials, mcp24_access, mcp_workflows, privacy_guard


_MATERIAL_FIELDS = {
    "inspiration": ("text", "source", "url", "note"),
    "clipping": ("title", "url", "published_on", "source", "note"),
    "resume": ("title", "note", "question"),
}
_EVIDENCE_FIELDS = ("title", "url", "source", "published_on", "note")
_RELATION_TYPES = ("comparison", "continuation", "counterexample", "example", "context")
_RELATION_ROLES = ("source", "example", "counterexample", "context", "contrast", "support", "opposition", "indirect", "unverified")
_SUPPORT_TYPES = ("supports", "opposes", "indirect", "unverified")
_SELECTIONS = ("full", "excerpt", "link_title")


def specs(tool, field, ident, direction, version, include_private):
    text = lambda size: field("string", minLength=1, maxLength=size)
    choice = lambda *values: field("string", enum=list(values))
    count = lambda low, high: field("integer", minimum=low, maximum=high)
    obj = lambda props, required: field("object", properties=props, required=list(required), additionalProperties=False)
    seq = lambda item, high, low=1: field("array", items=item, minItems=low, maxItems=high)
    material_fields = choice(*sorted({name for names in _MATERIAL_FIELDS.values() for name in names}))

    draft_material = obj({
        "kind": choice(*_MATERIAL_FIELDS), "id": ident,
        "selection": choice(*_SELECTIONS), "field": material_fields,
        "start": count(0, 1_000_000), "length": count(1, 20_000),
        "expected_version": version,
    }, ("kind", "id", "selection"))
    relation_material = obj({
        "kind": choice(*_MATERIAL_FIELDS), "id": ident,
        "expected_version": version, "expected_relation_version": version,
        "selection": choice(*_SELECTIONS), "field": material_fields,
        "start": count(0, 1_000_000), "length": count(1, 20_000),
        "role": choice(*_RELATION_ROLES), "position": count(0, 1_000_000),
        "description": field("string", maxLength=500),
    }, ("kind", "id", "expected_version", "selection", "role", "position"))
    new_prompt = obj({
        "text": text(1000), "direction": direction, "topic": ident,
        "depth": choice("light", "medium", "deep"), "parent_id": ident,
    }, ("text", "direction"))
    claim_evidence = obj({
        "evidence_id": ident, "expected_version": version,
        "clipping_id": ident, "material_version": version,
        "field": choice(*_EVIDENCE_FIELDS), "support_type": choice(*_SUPPORT_TYPES),
        "location": field("string", maxLength=200), "excerpt": field("string", maxLength=1000),
        "conclusion": field("string", maxLength=500),
    }, ("clipping_id", "material_version", "support_type"))
    claim = obj({
        "claim_id": ident, "expected_version": version,
        "text": text(500), "evidence": seq(claim_evidence, 20, 0),
    }, ("text",))
    page = {"limit": count(1, 100), "offset": count(0, 2**31 - 1)}

    return [
        tool("draft_prompt_from_materials", "按给定顺序读取多条指定素材的全文、精确片段或剪藏标题，返回一条未保存的对照、承接或反例草题及可回查出处。", {
            "materials": seq(draft_material, 12),
            "relation_type": choice("comparison", "continuation", "counterexample"),
            "direction": direction, "topic": ident, "depth": choice("light", "medium", "deep"),
        }, ("materials", "relation_type")),
        tool("save_prompt_material_relations", "保存新草题或给现有题增加有序的 typed 素材关系；题目与关系在同一事务提交。", {
            "request_id": ident, "prompt_id": ident, "expected_version": version,
            "prompt": new_prompt, "relation_type": choice(*_RELATION_TYPES),
            "materials": seq(relation_material, 20),
        }, ("request_id", "relation_type", "materials")),
        tool("remove_prompt_material_relation", "按关系版本移除一条题目与素材之间的关联。", {
            "request_id": ident, "relation_id": ident, "expected_version": version,
        }, ("request_id", "relation_id", "expected_version")),
        tool("read_entity_relations", "按题目或素材 ID 正向、反向分页读取 typed 关联、旧版来源标签及对应旧版说法尚未复核的证据。", {
            "kind": choice("prompt", *_MATERIAL_FIELDS), "object_id": ident, **page,
        }, ("kind", "object_id")),
        tool("write_claim_evidence", "按具体说法记录剪藏的支持、反对、间接或待核对范围、位置、引文及结论，并绑定当前说法文字修订。", {
            "request_id": ident, "prompt_id": ident, "expected_prompt_version": version,
            "claims": seq(claim, 15),
        }, ("request_id", "prompt_id", "expected_prompt_version", "claims")),
        tool("read_prompt_evidence", "按题目分页读取各说法的证据类型、引用位置、片段、结论和核对时间；旧版关联及对应旧版说法的证据会标为待核对。", {
            "prompt_id": ident, **page,
        }, ("prompt_id",)),
        tool("get_topic_project_progress", "分页读取专题全部成员、关系、续接问题、待核对说法和基于已保存依据的下一条线索；已删除成员保留为不可用条目。", {
            "project_id": ident, **page,
        }, ("project_id",)),
    ]


def schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS mcp25_prompt_material_relations (
            relation_id TEXT PRIMARY KEY,
            prompt_id TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
            material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
            material_kind TEXT NOT NULL CHECK (material_kind IN ('inspiration','clipping','resume')),
            relation_type TEXT NOT NULL CHECK (relation_type IN ('comparison','continuation','counterexample','example','context')),
            role TEXT NOT NULL CHECK (role IN ('source','example','counterexample','context','contrast','support','opposition','indirect','unverified')),
            position INTEGER NOT NULL CHECK (position >= 0),
            description TEXT NOT NULL DEFAULT '',
            selection TEXT NOT NULL CHECK (selection IN ('full','excerpt','link_title')),
            field TEXT,
            start INTEGER,
            length INTEGER,
            material_version INTEGER NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(prompt_id, material_id)
        );
        CREATE INDEX IF NOT EXISTS mcp25_relations_material
            ON mcp25_prompt_material_relations(material_id, position, relation_id);
        CREATE INDEX IF NOT EXISTS mcp25_relations_prompt
            ON mcp25_prompt_material_relations(prompt_id, position, relation_id);
        CREATE TABLE IF NOT EXISTS mcp25_claims (
            claim_id TEXT PRIMARY KEY,
            prompt_id TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            version INTEGER NOT NULL,
            text_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(prompt_id, text)
        );
        CREATE INDEX IF NOT EXISTS mcp25_claims_prompt ON mcp25_claims(prompt_id, updated_at, claim_id);
        CREATE TABLE IF NOT EXISTS mcp25_claim_evidence (
            evidence_id TEXT PRIMARY KEY,
            claim_id TEXT NOT NULL REFERENCES mcp25_claims(claim_id) ON DELETE CASCADE,
            prompt_id TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
            clipping_id TEXT NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
            material_version INTEGER NOT NULL,
            claim_text_version INTEGER NOT NULL DEFAULT 1,
            field TEXT CHECK (field IN ('title','url','source','published_on','note')),
            support_type TEXT NOT NULL CHECK (support_type IN ('supports','opposes','indirect','unverified')),
            location TEXT NOT NULL DEFAULT '',
            excerpt TEXT NOT NULL DEFAULT '',
            conclusion TEXT NOT NULL DEFAULT '',
            checked_at TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS mcp25_evidence_claim ON mcp25_claim_evidence(claim_id, updated_at, evidence_id);
        CREATE INDEX IF NOT EXISTS mcp25_evidence_prompt ON mcp25_claim_evidence(prompt_id, updated_at, evidence_id);
        CREATE INDEX IF NOT EXISTS mcp25_evidence_material ON mcp25_claim_evidence(clipping_id, updated_at, evidence_id);

        CREATE TRIGGER IF NOT EXISTS mcp25_relation_insert AFTER INSERT ON mcp25_prompt_material_relations
        BEGIN
            UPDATE mcp_read_state SET revision = revision + 1 WHERE singleton = 1;
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('relation', NEW.relation_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('prompt', NEW.prompt_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('material', NEW.material_id);
        END;
        CREATE TRIGGER IF NOT EXISTS mcp25_relation_update AFTER UPDATE ON mcp25_prompt_material_relations
        BEGIN
            UPDATE mcp_read_state SET revision = revision + 1, unsafe_revision = unsafe_revision + 1 WHERE singleton = 1;
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('relation', OLD.relation_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('relation', NEW.relation_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('prompt', OLD.prompt_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('prompt', NEW.prompt_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('material', OLD.material_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('material', NEW.material_id);
        END;
        CREATE TRIGGER IF NOT EXISTS mcp25_relation_delete AFTER DELETE ON mcp25_prompt_material_relations
        BEGIN
            UPDATE mcp_read_state SET revision = revision + 1, unsafe_revision = unsafe_revision + 1 WHERE singleton = 1;
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('relation', OLD.relation_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('prompt', OLD.prompt_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('material', OLD.material_id);
        END;

        CREATE TRIGGER IF NOT EXISTS mcp25_claim_insert AFTER INSERT ON mcp25_claims
        BEGIN
            UPDATE mcp_read_state SET revision = revision + 1 WHERE singleton = 1;
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('relation', NEW.claim_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('prompt', NEW.prompt_id);
        END;
        CREATE TRIGGER IF NOT EXISTS mcp25_claim_update AFTER UPDATE ON mcp25_claims
        BEGIN
            UPDATE mcp_read_state SET revision = revision + 1, unsafe_revision = unsafe_revision + 1 WHERE singleton = 1;
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('relation', OLD.claim_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('relation', NEW.claim_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('prompt', OLD.prompt_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('prompt', NEW.prompt_id);
        END;
        CREATE TRIGGER IF NOT EXISTS mcp25_claim_delete AFTER DELETE ON mcp25_claims
        BEGIN
            UPDATE mcp_read_state SET revision = revision + 1, unsafe_revision = unsafe_revision + 1 WHERE singleton = 1;
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('relation', OLD.claim_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('prompt', OLD.prompt_id);
        END;

        CREATE TRIGGER IF NOT EXISTS mcp25_evidence_insert AFTER INSERT ON mcp25_claim_evidence
        BEGIN
            UPDATE mcp_read_state SET revision = revision + 1 WHERE singleton = 1;
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('relation', NEW.evidence_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('prompt', NEW.prompt_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('material', NEW.clipping_id);
        END;
        CREATE TRIGGER IF NOT EXISTS mcp25_evidence_update AFTER UPDATE ON mcp25_claim_evidence
        BEGIN
            UPDATE mcp_read_state SET revision = revision + 1, unsafe_revision = unsafe_revision + 1 WHERE singleton = 1;
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('relation', OLD.evidence_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('relation', NEW.evidence_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('prompt', OLD.prompt_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('prompt', NEW.prompt_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('material', OLD.clipping_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('material', NEW.clipping_id);
        END;
        CREATE TRIGGER IF NOT EXISTS mcp25_evidence_delete AFTER DELETE ON mcp25_claim_evidence
        BEGIN
            UPDATE mcp_read_state SET revision = revision + 1, unsafe_revision = unsafe_revision + 1 WHERE singleton = 1;
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('relation', OLD.evidence_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('prompt', OLD.prompt_id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id) VALUES ('material', OLD.clipping_id);
        END;

        CREATE TRIGGER IF NOT EXISTS mcp25_prompt_endpoint_update AFTER UPDATE ON prompts
        BEGIN
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id)
                SELECT 'relation', relation_id FROM mcp25_prompt_material_relations WHERE prompt_id IN (OLD.id,NEW.id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id)
                SELECT 'relation', claim_id FROM mcp25_claims WHERE prompt_id IN (OLD.id,NEW.id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id)
                SELECT 'relation', evidence_id FROM mcp25_claim_evidence WHERE prompt_id IN (OLD.id,NEW.id);
        END;
        CREATE TRIGGER IF NOT EXISTS mcp25_material_endpoint_update AFTER UPDATE ON materials
        BEGIN
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id)
                SELECT 'relation', relation_id FROM mcp25_prompt_material_relations WHERE material_id IN (OLD.id,NEW.id);
            INSERT OR IGNORE INTO mcp_read_dirty(kind, entity_id)
                SELECT 'relation', evidence_id FROM mcp25_claim_evidence WHERE clipping_id IN (OLD.id,NEW.id);
        END;
    """)
    claim_columns = {row[1] for row in db.execute("PRAGMA table_info(mcp25_claims)")}
    if "text_version" not in claim_columns:
        db.execute("ALTER TABLE mcp25_claims ADD COLUMN text_version INTEGER NOT NULL DEFAULT 1")
    evidence_columns = {row[1] for row in db.execute("PRAGMA table_info(mcp25_claim_evidence)")}
    if "claim_text_version" not in evidence_columns:
        db.execute("ALTER TABLE mcp25_claim_evidence ADD COLUMN claim_text_version INTEGER NOT NULL DEFAULT 1")


def _error(code: str, message: str, **details):
    raise mcp_workflows.WorkflowError(code, message, details or None)


def _iso(now):
    return mcp_workflows.iso(now)


def _require_full(actor):
    if not mcp24_access.has_full_access(actor):
        _error("forbidden", "完整专题进度需要 full-access 宿主。")


def _prompt(book, db, prompt_id, channel, actor):
    row = book._find_prompt_for_actor(db, prompt_id, channel, actor, {})
    if not mcp24_access.has_full_access(actor) and not mcp24_access.has_scope(actor, f"prompt:{prompt_id}"):
        _error("not_found", "未找到可访问的命题。", prompt_id=prompt_id)
    return row


def _material(book, db, kind, identifier, channel, actor):
    row = materials._row(db, kind, identifier)
    if row["prompt_id"]:
        book._find_prompt_for_actor(db, row["prompt_id"], channel, actor, {})
    return row


def _material_field(actor, kind, identifier, field_name):
    if not mcp24_access.has_full_access(actor) and not mcp24_access.has_scope(actor, f"material:{identifier}:{field_name}"):
        _error("forbidden", "当前宿主没有读取此素材字段的精确授权。", kind=kind, id=identifier, field=field_name)


def _check_material_selection(item, row, actor):
    kind, selection = item["kind"], item["selection"]
    payload = json.loads(row["data_json"])
    if item.get("expected_version") is not None and row["version"] != item["expected_version"]:
        _error("version_conflict", "素材已更正，请重新读取后再拟题。", kind=kind, id=row["id"],
               expected_version=item["expected_version"], actual_version=row["version"])
    if selection == "full":
        if any(key in item for key in ("field", "start", "length")):
            _error("invalid_params", "full selection 不接受 field、start 或 length。", field="selection")
        fields = {name: payload[name] for name in _MATERIAL_FIELDS[kind] if isinstance(payload.get(name), str) and payload[name]}
        if not fields:
            _error("needs_clarification", "这条素材没有可读取的文字内容。", kind=kind, id=row["id"])
        for field_name in fields:
            _material_field(actor, kind, row["id"], field_name)
        content = fields
        selection_details = {"selection": "full", "fields": list(fields)}
    elif selection == "link_title":
        if kind != "clipping" or not payload.get("title"):
            _error("invalid_params", "link_title 仅适用于有标题的剪藏。", kind=kind, id=row["id"])
        if any(key in item for key in ("start", "length")) or item.get("field", "title") != "title":
            _error("invalid_params", "link_title 只读取 title 字段。", field="field")
        _material_field(actor, kind, row["id"], "title")
        content = {"title": payload["title"]}
        selection_details = {"selection": "link_title", "field": "title", "start": 0, "length": len(payload["title"])}
    else:
        if "field" not in item or "start" not in item or "length" not in item:
            _error("invalid_params", "excerpt 需要 field、start 和 length。", fields=["field", "start", "length"])
        field_name, start, length = item["field"], item["start"], item["length"]
        if field_name not in _MATERIAL_FIELDS[kind] or not isinstance(payload.get(field_name), str):
            _error("invalid_params", "所选字段不属于此素材或没有文字值。", field=field_name, kind=kind)
        _material_field(actor, kind, row["id"], field_name)
        value = payload[field_name]
        end = min(len(value), start + length)
        if start >= len(value):
            _error("invalid_params", "片段起点超出素材字段长度。", field=field_name, start=start, field_length=len(value))
        content = {field_name: value[start:end]}
        selection_details = {"selection": "excerpt", "field": field_name, "start": start, "length": end - start}
    return payload, content, selection_details


def _draft_prompt_from_materials(book, db, params, channel, validate, now, actor):
    if len(params["materials"]) < 2:
        _error("invalid_params", "对照、承接或反例拟题至少需要两条素材。", field="materials")
    seen = set()
    selected = []
    for position, item in enumerate(params["materials"]):
        key = (item["kind"], item["id"])
        if key in seen:
            _error("invalid_params", "同一素材在对照题中只能出现一次。", field="materials", index=position + 1)
        seen.add(key)
        row = _material(book, db, item["kind"], item["id"], channel, actor)
        payload, content, selection_details = _check_material_selection(item, row, actor)
        selected.append({
            "kind": item["kind"], "id": row["id"], "version": row["version"], "position": position,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "direction": row["direction"], "topic_id": row["topic_id"],
            **selection_details, "content": content,
            "source": {key: payload[key] for key in ("title", "url", "source", "published_on")
                       if payload.get(key) and (mcp24_access.has_full_access(actor)
                           or mcp24_access.has_scope(actor, f"material:{row['id']}:{key}"))},
        })

    first = selected[0]
    direction = params.get("direction") or first["direction"] or ("social_topics" if first["kind"] == "clipping" else "self_exploration")
    topic = params.get("topic") or first["topic_id"]
    focus_parts = []
    for position, item in enumerate(selected, 1):
        text_value = "；".join(str(value) for value in item["content"].values())
        focus_parts.append(f"{position}:{text_value}")
    focus = f"{params['relation_type']}｜" + "｜".join(focus_parts)
    draft_args = {"direction": direction, "depth": params.get("depth", "medium"), "focus": focus[:160]}
    if topic is not None:
        topic_row = book._resolve_topic(db, topic)
        if topic_row is None or topic_row["direction"] != direction:
            if params.get("topic") is not None:
                _error("invalid_params", "拟题方向与素材主题不一致。", field="topic")
            draft_args.pop("topic", None)
        else:
            draft_args["topic"] = topic_row["id"]
    prompt = book._draft_prompt(db, draft_args, channel)["data"]["prompt"]
    similar = book._similar_prompt_rows(db, prompt["text"], channel, actor=actor,
                                         direction=direction, topic_id=draft_args.get("topic"),
                                         depth=draft_args["depth"])
    return {
        "prompt": prompt,
        "relation_type": params["relation_type"],
        "materials": selected,
        "deduplication": {
            "duplicate": any(item["duplicate"] for item in similar),
            "similar": any(item["similar"] for item in similar),
            "candidates": [{"prompt_id": item["row"]["id"], "similarity": item["similarity"],
                            "duplicate": item["duplicate"], "version": item["row"]["version"]}
                           for item in similar
                           if mcp24_access.has_full_access(actor)
                           or mcp24_access.has_scope(actor, f"prompt:{item['row']['id']}")],
            "candidate_count": len(similar),
        },
        "save_parameters": {
            "tool": "save_prompt_material_relations",
            "arguments": {
                "prompt": {"text": prompt["text"], "direction": prompt["direction"],
                           **({"topic": prompt["topic_id"]} if prompt.get("topic_id") is not None else {}),
                           **({"depth": prompt["depth"]} if prompt.get("depth") is not None else {})},
                "relation_type": params["relation_type"],
                "materials": [{
                    "kind": item["kind"], "id": item["id"], "expected_version": item["version"],
                    "selection": item["selection"],
                    **({"field":item["field"],"start":item["start"],"length":item["length"]}
                       if item["selection"] == "excerpt" else {}),
                    "role": "source", "position": item["position"],
                } for item in selected],
            },
            "requires_new_request_id": True,
        },
    }


def _check_prompt_target(book, db, params, channel, actor):
    is_existing = "prompt_id" in params or "expected_version" in params
    is_new = "prompt" in params
    if is_existing == is_new:
        _error("invalid_params", "请提供 prompt_id 与 expected_version，或一个新 prompt 对象。", fields=["prompt_id", "expected_version", "prompt"])
    if is_existing:
        if "prompt_id" not in params or "expected_version" not in params:
            _error("invalid_params", "已有题关联需要 prompt_id 和 expected_version。", fields=["prompt_id", "expected_version"])
        row = _prompt(book, db, params["prompt_id"], channel, actor)
        if row["version"] != params["expected_version"]:
            _error("version_conflict", "题目已更新，请先查回当前版本。", prompt_id=row["id"],
                   expected_version=params["expected_version"], actual_version=row["version"],
                   next_call={"tool": "get_prompt", "arguments": {"prompt_id": row["id"]}})
        return row, False
    return None, True


def _save_prompt_material_relations(book, db, params, channel, validate, now, actor):
    privacy_guard.check_action(db, "save_prompt_material_relations", params)
    prompt_row, create_prompt = _check_prompt_target(book, db, params, channel, actor)
    materials_in = params["materials"]
    if not materials_in:
        _error("invalid_params", "至少需要一条素材关系。", field="materials")
    seen, checked_materials = set(), []
    for index, item in enumerate(materials_in):
        key = (item["kind"], item["id"])
        if key in seen:
            _error("invalid_params", "同一素材在一个关系写入中只能出现一次。", field="materials", index=index + 1)
        seen.add(key)
        row = _material(book, db, item["kind"], item["id"], channel, actor)
        if row["version"] != item["expected_version"]:
            _error("version_conflict", "素材版本已变化，请重新读取后再保存关系。", kind=item["kind"], id=item["id"],
                   expected_version=item["expected_version"], actual_version=row["version"])
        if item["selection"] == "excerpt":
            if "field" not in item or "start" not in item or "length" not in item:
                _error("invalid_params", "excerpt 关系需要 field、start 和 length。", fields=["field", "start", "length"])
            if item["field"] not in _MATERIAL_FIELDS[item["kind"]]:
                _error("invalid_params", "所选片段字段不属于该素材。", field="field", kind=item["kind"])
            _material_field(actor, item["kind"], item["id"], item["field"])
        elif item["selection"] == "link_title" and item["kind"] != "clipping":
            _error("invalid_params", "link_title 只适用于剪藏。", field="selection")
        elif item["selection"] == "link_title":
            payload = json.loads(row["data_json"])
            if not payload.get("title"):
                _error("invalid_params", "剪藏没有可关联的标题。", field="selection")
            if any(key in item for key in ("start", "length")) or item.get("field", "title") != "title":
                _error("invalid_params", "link_title 只关联 title 字段。", field="field")
            _material_field(actor, item["kind"], item["id"], "title")
        elif any(key in item for key in ("field", "start", "length")) and item["selection"] == "full":
            _error("invalid_params", "full 关系不接受 field、start 或 length。", field="selection")
        if item["selection"] == "excerpt":
            payload = json.loads(row["data_json"])
            value = payload.get(item["field"])
            if not isinstance(value, str) or item["start"] >= len(value):
                _error("invalid_params", "所选片段范围不在当前素材字段内。", field=item["field"], start=item["start"])
        checked_materials.append((item, row))

    if create_prompt:
        prompt_data = params["prompt"]
        if prompt_data.get("parent_id"):
            parent = _prompt(book, db, prompt_data["parent_id"], channel, actor)
            if not mcp24_access.has_full_access(actor) and not mcp24_access.has_scope(actor, f"prompt:{parent['id']}"):
                _error("not_found", "未找到可访问的父题。", prompt_id=parent["id"])
        prompt_args = {"request_id": mcp_workflows._child_request(params["request_id"], 1),
                       "text": prompt_data["text"], "direction": prompt_data["direction"]}
        prompt_args.update({key: prompt_data[key] for key in ("topic", "depth", "parent_id") if key in prompt_data})
        validate("save_prompt", prompt_args, channel)
        saved = book._run(db, "save_prompt", prompt_args, channel, actor, now)["data"]
        prompt_row = db.execute("SELECT * FROM prompts WHERE id = ?", (saved["id"],)).fetchone()
    relation_results, changed = [], False
    timestamp = _iso(now)
    for item, material_row in checked_materials:
        current = db.execute("SELECT * FROM mcp25_prompt_material_relations WHERE prompt_id = ? AND material_id = ?",
                             (prompt_row["id"], material_row["id"])).fetchone()
        if current is None and item.get("expected_relation_version") is not None:
            _error("version_conflict", "关系已移除，请重新读取后再保存。", kind=item["kind"], id=item["id"],
                   expected_relation_version=item["expected_relation_version"], actual_relation_version=None)
        if current is not None and item.get("expected_relation_version") != current["version"]:
            _error("version_conflict", "关系已更正，请先查回当前版本。", relation_id=current["relation_id"],
                   expected_relation_version=item.get("expected_relation_version"), actual_relation_version=current["version"])
        values = {
            "relation_type": params["relation_type"], "role": item["role"], "position": item["position"],
            "description": item.get("description", current["description"] if current else ""),
            "selection": item["selection"], "field": item.get("field"),
            "start": item.get("start"), "length": item.get("length"),
            "material_version": material_row["version"],
        }
        relation_updated_at = current["updated_at"] if current else timestamp
        if current is None:
            relation_id = uuid.uuid4().hex
            db.execute("""INSERT INTO mcp25_prompt_material_relations(
                relation_id,prompt_id,material_id,material_kind,relation_type,role,position,description,
                selection,field,start,length,material_version,version,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                (relation_id,prompt_row["id"],material_row["id"],item["kind"],values["relation_type"],
                 values["role"],values["position"],values["description"],values["selection"],values["field"],
                 values["start"],values["length"],values["material_version"],timestamp,timestamp))
            version = 1
            before = None
            changed = True
            relation_updated_at = timestamp
        else:
            old = {key: current[key] for key in values}
            before = {**old, "version": current["version"]}
            if old == values:
                relation_id, version = current["relation_id"], current["version"]
            else:
                version = current["version"] + 1
                db.execute("""UPDATE mcp25_prompt_material_relations SET relation_type=?,role=?,position=?,description=?,
                    selection=?,field=?,start=?,length=?,material_version=?,version=?,updated_at=? WHERE relation_id=?""",
                    (values["relation_type"],values["role"],values["position"],values["description"],
                     values["selection"],values["field"],values["start"],values["length"],
                     values["material_version"],version,timestamp,current["relation_id"]))
                relation_id = current["relation_id"]
                changed = True
                relation_updated_at = timestamp
        relation_results.append({
            "relation_id": relation_id, "prompt_id": prompt_row["id"],
            "material": {"kind": item["kind"], "id": material_row["id"], "version": material_row["version"]},
            "relation_type": values["relation_type"], "role": values["role"], "position": values["position"],
            "description": values["description"], "selection": values["selection"],
            "field": values["field"], "start": values["start"], "length": values["length"],
            "before": before, "version": version, "created_at": current["created_at"] if current else timestamp,
            "updated_at": relation_updated_at,
        })
    if changed:
        prompt_version = prompt_row["version"] + 1
        db.execute("UPDATE prompts SET version=?,updated_at=? WHERE id=?", (prompt_version,timestamp,prompt_row["id"]))
    else:
        prompt_version = prompt_row["version"]
    return mcp_workflows.receipt(params, prompt_id=prompt_row["id"], prompt_created=create_prompt,
                                 relation_type=params["relation_type"], relations=relation_results,
                                 before_version=prompt_row["version"], version=prompt_version,
                                 committed=True, changed=changed,
                                 next_call={"tool":"read_entity_relations","arguments":{"kind":"prompt","object_id":prompt_row["id"]}})


def _remove_prompt_material_relation(book, db, params, channel, validate, now, actor):
    row = db.execute("SELECT * FROM mcp25_prompt_material_relations WHERE relation_id = ?", (params["relation_id"],)).fetchone()
    if row is None:
        _error("not_found", "未找到此题目素材关系。", relation_id=params["relation_id"])
    prompt_row = _prompt(book, db, row["prompt_id"], channel, actor)
    _material(book, db, row["material_kind"], row["material_id"], channel, actor)
    if row["version"] != params["expected_version"]:
        _error("version_conflict", "关系已更正，请先查回当前版本。", relation_id=row["relation_id"],
               expected_version=params["expected_version"], actual_version=row["version"])
    db.execute("DELETE FROM mcp25_prompt_material_relations WHERE relation_id = ?", (row["relation_id"],))
    prompt_version = prompt_row["version"] + 1
    db.execute("UPDATE prompts SET version=?,updated_at=? WHERE id=?", (prompt_version,_iso(now),prompt_row["id"]))
    return mcp_workflows.receipt(params, relation_id=row["relation_id"], prompt_id=row["prompt_id"],
                                 before={"version": row["version"], "relation_type": row["relation_type"],
                                         "role": row["role"], "position": row["position"],
                                         "description": row["description"]},
                                 prompt_version=prompt_version, removed=True, committed=True)


def _claim_evidence_write(book, db, params, channel, validate, now, actor):
    prompt = _prompt(book, db, params["prompt_id"], channel, actor)
    if prompt["version"] != params["expected_prompt_version"]:
        _error("version_conflict", "题目已更新，请先读取当前证据和版本。", prompt_id=prompt["id"],
               expected_version=params["expected_prompt_version"], actual_version=prompt["version"],
               next_call={"tool":"read_prompt_evidence","arguments":{"prompt_id":prompt["id"]}})
    if not params["claims"]:
        _error("invalid_params", "至少需要一条要记录的说法。", field="claims")
    privacy_guard.check_action(db, "write_claim_evidence", params)
    timestamp = _iso(now)
    claim_results, changed = [], False
    for claim_input in params["claims"]:
        text_value = claim_input["text"].strip()
        if not text_value:
            _error("invalid_params", "待核对的说法不能为空。", field="claims.text")
        if claim_input.get("claim_id"):
            claim_row = db.execute("SELECT * FROM mcp25_claims WHERE claim_id=? AND prompt_id=?",
                                   (claim_input["claim_id"],prompt["id"])).fetchone()
            if claim_row is None:
                _error("not_found", "未找到此题目的说法。", claim_id=claim_input["claim_id"])
            if claim_input.get("expected_version") != claim_row["version"]:
                _error("version_conflict", "说法已更正，请先读取当前版本。", claim_id=claim_row["claim_id"],
                       expected_version=claim_input.get("expected_version"), actual_version=claim_row["version"])
            duplicate = db.execute("SELECT claim_id FROM mcp25_claims WHERE prompt_id=? AND text=? AND claim_id<>?",
                                   (prompt["id"],text_value,claim_row["claim_id"])).fetchone()
            if duplicate:
                _error("invalid_params", "同一题目不能保存两条完全相同的说法。", claim_id=duplicate["claim_id"])
            claim_id = claim_row["claim_id"]
            claim_version = claim_row["version"]
            claim_text_changed = claim_row["text"] != text_value
        else:
            claim_row = db.execute("SELECT * FROM mcp25_claims WHERE prompt_id=? AND text=?", (prompt["id"],text_value)).fetchone()
            if claim_row:
                claim_id, claim_version = claim_row["claim_id"], claim_row["version"]
                claim_text_changed = False
            else:
                claim_id, claim_version = uuid.uuid4().hex, 1
                claim_text_changed = True
                db.execute("INSERT INTO mcp25_claims(claim_id,prompt_id,text,version,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                           (claim_id,prompt["id"],text_value,claim_version,timestamp,timestamp))
        claim_text_version = 1 if claim_row is None else claim_row["text_version"] + int(claim_text_changed)
        claim_changed = claim_text_changed
        evidence_results = []
        for evidence_input in claim_input.get("evidence", []):
            clipping = _material(book, db, "clipping", evidence_input["clipping_id"], channel, actor)
            if clipping["version"] != evidence_input["material_version"]:
                _error("version_conflict", "剪藏来源已更正，请重新读取后再记证据。", clipping_id=clipping["id"],
                       expected_version=evidence_input["material_version"], actual_version=clipping["version"])
            field_name = evidence_input.get("field")
            if field_name is not None:
                if field_name not in _EVIDENCE_FIELDS:
                    _error("invalid_params", "证据字段不属于剪藏。", field="field")
                _material_field(actor, "clipping", clipping["id"], field_name)
            support_type = evidence_input["support_type"]
            location = evidence_input.get("location", "").strip()
            excerpt = evidence_input.get("excerpt", "").strip()
            conclusion = evidence_input.get("conclusion", "").strip()
            if support_type != "unverified" and (field_name is None or not location or not excerpt):
                _error("invalid_params", "支持、反对或间接证据需要字段、明确位置和原文片段。", fields=["field", "location", "excerpt"])
            if excerpt and field_name is None:
                _error("invalid_params", "原文片段需要对应剪藏字段。", field="field")
            if evidence_input.get("evidence_id"):
                evidence_row = db.execute("SELECT * FROM mcp25_claim_evidence WHERE evidence_id=? AND claim_id=?",
                                          (evidence_input["evidence_id"],claim_id)).fetchone()
                if evidence_row is None:
                    _error("not_found", "未找到此说法下的证据记录。", evidence_id=evidence_input["evidence_id"])
                if evidence_input.get("expected_version") != evidence_row["version"]:
                    _error("version_conflict", "证据记录已更正，请先读取当前版本。", evidence_id=evidence_row["evidence_id"],
                           expected_version=evidence_input.get("expected_version"), actual_version=evidence_row["version"])
            else:
                evidence_row = db.execute("""SELECT * FROM mcp25_claim_evidence WHERE claim_id=? AND clipping_id=?
                    AND field IS ? AND location=?""", (claim_id,clipping["id"],field_name,location)).fetchone()
                if evidence_row and evidence_input.get("expected_version") != evidence_row["version"]:
                    _error("version_conflict", "此来源位置已有证据记录，请先读取后再更正。", evidence_id=evidence_row["evidence_id"],
                           expected_version=evidence_input.get("expected_version"), actual_version=evidence_row["version"])
                if evidence_row is None and evidence_input.get("expected_version") is not None:
                    _error("version_conflict", "指定版本的证据记录已不存在。", expected_version=evidence_input["expected_version"])
            if evidence_row is None:
                evidence_id, evidence_version, before = uuid.uuid4().hex, 1, None
                db.execute("""INSERT INTO mcp25_claim_evidence(
                    evidence_id,claim_id,prompt_id,clipping_id,material_version,field,support_type,location,excerpt,conclusion,
                    claim_text_version,checked_at,version,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (evidence_id,claim_id,prompt["id"],clipping["id"],clipping["version"],field_name,support_type,location,excerpt,
                     conclusion,claim_text_version,timestamp,timestamp,timestamp))
                claim_changed = True
            else:
                evidence_id = evidence_row["evidence_id"]
                old_values = {key: evidence_row[key] for key in ("clipping_id","material_version","claim_text_version","field","support_type","location","excerpt","conclusion")}
                new_values = {"clipping_id":clipping["id"],"material_version":clipping["version"],"field":field_name,"support_type":support_type,
                              "claim_text_version":claim_text_version,"location":location,"excerpt":excerpt,"conclusion":conclusion}
                before = {**old_values,"version":evidence_row["version"],"checked_at":evidence_row["checked_at"]}
                if old_values == new_values and evidence_row["checked_at"] == timestamp:
                    evidence_version = evidence_row["version"]
                else:
                    evidence_version = evidence_row["version"] + 1
                    db.execute("""UPDATE mcp25_claim_evidence SET clipping_id=?,material_version=?,claim_text_version=?,field=?,support_type=?,location=?,
                        excerpt=?,conclusion=?,checked_at=?,version=?,updated_at=? WHERE evidence_id=?""",
                        (clipping["id"],clipping["version"],claim_text_version,field_name,support_type,location,excerpt,conclusion,
                         timestamp,evidence_version,timestamp,evidence_id))
                    claim_changed = True
            evidence_results.append({
                "evidence_id": evidence_id, "material": {"kind":"clipping","id":clipping["id"],"version":clipping["version"]},
                "support_type": support_type, "field": field_name, "location": location or None,
                "excerpt": excerpt or None, "conclusion": conclusion or None,
                "checked_at": timestamp, "before": before, "source_version": clipping["version"],
                "claim_text_version": claim_text_version, "version": evidence_version,
            })
        if claim_changed:
            claim_version = 1 if claim_row is None else claim_row["version"] + 1
            db.execute("UPDATE mcp25_claims SET text=?,version=?,text_version=?,updated_at=? WHERE claim_id=?",
                       (text_value,claim_version,claim_text_version,timestamp,claim_id))
            changed = True
        claim_results.append({"claim_id":claim_id,"text":text_value,"version":claim_version,
                              "text_version":claim_text_version,"evidence":evidence_results})
    if changed:
        prompt_version = prompt["version"] + 1
        db.execute("UPDATE prompts SET version=?,updated_at=? WHERE id=?", (prompt_version,timestamp,prompt["id"]))
    else:
        prompt_version = prompt["version"]
    return mcp_workflows.receipt(params, prompt_id=prompt["id"], before_version=prompt["version"],
                                 version=prompt_version, claims=claim_results, checked_at=timestamp,
                                 verification="recorded_scope_not_independent_fact_check", committed=True,
                                 changed=changed,
                                 next_call={"tool":"read_prompt_evidence","arguments":{"prompt_id":prompt["id"]}})


def _material_visible_for_relation(book, db, kind, identifier, channel, actor):
    raw = db.execute("SELECT * FROM materials WHERE id=? AND collection=?", (identifier,kind)).fetchone()
    if raw is None:
        return None
    if raw["prompt_id"]:
        prompt_visibility = db.execute("SELECT visibility FROM prompts WHERE id=?", (raw["prompt_id"],)).fetchone()
        if prompt_visibility is not None and prompt_visibility["visibility"] == "private" and not mcp24_access.has_full_access(actor):
            return None
        book._find_prompt_for_actor(db, raw["prompt_id"], channel, actor, {})
    return raw


def _visible_prompt_for_relation(book, db, prompt_id, channel, actor):
    raw = db.execute("SELECT visibility FROM prompts WHERE id=?", (prompt_id,)).fetchone()
    if raw is None or (raw["visibility"] == "private" and not mcp24_access.has_full_access(actor)):
        return None
    row = book._find_prompt_for_actor(db, prompt_id, channel, actor, {})
    if not mcp24_access.has_full_access(actor) and not mcp24_access.has_scope(actor, f"prompt:{prompt_id}"):
        return None
    return row


def _relation_item(book, db, row, channel, actor, *, direction):
    prompt = _visible_prompt_for_relation(book, db, row["prompt_id"], channel, actor)
    if prompt is None:
        return None
    material_row = _material_visible_for_relation(book, db, row["material_kind"], row["material_id"], channel, actor)
    if material_row is None:
        return None
    if row["field"] and not mcp24_access.has_full_access(actor) and not mcp24_access.has_scope(actor, f"material:{material_row['id']}:{row['field']}"):
        return None
    return {
        "relation_id": row["relation_id"],
        "source": {"kind":"prompt","id":row["prompt_id"],"version":prompt["version"]},
        "target": {"kind":row["material_kind"],"id":row["material_id"],"version":material_row["version"]},
        "relation_type": row["relation_type"], "role": row["role"], "position": row["position"],
        "description": row["description"], "selection": row["selection"], "field": row["field"],
        "start": row["start"], "length": row["length"], "source_version": row["material_version"],
        "direction": direction, "created_at": row["created_at"], "updated_at": row["updated_at"],
        "version": row["version"],
    }


def _legacy_relation(prompt_id, text_value, clipping_id, material_version, *, direction):
    stable = hashlib.sha256(f"{prompt_id}\0{text_value}\0{clipping_id}".encode("utf-8")).hexdigest()[:32]
    return {
        "relation_id": f"legacy:{stable}", "source": {"kind":"prompt","id":prompt_id},
        "target": {"kind":"clipping","id":clipping_id,"version":material_version},
        "relation_type":"legacy_claim_source", "role":"unverified", "position":None,
        "description":"旧版数据只保存了说法与来源的关联，没有记录具体引文或核对结论。",
        "claim":text_value, "selection":"full", "field":None, "start":None, "length":None,
        "source_version":None, "direction":direction, "created_at":None, "updated_at":None,
        "version":0, "legacy":True,
    }


def read_entity_relations(book, db, kind, object_id, channel, actor, *, limit=20, offset=0):
    if kind == "prompt":
        prompt = _prompt(book, db, object_id, channel, actor)
        rows = db.execute("""SELECT * FROM mcp25_prompt_material_relations WHERE prompt_id=?
            ORDER BY position,created_at,relation_id""", (object_id,)).fetchall()
        result = [item for row in rows if (item := _relation_item(book, db, row, channel, actor, direction="outgoing")) is not None]
        for legacy in db.execute("SELECT text,clipping_id FROM mcp_claim_sources WHERE prompt_id=? ORDER BY text,clipping_id", (object_id,)):
            material = _material_visible_for_relation(book, db, "clipping", legacy["clipping_id"], channel, actor)
            if material is not None:
                result.append(_legacy_relation(object_id, legacy["text"], legacy["clipping_id"], material["version"], direction="outgoing"))
        for evidence in db.execute("""SELECT e.*,c.text_version AS current_claim_text_version
            FROM mcp25_claim_evidence e JOIN mcp25_claims c ON c.claim_id=e.claim_id
            WHERE e.prompt_id=? ORDER BY e.updated_at,e.evidence_id""", (object_id,)):
            material = _material_visible_for_relation(book, db, "clipping", evidence["clipping_id"], channel, actor)
            if material is None:
                continue
            if evidence["field"] and not mcp24_access.has_full_access(actor) and not mcp24_access.has_scope(actor, f"material:{material['id']}:{evidence['field']}"):
                continue
            if evidence["excerpt"] and not evidence["field"] and not mcp24_access.has_full_access(actor):
                continue
            stale = evidence["claim_text_version"] != evidence["current_claim_text_version"]
            result.append({
                "relation_id":evidence["evidence_id"], "source":{"kind":"prompt","id":object_id,"version":prompt["version"]},
                "target":{"kind":"clipping","id":material["id"],"version":material["version"]},
                "relation_type":"claim_evidence", "role":"unverified" if stale else evidence["support_type"], "position":None,
                "description":evidence["conclusion"], "field":evidence["field"], "location":evidence["location"],
                "excerpt":evidence["excerpt"], "checked_at":evidence["checked_at"],
                "source_version":evidence["material_version"], "claim_text_version":evidence["claim_text_version"],
                **({"recorded_support_type":evidence["support_type"]} if stale else {}),
                "direction":"outgoing", "created_at":evidence["created_at"],"updated_at":evidence["updated_at"],
                "version":evidence["version"],
            })
    elif kind in _MATERIAL_FIELDS:
        material = _material_visible_for_relation(book, db, kind, object_id, channel, actor)
        if material is None:
            _error("not_found", "未找到可访问的素材。", kind=kind, id=object_id)
        rows = db.execute("""SELECT * FROM mcp25_prompt_material_relations WHERE material_id=?
            ORDER BY position,created_at,relation_id""", (object_id,)).fetchall()
        result = [item for row in rows if (item := _relation_item(book, db, row, channel, actor, direction="incoming")) is not None]
        if kind == "clipping":
            for legacy in db.execute("SELECT prompt_id,text FROM mcp_claim_sources WHERE clipping_id=? ORDER BY prompt_id,text", (object_id,)):
                prompt = _visible_prompt_for_relation(book, db, legacy["prompt_id"], channel, actor)
                if prompt is None:
                    continue
                result.append(_legacy_relation(legacy["prompt_id"], legacy["text"], object_id, material["version"], direction="incoming"))
            for evidence in db.execute("""SELECT e.*,c.text_version AS current_claim_text_version
                FROM mcp25_claim_evidence e JOIN mcp25_claims c ON c.claim_id=e.claim_id
                WHERE e.clipping_id=? ORDER BY e.updated_at,e.evidence_id""", (object_id,)):
                prompt = _visible_prompt_for_relation(book, db, evidence["prompt_id"], channel, actor)
                if prompt is None:
                    continue
                if evidence["field"] and not mcp24_access.has_full_access(actor) and not mcp24_access.has_scope(actor, f"material:{object_id}:{evidence['field']}"):
                    continue
                if evidence["excerpt"] and not evidence["field"] and not mcp24_access.has_full_access(actor):
                    continue
                stale = evidence["claim_text_version"] != evidence["current_claim_text_version"]
                result.append({
                    "relation_id":evidence["evidence_id"], "source":{"kind":"prompt","id":evidence["prompt_id"],"version":prompt["version"]},
                    "target":{"kind":"clipping","id":object_id,"version":material["version"]},
                    "relation_type":"claim_evidence", "role":"unverified" if stale else evidence["support_type"], "position":None,
                    "description":evidence["conclusion"], "field":evidence["field"],"location":evidence["location"],
                    "excerpt":evidence["excerpt"],"checked_at":evidence["checked_at"],
                    "source_version":evidence["material_version"], "claim_text_version":evidence["claim_text_version"],
                    **({"recorded_support_type":evidence["support_type"]} if stale else {}),
                    "direction":"incoming","created_at":evidence["created_at"],"updated_at":evidence["updated_at"],
                    "version":evidence["version"],
                })
    else:
        _error("invalid_params", "kind 必须是 prompt 或具体素材类型。", field="kind")
    total = len(result)
    items = result[offset:offset + limit]
    return {"items":items,"total":total,"next_offset":offset + len(items) if offset + len(items) < total else None}


def read_prompt_evidence(book, db, prompt_id, channel, actor, *, limit=50, offset=0):
    _prompt(book, db, prompt_id, channel, actor)
    rows = db.execute("SELECT * FROM mcp25_claims WHERE prompt_id=? ORDER BY updated_at DESC,claim_id", (prompt_id,)).fetchall()
    claims = {row["claim_id"]: {
        "claim_id":row["claim_id"],"text":row["text"],"version":row["version"],"text_version":row["text_version"],
        "items":[],"_sort":(row["updated_at"],row["claim_id"]),
    } for row in rows}
    claim_by_text = {row["text"]: row["claim_id"] for row in rows}
    legacy_rows = db.execute("SELECT text,clipping_id FROM mcp_claim_sources WHERE prompt_id=? ORDER BY text,clipping_id", (prompt_id,)).fetchall()
    for row in legacy_rows:
        claim_id = claim_by_text.get(row["text"])
        if claim_id is None:
            claim_id = "legacy:" + hashlib.sha256(f"{prompt_id}\0{row['text']}".encode("utf-8")).hexdigest()[:32]
            claims.setdefault(claim_id, {"claim_id":claim_id,"text":row["text"],"version":0,"items":[],"_sort":("",claim_id),"legacy":True})
        material = _material_visible_for_relation(book, db, "clipping", row["clipping_id"], channel, actor)
        if material is None:
            continue
        claims[claim_id]["items"].append({
            "evidence_id":"legacy:" + hashlib.sha256(f"{prompt_id}\0{row['text']}\0{row['clipping_id']}".encode("utf-8")).hexdigest()[:32],
            "material":{"kind":"clipping","id":row["clipping_id"],"version":material["version"]},
            "source_version":None,
            "support_type":"unverified","field":None,"location":None,"excerpt":None,
            "conclusion":"旧版记录只说明来源已关联，尚未核对该来源支持什么。",
            "checked_at":None,"version":0,"legacy":True,
        })
    for row in db.execute("SELECT * FROM mcp25_claim_evidence WHERE prompt_id=? ORDER BY updated_at DESC,evidence_id", (prompt_id,)):
        material = _material_visible_for_relation(book, db, "clipping", row["clipping_id"], channel, actor)
        if material is None:
            continue
        if row["field"] and not mcp24_access.has_full_access(actor) and not mcp24_access.has_scope(actor, f"material:{material['id']}:{row['field']}"):
            continue
        if row["excerpt"] and not row["field"] and not mcp24_access.has_full_access(actor):
            continue
        claim = claims[row["claim_id"]]
        stale = row["claim_text_version"] != claim["text_version"]
        claim["items"].append({
            "evidence_id":row["evidence_id"],
            "material":{"kind":"clipping","id":row["clipping_id"],"version":material["version"]},
            "source_version":row["material_version"],
            "claim_text_version":row["claim_text_version"],
            "support_type":"unverified" if stale else row["support_type"],
            **({"recorded_support_type":row["support_type"]} if stale else {}),
            "field":row["field"],
            "location":row["location"] or None,"excerpt":row["excerpt"] or None,
            "conclusion":row["conclusion"] or None,"checked_at":row["checked_at"],"version":row["version"],
        })
    ordered = sorted(claims.values(), key=lambda item: (item["_sort"][0], item["_sort"][1]), reverse=True)
    total = len(ordered)
    page = ordered[offset:offset + limit]
    for item in page:
        item.pop("_sort")
    return {"claims":page,"total":total,"next_offset":offset + len(page) if offset + len(page) < total else None}


def _unavailable_project_member(kind, identifier, position):
    return {"kind":kind,"id":identifier,"position":position,"status":"unavailable",
            "reason":"member_not_found","pending":[{"kind":"unavailable_member","reason":"member_not_found"}]}


def _project_member(book, db, ref, channel, actor, position):
    kind, identifier = ref["kind"], ref["id"]
    if kind == "prompt":
        if not db.execute("SELECT 1 FROM prompts WHERE id=?", (identifier,)).fetchone():
            return _unavailable_project_member(kind, identifier, position)
        row = _prompt(book, db, identifier, channel, actor)
        member = {"kind":"prompt","id":identifier,"position":position,"version":row["version"],
                  "text":row["text"],"direction":row["direction"],"topic_id":row["topic_id"],
                  "state":row["state"],"snoozed_until":row["snoozed_until"],"created_at":row["created_at"],"updated_at":row["updated_at"]}
        member["relations"] = read_entity_relations(book, db, "prompt", identifier, channel, actor, limit=2**31 - 1)["items"]
        member["claims"] = read_prompt_evidence(book, db, identifier, channel, actor, limit=2**31 - 1)["claims"]
        member["checkpoints"] = [dict(item) for item in db.execute("""SELECT id,source_conversation_id_public,prompt_version,
            cue,unanswered_question,progress,guide_step,created_at FROM mcp_thought_checkpoints WHERE prompt_id=?
            ORDER BY created_at DESC,id""", (identifier,))]
        member["answered"] = [{"kind":"writing_state","state":"written","prompt_id":identifier}] if row["state"] == "written" else []
        member["pending"] = []
        for checkpoint in member["checkpoints"]:
            if checkpoint["unanswered_question"]:
                member["pending"].append({"kind":"unanswered_question","checkpoint_id":checkpoint["id"],
                                          "text":checkpoint["unanswered_question"],"created_at":checkpoint["created_at"]})
        for claim in member["claims"]:
            verified = [item for item in claim["items"] if item["support_type"] in {"supports","opposes","indirect"}]
            if not verified:
                has_stale_evidence = any("recorded_support_type" in item for item in claim["items"])
                gap = {"kind":"claim","prompt_id":identifier,"claim_id":claim["claim_id"],"text":claim["text"],
                       "reason":"已有证据对应旧版说法，尚未按当前说法重新核对。" if has_stale_evidence else "没有已记录的支持、反对或间接证据。"}
                member["pending"].append(gap)
            else:
                member["answered"].append({"kind":"claim_evidence","claim_id":claim["claim_id"],
                                           "text":claim["text"],"evidence_ids":[item["evidence_id"] for item in verified]})
        if row["state"] != "written":
            member["pending"].append({"kind":"writing_status","state":row["state"],"prompt_id":identifier})
        return member
    if kind in _MATERIAL_FIELDS:
        if not db.execute("SELECT 1 FROM materials WHERE id=? AND collection=?", (identifier,kind)).fetchone():
            return _unavailable_project_member(kind, identifier, position)
        row = _material(book, db, kind, identifier, channel, actor)
        item = materials._view(db, row, content=True, channel=channel)
        member = {"kind":kind,"id":identifier,"position":position,"record":item}
        member["relations"] = read_entity_relations(book, db, kind, identifier, channel, actor, limit=2**31 - 1)["items"]
        return member
    _error("invalid_params", "专题成员类型无效。", kind=kind, id=identifier)


def read_project_progress(book, db, project_id, channel, actor, *, limit=50, offset=0):
    _require_full(actor)
    row = db.execute("SELECT * FROM mcp_topic_projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        _error("not_found", "未找到此专题。", project_id=project_id)
    refs = json.loads(row["items_json"])
    all_members = [_project_member(book, db, ref, channel, actor, position) for position, ref in enumerate(refs)]
    gaps = []
    next_clue = None
    for member in all_members:
        for pending in member.get("pending", []):
            gap = {"member_kind":member["kind"],"member_id":member["id"],**pending}
            gaps.append(gap)
            if next_clue is None:
                if pending["kind"] == "unanswered_question":
                    next_clue = {
                        "kind":"saved_question","text":pending["text"],
                        "basis":{"kind":"checkpoint","id":pending["checkpoint_id"],"prompt_id":member["id"],"created_at":pending["created_at"]},
                        "next_call":{"tool":"get_thought_context","arguments":{"prompt_id":member["id"],"detail":"full"}},
                    }
                elif pending["kind"] == "claim":
                    next_clue = {
                        "kind":"claim_to_check","text":pending["text"],
                        "basis":{"kind":"claim","id":pending["claim_id"],"prompt_id":member["id"],"reason":pending["reason"]},
                        "next_call":{"tool":"read_prompt_evidence","arguments":{"prompt_id":member["id"]}},
                    }
                elif pending["kind"] == "writing_status":
                    next_clue = {
                        "kind":"unfinished_prompt","text":member["text"],
                        "basis":{"kind":"prompt","id":member["id"],"state":pending["state"],"version":member["version"]},
                        "next_call":{"tool":"get_prompt","arguments":{"prompt_id":member["id"]}},
                    }
    if next_clue is None and all_members:
        first = next((member for member in all_members if member.get("status") != "unavailable"), None)
        if first is not None and first["kind"] == "prompt":
            next_clue = {"kind":"saved_prompt","text":first["text"],
                         "basis":{"kind":"prompt","id":first["id"],"version":first["version"]},
                         "next_call":{"tool":"get_prompt","arguments":{"prompt_id":first["id"]}}}
        elif first is not None:
            record = first["record"]
            text_value = record.get("title") or record.get("text") or record.get("question") or record.get("note")
            next_clue = {"kind":"saved_material","text":text_value,
                         "basis":{"kind":first["kind"],"id":first["id"],"version":record["version"]},
                         "next_call":{"tool":"get_full_record","arguments":{"kind":first["kind"],"id":first["id"]}}}
    total = len(all_members)
    page = all_members[offset:offset + limit]
    return {
        "project":{"id":row["id"],"title":row["title"],"version":row["version"],"updated_at":row["updated_at"],
                   "total_members":total,"next_offset":offset + len(page) if offset + len(page) < total else None},
        "members":page,"gaps":gaps,"next_clue":next_clue,"total":total,
        "next_offset":offset + len(page) if offset + len(page) < total else None,
    }


def _read_entity_relations(book, db, params, channel, validate, now, actor):
    return read_entity_relations(book, db, params["kind"], params["object_id"], channel, actor,
                                 limit=params.get("limit", 20), offset=params.get("offset", 0))


def _read_prompt_evidence(book, db, params, channel, validate, now, actor):
    return read_prompt_evidence(book, db, params["prompt_id"], channel, actor,
                                limit=params.get("limit", 50), offset=params.get("offset", 0))


def _get_topic_project_progress(book, db, params, channel, validate, now, actor):
    return read_project_progress(book, db, params["project_id"], channel, actor,
                                 limit=params.get("limit", 50), offset=params.get("offset", 0))


def _doc(*, relation_id, field, text, source_kind, source_id, target_kind, target_id,
         visibility, direction, topic_id, version, updated_at):
    return {"relation_id":relation_id,"field":field,"text":text,"source_kind":source_kind,"source_id":source_id,
            "target_kind":target_kind,"target_id":target_id,"visibility":visibility,"direction":direction,
            "topic_id":topic_id,"version":version,"updated_at":updated_at}


def _doc_visibility(db, prompt_id, material_id=None):
    prompt = db.execute("SELECT visibility FROM prompts WHERE id=?", (prompt_id,)).fetchone()
    if prompt is None or prompt["visibility"] == "private":
        return "private"
    if material_id:
        material = db.execute("SELECT prompt_id FROM materials WHERE id=?", (material_id,)).fetchone()
        if material and material["prompt_id"]:
            parent = db.execute("SELECT visibility FROM prompts WHERE id=?", (material["prompt_id"],)).fetchone()
            if parent is None or parent["visibility"] == "private":
                return "private"
    return "shared"


def _claim_docs(db, prompt_id):
    prompt = db.execute("SELECT direction,topic_id,visibility FROM prompts WHERE id=?", (prompt_id,)).fetchone()
    if prompt is None:
        return []
    visibility = "private" if prompt["visibility"] == "private" else "shared"
    docs = []
    for claim in db.execute("SELECT * FROM mcp25_claims WHERE prompt_id=? ORDER BY claim_id", (prompt_id,)):
        docs.append(_doc(relation_id=claim["claim_id"],field="mcp25_claims.text",text=claim["text"],
                         source_kind="prompt",source_id=prompt_id,target_kind=None,target_id=None,
                         visibility=visibility,direction=prompt["direction"],topic_id=prompt["topic_id"],
                         version=claim["version"],updated_at=claim["updated_at"]))
    for evidence in db.execute("""SELECT e.*,c.text_version AS current_claim_text_version
        FROM mcp25_claim_evidence e JOIN mcp25_claims c ON c.claim_id=e.claim_id
        WHERE e.prompt_id=? ORDER BY e.evidence_id""", (prompt_id,)):
        stale = evidence["claim_text_version"] != evidence["current_claim_text_version"]
        support_label = f"unverified (recorded {evidence['support_type']})" if stale else evidence["support_type"]
        for field_name in ("location","excerpt","conclusion"):
            value = evidence[field_name]
            if not value:
                continue
            docs.append(_doc(relation_id=evidence["evidence_id"],field=f"mcp25_claim_evidence.{field_name}",
                             text=f"{support_label}: {value}",source_kind="prompt",source_id=prompt_id,
                             target_kind="clipping",target_id=evidence["clipping_id"],
                             visibility=_doc_visibility(db,prompt_id,evidence["clipping_id"]),
                             direction=prompt["direction"],topic_id=prompt["topic_id"],version=evidence["version"],
                             updated_at=evidence["updated_at"]))
    for legacy in db.execute("SELECT text,clipping_id FROM mcp_claim_sources WHERE prompt_id=? ORDER BY text,clipping_id", (prompt_id,)):
        relation_id = "legacy:" + hashlib.sha256(f"{prompt_id}\0{legacy['text']}\0{legacy['clipping_id']}".encode("utf-8")).hexdigest()[:32]
        docs.append(_doc(relation_id=relation_id,field="mcp_claim_sources.text",
                         text=f"unverified: {legacy['text']}",source_kind="prompt",source_id=prompt_id,
                         target_kind="clipping",target_id=legacy["clipping_id"],
                         visibility=_doc_visibility(db,prompt_id,legacy["clipping_id"]),
                         direction=prompt["direction"],topic_id=prompt["topic_id"],version=0,updated_at=""))
    return docs


def searchable_relation_docs(db, kind, identifier):
    if kind == "relation":
        relation = db.execute("SELECT * FROM mcp25_prompt_material_relations WHERE relation_id=?", (identifier,)).fetchone()
        if relation:
            prompt = db.execute("SELECT direction,topic_id,visibility FROM prompts WHERE id=?", (relation["prompt_id"],)).fetchone()
            if prompt:
                return [_doc(relation_id=relation["relation_id"],field="mcp25_prompt_material_relations.description",
                             text=" ".join(value for value in (relation["relation_type"],relation["role"],relation["description"]) if value),
                             source_kind="prompt",source_id=relation["prompt_id"],target_kind=relation["material_kind"],
                             target_id=relation["material_id"],visibility=_doc_visibility(db,relation["prompt_id"],relation["material_id"]),
                             direction=prompt["direction"],topic_id=prompt["topic_id"],version=relation["version"],updated_at=relation["updated_at"])]
        claim = db.execute("SELECT prompt_id FROM mcp25_claims WHERE claim_id=?", (identifier,)).fetchone()
        if claim:
            return [doc for doc in _claim_docs(db, claim["prompt_id"]) if doc["relation_id"] == identifier]
        evidence = db.execute("SELECT prompt_id FROM mcp25_claim_evidence WHERE evidence_id=?", (identifier,)).fetchone()
        if evidence:
            return [doc for doc in _claim_docs(db, evidence["prompt_id"]) if doc["relation_id"] == identifier]
        for claim_source in db.execute("SELECT DISTINCT prompt_id FROM mcp_claim_sources"):
            docs = [doc for doc in _claim_docs(db, claim_source["prompt_id"]) if doc["relation_id"] == identifier]
            if docs:
                return docs
        return []
    if kind == "prompt":
        rows = db.execute("SELECT * FROM mcp25_prompt_material_relations WHERE prompt_id=? ORDER BY relation_id", (identifier,)).fetchall()
        prompt = db.execute("SELECT direction,topic_id,visibility FROM prompts WHERE id=?", (identifier,)).fetchone()
        docs = []
        if prompt:
            for row in rows:
                docs.extend(searchable_relation_docs(db, "relation", row["relation_id"]))
            docs.extend(_claim_docs(db, identifier))
        return docs
    if kind in {"material","inspiration","clipping","resume"}:
        material = db.execute("SELECT collection FROM materials WHERE id=?", (identifier,)).fetchone()
        if material is None or (kind != "material" and material["collection"] != kind):
            return []
        rows = db.execute("SELECT relation_id FROM mcp25_prompt_material_relations WHERE material_id=? ORDER BY relation_id", (identifier,)).fetchall()
        docs = [doc for row in rows for doc in searchable_relation_docs(db,"relation",row["relation_id"])]
        for evidence in db.execute("SELECT evidence_id FROM mcp25_claim_evidence WHERE clipping_id=? ORDER BY evidence_id", (identifier,)):
            docs.extend(searchable_relation_docs(db,"relation",evidence["evidence_id"]))
        for row in db.execute("SELECT DISTINCT prompt_id FROM mcp_claim_sources WHERE clipping_id=?", (identifier,)):
            docs.extend(_claim_docs(db,row["prompt_id"]))
        return [doc for doc in docs if doc["target_id"] == identifier or doc["source_id"] == identifier]
    if kind == "topic_project":
        project = db.execute("SELECT * FROM mcp_topic_projects WHERE id=?", (identifier,)).fetchone()
        if project is None:
            return []
        return [_doc(relation_id=identifier,field="mcp_topic_projects.title",text=project["title"],
                     source_kind="topic_project",source_id=identifier,target_kind=None,target_id=None,
                     visibility="private",direction=None,topic_id=None,version=project["version"],updated_at=project["updated_at"])]
    return []


HANDLERS = {
    "draft_prompt_from_materials": _draft_prompt_from_materials,
    "save_prompt_material_relations": _save_prompt_material_relations,
    "remove_prompt_material_relation": _remove_prompt_material_relation,
    "read_entity_relations": _read_entity_relations,
    "write_claim_evidence": _claim_evidence_write,
    "read_prompt_evidence": _read_prompt_evidence,
    "get_topic_project_progress": _get_topic_project_progress,
}
WRITE_ACTIONS = frozenset({"save_prompt_material_relations", "remove_prompt_material_relation", "write_claim_evidence"})


def handle(book, db, action, params, channel, validate, now, actor):
    return HANDLERS[action](book, db, params, channel, validate, now, actor)
