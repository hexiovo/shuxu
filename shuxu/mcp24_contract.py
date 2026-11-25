"""Host-scoped MCP planning, compatibility, and request contract."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import mcp_workflows, privacy_guard

PROTOCOL_VERSION = 1
SERVICE_VERSION = "1.4.0"
DIRECTORY_SCHEMA_VERSION = 1
PENDING_TTL = timedelta(minutes=30)
PREVIEW_TTL = timedelta(minutes=5)

WRITE_ACTIONS = {"dry_run_actions", "continue_pending_call"}
FILE_WRITE_ACTIONS = {"export_thought_bundle", "create_backup", "restore_backup"}
CONTRACT_TOOLS = {
    "plan_mcp_calls", "get_mcp_directory", "diagnose_mcp_compatibility",
    "compare_mcp_directories",
    "run_mcp_self_check", "get_request_status", "dry_run_actions", "continue_pending_call",
}


def specs(tool, field, ident, direction, version, include_private):
    text = lambda length: field("string", minLength=1, maxLength=length)
    choice = lambda *values: field("string", enum=list(values))
    count = lambda low, high: field("integer", minimum=low, maximum=high)
    obj = lambda properties: field("object", properties=properties)
    route_ids = [route[0] for route in mcp_workflows.ROUTES]

    def entry(name, description, properties, *required):
        return tool(name, description, properties, tuple(required))

    return [
        entry("plan_mcp_calls", "按现有场景或结构化意图生成只读调用计划；定位目标、核对版本和参数，不执行工具。", {
            "scenario": choice(*route_ids), "arguments_by_tool": obj({}),
            "intent": choice("get_prompt", "set_prompt_state", "snooze_prompt"),
            "prompt_id": ident, "conversation_id": ident, "reference": text(160),
            "keywords": field("array", items=text(80), minItems=1, maxItems=8),
            "state": choice("want_to_write", "in_progress", "written"),
            "time_text": text(160), "timezone": text(64),
            "expected_version": field("integer", minimum=1),
        }),
        entry("get_mcp_directory", "查询当前宿主 profile 与 allowed scopes 可见的真实 MCP 工具目录。", {
            "query": text(160), "limit": count(1, 100), "offset": field("integer", minimum=0),
        }),
        entry("compare_mcp_directories", "将宿主保存的完整目录快照与当前可见目录比较，返回结构化 schema、权限和迁移变化。", {
            "baseline": field("object", properties={
                "catalog_format": text(100), "directory_id": text(128),
                "protocol_version": count(1, 100), "service_version": text(40),
                "directory_version": text(40), "directory_schema_version": count(1, 100),
                "tool_count": count(0, 10000), "total_count": count(0, 10000),
                "offset": field("integer", minimum=0), "complete": field("boolean"),
                "capabilities": field("object", properties={"tool_count": count(0, 10000)}, additionalProperties=True),
                "identity": field("object", properties={
                    "host_id": text(128), "profile": text(128),
                    "authorization_revision": field("integer", minimum=0),
                }, additionalProperties=True),
                "tools": field("array", items=field("object", additionalProperties=True)),
            }, required=["tools"], additionalProperties=True),
            "limit": count(1, 100), "offset": field("integer", minimum=0),
        }, "baseline"),
        entry("diagnose_mcp_compatibility", "连接本机 loopback 握手并比对宿主预期协议/服务版本。", {
            "expected_protocol_version": count(1, 10), "expected_service_version": text(40),
            "expected_instance_id": ident, "expected_directory_id": ident,
        }, "expected_protocol_version", "expected_service_version"),
        entry("run_mcp_self_check", "返回不触碰业务数据的合成校验向量，供 MCP 宿主核对发现、结构化回执、时区、附件和歧义策略。", {}),
        entry("get_request_status", "按 request_id 查询当前 actor 的提交、失败或未知状态，不返回原始回执正文。", {
            "request_id": ident,
        }, "request_id"),
        entry("dry_run_actions", "只读预览一个已授权的精确工具动作、目标和当前版本；可为后续高影响操作创建短时绑定预览。", {
            "request_id": ident, "action": text(128), "arguments": obj({}), "bind_preview": field("boolean"),
        }, "request_id", "action", "arguments"),
        entry("continue_pending_call", "跨轮补齐一个已绑定的工具参数；检查版本、过期和权限，确认后只返回该精确工具的一次调用建议。", {
            "request_id": ident, "tool": text(128), "pending_id": ident,
            "expected_version": field("integer", minimum=1), "arguments": obj({}), "confirmed": field("boolean"),
        }, "request_id"),
    ]


def schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS mcp24_request_observations (
            request_id TEXT NOT NULL,
            host_id TEXT NOT NULL,
            profile TEXT NOT NULL,
            authorization_revision INTEGER NOT NULL,
            action TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('running','committed','completed','failed','unknown')),
            result_status TEXT,
            error_code TEXT,
            committed INTEGER NOT NULL DEFAULT 0,
            protected INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(host_id, profile, request_id)
        );
        CREATE INDEX IF NOT EXISTS mcp24_request_observations_updated
            ON mcp24_request_observations(host_id, profile, updated_at);
        CREATE TABLE IF NOT EXISTS mcp24_pending_calls (
            pending_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            host_id TEXT NOT NULL,
            profile TEXT NOT NULL,
            authorization_revision INTEGER NOT NULL,
            action TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            arguments_hash TEXT NOT NULL,
            missing_fields_json TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            version INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','confirmed','expired')),
            protected INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS mcp24_pending_calls_actor
            ON mcp24_pending_calls(host_id, profile, state, expires_at);
    """)


def _iso(now: datetime) -> str:
    return mcp_workflows.iso(now)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _actor_key(actor):
    if actor is None:
        raise mcp_workflows.WorkflowError("forbidden", "缺少已认证的宿主身份。")
    return actor["host_id"], actor["profile"], actor["authorization_revision"]


def _access():
    from . import mcp24_access
    return mcp24_access


def _tool_name(item):
    return item["name"] if isinstance(item, dict) else item.name


def _tool_signature(item):
    if isinstance(item, dict):
        keys = ("name", "description", "inputSchema", "category", "read_only", "destructive", "idempotent", "receipt")
        return {key: item[key] for key in keys if key in item}
    return {"name": item.name, "description": item.description, "inputSchema": item.inputSchema}


def filter_tools(actor, tool_specs):
    access = _access()
    return [_tool_entry(item, _write_names()) for item in tool_specs if access.allows(actor, _tool_name(item))]


def directory_id(actor, tool_specs):
    visible = filter_tools(actor, tool_specs)
    material = {
        "profile": actor["profile"],
        "authorization_revision": actor["authorization_revision"],
        "tools": sorted((_tool_signature(item) for item in visible), key=lambda item: item["name"]),
    }
    return hashlib.sha256(_json(material).encode("utf-8")).hexdigest()[:32]


def _registry():
    from . import book
    return {spec["name"]: spec for spec in book.TOOL_SPECS}


def _write_names():
    from . import book
    return book._WRITE_ACTIONS | FILE_WRITE_ACTIONS | WRITE_ACTIONS


def _require_allowed(actor, action):
    if not _access().allows(actor, action):
        raise mcp_workflows.WorkflowError("forbidden", "当前宿主 profile 或 allowed scopes 不允许此工具。", {"tool": action})


def _named_actor(actor):
    return actor["host_id"] not in {"legacy-stdio", "local-web"}


def _missing_scopes(actor, tool_name, arguments):
    required = []
    if tool_name == "share_excerpt" and arguments.get("id") and arguments.get("field"):
        if arguments.get("kind") == "prompt":
            required.append(f"prompt:{arguments['id']}")
        elif arguments.get("kind") in {"inspiration", "clipping", "resume"}:
            required.append(f"material:{arguments['id']}:{arguments['field']}")
    access = _access()
    return [scope for scope in required if not access.has_scope(actor, scope)]


def _plan(book, params, actor, now=None):
    _require_allowed(actor, "plan_mcp_calls")
    if "scenario" not in params:
        return _plan_intent(book, params, actor, now or datetime.now(timezone.utc))
    if "intent" in params:
        raise mcp_workflows.WorkflowError("invalid_params", "一次计划只提供 scenario 或 intent 中的一项。", {"fields": ["scenario", "intent"]})
    route = next(item for item in mcp_workflows.ROUTES if item[0] == params["scenario"])
    registry = _registry()
    writes = _write_names()
    supplied = params.get("arguments_by_tool", {})
    if not isinstance(supplied, dict) or any(not isinstance(value, dict) for value in supplied.values()):
        raise mcp_workflows.WorkflowError("invalid_params", "arguments_by_tool 必须按工具名映射到参数对象。")
    unknown_tools = sorted(set(supplied) - set(route[2]))
    if unknown_tools:
        raise mcp_workflows.WorkflowError("invalid_params", "参数包含当前场景未使用的工具。", {"tools": unknown_tools})
    steps = []
    prior_step = None
    for index, name in enumerate(route[2], 1):
        step_id = f"{params['scenario']}.{index}"
        spec = registry.get(name)
        allowed = bool(spec and _access().allows(actor, name))
        arguments = supplied.get(name, {})
        required = spec["inputSchema"].get("required", []) if spec else []
        if _named_actor(actor) and name == "share_excerpt" and "request_id" not in required:
            required = [*required, "request_id"]
        missing = [key for key in required if key not in arguments]
        properties = spec["inputSchema"].get("properties", {}) if spec else {}
        unknown = sorted(set(arguments) - set(properties))
        missing_scopes = _missing_scopes(actor, name, arguments) if allowed else []
        private_read_denied = _named_actor(actor) and name != "share_excerpt" and arguments.get("include_private") is True
        write = name in writes
        permitted = allowed and not missing_scopes and not private_read_denied
        steps.append({
            "step_id": step_id,
            "tool": name,
            "arguments": arguments if allowed else {},
            "depends_on": [prior_step] if prior_step else [],
            "missing_fields": missing if allowed else [],
            "unknown_fields": unknown if allowed else [],
            "missing_scopes": missing_scopes,
            "permission": "allowed" if permitted else "denied",
            "policy_restrictions": ["private_content_requires_share_excerpt"] if private_read_denied else [],
            "read_only": not write,
            "requires_user_confirmation": write,
            "callable_now": bool(permitted and not missing and not unknown and not write),
        })
        prior_step = step_id
    return {"scenario": route[0], "intent": route[1], "guidance": route[3], "steps": steps, "writes_performed": False}


