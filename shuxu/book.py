"""SQLite-backed local prompt book shared by the web page and MCP bridge."""

from __future__ import annotations

import difflib
import copy
import hashlib
import json
import math
import random
import re
import sqlite3
import unicodedata
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import (
    materials,
    mcp24_access,
    mcp24_contract,
    mcp24_ops,
    mcp24_reads,
    mcp24_transport,
    mcp_files,
    mcp25_history,
    mcp25_read,
    mcp25_relations,
    mcp25_thought,
    mcp_workflows,
    privacy_guard,
    seed,
)


_DIRECTION_IDS = tuple(item[0] for item in seed.DIRECTIONS)
_DEPTHS = ("light", "medium", "deep")
_STATES = ("want_to_write", "in_progress", "written")
_FEEDBACK = ("helpful", "not_helpful", "too_broad", "too_narrow", "not_relevant", "too_heavy", "too_academic")
_MAX_TEXT_LENGTH = 1000
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 50
_SIMILARITY_THRESHOLD = 0.72
_RESERVED_HOSTS = {mcp24_access.LEGACY_HOST_ID, mcp24_access.LOCAL_HOST_ID}
_TRACE_TARGET_QUERY_ACTIONS = {"get_request_status", "get_request_diagnostics"}


def _named_actor(actor: dict[str, Any]) -> bool:
    return actor["host_id"] not in _RESERVED_HOSTS


def _namespaced(actor: dict[str, Any], kind: str, value: str | None) -> str | None:
    return mcp24_access.namespace_id(actor, kind, value) if value and _named_actor(actor) else value