def _prompt_candidates(book, db, params, actor):
    rows = book._visible_rows(db, "mcp", actor)
    keywords = [*params.get("keywords", [])]
    if params.get("reference"):
        keywords.append(params["reference"])
    if not keywords:
        return []
    keywords = ["".join(value.casefold().split()) for value in keywords]
    matches = []
    for row in rows:
        text = "".join(row["text"].casefold().split())
        matched = [keyword for keyword in keywords if keyword and keyword in text]
        if matched:
            matches.append((len(matched), row))
    matches.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [{"prompt_id": row["id"], "text": row["text"], "direction": row["direction"],
             "topic_id": row["topic_id"], "version": row["version"], "matched_keywords": [keyword for keyword in keywords if keyword in "".join(row["text"].casefold().split())],
             "score": score}
            for score, row in matches[:10]]


def _reference_mode(value):
    if not isinstance(value, str):
        return None, None
    normalized = "".join(value.casefold().split()).strip("，。！？?!、:：")
    current = {"current", "当前", "当前题", "本题", "这题", "这道题", "刚才那题", "刚刚那题"}
    previous = {"previous", "prior", "last", "上次", "上一次", "上一题", "前一题", "之前那题", "刚才上一题"}
    if normalized in current:
        return "current", None
    if normalized in previous:
        return "previous", None
    for prefix, mode in (("上一次", "previous"), ("上一题", "previous"), ("上次", "previous"),
                         ("之前的", "previous"), ("之前", "previous"), ("当前题", "current"),
                         ("这道题", "current"), ("这题", "current")):
        if normalized.startswith(prefix):
            query = normalized[len(prefix):].lstrip("的:：，。-— ")
            return mode, query or None
    return None, normalized or None


def _conversation_prompts(book, db, conversation_id, actor):
    rows = book._visible_rows(db, "mcp", actor)
    by_id = {row["id"]: row for row in rows}
    entries = db.execute(
        "SELECT prompt_id FROM mcp_conversation_entries WHERE conversation_id=? AND prompt_id IS NOT NULL "
        "ORDER BY created_at DESC, rowid DESC", (conversation_id,),
    ).fetchall()
    identifiers = list(dict.fromkeys(entry["prompt_id"] for entry in entries))
    return [by_id[identifier] for identifier in identifiers if identifier in by_id]


def _matched_candidates(rows, keywords):
    normalized = ["".join(value.casefold().split()) for value in keywords if value]
    matches = []
    for row in rows:
        text = "".join(row["text"].casefold().split())
        found = [keyword for keyword in normalized if keyword in text]
        if found:
            matches.append({
                "prompt_id": row["id"], "text": row["text"], "direction": row["direction"],
                "topic_id": row["topic_id"], "version": row["version"],
                "matched_keywords": found, "score": len(found),
            })
    return sorted(matches, key=lambda item: (-item["score"], item["prompt_id"]))[:10]


def _plan_target(book, db, params, actor):
    identifier = params.get("prompt_id")
    reference_mode, reference_query = _reference_mode(params.get("reference"))
    if identifier:
        row = book._find_prompt_for_actor(db, identifier, "mcp", actor, {})
        return row, [{"prompt_id": row["id"], "direction": row["direction"], "topic_id": row["topic_id"], "version": row["version"], "match": "prompt_id"}]
    if params.get("reference") and not reference_mode and reference_query:
        row = db.execute("SELECT id FROM prompts WHERE id = ?", (params["reference"],)).fetchone()
        if row is not None:
            row = book._find_prompt_for_actor(db, row["id"], "mcp", actor, {})
            return row, [{"prompt_id": row["id"], "direction": row["direction"], "topic_id": row["topic_id"], "version": row["version"], "match": "reference_id"}]

    keywords = [*params.get("keywords", [])]
    if reference_query:
        keywords.append(reference_query)
    conversation_id = params.get("conversation_id")
    history = _conversation_prompts(book, db, conversation_id, actor) if conversation_id else []

    if reference_mode in {"current", "previous"} and not history:
        return None, []

    if reference_mode in {"current", "previous"} and history:
        position = 0 if reference_mode == "current" else 1
        selected = history[position] if len(history) > position else None
        if not keywords:
            if selected is None:
                return None, []
            return selected, [{"prompt_id": selected["id"], "direction": selected["direction"], "topic_id": selected["topic_id"], "version": selected["version"], "match": f"conversation_{reference_mode}"}]
        candidates = _matched_candidates(([selected] if selected is not None else []), keywords)
        if reference_mode == "previous" and candidates:
            top = candidates[0]["score"]
            best = [item for item in candidates if item["score"] == top]
            if len(best) == 1:
                return book._find_prompt_for_actor(db, best[0]["prompt_id"], "mcp", actor, {}), candidates
            return None, candidates
        if reference_mode == "current":
            if candidates:
                return book._find_prompt_for_actor(db, candidates[0]["prompt_id"], "mcp", actor, {}), candidates
            return None, []
        return None, _matched_candidates(history[1:], keywords)

    if keywords:
        history_candidates = _matched_candidates(history, keywords) if history else []
        historical = [item for item in history_candidates if not history or item["prompt_id"] != history[0]["id"]]
        candidates = historical or history_candidates
        if not history:
            candidates = _prompt_candidates(book, db, {**params, "reference": reference_query,
                                                       "keywords": params.get("keywords", [])}, actor)
        if not candidates:
            return None, []
        top = candidates[0]["score"]
        best = [item for item in candidates if item["score"] == top]
        if len(best) != 1:
            return None, candidates
        row = book._find_prompt_for_actor(db, best[0]["prompt_id"], "mcp", actor, {})
        return row, candidates

    if history:
        row = history[0]
        return row, [{"prompt_id": row["id"], "direction": row["direction"], "topic_id": row["topic_id"], "version": row["version"], "match": "conversation_current"}]
    return None, []


def _resolve_plan_time(text, zone_name, now):
    if not text or not zone_name:
        return None
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise mcp_workflows.WorkflowError("invalid_params", "请提供有效的 IANA 时区。", {"field": "timezone"}) from None
    current = now.astimezone(zone)
    value = text.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if re.search(r"[Tt ]\d{2}:\d{2}", value) is None:
            raise mcp_workflows.WorkflowError("needs_clarification", "日期没有指定具体时间。", {
                "time_text": value, "timezone": zone_name,
                "question": "请补充该日期的具体时间。",
            })
        return _localize_plan_time(parsed, zone, value, zone_name) if parsed.tzinfo is None else parsed.astimezone(zone)
    time_match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", value)
    hour, minute = (int(time_match.group(1)), int(time_match.group(2))) if time_match else (0, 0)
    if hour > 23 or minute > 59:
        raise mcp_workflows.WorkflowError("invalid_params", "时间文本中的时分无效。", {"field": "time_text"})
    day = None
    if any(word in value for word in ("今天", "今晚")):
        day = current.date()
    elif "明天" in value:
        day = (current + timedelta(days=1)).date()
    elif "后天" in value:
        day = (current + timedelta(days=2)).date()
    else:
        weekday_names = {"周一": 0, "星期一": 0, "周二": 1, "星期二": 1, "周三": 2, "星期三": 2,
                         "周四": 3, "星期四": 3, "周五": 4, "星期五": 4, "周六": 5, "星期六": 5,
                         "周日": 6, "星期日": 6, "周天": 6, "星期天": 6}
        weekday = next((number for name, number in weekday_names.items() if name in value), None)
        if "周末" in value:
            weekday = 5
        if weekday is not None:
            offset = (weekday - current.weekday()) % 7
            next_week = "下周" in value or "下星期" in value
            if next_week and offset:
                offset += 7
            elif next_week:
                offset = 7
            elif offset == 0 and time_match is None:
                raise mcp_workflows.WorkflowError("needs_clarification", "提到的星期就是今天，请确认使用今天还是下周。", {
                    "time_text": value, "timezone": zone_name,
                    "question": "你是指今天，还是下周同一天？",
                })
            if offset == 0 and (hour, minute) <= (current.hour, current.minute):
                raise mcp_workflows.WorkflowError("needs_clarification", "提到的星期时间今天已经过去。", {
                    "time_text": value, "timezone": zone_name,
                    "question": "你是指下周同一天，还是要修改具体时间？",
                })
            day = (current + timedelta(days=offset)).date()
    if day is None:
        raise mcp_workflows.WorkflowError("needs_clarification", "无法把这段时间文本唯一转换为本地时间。", {
            "time_text": value, "timezone": zone_name,
            "question": "请给出明确日期和时间，或使用明天、周日这类日期表达。",
        })
    if time_match is None:
        raise mcp_workflows.WorkflowError("needs_clarification", "日期没有指定具体时间。", {
            "time_text": value, "timezone": zone_name,
            "question": "请补充该日期的具体时间。",
        })
    return _localize_plan_time(datetime(day.year, day.month, day.day, hour, minute), zone, value, zone_name)


def _localize_plan_time(value, zone, source_text, zone_name):
    first = value.replace(tzinfo=zone, fold=0)
    second = value.replace(tzinfo=zone, fold=1)
    first_valid = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == value
    second_valid = second.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == value
    if first_valid and second_valid and first.utcoffset() != second.utcoffset():
        raise mcp_workflows.WorkflowError("needs_clarification", "这个本地时间因夏令时切换对应两个时刻。", {
            "time_text": source_text, "timezone": zone_name,
            "question": "请选择这一天较早还是较晚的那个本地时间。",
        })
    if not first_valid and not second_valid:
        raise mcp_workflows.WorkflowError("needs_clarification", "这个本地时间因夏令时切换不存在。", {
            "time_text": source_text, "timezone": zone_name,
            "question": "请提供一个存在的本地时间。",
        })
    return first if first_valid else second


def _plan_intent(book, params, actor, now):
    intent = params.get("intent")
    if intent not in {"get_prompt", "set_prompt_state", "snooze_prompt"}:
        raise mcp_workflows.WorkflowError("invalid_params", "请提供一个明确的结构化 intent。", {"field": "intent"})
    tool_name = intent
    if not _access().allows(actor, tool_name):
        return {"intent": intent, "permission": "denied", "writes_performed": False}
    if (params.get("keywords") or params.get("reference")) and not _access().allows(actor, "get_prompt"):
        return {"intent": intent, "permission": "denied", "required_tool": "get_prompt", "writes_performed": False}
    with closing(book._connect()) as db:
        target, candidates = _plan_target(book, db, params, actor)
        if target is None:
            return {"intent": intent, "needs_clarification": True,
                    "question": "请从这些候选命题中选择一条。" if candidates else "请提供命题编号、当前会话或命题关键词。",
                    "candidate_evidence": candidates, "writes_performed": False}
        if "expected_version" in params and params["expected_version"] != target["version"]:
            return {"intent": intent, "needs_clarification": True,
                    "conflict": {"prompt_id": target["id"], "expected_version": params["expected_version"], "actual_version": target["version"]},
                    "question": "目标题已更新，请确认是否按当前版本继续。", "writes_performed": False}
        arguments = {"prompt_id": target["id"], "expected_version": target["version"]}
        timezone_name = params.get("timezone")
        absolute_time = None
        if intent == "get_prompt":
            tool_name = "get_prompt"
            arguments = {"prompt_id": target["id"]}
            if _access().has_full_access(actor):
                arguments["detail"] = "full"
        elif intent == "set_prompt_state":
            if "state" not in params:
                return {"intent": intent, "target": {"prompt_id": target["id"], "version": target["version"]},
                        "missing_fields": ["state"], "candidate_evidence": candidates, "writes_performed": False}
            tool_name = "set_prompt_state"
            arguments.update({"state": params["state"], "request_id": "plan-" + uuid.uuid4().hex})
        else:
            if "time_text" not in params or "timezone" not in params:
                missing = [key for key in ("time_text", "timezone") if key not in params]
                return {"intent": intent, "target": {"prompt_id": target["id"], "version": target["version"]},
                        "missing_fields": missing, "candidate_evidence": candidates, "writes_performed": False}
            absolute = _resolve_plan_time(params["time_text"], timezone_name, now)
            if absolute <= now.astimezone(absolute.tzinfo):
                raise mcp_workflows.WorkflowError("needs_clarification", "解析后的暂缓时间已在当前时间之前。", {
                    "time_text": params["time_text"], "timezone": timezone_name,
                    "question": "这个时间已过去，请给一个未来的日期和时间。",
                })
            absolute_time = absolute.isoformat(timespec="seconds")
            tool_name = "snooze_prompt"
            arguments.update({"snoozed_until": absolute_time, "request_id": "plan-" + uuid.uuid4().hex})
    return {
        "intent": intent,
        "resolved": {"prompt_id": target["id"], "version": target["version"],
                     "direction": target["direction"], "topic_id": target["topic_id"]},
        "time_resolution": ({"source_text": params["time_text"], "timezone": timezone_name,
                             "absolute": absolute_time} if absolute_time else None),
        "calls": [{"tool": tool_name, "arguments": arguments,
                   "precondition": {"prompt_id": target["id"], "version": target["version"]},
                   "depends_on": [], "callable_now": True}],
        "dependencies": [],
        "candidate_evidence": candidates, "missing_fields": [],
        "needs_clarification": False, "writes_performed": False,
    }


_DESTRUCTIVE_TOOLS = {
    "restore_backup", "undo_action", "apply_actions", "remove_inspiration", "remove_clipping",
    "clear_resume_card", "remove_inspiration_image", "apply_retention_cleanup",
    "delete_prompt", "delete_topic", "delete_project", "delete_claim_source",
}
_SIDE_EFFECT_READS = {"share_excerpt", "receive_handoff"}


def _tool_category(name):
    if name in {"plan_mcp_calls", "get_mcp_directory", "diagnose_mcp_compatibility", "compare_mcp_directories", "run_mcp_self_check",
                "get_request_status", "get_request_diagnostics", "create_host", "configure_host", "revoke_host"}:
        return "host_and_service"
    if any(part in name for part in ("timeline", "prompt_event")):
        return "history_and_reporting"
    if any(part in name for part in ("relation", "evidence")):
        return "relations_and_evidence"
    if any(part in name for part in ("inspiration", "clipping", "resume", "material", "attachment", "image", "screenshot")):
        return "materials_and_attachments"
    if any(part in name for part in ("preference", "weight", "feedback")):
        return "preferences"
    if any(part in name for part in ("thought", "conversation", "checkpoint", "guide", "undo", "apply_actions", "bind_current", "resolve_reference", "next_prompt", "continue_thought", "handoff")):
        return "conversation_workflows"
    if name in {"dry_run_actions", "continue_pending_call"}:
        return "host_and_service"
    if any(part in name for part in ("full_record", "full_library", "export", "search_indexed", "topic_project", "claim_source", "source")):
        return "complete_library"
    if any(part in name for part in ("prompt", "topic", "direction")):
        return "prompts_and_topics"
    if any(part in name for part in ("backup", "retention", "maintenance", "restart", "cleanup", "job", "preview", "operation")):
        return "administration"
    return "other"


def _tool_entry(item, writes):
    name = _tool_name(item)
    required = item["inputSchema"].get("required", []) if isinstance(item, dict) else item.inputSchema.get("required", [])
    is_write = name in writes
    request_id_required = "request_id" in required
    has_receipt = is_write and request_id_required
    return {
        **_tool_signature(item),
        "category": _tool_category(name),
        "read_only": not is_write and name not in _SIDE_EFFECT_READS,
        "destructive": name in _DESTRUCTIVE_TOOLS,
        "idempotent": has_receipt,
        "receipt": {"type": "request_receipt" if has_receipt else "tool_result",
                    "request_id_required": request_id_required,
                    "status_tool": "get_request_status" if has_receipt else None},
    }


def _capabilities(actor, names):
    full = _access().has_full_access(actor)
    return {
        "full_access": full,
        "all_tools": full,
        "complete_record_reads": full and "get_full_record" in names and "list_full_records" in names,
        "full_text_context": full and "get_thought_context" in names,
        "indexed_search": "search_indexed_library" in names,
        "prompt_timeline": "get_prompt_timeline" in names,
        "prompt_event_review": "review_prompt_events" in names,
        "typed_relations": "read_entity_relations" in names,
        "claim_evidence": "read_prompt_evidence" in names,
        "project_progress": "get_topic_project_progress" in names,
        "original_image_chunks": full and "get_attachment_chunk" in names,
        "complete_library_export": full and {"export_full_library", "get_full_export_info", "read_full_export_chunk"}.issubset(names),
        "administration": full and any("backup" in name or "retention" in name or "maintenance" in name for name in names),
        "tool_count": len(names),
    }


def _directory(book, params, actor):
    _require_allowed(actor, "get_mcp_directory")
    from . import book as book_module
    visible = filter_tools(actor, book_module.TOOL_SPECS)
    query = params.get("query", "").casefold()
    limit = params.get("limit", 100)
    matching = sorted((item for item in visible
                       if not query or query in item["name"].casefold() or query in item["description"].casefold()),
                      key=lambda item: item["name"])
    offset = params.get("offset", 0)
    selected = matching[offset:offset + limit]
    names = {_tool_name(item) for item in visible}
    categories = {}
    for item in visible:
        name = _tool_name(item)
        categories.setdefault(_tool_category(name), []).append(name)
    complete = offset + len(selected) >= len(matching)
    return {
        "identity": {"host_id": actor["host_id"], "profile": actor["profile"],
                     "authorization_revision": actor["authorization_revision"]},
        "full_access": _access().has_full_access(actor),
        "directory_id": directory_id(actor, visible), "directory_version": SERVICE_VERSION,
        "directory_schema_version": DIRECTORY_SCHEMA_VERSION,
        "capabilities": _capabilities(actor, names),
        "categories": [{"category": name, "tools": sorted(tool_names), "tool_count": len(tool_names)}
                       for name, tool_names in sorted(categories.items())],
        "tools": [_tool_entry(item, _write_names()) for item in selected],
        "total_count": len(matching), "count": len(selected), "offset": offset,
        "complete": complete, "next_offset": None if complete else offset + len(selected),
    }