def _legacy_params(action: str, params: dict[str, Any], actor: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    mapped = copy.deepcopy(params)
    public_ids: dict[str, str] = {}
    if not _named_actor(actor):
        return mapped, public_ids
    keys = [("conversation_id", "conversation"), ("source_conversation_id", "conversation")]
    if action in {"correct_prompt_status", "undo_action"}:
        keys.append(("target_request_id", "request"))
    if action not in MCP24_HANDLERS:
        keys.append(("request_id", "request"))
    for key, kind in keys:
        value = mapped.get(key)
        if isinstance(value, str) and value:
            internal = _namespaced(actor, kind, value)
            mapped[key] = internal
            public_ids[internal] = value
    if action == "save_thought_checkpoint" and isinstance(params.get("conversation_id"), str):
        mapped["_source_conversation_id_public"] = params["conversation_id"]
    if action == "apply_actions":
        for step in mapped.get("actions", []):
            arguments = step.get("arguments", {})
            for key, kind in (("target_request_id", "request"), ("conversation_id", "conversation"),
                              ("source_conversation_id", "conversation")):
                value = arguments.get(key)
                if isinstance(value, str) and value:
                    internal = _namespaced(actor, kind, value)
                    arguments[key] = internal
                    public_ids[internal] = value
    return mapped, public_ids


def _public_result(result: dict[str, Any], public_ids: dict[str, str], *, named: bool) -> dict[str, Any]:
    def visit(value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {
                item_key: visit(item, item_key)
                for item_key, item in value.items()
                if not (named and item_key == "local_path")
            }
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, str) and key in {"request_id", "target_request_id", "conversation_id", "source_conversation_id"}:
            return public_ids.get(value, value)
        return value
    return visit(result)


def _field(field_type: str, **extra: Any) -> dict[str, Any]:
    return {"type": field_type, **extra}


def _tool(name: str, description: str, properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return {"name": name, "description": description, "inputSchema": schema}


_ID = _field("string", minLength=1, maxLength=200)
_TEXT = _field("string", minLength=1, maxLength=_MAX_TEXT_LENGTH)
_DIRECTION = _field("string", enum=list(_DIRECTION_IDS))
_DEPTH = _field("string", enum=list(_DEPTHS))
_STATE = _field("string", enum=list(_STATES))
_STATE_OR_NULL = _field(["string", "null"], enum=[*_STATES, None])
_VERSION = _field("integer", minimum=1)
_WEIGHT = _field("number", minimum=0)
_TIMESTAMP = _field("string", format="date-time", minLength=1)
_INCLUDE_PRIVATE = _field("boolean", description="仅在用户本轮明确授权查看这条私密命题时设为 true。")
_REASON = _field("string", minLength=1, maxLength=240)
_TAG_IDS = _field("array", items=_ID, description="标签 ID 列表。筛选时命题须包含全部所选标签。")


TOOL_SPECS = [
    _tool("pick_prompt", "按明确范围和已确认偏好取一题。", {
        "direction": _DIRECTION, "topic": _ID, "depth": _DEPTH,
        "current_id": _ID, "tag_ids": _TAG_IDS,
    }),
    _tool("get_prompt", "按精确 ID 读取一条命题；仅在用户本轮明确授权对应私密片段时设置 include_private=true。", {
        "prompt_id": _ID, "include_private": _INCLUDE_PRIVATE,
        "detail": _field("string", enum=["summary", "full"]),
    }, ("prompt_id",)),
    _tool("narrow_prompt", "从同题族中取一条更具体的命题；提供 conversation_id 时遵守该会话的有效选题条件。", {
        "prompt_id": _ID, "depth": _DEPTH, "conversation_id": _ID,
    }, ("prompt_id",)),
    _tool("get_angles", "按需返回一到两个命题切入点。", {
        "prompt_id": _ID, "direction": _DIRECTION, "topic": _ID,
    }),
    _tool("draft_prompt", "把本轮明确给出的主题或线索拟成一条本机草题；仅在用户要求时另行保存。", {
        "direction": _DIRECTION, "topic": _ID, "depth": _DEPTH,
        "focus": _field("string", minLength=1, maxLength=160),
    }, ("direction",)),
    _tool("check_prompt", "仅核对命题是否重复或近似，不返回旧题内容。", {
        "text": _TEXT, "direction": _DIRECTION, "topic": _ID,
        "depth": _DEPTH, "exclude_id": _ID,
    }, ("text",)),
    _tool("save_prompt", "保存一条命题；首次状态和暂缓可与保存同事务提交。", {
        "request_id": _ID, "text": _TEXT, "direction": _DIRECTION,
        "topic": _ID, "depth": _DEPTH, "state": _STATE,
        "snoozed_until": _TIMESTAMP, "parent_id": _ID, "tag_ids": _TAG_IDS,
    }, ("request_id", "text", "direction")),
    _tool("list_prompts", "按范围分页查看命题记录。", {
        "direction": _DIRECTION, "topic": _ID, "state": _STATE,
        "limit": _field("integer", minimum=1, maximum=_MAX_LIMIT),
        "offset": _field("integer", minimum=0), "tag_ids": _TAG_IDS,
    }),
    _tool("list_tags", "查看可用于命题标注和筛选的标签及 ID。", {}),
    _tool("add_tag", "创建一个可跨主题使用的命题标签。", {
        "request_id": _ID, "name": _field("string", minLength=1, maxLength=80),
    }, ("request_id", "name")),
    _tool("set_prompt_tags", "按当前命题版本替换整组标签；传空列表清空。增删个别标签时先读取当前标签。", {
        "request_id": _ID, "prompt_id": _ID, "tag_ids": _TAG_IDS, "expected_version": _VERSION,
    }, ("request_id", "prompt_id", "tag_ids", "expected_version")),
    _tool("set_prompt_state", "更正命题的写作状态。", {
        "request_id": _ID, "prompt_id": _ID, "state": _STATE_OR_NULL,
        "expected_version": _VERSION, "reason": _REASON,
    }, ("request_id", "prompt_id", "state", "expected_version")),
    _tool("snooze_prompt", "将命题暂缓到指定时间。", {
        "request_id": _ID, "prompt_id": _ID,
        "snoozed_until": _TIMESTAMP, "expected_version": _VERSION, "reason": _REASON,
    }, ("request_id", "prompt_id", "snoozed_until", "expected_version")),
    _tool("clear_snooze", "清除命题的暂缓期限。", {
        "request_id": _ID, "prompt_id": _ID, "expected_version": _VERSION, "reason": _REASON,
    }, ("request_id", "prompt_id", "expected_version")),
    _tool("get_preferences", "查看方向和主题的长期与近期偏好。", {}),
    _tool("get_preference_reason", "仅在用户本轮明确询问时，按方向或主题和作用期查询调整理由。", {
        "direction": _DIRECTION, "topic_id": _ID,
        "scope": _field("string", enum=["baseline", "recent"]),
    }, ("scope",)),
    _tool("add_topic", "在指定方向下新增一个小主题。", {
        "request_id": _ID, "direction": _DIRECTION,
        "name": _field("string", minLength=1, maxLength=80),
        "parent_id": _ID, "weight": _WEIGHT, "expected_version": _VERSION,
    }, ("request_id", "direction", "name", "expected_version")),
    _tool("update_topic", "更正小主题名称、父主题或启用状态。", {
        "request_id": _ID, "topic_id": _ID,
        "name": _field("string", minLength=1, maxLength=80),
        "parent_id": _field(["string", "null"], minLength=1, maxLength=200),
        "active": _field("boolean"), "expected_version": _VERSION,
    }, ("request_id", "topic_id", "expected_version")),
    _tool("set_direction_weight", "设置方向的长期或近期相对权重。", {
        "request_id": _ID, "direction": _DIRECTION, "weight": _WEIGHT,
        "scope": _field("string", enum=["baseline", "recent"]),
        "expires_at": _TIMESTAMP, "expected_version": _VERSION, "reason": _REASON,
    }, ("request_id", "direction", "weight", "scope", "expected_version")),
    _tool("set_topic_weight", "设置小主题的长期或近期相对权重。", {
        "request_id": _ID, "topic_id": _ID, "weight": _WEIGHT,
        "scope": _field("string", enum=["baseline", "recent"]),
        "expires_at": _TIMESTAMP, "expected_version": _VERSION, "reason": _REASON,
    }, ("request_id", "topic_id", "weight", "scope", "expected_version")),
    _tool("reset_weights", "将指定范围的权重恢复为默认值并清除近期覆盖。", {
        "request_id": _ID, "scope": _field("string", enum=["all", "directions", "topics"]),
        "direction": _DIRECTION, "topic_id": _ID, "expected_version": _VERSION,
    }, ("request_id", "expected_version")),
    _tool("end_preference", "结束方向或主题的近期权重调整。", {
        "request_id": _ID, "scope": _field("string", enum=["direction", "topic"]),
        "direction": _DIRECTION, "topic_id": _ID, "expected_version": _VERSION,
    }, ("request_id", "scope", "expected_version")),
    _tool("record_feedback", "保存一条明确范围的反馈，影响该范围内的选题顺序，不改变权重。", {
        "request_id": _ID, "scope": _field("string", enum=["prompt", "direction", "topic"]),
        "prompt_id": _ID, "direction": _DIRECTION, "topic": _ID,
        "feedback": _field("string", enum=list(_FEEDBACK)),
    }, ("request_id", "scope", "feedback")),
    _tool("list_feedback", "按本轮明确指定的命题、方向或主题，查看该范围最近的反馈。", {
        "prompt_id": _ID, "direction": _DIRECTION, "topic": _ID,
        "limit": _field("integer", minimum=1, maximum=30),
    }),
]

_MATERIAL_SPECS, _LOCAL_SPECS = materials.tool_specs(_tool, _field, _ID, _DIRECTION, _VERSION, _INCLUDE_PRIVATE)
TOOL_SPECS.extend(_MATERIAL_SPECS)
TOOL_SPECS.extend(mcp_workflows.specs(_tool, _field, _ID, _DIRECTION, _VERSION, _INCLUDE_PRIVATE, _TAG_IDS))
MCP24_MODULES = (mcp24_contract, mcp24_access, mcp24_transport, mcp24_reads, mcp24_ops, mcp25_history, mcp25_read, mcp25_thought, mcp25_relations)
for _module in MCP24_MODULES:
    TOOL_SPECS.extend(_module.specs(_tool, _field, _ID, _DIRECTION, _VERSION, _INCLUDE_PRIVATE))
LOCAL_ACTIONS = materials.LOCAL_ACTIONS
_SPECS_BY_NAME = {spec["name"]: spec for spec in [*TOOL_SPECS, *_LOCAL_SPECS]}
MCP24_HANDLERS = {name: module for module in MCP24_MODULES for name in module.HANDLERS}
_WRITE_ACTIONS = {
    "save_prompt", "set_prompt_state", "snooze_prompt", "clear_snooze", "add_tag", "set_prompt_tags",
    "add_topic", "update_topic", "set_direction_weight", "set_topic_weight",
    "reset_weights", "end_preference", "record_feedback",
} | materials.WRITE_ACTIONS | mcp_workflows.WRITE_ACTIONS | {
    "export_thought_bundle", "create_backup", "restore_backup",
} | set().union(*(module.WRITE_ACTIONS for module in MCP24_MODULES))


class _BookError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _BookError("invalid_params", "时间必须是有效的 ISO 8601 日期时间。", {"field": "timestamp"})
    if parsed.tzinfo is None:
        raise _BookError("invalid_params", "时间必须包含时区。", {"field": "timestamp"})
    return parsed.astimezone(timezone.utc)


def _error(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"status": code, "error": {"code": code, "message": message, "details": details or {}}}


def _validate_field(value, schema, name):
    expected = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    actual = {type(None): "null", bool: "boolean", int: "integer", float: "number", str: "string", dict: "object", list: "array"}.get(type(value))
    if actual not in expected and not (actual == "integer" and "number" in expected):
        raise _BookError("invalid_params", "参数类型不正确。", {"field": name, "expected": expected})
    if "enum" in schema and value not in schema["enum"]:
        raise _BookError("invalid_params", "参数值不在允许范围内。", {"field": name, "allowed": schema["enum"]})
    if actual == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", math.inf):
            raise _BookError("invalid_params", "字符串长度不符合要求。", {"field": name})
        if schema.get("format") == "date-time":
            _parse_timestamp(value)
    elif actual in ("integer", "number"):
        if not math.isfinite(value) or not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf):
            raise _BookError("invalid_params", "数值不符合要求。", {"field": name})
    elif actual == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", math.inf):
            raise _BookError("invalid_params", "列表长度不符合要求。", {"field": name})
        if "items" in schema:
            for index, item in enumerate(value):
                _validate_field(item, schema["items"], f"{name}[{index}]")
    elif actual == "object" and "properties" in schema:
        properties = schema["properties"]
        unknown = sorted(set(value) - set(properties))
        missing = sorted(set(schema.get("required", ())) - set(value))
        if missing or (unknown and schema.get("additionalProperties") is False):
            raise _BookError("invalid_params", "对象包含缺失或未知字段。", {"field": name, "missing": missing, "unknown": unknown})
        for key in value.keys() & properties.keys():
            _validate_field(value[key], properties[key], f"{name}.{key}")


def _validate_params(action: str, params: Any, channel: str) -> dict[str, Any]:
    if not isinstance(action, str) or action not in _SPECS_BY_NAME:
        raise _BookError("invalid_action", "不支持的书序操作。", {"action": action})
    if not isinstance(params, dict):
        raise _BookError("invalid_params", "params 必须是对象。")
    if action in LOCAL_ACTIONS and channel != "local":
        raise _BookError("not_found", "此操作仅供本机网页使用。")
    schema = _SPECS_BY_NAME[action]["inputSchema"]
    properties = schema["properties"]
    if action == "list_prompts" and channel == "local":
        properties = {**properties, "include_private": _INCLUDE_PRIVATE}
    unknown = sorted(set(params) - set(properties))
    if unknown:
        raise _BookError("invalid_params", "包含未定义的参数。", {"fields": unknown})
    missing = [name for name in schema.get("required", ()) if name not in params]
    if missing:
        raise _BookError("invalid_params", "缺少必需参数。", {"fields": missing})
    for name, value in params.items():
        _validate_field(value, properties[name], name)
    for name in ("request_id", "name", "focus", "reason", "conversation_id"):
        if name in params and isinstance(params[name], str) and not params[name].strip():
            raise _BookError("invalid_params", "参数不能为空白。", {"field": name})
    if action in ("save_prompt", "check_prompt") and not params["text"].strip():
        raise _BookError("invalid_params", "命题不能为空白。", {"field": "text"})
    if action in ("set_direction_weight", "set_topic_weight"):
        if params["scope"] == "baseline" and "expires_at" in params:
            raise _BookError("invalid_params", "长期权重不接受 expires_at。", {"field": "expires_at"})
    if action == "update_topic" and len(params) <= 3:
        raise _BookError("invalid_params", "至少提供一个要更正的主题字段。")
    if action == "record_feedback":
        scope = params["scope"]
        expected = {"prompt": "prompt_id", "direction": "direction", "topic": "topic"}[scope]
        if expected not in params:
            raise _BookError("invalid_params", "反馈需要对应明确范围。", {"field": expected})
        if scope == "prompt" and any(key in params for key in ("direction", "topic")):
            raise _BookError("invalid_params", "命题反馈只需提供 prompt_id。")
        if scope == "direction" and "topic" in params:
            raise _BookError("invalid_params", "方向反馈不接受 topic。")
        if scope == "topic" and "direction" in params:
            raise _BookError("invalid_params", "主题反馈不接受 direction。")
    if action == "end_preference":
        expected = "direction" if params["scope"] == "direction" else "topic_id"
        if expected not in params:
            raise _BookError("invalid_params", "结束近期偏好需要明确方向或主题。", {"field": expected})
        other = "topic_id" if params["scope"] == "direction" else "direction"
        if other in params:
            raise _BookError("invalid_params", "结束近期偏好时只指定一个范围。", {"field": other})
    if action == "reset_weights":
        if "topic_id" in params and "direction" in params:
            raise _BookError("invalid_params", "重置权重时不能同时指定 direction 与 topic_id。")
        if params.get("scope") == "directions" and "topic_id" in params:
            raise _BookError("invalid_params", "方向权重范围不接受 topic_id。")
        if params.get("scope", "all") == "all" and ("direction" in params or "topic_id" in params):
            raise _BookError("invalid_params", "恢复全部默认值时不接受单方向或单主题筛选。")
    return params


def _effective_weight(baseline: float, recent: float | None, expires_at: str | None, now: datetime) -> tuple[float, float | None, str | None]:
    if recent is not None and (expires_at is None or _parse_timestamp(expires_at) > now):
        return recent, recent, expires_at
    return baseline, None, None


def _prompt_dict(db: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> dict[str, Any]:
    snoozed_until = row["snoozed_until"]
    is_snoozed = snoozed_until is not None and _parse_timestamp(snoozed_until) > now
    return {
        "id": row["id"],
        "text": row["text"],
        "direction": row["direction"],
        "topic_id": row["topic_id"],
        "depth": row["depth"],
        "parent_id": row["parent_id"],
        "state": row["state"],
        "snoozed_until": snoozed_until,
        "is_snoozed": is_snoozed,
        "visibility": row["visibility"],
        "origin": row["origin"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "tags": _prompt_tags(db, row["id"]),
    }


def _prompt_tags(db, prompt_id):
    return [dict(tag) for tag in db.execute(
        """SELECT tags.id, tags.name FROM prompt_tags JOIN tags ON tags.id = prompt_tags.tag_id
           WHERE prompt_tags.prompt_id = ? ORDER BY tags.name_key""",
        (prompt_id,),
    )]


def _matching_tagged_prompt_ids(db, tag_ids):
    tag_ids = sorted(set(tag_ids))
    return {row[0] for row in db.execute(
        """SELECT prompt_id FROM prompt_tags WHERE tag_id IN (SELECT value FROM json_each(?))
           GROUP BY prompt_id HAVING COUNT(*) = ?""",
        (json.dumps(tag_ids), len(tag_ids)),
    )}


def _validated_tag_ids(db, tag_ids):
    tag_ids = sorted(set(tag_ids))
    found = {row[0] for row in db.execute(
        "SELECT id FROM tags WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(tag_ids),),
    )}
    if missing := sorted(set(tag_ids) - found):
        raise _BookError("not_found", "部分命题标签不存在。", {"tag_ids": missing})
    return tag_ids


class BookService:
    """A small SQLite business layer; each dispatch owns its connection."""

    def __init__(self, db_path: str | Path):
        self._uri = str(db_path) == ":memory:"
        self.db_path = f"file:shuxu-{uuid.uuid4().hex}?mode=memory&cache=shared" if self._uri else str(Path(db_path).expanduser().resolve())
        self._memory_anchor = self._connect() if self._uri else None
        if not self._uri:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._dispatch_scheduler = mcp24_ops.SQLiteServiceScheduler(self)
        self._mcp24_ops_runtime = mcp24_ops.runtime(self) if not self._uri else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5, isolation_level=None, uri=self._uri)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        mcp24_access.schema(connection)
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS directions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    baseline_weight REAL NOT NULL,
                    recent_weight REAL,
                    recent_expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS topics (
                    id TEXT PRIMARY KEY,
                    direction TEXT NOT NULL REFERENCES directions(id),
                    name TEXT NOT NULL,
                    parent_id TEXT REFERENCES topics(id),
                    active INTEGER NOT NULL DEFAULT 1,
                    baseline_weight REAL NOT NULL DEFAULT 1.0,
                    recent_weight REAL,
                    recent_expires_at TEXT,
                    UNIQUE(direction, name)
                );
                CREATE TABLE IF NOT EXISTS prompts (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    direction TEXT NOT NULL REFERENCES directions(id),
                    topic_id TEXT REFERENCES topics(id) ON DELETE SET NULL,
                    depth TEXT,
                    parent_id TEXT REFERENCES prompts(id) ON DELETE SET NULL,
                    state TEXT,
                    snoozed_until TEXT,
                    visibility TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS prompts_direction_topic ON prompts(direction, topic_id);
                CREATE INDEX IF NOT EXISTS prompts_state_snooze ON prompts(state, snoozed_until);
                CREATE TABLE IF NOT EXISTS tags (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    name_key TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS prompt_tags (
                    prompt_id TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                    tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                    PRIMARY KEY (prompt_id, tag_id)
                );
                CREATE INDEX IF NOT EXISTS prompt_tags_tag ON prompt_tags(tag_id, prompt_id);
                CREATE TABLE IF NOT EXISTS preference_meta (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    scope TEXT NOT NULL,
                    prompt_id TEXT REFERENCES prompts(id) ON DELETE SET NULL,
                    direction TEXT REFERENCES directions(id),
                    topic_id TEXT REFERENCES topics(id) ON DELETE SET NULL,
                    feedback TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS request_receipts (
                    request_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    params_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    channel TEXT
                );
                CREATE TABLE IF NOT EXISTS preference_reasons (
                    item_kind TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    reason TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (item_kind, item_id, scope)
                );
            """)
            materials.schema(connection)
            mcp_workflows.schema(connection)
            mcp24_access.schema(connection)
            mcp24_contract.schema(connection)
            mcp24_reads.schema(connection)
            mcp24_transport.schema(connection)
            mcp25_history.schema(connection)
            mcp25_read.schema(connection)
            mcp25_thought.schema(connection)
            mcp25_relations.schema(connection)
            mcp24_reads.ensure_relation_index(connection)
            if "channel" not in {row["name"] for row in connection.execute("PRAGMA table_info(request_receipts)")}:
                connection.execute("ALTER TABLE request_receipts ADD COLUMN channel TEXT")
            connection.execute("BEGIN IMMEDIATE")
            for direction_id, name, weight in seed.DIRECTIONS:
                connection.execute(
                    "INSERT OR IGNORE INTO directions(id, name, baseline_weight) VALUES (?, ?, ?)",
                    (direction_id, name, weight),
                )
            topic_ids = {None: None}
            topics_added = 0
            for direction_id, topic_id, name, parent_id in seed.TOPICS:
                topics_added += connection.execute(
                    "INSERT OR IGNORE INTO topics(id, direction, name, parent_id) VALUES (?, ?, ?, ?)",
                    (topic_id, direction_id, name, topic_ids[parent_id]),
                ).rowcount
                # An existing user topic with the same name can host the seed prompts.
                topic_ids[topic_id] = connection.execute(
                    """SELECT id FROM topics WHERE id = ? OR (direction = ? AND name = ?)
                       ORDER BY id != ? LIMIT 1""",
                    (topic_id, direction_id, name, topic_id),
                ).fetchone()["id"]
            now = _iso(_now())
            for prompt in seed.PROMPTS:
                connection.execute(
                    """INSERT OR IGNORE INTO prompts
                       (id, text, direction, topic_id, depth, parent_id, visibility, origin, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'shared', 'seed', ?, ?)""",
                    (prompt["id"], prompt["text"], prompt["direction"], topic_ids[prompt["topic_id"]],
                     prompt["depth"], prompt["parent_id"], now, now),
                )
            if topics_added:
                connection.execute("UPDATE preference_meta SET version = version + 1 WHERE singleton = 1")
            connection.execute("INSERT OR IGNORE INTO preference_meta(singleton, version) VALUES (1, 1)")
            mcp24_reads.sync_changed(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def dispatch(self, action: str, params: dict[str, Any], *, channel: str = "local",
                 actor: dict[str, Any] | None = None, request_id: str | None = None,
                 upstream_message_id: str | None = None, operation_slot: str | None = None) -> dict[str, Any]:
        actor = actor or (mcp24_access.legacy_actor() if channel == "mcp" else mcp24_access.local_actor())
        body_request_id = params.get("request_id") if isinstance(params, dict) else None
        if request_id is not None and body_request_id is not None and request_id != body_request_id:
            return mcp24_contract.normalize_receipt(
                _error("invalid_params", "request_id 必须与已提交工具参数一致。"),
                request_id=body_request_id, action=action,
            )
        request_id = body_request_id or request_id
        trace_request_id = uuid.uuid4().hex if action in _TRACE_TARGET_QUERY_ACTIONS else request_id
        internal_request_id = _namespaced(actor, "request", trace_request_id)
        runtime = self._mcp24_ops_runtime
        tracer = runtime.traces if runtime is not None and internal_request_id else None
        with tracer.span(internal_request_id, "dispatch", action, actor) if tracer else nullcontext() as span:
            result = self._dispatch_scheduler.dispatch(
                action, params, channel=channel, actor=actor, request_id=internal_request_id,
                upstream_message_id=upstream_message_id, operation_slot=operation_slot,
            )
            if span and result.get("status") != "ok":
                span.set_result(result.get("status", "unknown"), (result.get("error") or {}).get("code"))
        if tracer:
            with tracer.span(internal_request_id, "response_normalization", action, actor) as span:
                normalized = mcp24_contract.normalize_receipt(result, request_id=request_id, action=action)
                if normalized.get("status") != "ok":
                    span.set_result(normalized.get("status", "unknown"), (normalized.get("error") or {}).get("code"))
                return normalized
        return mcp24_contract.normalize_receipt(result, request_id=request_id, action=action)

    def _observe_dispatch(self, actor, request_id, action, phase, result):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            mcp24_contract.observe_dispatch(
                connection, actor=actor, request_id=request_id, action=action,
                phase=phase, status=(result or {}).get("status"), result=result, now=_now(),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _requires_bound_preview(action, params):
        if action != "apply_actions":
            return False
        destructive = {"remove_inspiration", "remove_clipping", "clear_resume_card"}
        return sum(step.get("tool") in destructive for step in params.get("actions", [])) >= 2

    def _guard_bound_action(self, connection, actor, action, params, channel, now):
        effect_params = {
            key: value for key, value in params.items()
            if key not in {"request_id", "confirmed", "preview_id", "bind_preview"}
        }
        versions = mcp24_contract.current_object_versions(
            connection, action, params, book=self, channel=channel, now=now, actor=actor,
        )
        mcp24_access.guard(
            connection, actor=actor, action=action,
            canonical_params_hash=mcp24_access.canonical_params_hash(action, effect_params),
            object_versions=versions, now=now, preview_id=params.get("preview_id"),
        )

    def _guard_restore(self, actor, params, channel, now):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._guard_bound_action(connection, actor, "restore_backup", params, channel, now)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _bind_restore_preview(self, actor, data, channel, now):
        effect_params = {"backup_id": data["backup_id"], "fingerprint": data["fingerprint"]}
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            versions = mcp24_contract.current_object_versions(
                connection, "restore_backup", effect_params, book=self,
                channel=channel, now=now, actor=actor,
            )
            preview = mcp24_access.create_bound_preview(
                connection, actor=actor, action="restore_backup",
                canonical_params_hash=mcp24_access.canonical_params_hash("restore_backup", effect_params),
                object_versions=versions, now=now,
                expires_at=now + mcp24_access.PREVIEW_TTL,
            )
            connection.commit()
            return preview
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _rebuild_read_index(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            mcp24_reads.backfill(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _private_excerpt(db, item):
        if item["kind"] == "prompt":
            row = db.execute("SELECT visibility FROM prompts WHERE id = ?", (item["id"],)).fetchone()
            return bool(row and row["visibility"] == "private")
        row = db.execute(
            "SELECT prompt_id FROM materials WHERE id = ? AND collection = ?",
            (item["id"], item["kind"]),
        ).fetchone()
        if row is None or row["prompt_id"] is None:
            return False
        prompt = db.execute("SELECT visibility FROM prompts WHERE id = ?", (row["prompt_id"],)).fetchone()
        return bool(prompt and prompt["visibility"] == "private")

    def _consume_export_permits(self, connection, actor, params, now):
        missing = []
        connection.execute("SAVEPOINT mcp24_export_permits")
        for index, item in enumerate(params.get("authorized_excerpts", []), 1):
            if not self._private_excerpt(connection, item):
                continue
            permit_params = {**item, "upstream_message_id": f"{params['request_id']}:excerpt:{index}"}
            try:
                mcp24_access.consume_excerpt_permit(
                    connection, actor=actor, params=permit_params, now=now,
                )
            except mcp24_access.AccessError as error:
                if error.code != "consent_required":
                    raise
                missing.append(permit_params)
        if missing:
            connection.execute("ROLLBACK TO SAVEPOINT mcp24_export_permits")
            connection.execute("RELEASE SAVEPOINT mcp24_export_permits")
            return missing
        connection.execute("RELEASE SAVEPOINT mcp24_export_permits")
        return []

    def _dispatch(self, action, params, channel, actor=None, upstream_message_id=None, operation_slot=None):
        actor = actor or (mcp24_access.legacy_actor() if channel == "mcp" else mcp24_access.local_actor())
        if action in _TRACE_TARGET_QUERY_ACTIONS:
            return self._dispatch_impl(action, params, channel, actor, upstream_message_id, operation_slot)
        request_id = params.get("request_id") if isinstance(params, dict) else None
        internal_request_id = _namespaced(actor, "request", request_id)
        if not isinstance(internal_request_id, str) or not internal_request_id:
            return self._dispatch_impl(action, params, channel, actor, upstream_message_id, operation_slot)
        self._observe_dispatch(actor, internal_request_id, action, "start", None)
        try:
            result = self._dispatch_impl(action, params, channel, actor, upstream_message_id, operation_slot)
        except BaseException:
            self._observe_dispatch(
                actor, internal_request_id, action, "end",
                {"status": "internal_error", "error": {"code": "internal_error"}},
            )
            raise
        self._observe_dispatch(actor, internal_request_id, action, "end", result)
        return result

    def _dispatch_impl(self, action, params, channel, actor=None, upstream_message_id=None, operation_slot=None):
        actor = actor or (mcp24_access.legacy_actor() if channel == "mcp" else mcp24_access.local_actor())
        try:
            if channel not in ("local", "mcp"):
                raise _BookError("invalid_channel", "channel 必须是 local 或 mcp。")
            params = _validate_params(action, params, channel)
            now = _now()
            if not mcp24_access.allows(actor, action):
                raise mcp24_access.AccessError("forbidden", "This host cannot access the requested tool.")
            named = _named_actor(actor)
            full_access = mcp24_access.has_full_access(actor)
            if named and params.get("include_private") is True and action != "share_excerpt" and not full_access:
                raise mcp24_access.AccessError("forbidden", "Named hosts must use an approved exact excerpt request for private content.")
            if action == "get_prompt" and params.get("detail") == "full" and not full_access:
                raise mcp24_access.AccessError("forbidden", "Full prompt details require a full-access host.")
            external_request_id = params.get("request_id")
            internal_request_id = _namespaced(actor, "request", external_request_id)
            execution_params, public_ids = _legacy_params(action, params, actor)
            receipt_params = copy.deepcopy(execution_params)
            if internal_request_id:
                receipt_params["request_id"] = internal_request_id
                public_ids[internal_request_id] = external_request_id
            if "conversation_id" in params and execution_params.get("conversation_id") != params["conversation_id"]:
                public_ids[execution_params["conversation_id"]] = params["conversation_id"]
            write = action in _WRITE_ACTIONS
            tracked_write = write and bool(internal_request_id)
            digest = hashlib.sha256(json.dumps(receipt_params, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest() if tracked_write else None
            if action == "export_full_library":
                return self._dispatch_full_export(
                    params, execution_params, receipt_params, channel, actor, upstream_message_id,
                    operation_slot, now, named, external_request_id, internal_request_id,
                    public_ids, digest,
                )
            if action in mcp_workflows.FILE_TOOLS:
                job = mcp24_ops.current_job_context()
                if job:
                    job.check_cancelled()
                if tracked_write:
                    connection = self._connect()
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        if named and action == "export_thought_bundle":
                            connection.execute("SAVEPOINT mcp24_file_idempotency")
                        idempotency = mcp24_access.begin_idempotency(
                            connection, actor=actor, upstream_message_id=upstream_message_id,
                            action=action, params=params, now=now, operation_slot=operation_slot,
                        ) if named else None
                        replay_params = copy.deepcopy(receipt_params)
                        replay_ref = idempotency.get("result_ref") if idempotency else None
                        if replay_ref:
                            replay_params["request_id"] = replay_ref
                            public_ids[replay_ref] = external_request_id
                        replay_digest = hashlib.sha256(json.dumps(replay_params, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                        if idempotency and idempotency["state"] == "replay":
                            replay = self._replay(connection, action, replay_params, channel, replay_digest, actor)
                            if replay is None:
                                raise _BookError("submission_unknown", "找到幂等记录，但没有可重放的文件回执。", {"request_id": external_request_id})
                            connection.commit()
                            return _public_result(replay, public_ids, named=named)
                        replay = self._replay(connection, action, receipt_params, channel, digest, actor)
                        if replay is not None:
                            if named and idempotency and idempotency["state"] == "new":
                                mcp24_access.complete_idempotency(
                                    connection, actor=actor, upstream_message_id=upstream_message_id,
                                    action=action, params=params, result_ref=internal_request_id,
                                    result_hash=hashlib.sha256(json.dumps(replay, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
                                    now=now, operation_slot=operation_slot,
                                )
                            connection.commit()
                            return _public_result(replay, public_ids, named=named)
                        if action == "export_thought_bundle":
                            privacy_guard.check_action(connection, action, params)
                        if named and action == "export_thought_bundle":
                            missing_excerpts = self._consume_export_permits(connection, actor, params, now)
                            if missing_excerpts:
                                connection.execute("ROLLBACK TO SAVEPOINT mcp24_file_idempotency")
                                connection.execute("RELEASE SAVEPOINT mcp24_file_idempotency")
                                excerpt_request_ids = [
                                    mcp24_access.register_excerpt_request(
                                        connection, actor=actor, params=item, now=now,
                                    )["request_id"]
                                    for item in missing_excerpts
                                ]
                                connection.commit()
                                raise mcp24_access.AccessError(
                                    "consent_required", "本机需要逐条批准这些私密导出片段。",
                                    {"excerpt_request_ids": excerpt_request_ids},
                                )
                            connection.execute("RELEASE SAVEPOINT mcp24_file_idempotency")
                        if named and action == "restore_backup":
                            self._guard_bound_action(connection, actor, action, params, channel, now)
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    finally:
                        connection.close()
                data = mcp_files.handle(self, action, execution_params, channel, now)
                if named and action == "preview_backup":
                    data["bound_preview"] = self._bind_restore_preview(actor, data, channel, now)
                if named and action == "export_thought_bundle":
                    data["export_request_id"] = internal_request_id
                if action == "restore_backup" and data:
                    self._rebuild_read_index()
                result = _public_result({"status": "ok", "data": data}, public_ids, named=named)
                if tracked_write:
                    connection = self._connect()
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        remembered = mcp_workflows.stored_result(action, result)
                        connection.execute(
                            "INSERT INTO request_receipts(request_id, action, params_hash, response_json, created_at, channel) VALUES (?, ?, ?, ?, ?, ?)",
                            (internal_request_id, action, digest, json.dumps(remembered, ensure_ascii=False, sort_keys=True), _iso(now), channel),
                        )
                        if named:
                            mcp24_access.complete_idempotency(
                                connection, actor=actor, upstream_message_id=upstream_message_id,
                                action=action, params=params, result_ref=internal_request_id,
                                result_hash=hashlib.sha256(json.dumps(remembered, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
                                now=now, operation_slot=operation_slot,
                            )
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    finally:
                        connection.close()
                return result
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE" if write or (named and action == "share_excerpt") else "BEGIN")
                idempotency = None
                if named and tracked_write:
                    idempotency = mcp24_access.begin_idempotency(
                        connection, actor=actor, upstream_message_id=upstream_message_id,
                        action=action, params=params, now=now, operation_slot=operation_slot,
                    )
                    if idempotency["state"] == "replay":
                        replay_params = copy.deepcopy(receipt_params)
                        replay_ref = idempotency.get("result_ref")
                        if replay_ref:
                            replay_params["request_id"] = replay_ref
                            public_ids[replay_ref] = external_request_id
                        replay_digest = hashlib.sha256(json.dumps(replay_params, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest() if tracked_write else None
                        replay = self._replay(connection, action, replay_params, channel, replay_digest, actor) if tracked_write else None
                        if replay is None:
                            if idempotency.get("tombstoned"):
                                raise _BookError("receipt_payload_pruned", "该成功回执的正文已按保留期限清理，不能重放为新写入。", {"request_id": external_request_id})
                            raise _BookError("submission_unknown", "找到幂等记录，但没有可重放的回执正文。", {"request_id": external_request_id})
                        connection.commit()
                        return _public_result(replay, public_ids, named=named)
                if tracked_write:
                    replay = self._replay(connection, action, receipt_params, channel, digest, actor)
                    if replay is not None:
                        if named and idempotency and idempotency["state"] == "new":
                            mcp24_access.complete_idempotency(
                                connection, actor=actor, upstream_message_id=upstream_message_id,
                                action=action, params=params, result_ref=internal_request_id,
                                result_hash=hashlib.sha256(json.dumps(replay, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
                                now=_now(), operation_slot=operation_slot,
                            )
                        connection.commit()
                        return _public_result(replay, public_ids, named=named)
                if named and action == "share_excerpt":
                    try:
                        mcp24_access.consume_excerpt_permit(
                            connection, actor=actor,
                            params={**params, "upstream_message_id": external_request_id}, now=now,
                        )
                    except mcp24_access.AccessError as error:
                        if error.code == "consent_required":
                            connection.commit()
                        raise
                    execution_params["authorized"] = True
                if named and self._requires_bound_preview(action, params):
                    self._guard_bound_action(connection, actor, action, params, channel, now)
                if named and action == "apply_actions":
                    for step in params.get("actions", []):
                        if (step["tool"] in MCP24_HANDLERS or step["tool"] in mcp_workflows.FILE_TOOLS
                                or step["tool"] == "share_excerpt"
                                or step.get("arguments", {}).get("include_private") is True
                                or not mcp24_access.allows(actor, step["tool"])):
                            raise mcp24_access.AccessError("forbidden", "This host cannot use one of the nested tools.")
                history_before = (mcp25_history.capture_dispatch(
                    connection, action, execution_params, params, channel, actor,
                ) if write else None)
                before = mcp_workflows.capture_before(connection, action, execution_params) if channel == "mcp" and write else None
                result = self._run(connection, action, execution_params, channel, actor, now)
                if before is not None:
                    change = mcp_workflows.capture_after(connection, before, result["data"])
                    result["data"]["undo_available"] = mcp_workflows.log_undo(connection, execution_params, [change], channel, _now())
                if write:
                    mcp25_history.record_dispatch(
                        connection, action, execution_params, params, result["data"], channel, actor,
                        _iso(_now()), before=history_before,
                    )
                    mcp24_reads.sync_changed(connection)
                if tracked_write:
                    remembered = mcp_workflows.stored_result(action, result)
                    connection.execute(
                        "INSERT INTO request_receipts(request_id, action, params_hash, response_json, created_at, channel) VALUES (?, ?, ?, ?, ?, ?)",
                        (internal_request_id, action, digest, json.dumps(remembered, ensure_ascii=False, sort_keys=True), _iso(_now()), channel),
                    )
                    if named and idempotency and idempotency["state"] == "new":
                        mcp24_access.complete_idempotency(
                            connection, actor=actor, upstream_message_id=upstream_message_id,
                            action=action, params=params, result_ref=internal_request_id,
                            result_hash=hashlib.sha256(json.dumps(remembered, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
                            now=_now(), operation_slot=operation_slot,
                        )
                job = mcp24_ops.current_job_context()
                if job and write:
                    job.check_cancelled()
                    job.begin_commit()
                connection.commit()
                return _public_result(result, public_ids, named=named)
            except mcp24_access.AccessError as error:
                if not (action == "share_excerpt" and error.code == "consent_required"):
                    connection.rollback()
                raise
            except (_BookError, materials.MaterialError, mcp_workflows.WorkflowError, privacy_guard.PrivacyViolation):
                connection.rollback()
                raise
            except sqlite3.OperationalError as error:
                connection.rollback()
                if "locked" not in str(error).lower() and "busy" not in str(error).lower():
                    raise
                details: dict[str, Any] = {"request_id": params.get("request_id")}
                if tracked_write:
                    check = self._connect()
                    try:
                        replay = self._replay(check, action, receipt_params, channel, digest, actor)
                    except sqlite3.OperationalError as confirm_error:
                        if "locked" not in str(confirm_error).lower() and "busy" not in str(confirm_error).lower():
                            raise
                        return _error("submission_unknown", "暂时无法确认提交结果；可用原 request_id 查询或重试。", details)
                    finally:
                        check.close()
                    if replay is not None:
                        return _public_result(replay, public_ids, named=named)
                    details["submission"] = "not_committed"
                return _error("temporarily_unavailable", "数据库暂时繁忙，可稍后重试。", details)
            finally:
                connection.close()
        except mcp24_access.AccessError as error:
            return _error(error.code, error.message, error.details)
        except (_BookError, materials.MaterialError, mcp_workflows.WorkflowError, privacy_guard.PrivacyViolation) as error:
            return _error(error.code, str(error), error.details)

    def _authorize_full_export(self, db, actor):
        mcp24_access._assert_current_actor(db, actor)
        if not mcp24_access.allows(actor, "export_full_library"):
            raise mcp24_access.AccessError("forbidden", "This host cannot access the requested tool.")
        mcp25_read._require_full(actor)

    def _finish_full_export_replay(self, replay, *, prevalidated_request_id,
                                   params, receipt_params, channel, actor, upstream_message_id,
                                   operation_slot, named, external_request_id, internal_request_id,
                                   public_ids, digest):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize_full_export(connection, actor)
            idempotency = mcp24_access.begin_idempotency(
                connection, actor=actor, upstream_message_id=upstream_message_id,
                action="export_full_library", params=params, now=_now(), operation_slot=operation_slot,
            ) if named else None
            replay_request_id = receipt_params["request_id"]
            if idempotency and idempotency["state"] == "replay":
                replay_params = copy.deepcopy(receipt_params)
                replay_ref = idempotency.get("result_ref")
                if replay_ref:
                    replay_params["request_id"] = replay_ref
                    public_ids[replay_ref] = external_request_id
                    replay_request_id = replay_ref
                replay_digest = hashlib.sha256(json.dumps(
                    replay_params, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")).hexdigest()
                replay = self._replay(connection, "export_full_library", replay_params, channel, replay_digest, actor)
                if replay is None:
                    if idempotency.get("tombstoned"):
                        raise _BookError("receipt_payload_pruned", "该成功回执的正文已按保留期限清理，不能重放为新写入。", {"request_id": external_request_id})
                    raise _BookError("submission_unknown", "找到幂等记录，但没有可重放的完整导出回执。", {"request_id": external_request_id})
            else:
                replay = self._replay(connection, "export_full_library", receipt_params, channel, digest, actor)
                if replay is None:
                    raise _BookError("submission_unknown", "完整导出回执在重放期间消失，不能安全生成新文件。", {"request_id": external_request_id})
            mcp25_read.validate_export_metadata(connection, actor, replay_request_id, replay, _now())
            if named and idempotency and idempotency["state"] == "new":
                mcp24_access.complete_idempotency(
                    connection, actor=actor, upstream_message_id=upstream_message_id,
                    action="export_full_library", params=params, result_ref=internal_request_id,
                    result_hash=hashlib.sha256(json.dumps(
                        mcp_workflows.stored_result("export_full_library", replay),
                        ensure_ascii=False, sort_keys=True,
                    ).encode("utf-8")).hexdigest(),
                    now=_now(), operation_slot=operation_slot,
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        if replay_request_id != prevalidated_request_id:
            verification = self._connect()
            try:
                verification.execute("BEGIN")
                self._authorize_full_export(verification, actor)
                mcp25_read.validate_export_replay(self, verification, actor, replay_request_id, replay, _now())
                verification.commit()
            except BaseException:
                verification.rollback()
                raise
            finally:
                verification.close()
        replay.get("data", {}).pop("download", None)
        return _public_result(replay, public_ids, named=named)

    def _dispatch_full_export(self, params, execution_params, receipt_params, channel, actor,
                              upstream_message_id, operation_slot, now, named, external_request_id,
                              internal_request_id, public_ids, digest):
        staged_path = None
        staged_export_id = None
        published_path = None
        committed = False
        try:
            preflight = self._connect()
            try:
                preflight.execute("BEGIN")
                self._authorize_full_export(preflight, actor)
                replay = self._replay(preflight, "export_full_library", receipt_params, channel, digest, actor)
                if replay is not None:
                    replay_request_id = receipt_params["request_id"]
                    mcp25_read.validate_export_replay(
                        self, preflight, actor, replay_request_id, replay, _now(),
                    )
                    preflight.commit()
                    return self._finish_full_export_replay(
                        replay, prevalidated_request_id=replay_request_id,
                        params=params, receipt_params=receipt_params, channel=channel, actor=actor,
                        upstream_message_id=upstream_message_id, operation_slot=operation_slot,
                        named=named, external_request_id=external_request_id,
                        internal_request_id=internal_request_id, public_ids=public_ids, digest=digest,
                    )
                _, export_params_hash = mcp25_read.export_filters(self, preflight, execution_params, actor)
                previous = mcp25_read.export_row_for_request(preflight, actor, execution_params["request_id"])
                if previous:
                    if previous["params_hash"] != export_params_hash:
                        raise mcp_workflows.WorkflowError("idempotency_conflict", "request_id 已用于不同的完整导出范围。")
                    mcp25_read.validate_export_artifact(self, previous, _now())
                preflight.commit()
            except BaseException:
                preflight.rollback()
                raise
            finally:
                preflight.close()

            staged_result = None
            if previous is None:
                mcp_files._folder(self, "mcp-full-exports")
                snapshot = mcp_files._readonly(Path(self.db_path))
                try:
                    snapshot.execute("BEGIN")
                    filters, export_params_hash = mcp25_read.export_filters(self, snapshot, execution_params, actor)
                    export_id = uuid.uuid4().hex
                    staged_export_id = export_id
                    staged_result = mcp_files.create_full_export(
                        self, snapshot, filters, actor, now, export_id,
                    )
                    staged_path = staged_result["path"]
                    snapshot.commit()
                except BaseException:
                    snapshot.rollback()
                    if staged_result:
                        staged_result["path"].unlink(missing_ok=True)
                    raise
                finally:
                    snapshot.close()

            connection = self._connect()
            replay_after_commit = None
            replay_request_id = None
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._authorize_full_export(connection, actor)
                idempotency = mcp24_access.begin_idempotency(
                    connection, actor=actor, upstream_message_id=upstream_message_id,
                    action="export_full_library", params=params, now=_now(), operation_slot=operation_slot,
                ) if named else None
                if idempotency and idempotency["state"] == "replay":
                    replay_params = copy.deepcopy(receipt_params)
                    replay_ref = idempotency.get("result_ref")
                    if replay_ref:
                        replay_params["request_id"] = replay_ref
                        public_ids[replay_ref] = external_request_id
                    replay_digest = hashlib.sha256(json.dumps(
                        replay_params, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")).hexdigest()
                    replay_after_commit = self._replay(
                        connection, "export_full_library", replay_params, channel, replay_digest, actor,
                    )
                    replay_request_id = replay_params["request_id"]
                    if replay_after_commit is None:
                        if idempotency.get("tombstoned"):
                            raise _BookError("receipt_payload_pruned", "该成功回执的正文已按保留期限清理，不能重放为新写入。", {"request_id": external_request_id})
                        raise _BookError("submission_unknown", "找到幂等记录，但没有可重放的完整导出回执。", {"request_id": external_request_id})
                else:
                    replay_after_commit = self._replay(
                        connection, "export_full_library", receipt_params, channel, digest, actor,
                    )
                    if replay_after_commit is not None:
                        replay_request_id = receipt_params["request_id"]
                        if named and idempotency and idempotency["state"] == "new":
                            mcp24_access.complete_idempotency(
                                connection, actor=actor, upstream_message_id=upstream_message_id,
                                action="export_full_library", params=params, result_ref=internal_request_id,
                                result_hash=hashlib.sha256(json.dumps(
                                    mcp_workflows.stored_result("export_full_library", replay_after_commit),
                                    ensure_ascii=False, sort_keys=True,
                                ).encode("utf-8")).hexdigest(),
                                now=_now(), operation_slot=operation_slot,
                            )
                if replay_after_commit is not None:
                    mcp25_read.validate_export_metadata(
                        connection, actor, replay_request_id, replay_after_commit, _now(),
                    )
                    connection.commit()
                elif previous is not None:
                    current = mcp25_read.export_row_for_request(connection, actor, execution_params["request_id"])
                    if current is None or (current["export_id"], current["sha256"], current["size_bytes"],
                                           current["file_name"], current["expires_at"]) != (
                            previous["export_id"], previous["sha256"], previous["size_bytes"],
                            previous["file_name"], previous["expires_at"]):
                        raise mcp_workflows.WorkflowError("version_conflict", "完整导出引用在重放期间发生变化。")
                    data = mcp25_read.export_result(current, execution_params["request_id"])
                    mcp25_read.validate_export_metadata(
                        connection, actor, execution_params["request_id"], {"data": data}, _now(),
                    )
                    history_before = mcp25_history.capture_dispatch(
                        connection, "export_full_library", execution_params, params, channel, actor,
                    )
                    mcp25_history.record_dispatch(
                        connection, "export_full_library", execution_params, params, data, channel, actor,
                        _iso(_now()), before=history_before,
                    )
                    mcp24_reads.sync_changed(connection)
                    result = _public_result({"status": "ok", "data": data}, public_ids, named=named)
                    remembered = mcp_workflows.stored_result("export_full_library", result)
                    connection.execute(
                        "INSERT INTO request_receipts(request_id, action, params_hash, response_json, created_at, channel) VALUES (?, ?, ?, ?, ?, ?)",
                        (internal_request_id, "export_full_library", digest,
                         json.dumps(remembered, ensure_ascii=False, sort_keys=True), _iso(_now()), channel),
                    )
                    if named and idempotency and idempotency["state"] == "new":
                        mcp24_access.complete_idempotency(
                            connection, actor=actor, upstream_message_id=upstream_message_id,
                            action="export_full_library", params=params, result_ref=internal_request_id,
                            result_hash=hashlib.sha256(json.dumps(
                                remembered, ensure_ascii=False, sort_keys=True,
                            ).encode("utf-8")).hexdigest(),
                            now=_now(), operation_slot=operation_slot,
                        )
                    job = mcp24_ops.current_job_context()
                    if job:
                        job.check_cancelled()
                        job.begin_commit()
                    connection.commit()
                    committed = True
                    return result
                else:
                    concurrent = mcp25_read.export_row_for_request(
                        connection, actor, execution_params["request_id"],
                    )
                    if concurrent is not None:
                        raise _BookError("submission_unknown", "完整导出已有文件引用但缺少可重放回执，请使用原 request_id 查询。", {"request_id": external_request_id})
                    folder = mcp_files._folder(self, "mcp-full-exports")
                    published_path = mcp_files._safe_file(folder, f"full-{staged_export_id}.jsonl")
                    staged_result["path"].replace(published_path)
                    publication_now = _now()
                    row = mcp25_read.insert_export(
                        connection, actor, execution_params["request_id"], export_params_hash,
                        staged_result, publication_now, staged_export_id,
                    )
                    data = mcp25_read.export_result(row, execution_params["request_id"], include_download=True)
                    history_before = mcp25_history.capture_dispatch(
                        connection, "export_full_library", execution_params, params, channel, actor,
                    )
                    mcp25_history.record_dispatch(
                        connection, "export_full_library", execution_params, params, data, channel, actor,
                        _iso(publication_now), before=history_before,
                    )
                    mcp24_reads.sync_changed(connection)
                    result = _public_result({"status": "ok", "data": data}, public_ids, named=named)
                    remembered = mcp_workflows.stored_result("export_full_library", result)
                    connection.execute(
                        "INSERT INTO request_receipts(request_id, action, params_hash, response_json, created_at, channel) VALUES (?, ?, ?, ?, ?, ?)",
                        (internal_request_id, "export_full_library", digest,
                         json.dumps(remembered, ensure_ascii=False, sort_keys=True), _iso(publication_now), channel),
                    )
                    if named and idempotency and idempotency["state"] == "new":
                        mcp24_access.complete_idempotency(
                            connection, actor=actor, upstream_message_id=upstream_message_id,
                            action="export_full_library", params=params, result_ref=internal_request_id,
                            result_hash=hashlib.sha256(json.dumps(
                                remembered, ensure_ascii=False, sort_keys=True,
                            ).encode("utf-8")).hexdigest(),
                            now=publication_now, operation_slot=operation_slot,
                        )
                    job = mcp24_ops.current_job_context()
                    if job:
                        job.check_cancelled()
                        job.begin_commit()
                    connection.commit()
                    committed = True
                    return result
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

            verification = self._connect()
            try:
                verification.execute("BEGIN")
                self._authorize_full_export(verification, actor)
                mcp25_read.validate_export_replay(
                    self, verification, actor, replay_request_id, replay_after_commit, _now(),
                )
                verification.commit()
            except BaseException:
                verification.rollback()
                raise
            finally:
                verification.close()
            replay_after_commit.get("data", {}).pop("download", None)
            return _public_result(replay_after_commit, public_ids, named=named)
        finally:
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)
            if published_path is not None and not committed:
                published_path.unlink(missing_ok=True)

    def _replay(self, db, action, params, channel, digest, actor=None):
        if channel == "mcp" and action in {"set_prompt_state", "snooze_prompt", "clear_snooze", "record_feedback", "set_prompt_tags"} and params.get("prompt_id"):
            self._find_prompt(db, params["prompt_id"], channel, {}, actor=actor)
        receipt = db.execute("SELECT * FROM request_receipts WHERE request_id = ?", (params["request_id"],)).fetchone()
        if receipt is None:
            return None
        if receipt["action"] != action or receipt["params_hash"] != digest or receipt["channel"] not in (None, channel):
            raise _BookError("idempotency_conflict", "request_id 已用于不同入口、操作或参数。", {"request_id": params["request_id"]})
        response = json.loads(receipt["response_json"])
        if response.get("receipt_payload_pruned") is True or response.get("status") == "receipt_payload_pruned":
            raise _BookError("receipt_payload_pruned", "该成功回执的正文已按保留期限清理，不能重放为新写入。", {"request_id": params["request_id"]})
        if channel == "mcp" and receipt["channel"] is None and action == "save_prompt":
            self._find_prompt(db, response["data"]["id"], channel, {}, actor=actor)
        return mcp_workflows.replay_result(self, db, action, params, channel, response)

    def _run(self, db: sqlite3.Connection, action: str, params: dict[str, Any], channel: str,
             actor: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
        now = now or _now()
        actor = actor or (mcp24_access.legacy_actor() if channel == "mcp" else mcp24_access.local_actor())
        if action in MCP24_HANDLERS:
            module = MCP24_HANDLERS[action]
            data = module.handle(self, db, action, params, channel, _validate_params, now, actor)
            return {"status": "ok", "data": data}
        if action in mcp_workflows.HANDLERS:
            data = mcp_workflows.handle(self, db, action, params, channel, _validate_params, now, actor=actor)
            return {"status": "ok", "data": data}
        if action in materials.ACTION_MAP or action in LOCAL_ACTIONS:
            collection_action = materials.ACTION_MAP.get(action)
            full_get = (mcp24_access.has_full_access(actor) and collection_action is not None
                        and collection_action[1] == "get")
            if full_get:
                params = {**params, "include_private": True}
            find_prompt = lambda connection, prompt_id, selected_channel, arguments: self._find_prompt(
                connection, prompt_id, selected_channel, arguments, actor=actor,
            )
            data = materials.handle(db, action, params, channel, _iso(now), self._resolve_topic, find_prompt)
            if full_get:
                data["prompt_id"] = materials._row(db, collection_action[0], params["id"])["prompt_id"]
            return {"status": "ok", "data": data}
        handlers = {
            "pick_prompt": self._pick_prompt,
            "get_prompt": self._get_prompt,
            "narrow_prompt": self._narrow_prompt,
            "get_angles": self._get_angles,
            "draft_prompt": self._draft_prompt,
            "check_prompt": self._check_prompt,
            "save_prompt": self._save_prompt,
            "list_prompts": self._list_prompts,
            "list_tags": self._list_tags,
            "add_tag": self._add_tag,
            "set_prompt_tags": self._set_prompt_tags,
            "set_prompt_state": self._set_prompt_state,
            "snooze_prompt": self._snooze_prompt,
            "clear_snooze": self._clear_snooze,
            "get_preferences": self._get_preferences,
            "get_preference_reason": self._get_preference_reason,
            "add_topic": self._add_topic,
            "update_topic": self._update_topic,
            "set_direction_weight": self._set_direction_weight,
            "set_topic_weight": self._set_topic_weight,
            "reset_weights": self._reset_weights,
            "end_preference": self._end_preference,
            "record_feedback": self._record_feedback,
            "list_feedback": self._list_feedback,
        }
        if action in {"get_prompt", "narrow_prompt", "get_angles", "check_prompt", "save_prompt", "list_prompts",
                      "set_prompt_state", "snooze_prompt", "clear_snooze", "record_feedback", "list_feedback", "set_prompt_tags"}:
            return handlers[action](db, params, channel, actor)
        if action == "pick_prompt":
            return handlers[action](db, params, channel, actor=actor)
        return handlers[action](db, params, channel)

    @staticmethod
    def _private_allowed(channel: str, params: dict[str, Any], actor: dict[str, Any] | None = None) -> bool:
        return (channel == "local" or params.get("include_private", False) is True
                or mcp24_access.has_full_access(actor))

    @staticmethod
    def _find_prompt(db: sqlite3.Connection, prompt_id: str, channel: str, params: dict[str, Any],
                     actor: dict[str, Any] | None = None) -> sqlite3.Row:
        row = db.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
        if row is None or (row["visibility"] == "private" and not BookService._private_allowed(channel, params, actor)):
            raise _BookError("not_found", "未找到可访问的命题。", {"prompt_id": prompt_id})
        return row

    def _find_prompt_for_actor(self, db: sqlite3.Connection, prompt_id: str, channel: str,
                               actor: dict[str, Any], params: dict[str, Any] | None = None) -> sqlite3.Row:
        return self._find_prompt(db, prompt_id, channel, params or {}, actor=actor)

    @staticmethod
    def _resolve_topic(db: sqlite3.Connection, value: str | None) -> sqlite3.Row | None:
        if value is None:
            return None
        row = db.execute("SELECT * FROM topics WHERE id = ?", (value,)).fetchone()
        if row is None:
            row = db.execute("SELECT * FROM topics WHERE name = ? COLLATE NOCASE", (value,)).fetchone()
        return row

    @staticmethod
    def _visible_rows(db: sqlite3.Connection, channel: str, actor: dict[str, Any] | None = None) -> list[sqlite3.Row]:
        if channel == "local" or mcp24_access.has_full_access(actor):
            return db.execute("SELECT * FROM prompts").fetchall()
        return db.execute("SELECT * FROM prompts WHERE visibility != 'private'").fetchall()

    @staticmethod
    def _family_roots(rows: list[sqlite3.Row]) -> dict[str, str]:
        parents = {row["id"]: row["parent_id"] for row in rows}
        roots: dict[str, str] = {}
        for prompt_id in parents:
            current = prompt_id
            seen: set[str] = set()
            while parents.get(current) in parents and current not in seen:
                seen.add(current)
                current = parents[current]
            roots[prompt_id] = current
        return roots

    def _prompt_candidates(self, db: sqlite3.Connection, params: dict[str, Any], channel: str, *,
                           excluded_ids=(), excluded_directions=(), allowed_depths=(), actor=None, relaxed=()):
        relaxed = set(relaxed)
        topic = self._resolve_topic(db, params.get("topic")) if "topic" not in relaxed else None
        direction = params.get("direction")
        if "topic" in params and topic is None and "topic" not in relaxed:
            raise _BookError("no_candidate", "指定的小主题不存在。", {"reason": "topic_not_found"})
        if topic is not None:
            if direction is not None and topic["direction"] != direction:
                raise _BookError("no_candidate", "指定方向与小主题不一致。", {"reason": "scope_conflict"})
            if "topic" not in relaxed:
                direction = topic["direction"]
            if not topic["active"] and "topic" not in relaxed:
                raise _BookError("no_candidate", "指定的小主题已停用。", {"reason": "topic_inactive"})

        now = _now()
        visible = self._visible_rows(db, channel, actor)
        all_rows = db.execute("SELECT * FROM prompts").fetchall()
        all_roots = self._family_roots(all_rows)
        roots = {row["id"]: all_roots[row["id"]] for row in visible}
        active_topics = {row["id"]: bool(row["active"]) for row in db.execute("SELECT id, active FROM topics")}
        current_id = params.get("current_id")
        current_root = all_roots.get(current_id) if current_id else None
        recent_roots = {all_roots[identifier] for identifier in excluded_ids if identifier in all_roots}
        blocked_families = set()
        for row in all_rows:
            if row["state"] == "written" or (row["snoozed_until"] and _parse_timestamp(row["snoozed_until"]) > now):
                blocked_families.add(all_roots[row["id"]])

        candidates = visible
        stages = []

        def apply(name, predicate, enabled=True):
            nonlocal candidates
            if not enabled:
                return
            before = len(candidates)
            candidates = [row for row in candidates if predicate(row)]
            stages.append({"filter": name, "before": before, "excluded": before - len(candidates), "after": len(candidates)})

        apply("excluded_directions", lambda row: row["direction"] not in excluded_directions,
              bool(excluded_directions) and "excluded_directions" not in relaxed)
        apply("time_depth", lambda row: row["depth"] in allowed_depths,
              bool(allowed_depths) and "time_depth" not in relaxed)
        apply("direction", lambda row: row["direction"] == direction,
              direction is not None and "direction" not in relaxed)
        apply("topic", lambda row: row["topic_id"] == topic["id"],
              topic is not None and "topic" not in relaxed)
        apply("depth", lambda row: row["depth"] == params["depth"],
              "depth" in params and "depth" not in relaxed)
        if params.get("tag_ids"):
            tagged = _matching_tagged_prompt_ids(db, params["tag_ids"])
            apply("tag_ids", lambda row: row["id"] in tagged)
        apply("inactive_topic", lambda row: row["topic_id"] is None or active_topics.get(row["topic_id"], False))
        apply("unavailable_family", lambda row: roots[row["id"]] not in blocked_families)
        apply("recent", lambda row: roots[row["id"]] not in recent_roots
              and (current_root is None or roots[row["id"]] != current_root)
              and not (current_id and current_root is None and row["id"] == current_id),
              "recent" not in relaxed)
        return topic, direction, candidates, stages

    def _prompt_candidate_report(self, db: sqlite3.Connection, params: dict[str, Any], channel: str, *,
                                 excluded_ids=(), excluded_directions=(), allowed_depths=(), relax_filters=(), actor=None) -> dict[str, Any]:
        relax_filters = (relax_filters,) if isinstance(relax_filters, str) else relax_filters
        try:
            _topic, _direction, candidates, stages = self._prompt_candidates(
                db, params, channel, excluded_ids=excluded_ids,
                excluded_directions=excluded_directions, allowed_depths=allowed_depths, actor=actor,
            )
        except _BookError as error:
            return {"candidate_count": 0, "filter_stages": [], "relaxations": [], "reason": error.details.get("reason", error.code)}
        relaxations = []
        for name in dict.fromkeys(relax_filters):
            if name not in {"direction", "topic", "depth", "time_depth", "excluded_directions", "recent"}:
                continue
            _topic, _direction, relaxed_candidates, relaxed_stages = self._prompt_candidates(
                db, params, channel, excluded_ids=excluded_ids,
                excluded_directions=excluded_directions, allowed_depths=allowed_depths,
                actor=actor, relaxed=(name,),
            )
            relaxations.append({
                "filter": name, "candidate_count": len(relaxed_candidates),
                "remaining_conditions": [stage["filter"] for stage in relaxed_stages],
            })
        report = {"candidate_count": len(candidates), "filter_stages": stages, "relaxations": relaxations}
        if not candidates:
            report["reason"] = "no_eligible_prompts"
        return report

    def _pick_prompt(self, db: sqlite3.Connection, params: dict[str, Any], channel: str, *, excluded_ids=(),
                     excluded_directions=(), allowed_depths=(), explain=False, actor=None) -> dict[str, Any]:
        topic, direction, candidates, _stages = self._prompt_candidates(
            db, params, channel, excluded_ids=excluded_ids,
            excluded_directions=excluded_directions, allowed_depths=allowed_depths, actor=actor,
        )
        now = _now()
        if not candidates:
            reason = "no_eligible_prompts"
            raise _BookError("no_candidate", "当前范围没有可出的命题。", {"reason": reason})
        preference = self._get_preferences(db, {}, channel)["data"]
        dir_weights = {item["id"]: item["effective_weight"] for item in preference["directions"]}
        topic_weights = {item["id"]: item["effective_weight"] for item in preference["topics"] if item["active"]}
        by_direction: dict[str, dict[str | None, list[sqlite3.Row]]] = {}
        for row in candidates:
            topic_id = row["topic_id"]
            if topic is None and topic_id is not None and topic_weights.get(topic_id, 1.0) <= 0:
                continue
            by_direction.setdefault(row["direction"], {}).setdefault(topic_id, []).append(row)
        if not by_direction:
            raise _BookError("no_candidate", "当前范围的主题权重均为零。", {"reason": "zero_topic_weights"})
        available_directions = list(by_direction)
        if direction is None:
            direction_weights = [dir_weights.get(item, 0.0) for item in available_directions]
            if not any(weight > 0 for weight in direction_weights):
                raise _BookError("no_candidate", "当前范围的方向权重均为零。", {"reason": "zero_direction_weights"})
            chosen_direction = random.choices(available_directions, weights=direction_weights, k=1)[0]
        else:
            chosen_direction = direction
        if chosen_direction not in by_direction:
            raise _BookError("no_candidate", "当前方向的主题权重均为零。", {"reason": "zero_topic_weights"})
        by_topic = by_direction[chosen_direction]
        if topic is None:
            available_topics = list(by_topic)
            weights = [topic_weights.get(item, 1.0) if item is not None else 1.0 for item in available_topics]
            chosen_topic = random.choices(available_topics, weights=weights, k=1)[0]
        else:
            chosen_topic = topic["id"]
        options = by_topic[chosen_topic]
        chosen, feedback = self._pick_with_feedback(db, options, chosen_direction, chosen_topic, "depth" in params, now)
        data = {"prompt": self._prompt_view(db, chosen, now, channel)}
        if explain:
            data["decision"] = {"direction": chosen_direction, "topic_id": chosen_topic,
                                "direction_weight": dir_weights.get(chosen_direction) if direction is None else None,
                                "topic_weight": topic_weights.get(chosen_topic, 1.0) if topic is None else None,
                                "within_topic_candidates": len(options), "feedback": feedback}
        return {"status": "ok", "data": data}

    @staticmethod
    def _pick_with_feedback(db, options, direction, topic_id, explicit_depth, now):
        prompt_feedback = {}
        scope_feedback = {}
        for item in db.execute(
            """SELECT scope, prompt_id, feedback FROM feedback WHERE created_at >= ? AND
               (scope = 'prompt' OR (scope = 'direction' AND direction = ?) OR (scope = 'topic' AND topic_id = ?))
               ORDER BY created_at DESC, rowid DESC""",
            (_iso(now - timedelta(days=7)), direction, topic_id),
        ):
            if item["scope"] == "prompt":
                prompt_feedback.setdefault(item["prompt_id"], item["feedback"])
            else:
                scope_feedback.setdefault(item["scope"], item["feedback"])
        preference = scope_feedback.get("topic") or scope_feedback.get("direction")

        def score(row):
            feedback = prompt_feedback.get(row["id"])
            ranking = 2 if feedback == "helpful" else -2 if feedback in ("not_helpful", "not_relevant", "too_broad", "too_narrow", "too_heavy", "too_academic") else 0
            if preference == "too_broad" and row["parent_id"]:
                ranking += 1
            elif preference == "too_narrow" and not row["parent_id"]:
                ranking += 1
            elif not explicit_depth and preference in ("too_heavy", "too_academic") and row["depth"] == "light":
                ranking += 1
            return ranking

        scores = [score(row) for row in options]
        best = max(scores)
        selected = random.choice([row for row, rank in zip(options, scores, strict=True) if rank == best])
        return selected, {"topic": scope_feedback.get("topic"), "direction": scope_feedback.get("direction"),
                          "prompt": prompt_feedback.get(selected["id"]), "selected_rank": best}

    def _get_prompt(self, db: sqlite3.Connection, params: dict[str, Any], channel: str,
                    actor: dict[str, Any] | None = None) -> dict[str, Any]:
        row = self._find_prompt(db, params["prompt_id"], channel, params, actor=actor)
        now = _now()
        prompt = (_prompt_dict(db, row, now) if params.get("detail") == "full" or mcp24_access.has_full_access(actor)
                  else self._prompt_view(db, row, now, channel, include_state=True))
        return {"status": "ok", "data": {"prompt": prompt}}

    def _narrow_prompt(self, db: sqlite3.Connection, params: dict[str, Any], channel: str,
                       actor: dict[str, Any] | None = None) -> dict[str, Any]:
        parent = self._find_prompt(db, params["prompt_id"], channel, params, actor=actor)
        now = _now()
        session = mcp25_thought.effective_session_conditions(
            db, params.get("conversation_id"), channel=channel, actor=actor, now=now,
        )
        conditions = session["conditions"]
        if (conditions.get("direction") not in (None, parent["direction"])
                or conditions.get("topic") not in (None, parent["topic_id"])):
            raise _BookError("no_candidate", "会话条件与当前题目范围不一致，不能跨题族窄化。",
                             {"reason": "session_scope_conflict", "conflict_fields": [
                                 key for key, actual in (("direction", parent["direction"]),
                                                         ("topic", parent["topic_id"]))
                                 if key in conditions and conditions[key] != actual
                             ]})
        if parent["direction"] in conditions.get("exclude_directions", ()):
            raise _BookError("no_candidate", "当前题目的方向在会话排除范围内。",
                             {"reason": "session_direction_excluded", "direction": parent["direction"]})
        effective = dict(conditions)
        sources = dict(session["sources"])
        expires_at = dict(session["expires_at"])
        conflicts = []
        if "depth" in params:
            if conditions.get("depth") not in (None, params["depth"]):
                conflicts.append("depth")
            effective["depth"] = params["depth"]
            sources["depth"] = {"source": "call"}
            expires_at.pop("depth", None)
        all_rows = db.execute("SELECT * FROM prompts").fetchall()
        roots = self._family_roots(all_rows)
        family_root = roots[parent["id"]]
        blocked = any(
            roots[row["id"]] == family_root and (
                row["state"] == "written" or
                (row["snoozed_until"] and _parse_timestamp(row["snoozed_until"]) > now)
            )
            for row in all_rows
        )
        if blocked:
            raise _BookError("no_candidate", "该题族已写过或仍在暂缓期，当前不能再次推荐。", {"reason": "family_blocked"})
        children = db.execute("SELECT * FROM prompts WHERE parent_id = ?", (parent["id"],)).fetchall()
        children = [row for row in children if channel == "local" or mcp24_access.has_full_access(actor) or row["visibility"] != "private"]
        depth = effective.get("depth")
        allowed_depths = []
        if depth is not None:
            children = [row for row in children if row["depth"] == depth]
        elif conditions.get("minutes") is not None and conditions["minutes"] <= 15:
            allowed_depths = sorted({"light"} if conditions["minutes"] <= 5 else {"light", "medium"})
            children = [row for row in children if row["depth"] in allowed_depths]
        effective_conditions = {**effective, "allowed_depths": allowed_depths,
                                "condition_sources": sources, "condition_expires_at": expires_at,
                                "conflicts": conflicts}
        if not children:
            if any(key in conditions for key in ("direction", "topic", "depth", "minutes", "exclude_directions")):
                raise _BookError("no_candidate", "当前题族没有符合会话条件的子题。",
                                 {"reason": "session_conditions_no_candidate",
                                  "effective_conditions": effective_conditions})
            text = parent["text"].rstrip("。？?!！")
            draft = seed.NARROW[parent["direction"]].format(focus=text[:100])
            result = self._draft_view(draft, parent["direction"], parent["topic_id"], depth or "light", parent["id"])
            if params.get("conversation_id"):
                result["data"]["effective_conditions"] = effective_conditions
            return result
        data = {"prompt": self._prompt_view(db, random.choice(children), now, channel)}
        if params.get("conversation_id"):
            data["effective_conditions"] = effective_conditions
        return {"status": "ok", "data": data}

    @staticmethod
    def _prompt_view(db: sqlite3.Connection, row: sqlite3.Row, now: datetime, channel: str,
                     include_state: bool = False) -> dict[str, Any]:
        prompt = _prompt_dict(db, row, now)
        if channel == "mcp":
            keys = ["id", "text", "direction", "topic_id", "depth", "parent_id", "version", "tags"]
            if include_state and row["visibility"] != "private":
                keys.extend(("state", "snoozed_until"))
            return {key: prompt[key] for key in keys}
        return prompt

    def _get_angles(self, db: sqlite3.Connection, params: dict[str, Any], channel: str,
                    actor: dict[str, Any] | None = None) -> dict[str, Any]:
        if "prompt_id" in params:
            prompt = self._find_prompt(db, params["prompt_id"], channel, params, actor=actor)
            direction, topic_id = prompt["direction"], prompt["topic_id"]
        elif "direction" in params:
            direction = params["direction"]
            topic = self._resolve_topic(db, params.get("topic"))
            if "topic" in params and (topic is None or topic["direction"] != direction):
                raise _BookError("invalid_params", "草题方向与主题不一致。")
            topic_id = topic["id"] if topic else None
        else:
            raise _BookError("invalid_params", "请提供命题编号或草题方向。")
        return {"status": "ok", "data": {"angles": self._angles(direction, topic_id)}}

    @staticmethod
    def _angles(direction, topic_id):
        return list(seed.TOPIC_ANGLES.get(topic_id, seed.ANGLES[direction]))[:2]

    def _draft_view(self, text, direction, topic_id, depth, parent_id=None):
        return {"status": "ok", "data": {"prompt": {
            "id": None, "text": text, "direction": direction, "topic_id": topic_id, "depth": depth,
            "parent_id": parent_id, "draft": True,
        }}}

    def _draft_prompt(self, db, params, channel):
        direction = params["direction"]
        topic = self._resolve_topic(db, params.get("topic"))
        if "topic" in params and (topic is None or not topic["active"] or topic["direction"] != direction):
            raise _BookError("no_candidate", "指定的小主题不可用。", {"reason": "topic_unavailable"})
        focus = params.get("focus") or (topic["name"] if topic else dict((key, name) for key, name, _ in seed.DIRECTIONS)[direction])
        focus = focus.strip().rstrip("。？?!！") or focus.strip()
        depth = params.get("depth", "medium")
        drafts = [item.format(focus=focus) for item in seed.DRAFTS[direction]]
        if depth == "deep":
            drafts.reverse()
        elif depth == "medium":
            drafts = drafts[1:] + drafts[:1]
        all_texts = {self._normalize_text(row[0]) for row in db.execute("SELECT text FROM prompts")}
        result = next((draft for draft in drafts if self._normalize_text(draft) not in all_texts), None)
        if result is None:
            raise _BookError("no_candidate", "这个线索已有相同的草题，请缩小或换一个线索。", {"reason": "duplicate_drafts"})
        return self._draft_view(result, direction, topic["id"] if topic else None, depth)

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        if left == right:
            return 1.0
        ratio = difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()
        left_pairs = {left[index:index + 2] for index in range(max(0, len(left) - 1))}
        right_pairs = {right[index:index + 2] for index in range(max(0, len(right) - 1))}
        if not left_pairs or not right_pairs:
            return ratio
        overlap = len(left_pairs & right_pairs) / len(left_pairs | right_pairs)
        return max(ratio, overlap)

    def _similar_prompt_rows(self, db: sqlite3.Connection, text: str, channel: str, *,
                             actor: dict[str, Any] | None = None, exclude_id: str | None = None,
                             direction: str | None = None, topic_id: str | None = None,
                             depth: str | None = None) -> list[dict[str, Any]]:
        normalized = self._normalize_text(text)
        candidates = []
        for row in self._visible_rows(db, channel, actor):
            if row["id"] == exclude_id:
                continue
            if direction is not None and row["direction"] != direction:
                continue
            if topic_id is not None and row["topic_id"] != topic_id:
                continue
            if depth is not None and row["depth"] != depth:
                continue
            old_text = self._normalize_text(row["text"])
            duplicate = normalized == old_text
            similarity = self._similarity(normalized, old_text)
            if duplicate or similarity >= _SIMILARITY_THRESHOLD:
                candidates.append({"row": row, "similarity": similarity,
                                   "duplicate": duplicate,
                                   "similar": not duplicate and similarity >= _SIMILARITY_THRESHOLD})
        return sorted(candidates, key=lambda item: (-item["similarity"], item["row"]["id"]))

    def _check_prompt(self, db: sqlite3.Connection, params: dict[str, Any], channel: str,
                      actor: dict[str, Any] | None = None) -> dict[str, Any]:
        text = self._normalize_text(params["text"])
        topic = self._resolve_topic(db, params.get("topic"))
        if "topic" in params and topic is None:
            raise _BookError("not_found", "未找到指定的小主题。", {"topic": params["topic"]})
        candidates = self._similar_prompt_rows(
            db, params["text"], channel, actor=actor, exclude_id=params.get("exclude_id"),
            direction=params.get("direction"), topic_id=topic["id"] if topic else None,
            depth=params.get("depth"),
        )
        duplicate = any(item["duplicate"] for item in candidates)
        similar = any(item["similar"] for item in candidates)
        return {"status": "ok", "data": {"duplicate": duplicate, "similar": similar and not duplicate}}

    def _save_prompt(self, db: sqlite3.Connection, params: dict[str, Any], channel: str,
                     actor: dict[str, Any] | None = None) -> dict[str, Any]:
        topic = self._resolve_topic(db, params["topic"]) if "topic" in params else None
        if "topic" in params and topic is None:
            raise _BookError("not_found", "未找到指定的小主题。", {"topic": params["topic"]})
        if topic is not None and topic["direction"] != params["direction"]:
            raise _BookError("invalid_params", "命题方向与小主题不一致。", {"field": "topic"})
        if "parent_id" in params:
            parent = self._find_prompt(db, params["parent_id"], channel, params, actor=actor)
            expected_topic_id = topic["id"] if topic else None
            if parent["direction"] != params["direction"] or parent["topic_id"] != expected_topic_id:
                raise _BookError("invalid_params", "收窄命题必须与父题属于同一方向和小主题。", {"field": "parent_id"})
        privacy_guard.check_action(db, "save_prompt", params)
        tag_ids = _validated_tag_ids(db, params["tag_ids"]) if params.get("tag_ids") else []
        now = _iso(_now())
        prompt_id = str(uuid.uuid4())
        state = params.get("state")
        snoozed_until = _iso(_parse_timestamp(params["snoozed_until"])) if "snoozed_until" in params else None
        if snoozed_until is not None and _parse_timestamp(snoozed_until) <= _now():
            raise _BookError("invalid_params", "暂缓时间必须晚于当前时间。", {"field": "snoozed_until"})
        db.execute(
            """INSERT INTO prompts
               (id, text, direction, topic_id, depth, parent_id, state, snoozed_until,
                visibility, origin, version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (prompt_id, params["text"].strip(), params["direction"], topic["id"] if topic else None,
             params.get("depth"), params.get("parent_id"), state, snoozed_until,
             "shared" if channel == "mcp" else "private", "mcp" if channel == "mcp" else "web", now, now),
        )
        db.executemany("INSERT INTO prompt_tags(prompt_id, tag_id) VALUES (?, ?)",
                       [(prompt_id, tag_id) for tag_id in tag_ids])
        return {"status": "ok", "data": {
            "id": prompt_id, "version": 1, "request_id": params["request_id"],
            "state": state, "snoozed_until": snoozed_until,
        }}

    def _list_prompts(self, db: sqlite3.Connection, params: dict[str, Any], channel: str,
                      actor: dict[str, Any] | None = None) -> dict[str, Any]:
        rows = self._visible_rows(db, channel, actor)
        topic = self._resolve_topic(db, params["topic"]) if "topic" in params else None
        if "topic" in params and topic is None:
            raise _BookError("not_found", "未找到指定的小主题。", {"topic": params["topic"]})
        if "direction" in params:
            rows = [row for row in rows if row["direction"] == params["direction"]]
        if topic is not None:
            rows = [row for row in rows if row["topic_id"] == topic["id"]]
        if "state" in params:
            rows = [row for row in rows if row["state"] == params["state"]]
        if params.get("tag_ids"):
            tagged = _matching_tagged_prompt_ids(db, params["tag_ids"])
            rows = [row for row in rows if row["id"] in tagged]
        rows.sort(key=lambda row: (row["updated_at"], row["id"]), reverse=True)
        limit = params.get("limit", _DEFAULT_LIMIT)
        offset = params.get("offset", 0)
        now = _now()
        return {"status": "ok", "data": {
            "prompts": [self._prompt_view(db, row, now, channel, include_state=True) for row in rows[offset:offset + limit]],
            "total": len(rows), "limit": limit, "offset": offset,
        }}

    def _list_tags(self, db, params, channel):
        tags = [dict(row) for row in db.execute("SELECT id, name FROM tags ORDER BY name_key")]
        return {"status": "ok", "data": {"tags": tags}}

    def _add_tag(self, db, params, channel):
        name = params["name"].strip()
        name_key = unicodedata.normalize("NFKC", name).casefold()
        if existing := db.execute("SELECT id FROM tags WHERE name_key = ?", (name_key,)).fetchone():
            raise _BookError("tag_conflict", "已有同名标签。", {"tag_id": existing["id"]})
        tag_id = str(uuid.uuid4())
        db.execute("INSERT INTO tags(id, name, name_key) VALUES (?, ?, ?)", (tag_id, name, name_key))
        return {"status": "ok", "data": {"request_id": params["request_id"], "tag": {"id": tag_id, "name": name}}}

    def _set_prompt_tags(self, db, params, channel, actor=None):
        prompt_id = params["prompt_id"]
        row = self._assert_prompt_version(db, prompt_id, params["expected_version"], channel, actor)
        desired = set(_validated_tag_ids(db, params["tag_ids"]))
        current = {tag["id"] for tag in _prompt_tags(db, prompt_id)}
        version = row["version"]
        if desired != current:
            db.executemany("DELETE FROM prompt_tags WHERE prompt_id = ? AND tag_id = ?",
                           [(prompt_id, tag_id) for tag_id in current - desired])
            db.executemany("INSERT INTO prompt_tags(prompt_id, tag_id) VALUES (?, ?)",
                           [(prompt_id, tag_id) for tag_id in desired - current])
            version += 1
            db.execute("UPDATE prompts SET version = ?, updated_at = ? WHERE id = ?", (version, _iso(_now()), prompt_id))
        return {"status": "ok", "data": {"request_id": params["request_id"], "id": prompt_id,
                                         "version": version, "tags": _prompt_tags(db, prompt_id)}}

    def _assert_prompt_version(self, db: sqlite3.Connection, prompt_id: str, expected: int, channel: str,
                               actor: dict[str, Any] | None = None) -> sqlite3.Row:
        row = self._find_prompt(db, prompt_id, channel, {}, actor=actor)
        if row["version"] != expected:
            raise _BookError("version_conflict", "命题已被更正，请刷新后重试。", {"expected_version": expected, "actual_version": row["version"]})
        return row

    def _set_prompt_state(self, db: sqlite3.Connection, params: dict[str, Any], channel: str,
                          actor: dict[str, Any] | None = None) -> dict[str, Any]:
        row = self._assert_prompt_version(db, params["prompt_id"], params["expected_version"], channel, actor)
        privacy_guard.check_action(db, "set_prompt_state", params)
        version = row["version"]
        if row["state"] != params["state"]:
            version += 1
            db.execute("UPDATE prompts SET state = ?, version = ?, updated_at = ? WHERE id = ?",
                       (params["state"], version, _iso(_now()), row["id"]))
        return {"status": "ok", "data": {
            "request_id": params["request_id"], "id": row["id"], "version": version,
            "state": params["state"], "snoozed_until": row["snoozed_until"],
        }}

    def _snooze_prompt(self, db: sqlite3.Connection, params: dict[str, Any], channel: str,
                       actor: dict[str, Any] | None = None) -> dict[str, Any]:
        row = self._assert_prompt_version(db, params["prompt_id"], params["expected_version"], channel, actor)
        privacy_guard.check_action(db, "snooze_prompt", params)
        snoozed_until = _iso(_parse_timestamp(params["snoozed_until"]))
        if _parse_timestamp(snoozed_until) <= _now():
            raise _BookError("invalid_params", "暂缓时间必须晚于当前时间。", {"field": "snoozed_until"})
        version = row["version"]
        if row["snoozed_until"] != snoozed_until:
            version += 1
            db.execute("UPDATE prompts SET snoozed_until = ?, version = ?, updated_at = ? WHERE id = ?",
                       (snoozed_until, version, _iso(_now()), row["id"]))
        return {"status": "ok", "data": {
            "request_id": params["request_id"], "id": row["id"], "version": version,
            "state": row["state"], "snoozed_until": snoozed_until,
        }}

    def _clear_snooze(self, db: sqlite3.Connection, params: dict[str, Any], channel: str,
                      actor: dict[str, Any] | None = None) -> dict[str, Any]:
        row = self._assert_prompt_version(db, params["prompt_id"], params["expected_version"], channel, actor)
        privacy_guard.check_action(db, "clear_snooze", params)
        version = row["version"]
        if row["snoozed_until"] is not None:
            version += 1
            db.execute("UPDATE prompts SET snoozed_until = NULL, version = ?, updated_at = ? WHERE id = ?",
                       (version, _iso(_now()), row["id"]))
        return {"status": "ok", "data": {
            "request_id": params["request_id"], "id": row["id"], "version": version,
            "state": row["state"], "snoozed_until": None,
        }}

    def _get_preferences(self, db: sqlite3.Connection, params: dict[str, Any], channel: str) -> dict[str, Any]:
        now = _now()
        version = db.execute("SELECT version FROM preference_meta WHERE singleton = 1").fetchone()[0]
        reasons = {
            (row["item_kind"], row["item_id"], row["scope"]): row["reason"]
            for row in db.execute("SELECT * FROM preference_reasons")
        } if channel == "local" else {}
        directions = []
        direction_rows = {row["id"]: row for row in db.execute("SELECT * FROM directions")}
        for direction_id, _, _ in seed.DIRECTIONS:
            row = direction_rows[direction_id]
            effective, recent, expires_at = _effective_weight(row["baseline_weight"], row["recent_weight"], row["recent_expires_at"], now)
            directions.append({
                "id": row["id"], "name": row["name"],
                "baseline_weight": row["baseline_weight"], "effective_weight": effective,
                "recent_weight": recent, "expires_at": expires_at,
            })
            if channel == "local":
                directions[-1].update(baseline_reason=reasons.get(("direction", row["id"], "baseline")), recent_reason=reasons.get(("direction", row["id"], "recent")) if recent is not None else None)
        topics = []
        for row in db.execute("SELECT * FROM topics ORDER BY direction, name"):
            effective, recent, expires_at = _effective_weight(row["baseline_weight"], row["recent_weight"], row["recent_expires_at"], now)
            topics.append({
                "id": row["id"], "direction": row["direction"], "name": row["name"],
                "parent_id": row["parent_id"], "active": bool(row["active"]),
                "baseline_weight": row["baseline_weight"], "effective_weight": effective,
                "recent_weight": recent, "expires_at": expires_at,
            })
            if channel == "local":
                topics[-1].update(baseline_reason=reasons.get(("topic", row["id"], "baseline")), recent_reason=reasons.get(("topic", row["id"], "recent")) if recent is not None else None)
        return {"status": "ok", "data": {"version": version, "directions": directions, "topics": topics}}

    def _get_preference_reason(self, db, params, channel):
        if ("direction" in params) == ("topic_id" in params):
            raise _BookError("invalid_params", "请只指定一个方向或一个主题。")
        kind = "direction" if "direction" in params else "topic"
        identifier = params["direction" if kind == "direction" else "topic_id"]
        if kind == "topic" and self._resolve_topic(db, identifier) is None:
            raise _BookError("not_found", "未找到指定主题。")
        scope = params["scope"]
        row = db.execute("SELECT reason, updated_at FROM preference_reasons WHERE item_kind = ? AND item_id = ? AND scope = ?", (kind, identifier, scope)).fetchone()
        return {"status": "ok", "data": {
            "id": identifier, "kind": kind, "scope": scope,
            "reason": row["reason"] if row else None, "updated_at": row["updated_at"] if row else None,
        }}

    @staticmethod
    def _save_preference_reason(db, kind, identifier, params):
        db.execute(
            """INSERT INTO preference_reasons(item_kind, item_id, scope, reason, updated_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(item_kind, item_id, scope) DO UPDATE SET reason = excluded.reason, updated_at = excluded.updated_at""",
            (kind, identifier, params["scope"], params.get("reason", "").strip() or None, _iso(_now())),
        )

    @staticmethod
    def _preference_version(db: sqlite3.Connection, expected: int) -> int:
        current = db.execute("SELECT version FROM preference_meta WHERE singleton = 1").fetchone()[0]
        if current != expected:
            raise _BookError("version_conflict", "偏好已被更改，请刷新后重试。", {"expected_version": expected, "actual_version": current})
        return current

    @staticmethod
    def _bump_preference_version(db: sqlite3.Connection, current: int) -> int:
        version = current + 1
        db.execute("UPDATE preference_meta SET version = ? WHERE singleton = 1", (version,))
        return version

    def _add_topic(self, db: sqlite3.Connection, params: dict[str, Any], channel: str) -> dict[str, Any]:
        version = self._preference_version(db, params["expected_version"])
        name = params["name"].strip()
        if db.execute("SELECT 1 FROM topics WHERE direction = ? AND name = ? COLLATE NOCASE", (params["direction"], name)).fetchone():
            raise _BookError("topic_conflict", "该方向下已有同名主题。", {"direction": params["direction"], "name": name})
        parent_id = params.get("parent_id")
        if parent_id is not None:
            parent = db.execute("SELECT direction FROM topics WHERE id = ?", (parent_id,)).fetchone()
            if parent is None or parent["direction"] != params["direction"]:
                raise _BookError("not_found", "未找到同方向的父主题。", {"parent_id": parent_id})
        topic_id = str(uuid.uuid4())
        weight = params.get("weight", 1.0)
        db.execute("INSERT INTO topics(id, direction, name, parent_id, baseline_weight) VALUES (?, ?, ?, ?, ?)",
                   (topic_id, params["direction"], name, parent_id, weight))
        new_version = self._bump_preference_version(db, version)
        return {"status": "ok", "data": {
            "request_id": params["request_id"], "version": new_version, "scope": "topic",
            "topic": {"id": topic_id, "direction": params["direction"], "name": name,
                      "parent_id": parent_id, "active": True, "baseline_weight": weight,
                      "effective_weight": weight, "recent_weight": None, "expires_at": None},
        }}

    def _update_topic(self, db: sqlite3.Connection, params: dict[str, Any], channel: str) -> dict[str, Any]:
        version = self._preference_version(db, params["expected_version"])
        row = db.execute("SELECT * FROM topics WHERE id = ?", (params["topic_id"],)).fetchone()
        if row is None:
            raise _BookError("not_found", "未找到指定的小主题。", {"topic_id": params["topic_id"]})
        name = params.get("name", row["name"]).strip() if "name" in params else row["name"]
        if name != row["name"] and db.execute(
            "SELECT 1 FROM topics WHERE direction = ? AND name = ? COLLATE NOCASE AND id != ?",
            (row["direction"], name, row["id"]),
        ).fetchone():
            raise _BookError("topic_conflict", "该方向下已有同名主题。", {"direction": row["direction"], "name": name})
        parent_id = params.get("parent_id", row["parent_id"])
        if parent_id is not None:
            parent = db.execute("SELECT direction FROM topics WHERE id = ?", (parent_id,)).fetchone()
            if parent is None or parent["direction"] != row["direction"]:
                raise _BookError("not_found", "未找到同方向的父主题。", {"parent_id": parent_id})
            current = parent_id
            while current is not None:
                if current == row["id"]:
                    raise _BookError("topic_conflict", "主题层级不能形成循环。", {"topic_id": row["id"]})
                parent_row = db.execute("SELECT parent_id FROM topics WHERE id = ?", (current,)).fetchone()
                current = parent_row["parent_id"] if parent_row else None
        active = int(params.get("active", bool(row["active"])))
        db.execute("UPDATE topics SET name = ?, parent_id = ?, active = ? WHERE id = ?",
                   (name, parent_id, active, row["id"]))
        new_version = self._bump_preference_version(db, version)
        updated = db.execute("SELECT * FROM topics WHERE id = ?", (row["id"],)).fetchone()
        effective, recent, expires_at = _effective_weight(updated["baseline_weight"], updated["recent_weight"], updated["recent_expires_at"], _now())
        return {"status": "ok", "data": {
            "request_id": params["request_id"], "version": new_version, "scope": "topic",
            "topic": {"id": updated["id"], "direction": updated["direction"], "name": updated["name"],
                      "parent_id": updated["parent_id"], "active": bool(updated["active"]),
                      "baseline_weight": updated["baseline_weight"], "effective_weight": effective,
                      "recent_weight": recent, "expires_at": expires_at},
        }}

    def _set_direction_weight(self, db: sqlite3.Connection, params: dict[str, Any], channel: str) -> dict[str, Any]:
        version = self._preference_version(db, params["expected_version"])
        row = db.execute("SELECT * FROM directions WHERE id = ?", (params["direction"],)).fetchone()
        before = {"baseline_weight": row["baseline_weight"], "recent_weight": row["recent_weight"], "expires_at": row["recent_expires_at"]}
        weight = params["weight"]
        if params["scope"] == "baseline":
            db.execute("UPDATE directions SET baseline_weight = ? WHERE id = ?", (weight, row["id"]))
            expires_at = row["recent_expires_at"]
        else:
            expires_at = _iso(_parse_timestamp(params["expires_at"])) if "expires_at" in params else None
            if expires_at is not None and _parse_timestamp(expires_at) <= _now():
                raise _BookError("invalid_params", "近期调整的到期时间必须晚于当前时间。", {"field": "expires_at"})
            db.execute("UPDATE directions SET recent_weight = ?, recent_expires_at = ? WHERE id = ?", (weight, expires_at, row["id"]))
        self._save_preference_reason(db, "direction", row["id"], params)
        new_version = self._bump_preference_version(db, version)
        return {"status": "ok", "data": {
            "request_id": params["request_id"], "version": new_version, "scope": params["scope"],
            "direction": row["id"], "before": before,
            "after": {"weight": weight, "expires_at": expires_at},
        }}

    def _set_topic_weight(self, db: sqlite3.Connection, params: dict[str, Any], channel: str) -> dict[str, Any]:
        version = self._preference_version(db, params["expected_version"])
        row = db.execute("SELECT * FROM topics WHERE id = ?", (params["topic_id"],)).fetchone()
        if row is None:
            raise _BookError("not_found", "未找到指定的小主题。", {"topic_id": params["topic_id"]})
        before = {"baseline_weight": row["baseline_weight"], "recent_weight": row["recent_weight"], "expires_at": row["recent_expires_at"]}
        weight = params["weight"]
        if params["scope"] == "baseline":
            db.execute("UPDATE topics SET baseline_weight = ? WHERE id = ?", (weight, row["id"]))
            expires_at = row["recent_expires_at"]
        else:
            expires_at = _iso(_parse_timestamp(params["expires_at"])) if "expires_at" in params else None
            if expires_at is not None and _parse_timestamp(expires_at) <= _now():
                raise _BookError("invalid_params", "近期调整的到期时间必须晚于当前时间。", {"field": "expires_at"})
            db.execute("UPDATE topics SET recent_weight = ?, recent_expires_at = ? WHERE id = ?", (weight, expires_at, row["id"]))
        self._save_preference_reason(db, "topic", row["id"], params)
        new_version = self._bump_preference_version(db, version)
        return {"status": "ok", "data": {
            "request_id": params["request_id"], "version": new_version, "scope": params["scope"],
            "topic_id": row["id"], "before": before,
            "after": {"weight": weight, "expires_at": expires_at},
        }}

    def _reset_weights(self, db: sqlite3.Connection, params: dict[str, Any], channel: str) -> dict[str, Any]:
        version = self._preference_version(db, params["expected_version"])
        scope = params.get("scope", "all")
        direction = params.get("direction")
        topic_id = params.get("topic_id")
        if topic_id is not None:
            topic = db.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
            if topic is None:
                raise _BookError("not_found", "未找到指定的小主题。", {"topic_id": topic_id})
            db.execute("UPDATE topics SET baseline_weight = 1.0, recent_weight = NULL, recent_expires_at = NULL WHERE id = ?", (topic_id,))
            db.execute("DELETE FROM preference_reasons WHERE item_kind = 'topic' AND item_id = ?", (topic_id,))
        else:
            if scope in ("all", "directions"):
                if direction:
                    default = {item[0]: item[2] for item in seed.DIRECTIONS}[direction]
                    db.execute("UPDATE directions SET baseline_weight = ?, recent_weight = NULL, recent_expires_at = NULL WHERE id = ?", (default, direction))
                    db.execute("DELETE FROM preference_reasons WHERE item_kind = 'direction' AND item_id = ?", (direction,))
                else:
                    for direction_id, _, default in seed.DIRECTIONS:
                        db.execute("UPDATE directions SET baseline_weight = ?, recent_weight = NULL, recent_expires_at = NULL WHERE id = ?", (default, direction_id))
                    db.execute("DELETE FROM preference_reasons WHERE item_kind = 'direction'")
            if scope in ("all", "topics"):
                if direction:
                    db.execute("UPDATE topics SET baseline_weight = 1.0, recent_weight = NULL, recent_expires_at = NULL WHERE direction = ?", (direction,))
                    db.execute("DELETE FROM preference_reasons WHERE item_kind = 'topic' AND item_id IN (SELECT id FROM topics WHERE direction = ?)", (direction,))
                else:
                    db.execute("UPDATE topics SET baseline_weight = 1.0, recent_weight = NULL, recent_expires_at = NULL")
                    db.execute("DELETE FROM preference_reasons WHERE item_kind = 'topic'")
        new_version = self._bump_preference_version(db, version)
        defaults = {item[0]: item[2] for item in seed.DIRECTIONS}
        direction_defaults = None
        if scope in ("all", "directions"):
            direction_defaults = {direction: defaults[direction]} if direction else defaults
        return {"status": "ok", "data": {
            "request_id": params["request_id"], "version": new_version,
            "scope": scope, "direction": direction, "topic_id": topic_id,
            "after": {"direction_weights": direction_defaults,
                      "topic_weight": 1.0 if scope in ("all", "topics") or topic_id is not None else None},
        }}

    def _end_preference(self, db: sqlite3.Connection, params: dict[str, Any], channel: str) -> dict[str, Any]:
        version = self._preference_version(db, params["expected_version"])
        if params["scope"] == "direction":
            row = db.execute("SELECT * FROM directions WHERE id = ?", (params["direction"],)).fetchone()
            if row is None:
                raise _BookError("not_found", "未找到指定方向。", {"direction": params["direction"]})
            changed = row["recent_weight"] is not None or row["recent_expires_at"] is not None
            if changed:
                db.execute("UPDATE directions SET recent_weight = NULL, recent_expires_at = NULL WHERE id = ?", (row["id"],))
        else:
            row = db.execute("SELECT * FROM topics WHERE id = ?", (params["topic_id"],)).fetchone()
            if row is None:
                raise _BookError("not_found", "未找到指定的小主题。", {"topic_id": params["topic_id"]})
            changed = row["recent_weight"] is not None or row["recent_expires_at"] is not None
            if changed:
                db.execute("UPDATE topics SET recent_weight = NULL, recent_expires_at = NULL WHERE id = ?", (row["id"],))
        if changed:
            db.execute("DELETE FROM preference_reasons WHERE item_kind = ? AND item_id = ? AND scope = 'recent'", (params["scope"], row["id"]))
        new_version = self._bump_preference_version(db, version) if changed else version
        return {"status": "ok", "data": {
            "request_id": params["request_id"], "version": new_version,
            "scope": params["scope"], "ended": changed,
        }}

    def _record_feedback(self, db: sqlite3.Connection, params: dict[str, Any], channel: str,
                         actor: dict[str, Any] | None = None) -> dict[str, Any]:
        scope = params["scope"]
        prompt_id = params.get("prompt_id")
        direction = params.get("direction")
        topic_id = None
        if scope == "prompt":
            self._find_prompt(db, prompt_id, channel, {}, actor=actor)
        elif scope == "topic":
            topic = self._resolve_topic(db, params["topic"])
            if topic is None:
                raise _BookError("not_found", "未找到指定的小主题。", {"topic": params["topic"]})
            topic_id = topic["id"]
            direction = topic["direction"]
        privacy_guard.check_action(db, "record_feedback", {**params, "direction": direction})
        feedback_id = str(uuid.uuid4())
        created_at = _iso(_now())
        db.execute(
            "INSERT INTO feedback(id, request_id, scope, prompt_id, direction, topic_id, feedback, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (feedback_id, params["request_id"], scope, prompt_id, direction, topic_id, params["feedback"], created_at),
        )
        return {"status": "ok", "data": {
            "request_id": params["request_id"], "feedback_id": feedback_id,
            "scope": scope, "prompt_id": prompt_id, "direction": direction,
            "topic_id": topic_id, "feedback": params["feedback"], "created_at": created_at,
        }}

    def _list_feedback(self, db, params, channel, actor=None):
        filters = [name for name in ("prompt_id", "direction", "topic") if name in params]
        if len(filters) != 1:
            raise _BookError("invalid_params", "请只指定命题、方向或主题中的一种范围。")
        field = filters[0]
        value = params[field]
        if field == "prompt_id":
            self._find_prompt(db, value, channel, {}, actor=actor)
        elif field == "topic":
            topic = self._resolve_topic(db, value)
            if topic is None:
                raise _BookError("not_found", "未找到指定主题。")
            field, value = "topic_id", topic["id"]
        items = db.execute(
            f"SELECT scope, feedback, created_at FROM feedback WHERE {field} = ? AND scope = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (value, {"prompt_id": "prompt", "direction": "direction", "topic_id": "topic"}[field], params.get("limit", 10)),
        )
        return {"status": "ok", "data": {"items": [dict(item) for item in items]}}