def _baseline_directory(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("tools"), list):
        raise mcp_workflows.WorkflowError("invalid_baseline", "请提供完整的已保存目录或 catalog 快照。", {"field": "baseline.tools"})

    tools = snapshot["tools"]
    catalog = snapshot.get("catalog_format") == "shuxu-mcp-tool-catalog/v1"
    directory = isinstance(snapshot.get("directory_id"), str)
    if catalog:
        expected = snapshot.get("tool_count")
        source = "tool_catalog"
        complete = expected == len(tools)
    elif directory:
        expected = snapshot.get("total_count")
        source = "mcp_directory"
        complete = (snapshot.get("complete") is True and snapshot.get("offset", 0) == 0
                    and expected == len(tools))
        capabilities = snapshot.get("capabilities", {})
        if not isinstance(capabilities, dict):
            raise mcp_workflows.WorkflowError("invalid_baseline", "baseline capabilities 必须是对象。", {"field": "baseline.capabilities"})
        capability_count = capabilities.get("tool_count")
        complete = complete and capability_count == expected
    else:
        raise mcp_workflows.WorkflowError("invalid_baseline", "baseline 不是支持的 MCP 目录或 catalog 快照。", {"field": "baseline"})
    if not complete:
        raise mcp_workflows.WorkflowError("baseline_incomplete", "目录比较需要未筛选且完整的 baseline。", {
            "expected_count": expected, "received_count": len(tools),
            "next_offset": snapshot.get("next_offset"),
        })

    by_name = {}
    for item in tools:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("inputSchema"), dict):
            raise mcp_workflows.WorkflowError("invalid_baseline", "baseline 工具项缺少 name 或 inputSchema。", {"field": "baseline.tools"})
        if item["name"] in by_name:
            raise mcp_workflows.WorkflowError("invalid_baseline", "baseline 工具名重复。", {"field": "baseline.tools"})
        by_name[item["name"]] = item

    identity = snapshot.get("identity") if source == "mcp_directory" else None
    return {
        "source": source,
        "snapshot_id": _hash(snapshot),
        "directory_id": snapshot.get("directory_id"),
        "protocol_version": snapshot.get("protocol_version"),
        "service_version": snapshot.get("service_version", snapshot.get("directory_version")),
        "directory_version": snapshot.get("directory_version", snapshot.get("service_version")),
        "directory_schema_version": snapshot.get("directory_schema_version"),
        "identity": identity,
        "authorization_revision": (identity or {}).get("authorization_revision"),
        "tool_count": len(tools),
        "complete": True,
        "tools": by_name,
    }


def _pointer(path, part):
    escaped = str(part).replace("~", "~0").replace("/", "~1")
    return f"{path}/{escaped}"


def _schema_change_impact(path, change, before, after):
    leaf = path.rsplit("/", 1)[-1]
    if leaf == "required":
        return "breaking" if set(after or ()) - set(before or ()) else "compatible"
    if leaf == "enum":
        return "breaking" if set(before or ()) - set(after or ()) else "compatible"
    if leaf == "type":
        old_types = set(before if isinstance(before, list) else [before])
        new_types = set(after if isinstance(after, list) else [after])
        return "compatible" if old_types.issubset(new_types) else "breaking"
    if leaf == "description":
        return "informational"
    if leaf == "default":
        return "behavior_change"
    if leaf in {"minimum", "exclusiveMinimum", "minLength", "minItems"}:
        return "breaking" if change == "changed" and after > before else "compatible"
    if leaf in {"maximum", "exclusiveMaximum", "maxLength", "maxItems"}:
        return "breaking" if change == "changed" and after < before else "compatible"
    if change == "added" and "/properties/" in path:
        return "compatible"
    if change == "removed" and "/properties/" in path:
        return "breaking"
    if change == "added":
        return "compatible"
    if change == "removed":
        return "breaking"
    return "breaking"


def _schema_changes(before, after, path="inputSchema"):
    changes = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(before.keys() | after.keys()):
            child_path = _pointer(path, key)
            if key not in before:
                value = after[key]
                changes.append({"path": child_path, "change": "added", "before": None, "after": value,
                                "impact": _schema_change_impact(child_path, "added", None, value)})
            elif key not in after:
                value = before[key]
                changes.append({"path": child_path, "change": "removed", "before": value, "after": None,
                                "impact": _schema_change_impact(child_path, "removed", value, None)})
            else:
                changes.extend(_schema_changes(before[key], after[key], child_path))
    elif isinstance(before, list) and isinstance(after, list):
        if before != after:
            added = [value for value in after if value not in before]
            removed = [value for value in before if value not in after]
            changes.append({"path": path, "change": "changed", "before": before, "after": after,
                            "added_values": added, "removed_values": removed,
                            "impact": _schema_change_impact(path, "changed", before, after)})
    elif before != after:
        changes.append({"path": path, "change": "changed", "before": before, "after": after,
                        "impact": _schema_change_impact(path, "changed", before, after)})
    return changes


def _tool_policy(item):
    annotations = item.get("annotations", {})
    required = item.get("inputSchema", {}).get("required", [])
    read_only = item.get("read_only", annotations.get("readOnlyHint"))
    request_id_required = "request_id" in required
    receipt = item.get("receipt")
    if receipt is None and read_only is not None:
        has_receipt = read_only is False and request_id_required
        receipt = {"type": "request_receipt" if has_receipt else "tool_result",
                   "request_id_required": request_id_required,
                   "status_tool": "get_request_status" if has_receipt else None}
    return {
        "category": item.get("category"),
        "read_only": read_only,
        "destructive": item.get("destructive", annotations.get("destructiveHint")),
        "idempotent": item.get("idempotent", annotations.get("idempotentHint")),
        "receipt": receipt,
    }


def _policy_changes(before, after):
    old = _tool_policy(before)
    new = _tool_policy(after)
    changes = []
    for key in ("category", "read_only", "destructive", "idempotent", "receipt"):
        if old[key] is None or new[key] is None or old[key] == new[key]:
            continue
        impact = "behavior_change"
        if ((key == "read_only" and old[key] is True and new[key] is False)
                or (key == "destructive" and old[key] is False and new[key] is True)
                or (key == "idempotent" and old[key] is True and new[key] is False)):
            impact = "breaking"
        changes.append({"path": f"permissions/{key}", "change": "changed",
                        "before": old[key], "after": new[key], "impact": impact})
    return changes


def _compatibility(changes):
    impacts = {item["impact"] for item in changes}
    if "breaking" in impacts:
        return "breaking"
    if "behavior_change" in impacts:
        return "backward_compatible_with_behavior_changes"
    return "backward_compatible"


def _migration_suggestion(name, item, previous, compatibility):
    properties = item.get("inputSchema", {}).get("properties", {})
    required = item.get("inputSchema", {}).get("required", [])
    arguments = {key: value["default"] for key, value in properties.items() if "default" in value}
    missing = [key for key in required if key not in arguments]
    previous_properties = (previous or {}).get("inputSchema", {}).get("properties", {})
    previous_required = set((previous or {}).get("inputSchema", {}).get("required", []))
    return {
        "tool": name,
        "arguments": arguments,
        "schema_ready": not missing,
        "missing_required_fields": missing,
        "new_required_fields": sorted(set(required) - previous_required),
        "added_optional_fields": sorted(set(properties) - set(previous_properties) - set(required)),
        "reuse_previous_arguments": previous is not None and compatibility != "breaking",
        "replacement_tool": None,
        "next_step": "调用时提供缺失必填字段；业务目标或版本字段可先用 plan_mcp_calls 解析。" if missing else "先确认 permission.currently_callable，再使用 arguments。",
    }


def _compare_mcp_directories(book, params, actor):
    _require_allowed(actor, "compare_mcp_directories")
    baseline = _baseline_directory(params["baseline"])
    from . import book as book_module
    registered = {item["name"]: item for item in book_module.TOOL_SPECS}
    visible = {item["name"]: item for item in filter_tools(actor, book_module.TOOL_SPECS)}
    access = _access()
    identity = baseline["identity"] or {}
    same_actor = (baseline["source"] == "mcp_directory"
                  and identity.get("host_id") == actor["host_id"]
                  and identity.get("profile") == actor["profile"])
    baseline_tools = baseline["tools"]
    differences = []

    for name in sorted(baseline_tools.keys() | visible.keys()):
        before = baseline_tools.get(name)
        after = visible.get(name)
        is_registered = name in registered
        allowed_now = access.allows(actor, name)
        if before is None:
            difference = {
                "tool": name, "change": "added", "compatibility": "backward_compatible",
                "schema_changes": [{"path": "tool", "change": "added", "before": None, "after": name, "impact": "compatible"}],
                "permission": {"baseline_visible": False if same_actor else None,
                               "currently_allowed": allowed_now, "currently_registered": is_registered,
                               "currently_callable": allowed_now and is_registered,
                               "change": "gained" if same_actor else "unknown"},
                "migration": _migration_suggestion(name, after, None, "backward_compatible"),
                "baseline_tool": None,
                "current_tool": {key: after[key] for key in ("name", "description", "inputSchema", "category", "read_only", "destructive", "idempotent", "receipt") if key in after},
            }
            differences.append(difference)
            continue
        if after is None:
            if same_actor and not allowed_now:
                change = "access_lost"
                compatibility = "breaking"
                permission_change = "denied_now"
            elif same_actor:
                change = "unavailable"
                compatibility = "breaking"
                permission_change = "allowed_but_unavailable"
            elif access.has_full_access(actor):
                change = "removed"
                compatibility = "breaking"
                permission_change = "not_applicable"
            else:
                change = "not_visible"
                compatibility = "unknown"
                permission_change = "unknown"
            differences.append({
                "tool": name, "change": change, "compatibility": compatibility,
                "schema_changes": [{"path": "tool", "change": "removed" if change == "removed" else "not_visible",
                                    "before": name, "after": None,
                                    "impact": "breaking" if compatibility == "breaking" else "unknown"}],
                "permission": {"baseline_visible": True if baseline["source"] == "mcp_directory" else None,
                               "currently_allowed": allowed_now, "currently_callable": False,
                               "change": permission_change},
                "migration": None,
                "baseline_tool": {key: before[key] for key in ("name", "description", "inputSchema", "category", "read_only", "destructive", "idempotent", "receipt", "annotations") if key in before},
                "current_tool": None,
            })
            continue

        changes = _schema_changes(before["inputSchema"], after["inputSchema"])
        if before.get("description") != after.get("description"):
            changes.append({"path": "description", "change": "changed", "before": before.get("description"),
                            "after": after.get("description"), "impact": "informational"})
        changes.extend(_policy_changes(before, after))
        if not changes:
            continue
        compatibility = _compatibility(changes)
        differences.append({
            "tool": name, "change": "changed", "compatibility": compatibility,
            "schema_changes": changes,
            "permission": {"baseline_visible": True if baseline["source"] == "mcp_directory" else None,
                           "currently_allowed": allowed_now, "currently_registered": is_registered,
                           "currently_callable": allowed_now and is_registered,
                           "change": "unchanged" if same_actor else "unknown"},
            "migration": _migration_suggestion(name, after, before, compatibility),
            "baseline_tool": {key: before[key] for key in ("name", "description", "inputSchema", "category", "read_only", "destructive", "idempotent", "receipt", "annotations") if key in before},
            "current_tool": {key: after[key] for key in ("name", "description", "inputSchema", "category", "read_only", "destructive", "idempotent", "receipt") if key in after},
        })

    differences.sort(key=lambda item: (item["tool"], item["change"]))
    offset = params.get("offset", 0)
    limit = params.get("limit", 100)
    page = differences[offset:offset + limit]
    counts = {name: sum(item["change"] == name for item in differences)
              for name in ("added", "removed", "changed", "access_lost", "unavailable", "not_visible")}
    comparable = same_actor
    baseline_names = set(baseline_tools)
    current_names = set(visible)
    permission_delta = {
        "comparable": comparable,
        "baseline_authorization_revision": baseline["authorization_revision"],
        "current_authorization_revision": actor["authorization_revision"],
        "authorization_revision_changed": (baseline["authorization_revision"] != actor["authorization_revision"])
            if comparable else None,
        "newly_visible": sorted(current_names - baseline_names) if comparable else None,
        "no_longer_visible": sorted(baseline_names - current_names) if comparable else None,
        "currently_denied": sorted(name for name in baseline_names if not access.allows(actor, name)) if comparable else None,
    }
    current_directory = {
        "directory_id": directory_id(actor, book_module.TOOL_SPECS),
        "directory_version": SERVICE_VERSION, "directory_schema_version": DIRECTORY_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION, "service_version": SERVICE_VERSION,
        "identity": {"host_id": actor["host_id"], "profile": actor["profile"],
                     "authorization_revision": actor["authorization_revision"]},
        "full_access": access.has_full_access(actor), "tool_count": len(visible), "complete": True,
        "capabilities": _capabilities(actor, set(visible)),
    }
    protocol_changed = (baseline["protocol_version"] is not None
                        and baseline["protocol_version"] != PROTOCOL_VERSION)
    directory_schema_changed = (baseline["directory_schema_version"] is not None
                                and baseline["directory_schema_version"] != DIRECTORY_SCHEMA_VERSION)
    schema_breaking = any(item["compatibility"] == "breaking" for item in differences)
    permission_lost = comparable and bool(permission_delta["currently_denied"])
    compatible = False if (protocol_changed or directory_schema_changed or schema_breaking or permission_lost) else (True if comparable else None)
    compatibility = ("breaking" if compatible is False else
                      "backward_compatible" if compatible is True else
                      "schema_backward_compatible_permissions_unknown")
    compatibility_reasons = []
    if protocol_changed:
        compatibility_reasons.append({"code": "protocol_version_changed", "before": baseline["protocol_version"], "after": PROTOCOL_VERSION})
    if directory_schema_changed:
        compatibility_reasons.append({"code": "directory_schema_version_changed",
                                      "before": baseline["directory_schema_version"], "after": DIRECTORY_SCHEMA_VERSION})
    for item in differences:
        if item["change"] == "access_lost":
            continue
        breaking_paths = [change["path"] for change in item["schema_changes"] if change["impact"] == "breaking"]
        if breaking_paths:
            code = "tool_unavailable" if item["change"] in {"removed", "unavailable"} else "breaking_tool_change"
            compatibility_reasons.append({"code": code, "tool": item["tool"], "paths": breaking_paths})
    if permission_lost:
        compatibility_reasons.append({"code": "permission_denied_now", "tools": permission_delta["currently_denied"]})
    if not compatibility_reasons:
        compatibility_reasons.append({"code": "no_breaking_schema_changes",
                                      "permission_comparison": "comparable" if comparable else "unknown"})
    complete = offset + len(page) >= len(differences)
    return {
        "compatible": compatible, "compatibility": compatibility,
        "compatibility_reasons": compatibility_reasons,
        "baseline": {key: baseline[key] for key in (
            "source", "snapshot_id", "directory_id", "protocol_version", "service_version",
            "directory_version", "directory_schema_version", "identity", "authorization_revision", "tool_count", "complete",
        )},
        "current": current_directory,
        "version_changes": {
            "protocol_version": {"baseline": baseline["protocol_version"], "current": PROTOCOL_VERSION,
                                  "changed": protocol_changed},
            "service_version": {"baseline": baseline["service_version"], "current": SERVICE_VERSION,
                                "changed": baseline["service_version"] != SERVICE_VERSION},
            "directory_schema_version": {"baseline": baseline["directory_schema_version"],
                                          "current": DIRECTORY_SCHEMA_VERSION,
                                          "changed": directory_schema_changed if baseline["directory_schema_version"] is not None else None},
        },
        "permission_delta": permission_delta,
        "counts": {**counts, "total": len(differences)},
        "changes": page, "offset": offset, "limit": limit,
        "complete": complete, "next_offset": None if complete else offset + len(page),
    }


def _diagnose(book, params, actor):
    _require_allowed(actor, "diagnose_mcp_compatibility")
    from . import server
    try:
        info = server.ensure_server()
    except RuntimeError:
        raise mcp_workflows.WorkflowError("service_unavailable", "本机服务未能完成握手。")
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/api/mcp24/handshake",
        headers={"X-Book-Bridge": info["bridge_token"], "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        raise mcp_workflows.WorkflowError("service_unavailable", "本机 MCP 握手未完成。")
    if payload.get("status") != "ok" or not isinstance(payload.get("data"), dict):
        raise mcp_workflows.WorkflowError("service_unavailable", "本机 MCP 握手未返回有效服务信息。")
    data = payload["data"]
    from . import book as book_module
    actor_directory = directory_id(actor, book_module.TOOL_SPECS)
    checks = {
        "protocol_version": data.get("protocol_version") == params["expected_protocol_version"],
        "service_version": data.get("service_version") == params["expected_service_version"],
    }
    if params.get("expected_instance_id") is not None:
        checks["instance_id"] = data.get("instance_id") == params["expected_instance_id"]
    if params.get("expected_directory_id") is not None:
        checks["directory_id"] = actor_directory == params["expected_directory_id"]
    return {
        "compatible": all(checks.values()), "checks": checks,
        "protocol_version": data.get("protocol_version"), "service_version": data.get("service_version"),
        "instance_id": data.get("instance_id"), "directory_id": actor_directory,
        "loopback_directory_id": data.get("directory_id"),
        "authorization_revision": actor["authorization_revision"],
        "loopback_authorization_revision": data.get("authorization_revision"),
    }


def _self_check(book, actor):
    _require_allowed(actor, "run_mcp_self_check")
    from . import book as book_module
    registry = {item["name"]: item for item in book_module.TOOL_SPECS}
    image = registry.get("save_chat_screenshot", {}).get("inputSchema", {}).get("properties", {})
    routes = {item[0]: item for item in mcp_workflows.ROUTES}
    synthetic = normalize_receipt(
        {"status": "ok", "data": {"synthetic": True}, "error": None},
        request_id="synthetic-request", action="synthetic_read",
    )
    valid_time = datetime.fromisoformat("2030-01-02T03:04:05+00:00")
    naive_time = datetime.fromisoformat("2030-01-02T03:04:05")
    route = routes["01"]
    checks = [
        {"name": "tool_discovery", "ok": any(item["name"] == "run_mcp_self_check" for item in filter_tools(actor, book_module.TOOL_SPECS)), "method": "tools/list"},
        {"name": "structured_envelope", "ok": all(key in synthetic for key in ("status", "data", "error", "envelope")) and synthetic["data"]["synthetic"] is True, "expected_fields": ["status", "data", "error", "envelope"]},
        {"name": "timezone", "ok": valid_time.tzinfo is not None and naive_time.tzinfo is None, "valid_example": "2030-01-02T03:04:05Z", "invalid_example": "2030-01-02T03:04:05"},
        {"name": "attachment", "ok": "image_data" in image and "image_type" in image and "path" not in image, "fields": ["image_type", "image_data"], "path_input": False},
        {"name": "ambiguity", "ok": route[2][0] == "resolve_reference" and route[2][1] == "bind_current_prompt" and route[2][1] in _write_names(), "synthetic_status": "needs_clarification", "writes_performed": False},
    ]
    return {
        "passed": all(item["ok"] for item in checks), "checks": checks,
        "synthetic_only": True, "writes_performed": False,
        "host_verification": {"discover_tools": "tools/list", "verify_structured_result": ["status", "data", "error", "envelope"]},
    }


def _status(db, params, actor):
    _require_allowed(actor, "get_request_status")
    host_id, profile, _revision = _actor_key(actor)
    external_request_id = params["request_id"]
    request_ids = [_access().namespace_id(actor, "request", external_request_id)]
    if not _named_actor(actor) and external_request_id not in request_ids:
        request_ids.append(external_request_id)
    row = None
    request_id = request_ids[0]
    for candidate in request_ids:
        row = db.execute(
            "SELECT * FROM mcp24_request_observations WHERE host_id=? AND profile=? AND request_id=?",
            (host_id, profile, candidate),
        ).fetchone()
        if row is not None:
            request_id = candidate
            break
    if row is None:
        for candidate in request_ids:
            receipt = db.execute(
                "SELECT action, created_at FROM request_receipts WHERE request_id=? ORDER BY created_at DESC LIMIT 1",
                (candidate,),
            ).fetchone()
            if receipt is not None:
                return {
                    "request_id": external_request_id, "action": receipt["action"], "state": "committed",
                    "found": True, "committed": True, "status": "ok", "error_code": None,
                    "created_at": receipt["created_at"], "updated_at": receipt["created_at"],
                    "retry": None, "next_action": None,
                }
        return {"request_id": external_request_id, "state": "unknown", "found": False, "committed": False, "retry": None}
    receipt = db.execute(
        "SELECT action, created_at FROM request_receipts WHERE request_id=? AND action=?",
        (request_id, row["action"]),
    ).fetchone()
    state = "committed" if receipt is not None else row["state"]
    if state == "running":
        state = "unknown"
    retry = {"request_id": external_request_id, "same_arguments": True} if state == "unknown" else None
    next_action = "retry_same_request_id_and_arguments" if state == "unknown" else None
    return {
        "request_id": external_request_id, "action": row["action"], "state": state,
        "found": True, "committed": state == "committed", "status": row["result_status"],
        "error_code": row["error_code"], "created_at": row["created_at"],
        "updated_at": row["updated_at"], "retry": retry, "next_action": next_action,
    }


def _table_version(db, table, key, identifier, version_column="version"):
    row = db.execute(f'SELECT {version_column} FROM "{table}" WHERE "{key}"=?', (identifier,)).fetchone()
    return row[version_column] if row is not None else None


def _ops_record(book, table, key, identifier, actor):
    path = getattr(book, "_mcp24_ops_path", None)
    if path is None and book is not None and not getattr(book, "_uri", False):
        path = Path(book.db_path).expanduser().resolve().parent / "mcp24-operations.sqlite3"
    if path is None or not Path(path).is_file():
        return None
    identity = {
        "host_id": actor["host_id"], "profile": actor["profile"],
        "scopes": sorted(actor["scopes"]), "authorization_revision": actor["authorization_revision"],
    }
    actor_domain = _hash(identity)
    uri = Path(path).expanduser().resolve().as_uri() + "?mode=ro"
    db_ops = sqlite3.connect(uri, uri=True, timeout=2)
    db_ops.row_factory = sqlite3.Row
    try:
        exists = db_ops.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if exists is None:
            return None
        if table == "mcp24_cleanup_plans":
            return db_ops.execute(f"SELECT fingerprint,state,created_at FROM {table} WHERE {key}=? AND actor_domain=?", (identifier, actor_domain)).fetchone()
        if table == "mcp24_async_jobs":
            return db_ops.execute(f"SELECT action,status,cancel_requested,result_pruned,updated_at FROM {table} WHERE {key}=? AND actor_domain=?", (identifier, actor_domain)).fetchone()
        return None
    finally:
        db_ops.close()


def _database_fingerprint(db):
    digest = hashlib.sha256()
    tables = db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND name <> 'request_receipts' AND name NOT LIKE 'mcp24_%' ORDER BY name"
    )
    for table_row in tables:
        table = table_row["name"]
        digest.update(_json({"name": table, "schema": table_row["sql"]}).encode("utf-8"))
        quoted = table.replace('"', '""')
        for row in db.execute(f'SELECT * FROM "{quoted}" ORDER BY rowid'):
            values = []
            for value in row:
                if isinstance(value, bytes):
                    values.append({"blob_sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)})
                else:
                    values.append(value)
            digest.update(_json(values).encode("utf-8"))
    return digest.hexdigest()


def current_object_versions(db, action, params, *, book=None, channel="mcp", now=None, actor=None):
    """Return privacy-safe live version tokens used by dry-run and access guard."""
    versions = {}
    if action == "restore_backup":
        from . import mcp_files
        preview = mcp_files.handle(book, "preview_backup", {"backup_id": params["backup_id"]}, channel, now)
        fingerprint = preview["fingerprint"]
        if params.get("fingerprint") is not None and params["fingerprint"] != fingerprint:
            raise mcp_workflows.WorkflowError("version_conflict", "备份已变化，请重新预览。", {"backup_id": params["backup_id"]})
        versions[f"backup:{params['backup_id']}"] = fingerprint
        versions["database:current"] = _database_fingerprint(db)
        return versions
    if action == "apply_actions":
        for step in params["actions"]:
            versions.update(current_object_versions(db, step["tool"], step["arguments"], book=book, channel=channel, now=now, actor=actor))
        return versions
    upload_id = params.get("upload_id") if action in {
        "put_attachment_upload_chunk", "finish_attachment_upload", "cancel_attachment_upload",
    } else None
    if upload_id:
        host_id, profile, revision = _actor_key(actor)
        row = db.execute(
            "SELECT status, image_type, size_bytes, sha256, updated_at, expires_at "
            "FROM mcp24_attachment_uploads WHERE upload_id=? AND host_id=? AND profile=? AND authorization_revision=?",
            (upload_id, host_id, profile, revision),
        ).fetchone()
        if row is not None:
            chunks = [dict(item) for item in db.execute(
                "SELECT part_number, part_sha256, size_bytes FROM mcp24_attachment_chunks "
                "WHERE upload_id=? ORDER BY part_number", (upload_id,),
            )]
            versions[f"upload:{upload_id}"] = _hash({"upload": dict(row), "chunks": chunks})
    prompt_id = params.get("prompt_id")
    if prompt_id:
        value = _table_version(db, "prompts", "id", prompt_id)
        if value is not None:
            versions[f"prompt:{prompt_id}"] = value
    material_id = params.get("id")
    if material_id and any(part in action for part in ("inspiration", "clipping", "resume")):
        value = _table_version(db, "materials", "id", material_id)
        if value is not None:
            versions[f"material:{material_id}"] = value
    topic_id = params.get("topic_id")
    if topic_id:
        value = _table_version(db, "preference_meta", "singleton", 1)
        if value is not None:
            versions[f"topic:{topic_id}"] = value
    project_id = params.get("id") if action in {"save_topic_project", "get_topic_project"} else None
    if project_id:
        value = _table_version(db, "mcp_topic_projects", "id", project_id)
        if value is not None:
            versions[f"topic_project:{project_id}"] = value
    if action in {"add_topic", "update_topic", "set_direction_weight", "set_topic_weight", "reset_weights", "end_preference"}:
        value = _table_version(db, "preference_meta", "singleton", 1)
        if value is not None:
            versions["preference:all"] = value
    if action == "set_topic_aliases" and topic_id:
        value = _table_version(db, "mcp_read_topic_aliases", "topic_id", topic_id)
        versions[f"topic_aliases:{topic_id}"] = value or 0
    undo_id = params.get("target_request_id")
    if action == "undo_action" and undo_id:
        row = db.execute("SELECT changes_json, undone_at FROM mcp_undo_log WHERE request_id=?", (undo_id,)).fetchone()
        if row is not None:
            versions[f"undo:{undo_id}"] = _hash({"changes": row["changes_json"], "undone_at": row["undone_at"]})
    if action in {"apply_retention_cleanup", "cancel_async_job", "resume_async_job"}:
        if action == "apply_retention_cleanup":
            identifier, table, key, label = params["plan_id"], "mcp24_cleanup_plans", "plan_id", "cleanup_plan"
        else:
            identifier, table, key, label = params["job_id"], "mcp24_async_jobs", "job_id", "job"
        row = _ops_record(book, table, key, identifier, actor)
        if row is not None:
            fields = ("fingerprint", "state", "created_at") if label == "cleanup_plan" else ("action", "status", "cancel_requested", "result_pruned", "updated_at")
            versions[f"{label}:{identifier}"] = _hash({name: row[name] for name in fields})
    if action == "receive_handoff" and params.get("handoff_id"):
        table = _access()._table(db, "handoffs")
        row = db.execute(f"SELECT prompt_version, clues_json, expires_at, consumed_at FROM {table} WHERE handoff_id=?", (params["handoff_id"],)).fetchone()
        if row is not None:
            versions[f"handoff:{params['handoff_id']}"] = _hash(dict(row))
    for clue in params.get("clues", ()):
        value = _table_version(db, "materials", "id", clue["id"])
        if value is not None:
            versions[f"material:{clue['id']}:{clue['field']}"] = value
    for claim in params.get("claims", ()):
        for identifier in claim.get("clipping_ids", ()):
            value = _table_version(db, "materials", "id", identifier)
            if value is not None:
                versions[f"material:{identifier}"] = value
    for excerpt in params.get("authorized_excerpts", ()):
        if excerpt["kind"] == "prompt":
            value = _table_version(db, "prompts", "id", excerpt["id"])
            if value is not None:
                versions[f"prompt:{excerpt['id']}"] = value
        elif excerpt["kind"] in {"inspiration", "clipping", "resume"}:
            value = _table_version(db, "materials", "id", excerpt["id"])
            if value is not None:
                versions[f"material:{excerpt['id']}:{excerpt['field']}"] = value
    for reference in params.get("items", ()):
        kind, identifier = reference["kind"], reference["id"]
        if kind == "prompt":
            value = _table_version(db, "prompts", "id", identifier)
            if value is not None:
                versions[f"prompt:{identifier}"] = value
        elif kind in {"inspiration", "clipping", "resume"}:
            value = _table_version(db, "materials", "id", identifier)
            if value is not None:
                versions[f"material:{identifier}"] = value
        elif kind == "topic":
            value = _table_version(db, "preference_meta", "singleton", 1)
            if value is not None:
                versions[f"topic:{identifier}"] = value
    return versions


def _target_references(action, params, versions):
    if action == "apply_actions":
        return [
            {"action": step["tool"], "objects": _target_references(step["tool"], step["arguments"], versions)}
            for step in params["actions"]
        ]
    references = []
    typed = (("prompt", "prompt_id"), ("topic", "topic_id"), ("backup", "backup_id"),
             ("undo", "target_request_id"), ("task", "task_id"),
             ("upload", "upload_id"))
    for kind, field_name in typed:
        identifier = params.get(field_name)
        if identifier:
            key = f"{kind}:{identifier}"
            references.append({"type": kind, "id": identifier, "version": versions.get(key), "observed": key in versions})
    for field_name, kind in (("handoff_id", "handoff"), ("job_id", "job")):
        identifier = params.get(field_name)
        if identifier:
            key = f"{kind}:{identifier}"
            references.append({"type": kind, "id": identifier, "version": versions.get(key), "observed": key in versions})
    topic_id = params.get("topic_id")
    if topic_id and f"topic_aliases:{topic_id}" in versions:
        references.append({"type": "topic_aliases", "id": topic_id, "version": versions[f"topic_aliases:{topic_id}"], "observed": True})
    plan_id = params.get("plan_id")
    if plan_id:
        key = f"cleanup_plan:{plan_id}"
        references.append({"type": "cleanup_plan", "id": plan_id, "version": versions.get(key), "observed": key in versions})
    for clue in params.get("clues", ()):
        key = f"material:{clue['id']}:{clue['field']}"
        references.append({"type": "material_field", "id": clue["id"], "field": clue["field"], "version": versions.get(key), "observed": key in versions})
    for excerpt in params.get("authorized_excerpts", ()):
        kind = "prompt" if excerpt["kind"] == "prompt" else "material"
        key = f"{kind}:{excerpt['id']}" if kind == "prompt" else f"material:{excerpt['id']}:{excerpt['field']}"
        references.append({"type": kind, "id": excerpt["id"], "field": excerpt.get("field"),
                           "version": versions.get(key), "observed": key in versions})
    for claim in params.get("claims", ()):
        for identifier in claim.get("clipping_ids", ()):
            key = f"material:{identifier}"
            references.append({"type": "material", "id": identifier,
                               "version": versions.get(key), "observed": key in versions})
    for reference in params.get("items", ()):
        kind, identifier = reference["kind"], reference["id"]
        version_kind = "material" if kind in {"inspiration", "clipping", "resume"} else kind
        key = f"{version_kind}:{identifier}"
        references.append({"type": version_kind, "id": identifier, "version": versions.get(key), "observed": key in versions})
    identifier = params.get("id")
    if identifier:
        if any(part in action for part in ("inspiration", "clipping", "resume")):
            kind = "material"
        elif "topic_project" in action:
            kind = "topic_project"
        else:
            kind = "object"
        key = f"{kind}:{identifier}"
        references.append({"type": kind, "id": identifier, "version": versions.get(key), "observed": key in versions})
    if "database:current" in versions:
        references.append({"type": "database", "id": "current", "version": versions["database:current"], "observed": True})
    return references


def _dry_run(book, db, params, channel, validate, now, actor):
    _require_allowed(actor, "dry_run_actions")
    action = params["action"]
    _require_allowed(actor, action)
    spec = _registry().get(action)
    if spec is None or action in {"dry_run_actions", "continue_pending_call", "get_request_status"}:
        raise mcp_workflows.WorkflowError("invalid_action", "只能预览目录中已注册的业务工具。", {"action": action})
    if action not in _write_names():
        raise mcp_workflows.WorkflowError("invalid_action", "dry-run 只接受会改变状态的动作。", {"action": action})
    arguments = validate(action, params["arguments"], channel)
    if action == "apply_actions":
        for step in arguments["actions"]:
            if step["tool"] in CONTRACT_TOOLS or not _access().allows(actor, step["tool"]):
                raise mcp_workflows.WorkflowError("forbidden", "当前宿主不能预览此批量动作中的一个工具。", {"tool": step["tool"]})
    versions = current_object_versions(db, action, arguments, book=book, channel=channel, now=now, actor=actor)
    summary = {
        "actions": [{"tool": action, "objects": _target_references(action, arguments, versions),
                     "expected_versions": {key: value for key, value in arguments.items() if key in {"expected_version", "fingerprint"}}}],
        "object_versions": versions,
    }
    result = {"preview": summary, "writes_performed": False, "bound_preview": None}
    if params.get("bind_preview"):
        if action not in _write_names() or not versions:
            raise mcp_workflows.WorkflowError("invalid_params", "绑定预览需要可授权的写入动作和可核对的目标版本。")
        effect_params = {key: value for key, value in arguments.items() if key not in {"request_id", "confirmed", "preview_id", "bind_preview"}}
        access = _access()
        try:
            preview = access.create_bound_preview(
                db, actor=actor, action=action,
                canonical_params_hash=access.canonical_params_hash(action, effect_params),
                object_versions=versions, now=now, expires_at=now + PREVIEW_TTL,
            )
        except access.AccessError as error:
            raise mcp_workflows.WorkflowError(error.code, error.message, error.details)
        result["bound_preview"] = preview
    return result


def _argument_schema(tool_name):
    spec = _registry().get(tool_name)
    if spec is None:
        raise mcp_workflows.WorkflowError("invalid_action", "待续接工具不在真实目录中。", {"tool": tool_name})
    return spec["inputSchema"]


def _validate_partial(tool_name, arguments):
    from . import book
    properties = _argument_schema(tool_name).get("properties", {})
    for key, value in arguments.items():
        book._validate_field(value, properties[key], f"{tool_name}.{key}")


def _pending_result(row, missing_fields):
    return {
        "pending_id": row["pending_id"], "tool": row["action"], "state": row["state"],
        "version": row["version"], "expires_at": row["expires_at"],
        "missing_fields": missing_fields, "confirmed": row["state"] == "confirmed",
    }


def _continue_pending(book, db, params, channel, validate, now, actor):
    _require_allowed(actor, "continue_pending_call")
    host_id, profile, revision = _actor_key(actor)
    registry = _registry()
    args_patch = params.get("arguments", {})
    if "pending_id" not in params:
        tool_name = params.get("tool")
        if not tool_name:
            raise mcp_workflows.WorkflowError("needs_clarification", "请先指定要续接的工具。", {"question": "要补充哪个工具调用？", "missing_fields": ["tool"]})
        _require_allowed(actor, tool_name)
        if tool_name not in registry or tool_name in CONTRACT_TOOLS:
            raise mcp_workflows.WorkflowError("invalid_action", "待续接目标必须是目录中的业务工具。", {"tool": tool_name})
        argument_schema = registry[tool_name]["inputSchema"]
        properties = argument_schema.get("properties", {})
        unknown = sorted(set(args_patch) - set(properties))
        if unknown:
            raise mcp_workflows.WorkflowError("invalid_params", "参数包含该工具未声明的字段。", {"fields": unknown})
        _validate_partial(tool_name, args_patch)
        required = argument_schema.get("required", [])
        missing = [key for key in required if key not in args_patch]
        arguments = validate(tool_name, args_patch, channel) if not missing else args_patch
        privacy_guard.check_action(db, tool_name, arguments)
        mcp_workflows.check_staged_action_privacy(book, db, tool_name, arguments, channel, actor)
        pending_id = uuid.uuid4().hex
        expires_at = _iso(now + PENDING_TTL)
        created_at = _iso(now)
        db.execute(
            "INSERT INTO mcp24_pending_calls(pending_id,request_id,host_id,profile,authorization_revision,action,arguments_json,arguments_hash,missing_fields_json,expires_at,version,state,protected,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,1,'pending',1,?,?)",
            (pending_id, _access().namespace_id(actor, "request", params["request_id"]), host_id, profile, revision, tool_name,
             _json(arguments), _hash(arguments), _json(missing), expires_at, created_at, created_at),
        )
        row = db.execute("SELECT * FROM mcp24_pending_calls WHERE pending_id=?", (pending_id,)).fetchone()
        return {**_pending_result(row, missing), "next_action": "provide_missing_fields" if missing else "review_and_confirm"}

    row = db.execute(
        "SELECT * FROM mcp24_pending_calls WHERE pending_id=? AND host_id=? AND profile=?",
        (params["pending_id"], host_id, profile),
    ).fetchone()
    if row is None:
        raise mcp_workflows.WorkflowError("not_found", "未找到当前宿主的待续接调用。")
    if row["authorization_revision"] != revision:
        raise mcp_workflows.WorkflowError("authorization_changed", "宿主授权版本已变化，请重新建立待续接调用。", {"pending_id": row["pending_id"]})
    if datetime.fromisoformat(row["expires_at"]) <= now:
        raise mcp_workflows.WorkflowError("expired", "待续接调用已过期，请重新建立。", {"pending_id": row["pending_id"]})
    if row["state"] == "confirmed":
        raise mcp_workflows.WorkflowError("already_consumed", "该待续接调用已确认并返回过一次调用建议。", {"pending_id": row["pending_id"]})
    if params.get("expected_version") != row["version"]:
        raise mcp_workflows.WorkflowError("version_conflict", "待续接内容已变化，请读取当前版本后重试。", {"pending_id": row["pending_id"], "actual_version": row["version"]})
    arguments = json.loads(row["arguments_json"])
    if _hash(arguments) != row["arguments_hash"]:
        raise mcp_workflows.WorkflowError("pending_call_conflict", "待续接参数记录与保存摘要不一致。", {"pending_id": row["pending_id"]})
    conflict = [key for key, value in args_patch.items() if key in arguments and arguments[key] != value]
    if conflict:
        raise mcp_workflows.WorkflowError("parameter_conflict", "补充参数与已保存值冲突，请重新建立待续接调用。", {"fields": conflict})
    arguments.update(args_patch)
    tool_name = row["action"]
    if params.get("tool") is not None and params["tool"] != tool_name:
        raise mcp_workflows.WorkflowError("parameter_conflict", "续接工具与已绑定目标不一致。", {"tool": tool_name})
    _require_allowed(actor, tool_name)
    argument_schema = _argument_schema(tool_name)
    properties = argument_schema.get("properties", {})
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise mcp_workflows.WorkflowError("invalid_params", "参数包含该工具未声明的字段。", {"fields": unknown})
    required = argument_schema.get("required", [])
    if _named_actor(actor) and tool_name == "share_excerpt" and "request_id" not in required:
        required = [*required, "request_id"]
    missing = [key for key in required if key not in arguments]
    _validate_partial(tool_name, arguments)
    normalized = validate(tool_name, arguments, channel) if not missing else arguments
    privacy_guard.check_action(db, tool_name, normalized)
    mcp_workflows.check_staged_action_privacy(book, db, tool_name, normalized, channel, actor)
    if missing:
        db.execute(
            "UPDATE mcp24_pending_calls SET arguments_json=?,arguments_hash=?,missing_fields_json=?,version=version+1,updated_at=? WHERE pending_id=?",
            (_json(normalized), _hash(normalized), _json(missing), _iso(now), row["pending_id"]),
        )
        updated = db.execute("SELECT * FROM mcp24_pending_calls WHERE pending_id=?", (row["pending_id"],)).fetchone()
        return {**_pending_result(updated, missing), "next_action": "provide_missing_fields"}
    if _named_actor(actor) and tool_name != "share_excerpt" and normalized.get("include_private") is True:
        raise mcp_workflows.WorkflowError("forbidden", "具名宿主读取私密内容必须使用精确片段授权工具。")
    missing_scopes = _missing_scopes(actor, tool_name, normalized)
    if missing_scopes:
        raise mcp_workflows.WorkflowError("forbidden", "当前宿主没有目标字段的授权范围。", {"missing_scopes": missing_scopes})
    if params.get("confirmed") is not True:
        db.execute(
            "UPDATE mcp24_pending_calls SET arguments_json=?,arguments_hash=?,missing_fields_json='[]',version=version+1,updated_at=? WHERE pending_id=?",
            (_json(normalized), _hash(normalized), _iso(now), row["pending_id"]),
        )
        final = db.execute("SELECT * FROM mcp24_pending_calls WHERE pending_id=?", (row["pending_id"],)).fetchone()
        return {**_pending_result(final, []), "confirmed": False, "next_action": "confirm_exact_tool_call"}
    db.execute(
        "UPDATE mcp24_pending_calls SET arguments_json=?,arguments_hash=?,missing_fields_json='[]',version=version+1,state='confirmed',protected=1,updated_at=? WHERE pending_id=?",
        (_json(normalized), _hash(normalized), _iso(now), row["pending_id"]),
    )
    final = db.execute("SELECT * FROM mcp24_pending_calls WHERE pending_id=?", (row["pending_id"],)).fetchone()
    return {**_pending_result(final, []), "state": "ready", "confirmed": True, "target_call_not_executed": True,
            "next_call": {"tool": tool_name, "arguments": normalized, "condition": "用户已确认；通过正常工具入口执行一次"}}


def _next_action(result):
    data = result.get("data") or {}
    error = result.get("error") or {}
    details = error.get("details") or {}
    call = data.get("next_call") or details.get("next_call")
    if call:
        return {"tool": call.get("tool"), "condition": call.get("condition")}
    if data.get("next_calls") or details.get("next_calls"):
        return "follow_next_calls_after_conditions"
    return None


def normalize_receipt(result, *, request_id, action):
    """Keep the legacy envelope intact and append a safe, uniform receipt envelope."""
    normalized = dict(result)
    envelope = dict(normalized.get("envelope") or {})
    status = normalized.get("status")
    data = normalized.get("data") or {}
    if data.get("committed") is True or (status == "ok" and action in _write_names()):
        state = "committed"
    elif status == "submission_unknown":
        state = "unknown"
    elif status == "ok":
        state = "completed"
    elif status:
        state = "failed"
    else:
        state = "unknown"
    envelope["receipt"] = {
        "request_id": request_id, "action": action, "state": state,
        "committed": state == "committed",
        "retry": {"request_id": request_id, "same_arguments": True} if state == "unknown" else None,
        "next_action": _next_action(normalized),
    }
    normalized["envelope"] = envelope
    return normalized


def observe_dispatch(db, *, actor, request_id, action, phase, status, result, now):
    """Persist actor-scoped safe status metadata; never retain result bodies."""
    if actor is None or not request_id:
        return None
    host_id, profile, revision = _actor_key(actor)
    created_at = _iso(now)
    if phase == "start":
        db.execute(
            "INSERT INTO mcp24_request_observations(request_id,host_id,profile,authorization_revision,action,state,result_status,error_code,committed,protected,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'running','running',NULL,0,1,?,?) "
            "ON CONFLICT(host_id,profile,request_id) DO NOTHING",
            (request_id, host_id, profile, revision, action, created_at, created_at),
        )
        return None
    if phase != "end":
        raise ValueError("phase must be start or end")
    result = result or {}
    result_status = result.get("status", status)
    error = result.get("error") or {}
    error_code = error.get("code")
    data = result.get("data") or {}
    receipt = db.execute("SELECT 1 FROM request_receipts WHERE request_id=? AND action=?", (request_id, action)).fetchone()
    if receipt is not None or data.get("committed") is True:
        state = "committed"
    elif result_status == "submission_unknown" or result_status == "unknown":
        state = "unknown"
    elif result_status == "ok":
        state = "unknown" if action in _write_names() else "completed"
    elif result_status:
        state = "failed"
    else:
        state = "unknown"
    committed = int(state == "committed")
    db.execute(
        "INSERT INTO mcp24_request_observations(request_id,host_id,profile,authorization_revision,action,state,result_status,error_code,committed,protected,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,1,?,?) "
        "ON CONFLICT(host_id,profile,request_id) DO UPDATE SET "
        "authorization_revision=excluded.authorization_revision,action=excluded.action,state=excluded.state,result_status=excluded.result_status,error_code=excluded.error_code,committed=excluded.committed,updated_at=excluded.updated_at "
        "WHERE mcp24_request_observations.state = 'running'",
        (request_id, host_id, profile, revision, action, state, result_status, error_code, committed, created_at, created_at),
    )
    return None


HANDLERS = {
    "plan_mcp_calls": lambda book, db, params, channel, validate, now, actor: _plan(book, params, actor, now),
    "get_mcp_directory": lambda book, db, params, channel, validate, now, actor: _directory(book, params, actor),
    "compare_mcp_directories": lambda book, db, params, channel, validate, now, actor: _compare_mcp_directories(book, params, actor),
    "diagnose_mcp_compatibility": lambda book, db, params, channel, validate, now, actor: _diagnose(book, params, actor),
    "run_mcp_self_check": lambda book, db, params, channel, validate, now, actor: _self_check(book, actor),
    "get_request_status": lambda book, db, params, channel, validate, now, actor: _status(db, params, actor),
    "dry_run_actions": _dry_run,
    "continue_pending_call": _continue_pending,
}


def handle(book, db, action, params, channel, validate, now, actor):
    return HANDLERS[action](book, db, params, channel, validate, now, actor)
