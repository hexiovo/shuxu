"""Session conditions, prompt-family decisions, unfinished prompts and question styles."""

from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timedelta, timezone

from . import mcp24_access, mcp_workflows, privacy_guard


_CONDITION_FIELDS = ("direction", "topic", "depth", "minutes", "exclude_directions", "avoid_days")
_QUESTION_MODES = ("experience", "contrast", "counterexample", "boundary", "evidence", "narrow")
_UNFINISHED_PRIORITY = ("in_progress", "want_to_write", "due_snooze", "checkpoint_only")


def specs(tool, field, ident, direction, version, include_private):
    text = lambda size: field("string", minLength=1, maxLength=size)
    choice = lambda *values: field("string", enum=list(values))
    count = lambda low, high: field("integer", minimum=low, maximum=high)
    version_zero = field("integer", minimum=0)
    timestamp = field("string", minLength=1, format="date-time")
    condition_fields = {
        "direction": direction, "topic": ident,
        "depth": choice("light", "medium", "deep"), "minutes": count(1, 240),
        "exclude_directions": field("array", items=direction, minItems=1, maxItems=3),
        "avoid_days": count(0, 90),
    }
    relation = choice("same_family", "separate")
    modes = choice(*_QUESTION_MODES)
    return [
        tool("get_session_conditions", "读取当前会话有效的选题条件、来源轮次和到期时间。", {
            "conversation_id": ident,
        }, ("conversation_id",)),
        tool("set_session_conditions", "在指定会话中保存选题条件；本次 next_prompt 明确传入的值优先覆盖。", {
            "request_id": ident, "conversation_id": ident, "expected_version": version_zero,
            "conditions": field("object", properties=condition_fields, additionalProperties=False),
            "expires_at": timestamp,
        }, ("request_id", "conversation_id", "expected_version", "conditions")),
        tool("end_session_conditions", "结束当前会话的全部或指定选题条件。", {
            "request_id": ident, "conversation_id": ident, "expected_version": version_zero,
            "fields": field("array", items=choice(*_CONDITION_FIELDS), minItems=1, maxItems=len(_CONDITION_FIELDS)),
        }, ("request_id", "conversation_id", "expected_version")),
        tool("get_prompt_family", "读取题目的已确认题族，并列出可核对的近似题候选。", {
            "prompt_id": ident, "limit": count(1, 30),
        }, ("prompt_id",)),
        tool("set_prompt_family_relation", "确认两题同族或将自动近似项拆开；保留原 parent_id 收窄关系。", {
            "request_id": ident, "prompt_id": ident, "related_prompt_id": ident,
            "relation": relation, "expected_version": version_zero, "reason": text(300),
        }, ("request_id", "prompt_id", "related_prompt_id", "relation", "expected_version")),
        tool("pick_unfinished_prompt", "从可继续的未完成题、到期暂缓题和有续接点的题中按实际活动排序选一题。", {
            "request_id": ident, "conversation_id": ident, "direction": direction, "topic": ident,
            "depth": choice("light", "medium", "deep"), "minutes": count(1, 240),
            "exclude_directions": field("array", items=direction, minItems=1, maxItems=3),
            "avoid_days": count(0, 90), "exclude_family_ids": field("array", items=ident, minItems=1, maxItems=20),
            "limit": count(1, 20),
        }, ("request_id", "conversation_id")),
        tool("list_unfinished_prompts", "按与未完成题推荐相同的过滤和排序分页读取候选，不记录新的推荐。", {
            "conversation_id": ident, "direction": direction, "topic": ident,
            "depth": choice("light", "medium", "deep"), "minutes": count(1, 240),
            "exclude_directions": field("array", items=direction, minItems=1, maxItems=3),
            "avoid_days": count(0, 90), "exclude_family_ids": field("array", items=ident, minItems=1, maxItems=20),
            "limit": count(1, 50), "offset": count(0, 10000),
        }, ("conversation_id",)),
        tool("get_guide_history", "按题目或会话分页读取已保存的追问方式、用户明确线索和问题，不含用户回答。", {
            "conversation_id": ident, "prompt_id": ident,
            "history_scope": choice("same_host", "all_hosts"), "limit": count(1, 50),
            "offset": count(0, 10000),
        }),
        tool("set_question_preference", "设置长期或近期追问方式偏好，沿用全局偏好版本；scope=recent 必须提供 expires_at，scope=baseline 不可提供 expires_at。", {
            "request_id": ident, "expected_version": version, "mode": modes, "direction": direction,
            "scope": choice("baseline", "recent"), "strength": field("number", minimum=0, maximum=2),
            "expires_at": timestamp, "reason": text(300),
        }, ("request_id", "expected_version", "mode", "scope", "strength")),
        tool("get_question_preferences", "读取问法偏好的长期值、近期值、生效强度和变更历史。", {
            "direction": direction, "include_history": field("boolean"), "limit": count(1, 50),
        }),
        tool("end_question_preference", "结束指定方向或默认范围内某种问法的近期偏好。", {
            "request_id": ident, "expected_version": version, "mode": modes, "direction": direction,
        }, ("request_id", "expected_version", "mode")),
    ]


def schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS mcp25_session_conditions (
            conversation_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
            conditions_json TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mcp25_prompt_family_relations (
            prompt_id TEXT NOT NULL, related_prompt_id TEXT NOT NULL,
            relation TEXT NOT NULL CHECK(relation IN ('same_family','separate')),
            reason TEXT, version INTEGER NOT NULL, request_id TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(prompt_id, related_prompt_id), CHECK(prompt_id < related_prompt_id)
        );
        CREATE INDEX IF NOT EXISTS mcp25_prompt_family_right
            ON mcp25_prompt_family_relations(related_prompt_id, prompt_id);
        CREATE TABLE IF NOT EXISTS mcp25_question_preferences (
            direction TEXT NOT NULL, mode TEXT NOT NULL, scope TEXT NOT NULL,
            strength REAL NOT NULL, expires_at TEXT, reason TEXT,
            updated_at TEXT NOT NULL, PRIMARY KEY(direction, mode, scope)
        );
        CREATE TABLE IF NOT EXISTS mcp25_question_preference_history (
            event_id TEXT PRIMARY KEY, direction TEXT NOT NULL, mode TEXT NOT NULL, scope TEXT NOT NULL,
            before_json TEXT, after_json TEXT, preference_version INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS mcp25_question_preference_history_time
            ON mcp25_question_preference_history(direction, mode, created_at DESC);
        CREATE TABLE IF NOT EXISTS mcp25_guide_history (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
            prompt_id TEXT NOT NULL, step INTEGER NOT NULL, question_mode TEXT NOT NULL,
            focus TEXT NOT NULL, question TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS mcp25_guide_history_prompt_time
            ON mcp25_guide_history(prompt_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS mcp25_guide_history_conversation_time
            ON mcp25_guide_history(conversation_id, created_at DESC);
    """)


def _iso(now):
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def effective_session_conditions(db, conversation_id, *, channel, actor, now):
    if conversation_id is None:
        return {"conditions": {}, "sources": {}, "expires_at": {}}
    row = db.execute(
        "SELECT conditions_json FROM mcp25_session_conditions WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return {"conditions": {}, "sources": {}, "expires_at": {}}
    saved = json.loads(row["conditions_json"])
    conditions, sources, expires_at = {}, {}, {}
    for name, entry in saved.items():
        expiry = entry["expires_at"]
        if expiry is not None and _parse_time(expiry) <= now.astimezone(timezone.utc):
            continue
        conditions[name] = entry["value"]
        sources[name] = {"source": "session", "request_id": entry["source_request_id"],
                         "set_at": entry["set_at"], "expires_at": expiry}
        expires_at[name] = expiry
    return {"conditions": conditions, "sources": sources, "expires_at": expires_at}


def resolve_session_conditions(book, db, params, channel, actor, now, *, fields=_CONDITION_FIELDS):
    current = effective_session_conditions(
        db, params.get("conversation_id"), channel=channel, actor=actor, now=now,
    )
    ignored = set(params.get("ignore_session_fields", ()))
    conditions = {key: value for key, value in current["conditions"].items() if key not in ignored}
    sources = {key: value for key, value in current["sources"].items() if key not in ignored}
    expiries = {key: value for key, value in current["expires_at"].items() if key not in ignored}
    inherited = list(conditions)
    conflicts = []
    for key in fields:
        if key not in params:
            continue
        value = params[key]
        if key == "topic":
            topic = book._resolve_topic(db, value)
            if topic is None or not topic["active"]:
                mcp_workflows.resolve_error("no_candidate", "指定的小主题不可用。", field="topic")
            value = topic["id"]
        if key == "exclude_directions":
            value = list(dict.fromkeys(value))
        if key in conditions and conditions[key] != value:
            conflicts.append(key)
        conditions[key] = value
        sources[key] = {"source": "call"}
        expiries.pop(key, None)
    topic = book._resolve_topic(db, conditions.get("topic")) if conditions.get("topic") else None
    if "topic" in conditions and topic is None:
        mcp_workflows.resolve_error("no_candidate", "指定的小主题不存在。", field="topic")
    if topic is not None and conditions.get("direction") and topic["direction"] != conditions["direction"]:
        direction_source = sources.get("direction", {}).get("source")
        topic_source = sources.get("topic", {}).get("source")
        if direction_source == "call" and topic_source == "call":
            mcp_workflows.resolve_error("needs_clarification", "本次方向与主题冲突。", conflict_fields=["direction", "topic"])
        if direction_source == "call":
            conditions.pop("topic", None)
            sources.pop("topic", None)
            expiries.pop("topic", None)
            conflicts.append("topic")
        else:
            conditions.pop("direction", None)
            sources.pop("direction", None)
            expiries.pop("direction", None)
            conflicts.append("direction")
    return {
        "conditions": conditions, "sources": sources, "expires_at": expiries,
        "inherited_fields": [key for key in inherited if key in conditions and sources.get(key, {}).get("source") == "session"],
        "conflicts": list(dict.fromkeys(conflicts)),
    }


def _session_view(db, conversation_id, now):
    row = db.execute(
        "SELECT version, conditions_json, updated_at FROM mcp25_session_conditions WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return {"version": 0, "conditions": {}, "sources": {}, "expired_fields": [], "updated_at": None}
    saved = json.loads(row["conditions_json"])
    conditions, sources, expired = {}, {}, []
    for name, entry in saved.items():
        expiry = entry["expires_at"]
        if expiry is not None and _parse_time(expiry) <= now.astimezone(timezone.utc):
            expired.append(name)
            continue
        conditions[name] = entry["value"]
        sources[name] = {"source": "session", "request_id": entry["source_request_id"],
                         "set_at": entry["set_at"], "expires_at": expiry}
    return {"version": row["version"], "conditions": conditions, "sources": sources,
            "expired_fields": sorted(expired), "updated_at": row["updated_at"]}


def get_session_conditions(book, db, params, channel, validate, now, actor):
    return {"conversation_id": params["conversation_id"], **_session_view(db, params["conversation_id"], now),
            "precedence": ["call", "session", "preference"]}


def set_session_conditions(book, db, params, channel, validate, now, actor):
    conditions = dict(params["conditions"])
    if not conditions:
        mcp_workflows.resolve_error("invalid_params", "conditions 至少指定一个会话条件。", fields=["conditions"])
    topic = book._resolve_topic(db, conditions.get("topic")) if "topic" in conditions else None
    if "topic" in conditions and (topic is None or not topic["active"]):
        mcp_workflows.resolve_error("not_found", "未找到可用的小主题。", topic=conditions["topic"])
    if topic is not None:
        conditions["topic"] = topic["id"]
        if conditions.get("direction") and conditions["direction"] != topic["direction"]:
            mcp_workflows.resolve_error("invalid_params", "direction 与 topic 必须属于同一方向。", fields=["direction", "topic"])
    if "exclude_directions" in conditions:
        conditions["exclude_directions"] = list(dict.fromkeys(conditions["exclude_directions"]))
        if not conditions["exclude_directions"]:
            mcp_workflows.resolve_error("invalid_params", "exclude_directions 至少指定一个方向。", fields=["exclude_directions"])
    expiry = _iso(_parse_time(params["expires_at"])) if "expires_at" in params else None
    if expiry is not None and _parse_time(expiry) <= now.astimezone(timezone.utc):
        mcp_workflows.resolve_error("invalid_params", "expires_at 必须晚于当前时间。", fields=["expires_at"])
    row = db.execute("SELECT * FROM mcp25_session_conditions WHERE conversation_id = ?", (params["conversation_id"],)).fetchone()
    version = row["version"] if row else 0
    if version != params["expected_version"]:
        mcp_workflows.resolve_error("version_conflict", "会话条件已变化，请先读取当前值。",
                                    conversation_id=params["conversation_id"], expected_version=params["expected_version"], actual_version=version)
    saved = json.loads(row["conditions_json"]) if row else {}
    set_at = _iso(now)
    for name, value in conditions.items():
        saved[name] = {"value": value, "set_at": set_at, "expires_at": expiry,
                       "source_request_id": params["request_id"]}
    new_version = version + 1
    db.execute(
        "INSERT INTO mcp25_session_conditions VALUES (?, ?, ?, ?) "
        "ON CONFLICT(conversation_id) DO UPDATE SET version=excluded.version, conditions_json=excluded.conditions_json, updated_at=excluded.updated_at",
        (params["conversation_id"], new_version, json.dumps(saved, ensure_ascii=False), set_at),
    )
    return mcp_workflows.receipt(params, version=new_version, conversation_id=params["conversation_id"],
                                 conditions=conditions, sources={key: {"source": "session", "set_at": set_at, "expires_at": expiry} for key in conditions},
                                 expires_at={key: expiry for key in conditions})


def end_session_conditions(book, db, params, channel, validate, now, actor):
    row = db.execute("SELECT * FROM mcp25_session_conditions WHERE conversation_id = ?", (params["conversation_id"],)).fetchone()
    if row is None:
        if params["expected_version"] != 0:
            mcp_workflows.resolve_error("version_conflict", "会话条件已变化，请先读取当前值。",
                                        conversation_id=params["conversation_id"], expected_version=params["expected_version"], actual_version=0)
        return mcp_workflows.receipt(params, conversation_id=params["conversation_id"], version=0, ended_fields=[], ended=False)
    if row["version"] != params["expected_version"]:
        mcp_workflows.resolve_error("version_conflict", "会话条件已变化，请先读取当前值。",
                                    conversation_id=params["conversation_id"], expected_version=params["expected_version"], actual_version=row["version"])
    saved = json.loads(row["conditions_json"])
    fields = params.get("fields", list(saved))
    ended = [name for name in fields if name in saved]
    if not ended:
        return mcp_workflows.receipt(params, conversation_id=params["conversation_id"], version=row["version"], ended_fields=[], ended=False)
    for name in ended:
        saved.pop(name)
    version = row["version"] + 1
    db.execute("UPDATE mcp25_session_conditions SET version=?,conditions_json=?,updated_at=? WHERE conversation_id=?",
               (version, json.dumps(saved, ensure_ascii=False), _iso(now), params["conversation_id"]))
    return mcp_workflows.receipt(params, conversation_id=params["conversation_id"], version=version, ended_fields=ended, ended=True)


def _pair(left, right):
    return tuple(sorted((left, right)))


def _prompt_for_actor(book, db, prompt_id, channel, actor):
    return mcp_workflows._find_prompt_for_actor(book, db, prompt_id, channel, actor)


def _prompt_view(book, row, channel, actor, now):
    value = book._prompt_view(row, now, channel, include_state=True)
    if channel == "mcp" and row["visibility"] == "private" and mcp24_access.has_full_access(actor):
        value.update(state=row["state"], snoozed_until=row["snoozed_until"])
    return value


def _family_graph(book, db, channel, actor):
    rows = book._visible_rows(db, channel, actor)
    by_id = {row["id"]: row for row in rows}
    parents = book._family_roots(rows)
    relation_rows = db.execute("SELECT * FROM mcp25_prompt_family_relations").fetchall()
    parent = {identifier: identifier for identifier in by_id}

    def root(identifier):
        while parent[identifier] != identifier:
            parent[identifier] = parent[parent[identifier]]
            identifier = parent[identifier]
        return identifier

    def union(left, right):
        lroot, rroot = root(left), root(right)
        if lroot != rroot:
            parent[max(lroot, rroot)] = min(lroot, rroot)

    children = {}
    for identifier, family_root in parents.items():
        children.setdefault(family_root, []).append(identifier)
    for members in children.values():
        for identifier in members[1:]:
            union(members[0], identifier)
    relations = {}
    for row in relation_rows:
        if row["prompt_id"] not in by_id or row["related_prompt_id"] not in by_id:
            continue
        relations[_pair(row["prompt_id"], row["related_prompt_id"])] = row
        if row["relation"] == "same_family":
            union(row["prompt_id"], row["related_prompt_id"])
    return by_id, parents, parent, root, relations


def _family_ids(book, db, prompt_id, channel, actor):
    by_id, _parents, _parent, root, _relations = _family_graph(book, db, channel, actor)
    if prompt_id not in by_id:
        return []
    return sorted(identifier for identifier in by_id if root(identifier) == root(prompt_id))


def get_prompt_family(book, db, params, channel, validate, now, actor):
    selected = _prompt_for_actor(book, db, params["prompt_id"], channel, actor)
    by_id, parents, _parent, root, relations = _family_graph(book, db, channel, actor)
    family_ids = sorted(identifier for identifier in by_id if root(identifier) == root(selected["id"]))
    parent_ids = sorted(identifier for identifier in by_id if parents[identifier] == parents[selected["id"]])
    related = []
    separate = []
    for pair, relation in relations.items():
        if selected["id"] not in pair:
            continue
        other_id = pair[0] if pair[1] == selected["id"] else pair[1]
        view = _prompt_view(book, by_id[other_id], channel, actor, now)
        item = {"prompt": view, "relation": relation["relation"], "reason": relation["reason"],
                "version": relation["version"], "updated_at": relation["updated_at"], "source": "user_confirmed"}
        (related if relation["relation"] == "same_family" else separate).append(item)
    near = book._similar_prompt_rows(db, selected["text"], channel, actor=actor, exclude_id=selected["id"])
    candidates = []
    for match in near:
        other_id = match["row"]["id"]
        relation = relations.get(_pair(selected["id"], other_id))
        if relation is not None:
            continue
        candidates.append({"prompt": _prompt_view(book, match["row"], channel, actor, now),
                           "similarity": round(match["similarity"], 3), "duplicate": match["duplicate"],
                           "reason": "题文一致" if match["duplicate"] else f"Book 相似度 {match['similarity']:.3f}，阈值 0.72"})
    limit = params.get("limit", 10)
    return {
        "prompt": _prompt_view(book, selected, channel, actor, now),
        "family_ids": family_ids,
        "parent_family_ids": parent_ids,
        "parent_family": [_prompt_view(book, by_id[item], channel, actor, now) for item in parent_ids],
        "confirmed_relations": related, "separated_relations": separate,
        "similar_candidates": candidates[:limit], "similar_candidate_count": len(candidates),
        "has_more": len(candidates) > limit,
    }


def set_prompt_family_relation(book, db, params, channel, validate, now, actor):
    if params["prompt_id"] == params["related_prompt_id"]:
        mcp_workflows.resolve_error("invalid_params", "题目不能与自身建立题族关系。", fields=["related_prompt_id"])
    left = _prompt_for_actor(book, db, params["prompt_id"], channel, actor)
    right = _prompt_for_actor(book, db, params["related_prompt_id"], channel, actor)
    rows = book._visible_rows(db, channel, actor)
    parent_roots = book._family_roots(rows)
    if params["relation"] == "separate" and parent_roots[left["id"]] == parent_roots[right["id"]]:
        mcp_workflows.resolve_error("needs_clarification", "两题已有 parent_id 确认的收窄关系，不能拆开自动近似关系来覆盖它。",
                                    question="是否要拆开的是这组题目，还是仅忽略本次相近候选？", prompt_id=left["id"], related_prompt_id=right["id"])
    for row in (left, right):
        privacy_guard.check_material_write(
            db, content={"prompt": row["text"], "reason": params.get("reason", "")},
            direction=row["direction"], topic_id=row["topic_id"], prompt_id=row["id"],
        )
    a, b = _pair(left["id"], right["id"])
    previous = db.execute("SELECT * FROM mcp25_prompt_family_relations WHERE prompt_id=? AND related_prompt_id=?", (a, b)).fetchone()
    actual_version = previous["version"] if previous else 0
    if actual_version != params["expected_version"]:
        mcp_workflows.resolve_error("version_conflict", "题族关系已变化，请先读取当前关系。",
                                    expected_version=params["expected_version"], actual_version=actual_version,
                                    next_call=mcp_workflows.next_call("get_prompt_family", {"prompt_id": left["id"]}))
    version = actual_version + 1
    updated_at = _iso(now)
    db.execute(
        "INSERT INTO mcp25_prompt_family_relations VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(prompt_id,related_prompt_id) DO UPDATE SET relation=excluded.relation,reason=excluded.reason,version=excluded.version,request_id=excluded.request_id,updated_at=excluded.updated_at",
        (a, b, params["relation"], params.get("reason"), version, params["request_id"], updated_at, updated_at),
    )
    return mcp_workflows.receipt(params, prompt_id=left["id"], related_prompt_id=right["id"],
                                 relation=params["relation"], version=version, updated_at=updated_at)


def _session_filtered_candidates(book, db, params, channel, actor, now):
    resolved = resolve_session_conditions(book, db, params, channel, actor, now)
    conditions = resolved["conditions"]
    if "topic" in conditions:
        topic = book._resolve_topic(db, conditions["topic"])
        if topic is None or not topic["active"]:
            mcp_workflows.resolve_error("no_candidate", "当前会话主题已不可用。", field="topic")
    return resolved


def _unfinished_dispatch(book, db, params, channel, validate, now, actor):
    resolved = _session_filtered_candidates(book, db, params, channel, actor, now)
    conditions = resolved["conditions"]
    limit = params.get("limit", 8)
    avoid_days = conditions.get("avoid_days", 0)
    excluded_families = set()
    for identifier in params.get("exclude_family_ids", ()):
        family = _family_ids(book, db, identifier, channel, actor)
        if not family:
            mcp_workflows.resolve_error("not_found", "未找到可访问的排除题族。", prompt_id=identifier)
        excluded_families.update(family)
    if conditions.get("topic") and conditions.get("direction"):
        topic = book._resolve_topic(db, conditions["topic"])
        if topic and topic["direction"] != conditions["direction"]:
            mcp_workflows.resolve_error("needs_clarification", "会话方向与主题冲突。", conflict_fields=["direction", "topic"])
    time_depths = {"light"} if conditions.get("minutes") is not None and conditions["minutes"] <= 5 else {"light", "medium"} if conditions.get("minutes") is not None and conditions["minutes"] <= 15 else set()
    if "depth" in conditions:
        time_depths.clear()
    excluded_directions = set(conditions.get("exclude_directions", ()))
    condition_sources = dict(resolved["sources"])
    if "avoid_days" not in condition_sources:
        condition_sources["avoid_days"] = {"source": "default"}
    rows = book._visible_rows(db, channel, actor)
    roots = book._family_roots(rows)
    now_value = _iso(now)
    topic = book._resolve_topic(db, conditions.get("topic")) if conditions.get("topic") else None
    if topic is not None and not topic["active"]:
        mcp_workflows.resolve_error("no_candidate", "指定的小主题目前不可用。", field="topic")
    if topic and conditions.get("direction") and topic["direction"] != conditions["direction"]:
        mcp_workflows.resolve_error("needs_clarification", "方向与主题冲突。", conflict_fields=["direction", "topic"])
    checkpoint_prefix = mcp_workflows._checkpoint_scope(actor, channel, "same_host")
    checkpoint_sql = "SELECT * FROM mcp_thought_checkpoints"
    checkpoint_values = []
    if checkpoint_prefix is not None:
        checkpoint_sql += " WHERE substr(source_conversation_id,1,?)=?"
        checkpoint_values.extend((len(checkpoint_prefix), checkpoint_prefix))
    checkpoint_rows = db.execute(checkpoint_sql, checkpoint_values).fetchall()
    checkpoints = {}
    for row in checkpoint_rows:
        checkpoints.setdefault(row["prompt_id"], []).append(row)
    recent_ids = set()
    if avoid_days:
        recent_sql = "SELECT DISTINCT prompt_id FROM mcp_conversation_entries WHERE action IN ('pick','due','unfinished_pick') AND created_at >= ?"
        recent_values = [_iso(now - timedelta(days=avoid_days))]
        if checkpoint_prefix is not None:
            recent_sql += " AND substr(conversation_id,1,?)=?"
            recent_values.extend((len(checkpoint_prefix), checkpoint_prefix))
        recent_ids = {row[0] for row in db.execute(recent_sql, recent_values)}
    activity_sql = "SELECT prompt_id, MAX(created_at) FROM mcp_conversation_entries WHERE action != 'bind'"
    activity_values = []
    if checkpoint_prefix is not None:
        activity_sql += " AND substr(conversation_id,1,?)=?"
        activity_values.extend((len(checkpoint_prefix), checkpoint_prefix))
    activity_sql += " GROUP BY prompt_id"
    activity = {prompt_id: created_at for prompt_id, created_at in db.execute(activity_sql, activity_values)}
    guide_sql = "SELECT prompt_id, MAX(created_at) FROM mcp25_guide_history"
    guide_values = []
    if checkpoint_prefix is not None:
        guide_sql += " WHERE substr(conversation_id,1,?)=?"
        guide_values.extend((len(checkpoint_prefix), checkpoint_prefix))
    guide_sql += " GROUP BY prompt_id"
    for prompt_id, created_at in db.execute(guide_sql, guide_values):
        activity[prompt_id] = max(activity.get(prompt_id, ""), created_at)
    candidates = []
    future_snoozed = 0
    new_prompts = 0
    excluded_roots = {roots[identifier] for identifier in excluded_families if identifier in roots}
    for row in rows:
        root = roots[row["id"]]
        snoozed = row["snoozed_until"]
        if row["state"] == "written":
            continue
        if root in excluded_roots:
            continue
        if conditions.get("direction") and row["direction"] != conditions["direction"]:
            continue
        if conditions.get("topic") and row["topic_id"] != topic["id"]:
            continue
        if row["direction"] in excluded_directions:
            continue
        if time_depths and row["depth"] not in time_depths:
            continue
        if conditions.get("depth") and row["depth"] != conditions["depth"]:
            continue
        if row["state"] in (None, "") and not snoozed:
            new_prompts += 1
        if snoozed and snoozed > now_value:
            future_snoozed += 1
            continue
        is_due = bool(snoozed and snoozed <= now_value)
        has_checkpoint = bool(checkpoints.get(row["id"]))
        if row["state"] not in {"want_to_write", "in_progress"} and not is_due and not has_checkpoint:
            continue
        latest_checkpoint = max(checkpoints.get(row["id"], ()), key=lambda item: (item["created_at"], item["id"]), default=None)
        rank = 0 if row["state"] == "in_progress" else 1 if row["state"] == "want_to_write" else 2 if is_due else 3
        last_activity = max(row["updated_at"], activity.get(row["id"], ""),
                            latest_checkpoint["created_at"] if latest_checkpoint else "")
        candidates.append({"row": row, "rank": rank, "is_due": is_due, "checkpoint": latest_checkpoint,
                           "last_activity": last_activity, "root": root})
    eligible_before_recent = len(candidates)
    recently_excluded = [item for item in candidates if item["row"]["id"] in recent_ids]
    candidates = [item for item in candidates if item["row"]["id"] not in recent_ids]
    candidates.sort(key=lambda item: item["row"]["id"])
    candidates.sort(key=lambda item: item["last_activity"], reverse=True)
    candidates.sort(key=lambda item: (item["rank"], 0 if item["checkpoint"] else 1))
    if params.get("_list_only"):
        limit, offset = params.get("limit", 20), params.get("offset", 0)
        page = candidates[offset:offset + limit]
        items = [
            {"prompt": _prompt_view(book, item["row"], channel, actor, now), "position": offset + index + 1,
             "priority_rank": item["rank"] + 1, "state": item["row"]["state"],
             "last_activity": item["last_activity"],
             "checkpoint_id": item["checkpoint"]["id"] if item["checkpoint"] else None,
             "continuation_point": mcp_workflows._checkpoint_view(item["checkpoint"]) if item["checkpoint"] else None,
             "rank_reason": {"status_priority": _UNFINISHED_PRIORITY[item["rank"]],
                             "saved_checkpoint_first": bool(item["checkpoint"]),
                             "latest_activity": item["last_activity"], "prompt_id_tiebreak": item["row"]["id"]}}
            for index, item in enumerate(page)
        ]
        next_offset = offset + len(items) if offset + len(items) < len(candidates) else None
        return {"items": items, "total": len(candidates), "offset": offset,
                "next_offset": next_offset, "complete": next_offset is None,
                "snapshot_at": now_value,
                "effective_conditions": {**conditions, "avoid_days": avoid_days,
                                         "sources": condition_sources, "conflicts": resolved["conflicts"]},
                "eligible_before_recent_count": eligible_before_recent,
                "recently_excluded_count": len(recently_excluded),
                "new_prompt_count": new_prompts, "future_snoozed_count": future_snoozed,
                "rank_basis": ["in_progress", "want_to_write", "due_snooze", "checkpoint_only",
                               "saved_checkpoint_first", "latest_activity", "prompt_id_tiebreak"],
                "explanation": "已写完题目不在续接池；新题和未到期暂缓题不是当前续接候选。"}
    if not candidates:
        details = {
            "candidate_count": 0, "eligible_before_recent_count": eligible_before_recent,
            "recently_excluded_count": len(recently_excluded),
            "new_prompt_count": new_prompts, "future_snoozed_count": future_snoozed,
            "effective_conditions": {**conditions, "avoid_days": avoid_days,
                                     "sources": condition_sources, "conflicts": resolved["conflicts"]},
            "explanation": "已写完题目不在续接池；新题和未到期暂缓题不是当前续接候选。",
        }
        if recently_excluded:
            details["next_call"] = mcp_workflows.next_call(
                "pick_unfinished_prompt",
                {"conversation_id": params["conversation_id"], "avoid_days": 0},
                condition="用户确认忽略近期重复限制后，补新的 request_id",
            )
        mcp_workflows.resolve_error(
            "no_candidate", "当前范围没有符合条件的未完成题。", **details,
        )
    picked = candidates[0]
    chosen = picked["row"]
    prompt = _prompt_view(book, chosen, channel, actor, now)
    alternatives = []
    alternative_rows = candidates[1:limit + 1]
    for position, item in enumerate(alternative_rows, start=2):
        view = _prompt_view(book, item["row"], channel, actor, now)
        alternatives.append({"prompt": view, "position": position, "priority_rank": item["rank"] + 1,
                             "state": item["row"]["state"],
                             "last_activity": item["last_activity"],
                             "checkpoint_id": item["checkpoint"]["id"] if item["checkpoint"] else None,
                             "continuation_point": mcp_workflows._checkpoint_view(item["checkpoint"]) if item["checkpoint"] else None,
                             "rank_reason": {"status_priority": _UNFINISHED_PRIORITY[item["rank"]],
                                             "saved_checkpoint_first": bool(item["checkpoint"]),
                                             "latest_activity": item["last_activity"], "prompt_id_tiebreak": item["row"]["id"]}})
    decision = {
        "method": "unfinished_activity_priority", "candidate_count": len(candidates),
        "eligible_before_recent_count": eligible_before_recent,
        "recently_excluded_count": len(recently_excluded), "avoid_days": avoid_days,
        "effective_conditions": {**conditions, "avoid_days": avoid_days,
                                 "sources": condition_sources, "conflicts": resolved["conflicts"]},
        "rank_basis": ["in_progress", "want_to_write", "due_snooze", "checkpoint_only", "saved_checkpoint_first", "latest_activity"],
        "selected_rank": 1, "priority_rank": picked["rank"] + 1, "last_activity": picked["last_activity"],
        "checkpoint_id": picked["checkpoint"]["id"] if picked["checkpoint"] else None,
        "continuation_point": mcp_workflows._checkpoint_view(picked["checkpoint"]) if picked["checkpoint"] else None,
        "resume_card_ids": [row[0] for row in db.execute(
            "SELECT id FROM materials WHERE collection='resume' AND prompt_id=? ORDER BY updated_at DESC LIMIT 3", (chosen["id"],)
        )],
    }
    decision_id = mcp_workflows._decision(db, params, chosen["id"], "unfinished_pick", decision, now)
    alternative_count = max(0, len(candidates) - 1)
    return mcp_workflows.receipt(params, prompt=prompt, decision_id=decision_id, decision=decision,
                                 alternatives=alternatives, alternative_count=alternative_count,
                                 has_more=len(alternatives) < alternative_count)


def pick_unfinished_prompt(book, db, params, channel, validate, now, actor):
    return _unfinished_dispatch(book, db, params, channel, validate, now, actor)


def list_unfinished_prompts(book, db, params, channel, validate, now, actor):
    return _unfinished_dispatch(book, db, {**params, "_list_only": True}, channel, validate, now, actor)


def _question_preference_values(book, db, direction, now):
    rows = db.execute("SELECT * FROM mcp25_question_preferences WHERE direction IN ('*', ?)", (direction or "*",)).fetchall()
    by_key = {(row["direction"], row["mode"], row["scope"]): row for row in rows}
    values = []
    active_override = False
    for mode in _QUESTION_MODES:
        choices = []
        for scope in ("recent", "baseline"):
            specific = by_key.get((direction or "*", mode, scope)) if direction else None
            global_row = by_key.get(("*", mode, scope))
            row = specific
            if row and scope == "recent" and row["expires_at"] and _parse_time(row["expires_at"]) <= now.astimezone(timezone.utc):
                row = None
            if row is None:
                row = global_row
                if row and scope == "recent" and row["expires_at"] and _parse_time(row["expires_at"]) <= now.astimezone(timezone.utc):
                    row = None
            if row:
                choices.append((row, "direction" if specific is row else "default"))
        chosen = choices[0][0] if choices else None
        source = choices[0][1] if choices else "default"
        if chosen:
            active_override = True
            values.append({"mode": mode, "strength": chosen["strength"], "scope": chosen["scope"],
                           "direction": None if chosen["direction"] == "*" else chosen["direction"],
                           "source": source, "expires_at": chosen["expires_at"], "reason": chosen["reason"]})
        else:
            values.append({"mode": mode, "strength": 1.0, "scope": "default", "direction": None,
                           "source": "default", "expires_at": None, "reason": None})
    return values, active_override


def select_question_mode(book, db, direction, requested_mode, step, now):
    values, has_override = _question_preference_values(book, db, direction, now)
    if requested_mode is not None:
        return requested_mode, {"source": "call", "effective_preferences": values}
    if not has_override:
        default = ("experience", "evidence", "narrow")[min(step, 3) - 1]
        return default, {"source": "legacy_step", "effective_preferences": values}
    available = [item for item in values if item["strength"] > 0]
    if not available:
        mcp_workflows.resolve_error("needs_clarification", "当前问法偏好将所有方式都设为零。",
                                    question="这轮希望用哪种追问方式？", question_modes=list(_QUESTION_MODES))
    selected = random.choices(available, weights=[item["strength"] for item in available], k=1)[0]
    return selected["mode"], {"source": "preference", "effective_preferences": values}


def question_for_mode(mode, step, focus):
    subject = f"围绕「{focus}」，" if focus else ""
    if mode == "experience":
        return "最近一次让你想到这道题的具体情境是什么？" if step == 1 else f"{subject}当时最具体的一个细节是什么？"
    if mode == "contrast":
        return f"{subject}与一个相似或相反的情境相比，最明显的不同是什么？"
    if mode == "counterexample":
        return f"{subject}什么事实会让你改变刚才的判断？"
    if mode == "boundary":
        return f"{subject}在什么条件下，这个判断可能不再成立？"
    if mode == "evidence":
        return f"{subject}你能指出哪一项具体事实来支持或质疑它？"
    return f"{subject}如果只保留一个尚未回答的小问题，你最想弄清什么？"


def _history_prefix(actor, channel, history_scope):
    return mcp_workflows._checkpoint_scope(actor, channel, history_scope)


def guide_history_context(db, prompt_id, channel, actor, history_scope="same_host", limit=3):
    prefix = _history_prefix(actor, channel, history_scope)
    clause, values = ["prompt_id=?"], [prompt_id]
    if prefix is not None:
        clause.append("substr(conversation_id,1,?)=?")
        values.extend((len(prefix), prefix))
    rows = db.execute(
        "SELECT question_mode,focus,question,step,created_at FROM mcp25_guide_history WHERE " + " AND ".join(clause) +
        " ORDER BY created_at DESC,id DESC LIMIT ?", (*values, limit),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def record_guide_step(book, db, params, channel, actor, prompt, step, question_mode, question, now):
    focus = params.get("focus", "").strip()
    privacy_guard.check_material_write(
        db, content={"prompt": prompt["text"], "focus": focus, "question": question},
        direction=prompt["direction"], topic_id=prompt["topic_id"], prompt_id=prompt["id"],
    )
    db.execute(
        "INSERT INTO mcp25_guide_history VALUES (?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, params["conversation_id"], prompt["id"], step, question_mode, focus, question, _iso(now)),
    )


def get_guide_history(book, db, params, channel, validate, now, actor):
    if not (params.get("conversation_id") or params.get("prompt_id")):
        mcp_workflows.resolve_error("invalid_params", "请指定 conversation_id 或 prompt_id。", fields=["conversation_id", "prompt_id"])
    if params.get("prompt_id"):
        _prompt_for_actor(book, db, params["prompt_id"], channel, actor)
    prefix = _history_prefix(actor, channel, params.get("history_scope", "same_host"))
    where, values = [], []
    if prefix is not None:
        where.append("substr(conversation_id,1,?)=?")
        values.extend((len(prefix), prefix))
    if "conversation_id" in params:
        where.append("conversation_id=?")
        values.append(params["conversation_id"])
    if "prompt_id" in params:
        where.append("prompt_id=?")
        values.append(params["prompt_id"])
    clause = " WHERE " + " AND ".join(where) if where else ""
    total = db.execute("SELECT count(*) FROM mcp25_guide_history" + clause, values).fetchone()[0]
    limit, offset = params.get("limit", 20), params.get("offset", 0)
    rows = db.execute("SELECT * FROM mcp25_guide_history" + clause + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                      (*values, limit, offset)).fetchall()
    return {"items": [dict(row) for row in rows], "total": total, "offset": offset,
            "has_more": offset + len(rows) < total}


def set_question_preference(book, db, params, channel, validate, now, actor):
    direction = params.get("direction", "*")
    if direction != "*" and db.execute("SELECT 1 FROM directions WHERE id=?", (direction,)).fetchone() is None:
        mcp_workflows.resolve_error("not_found", "未找到指定方向。", direction=direction)
    if params["scope"] == "baseline" and "expires_at" in params:
        mcp_workflows.resolve_error("invalid_params", "长期问法偏好不接受 expires_at。", fields=["expires_at"])
    if params["scope"] == "recent" and "expires_at" not in params:
        mcp_workflows.resolve_error("invalid_params", "近期问法偏好必须设置 expires_at。", fields=["expires_at"])
    expiry = _iso(_parse_time(params["expires_at"])) if "expires_at" in params else None
    if expiry is not None and _parse_time(expiry) <= now.astimezone(timezone.utc):
        mcp_workflows.resolve_error("invalid_params", "expires_at 必须晚于当前时间。", fields=["expires_at"])
    privacy_guard.check_action(db, "set_question_preference", params)
    version = book._preference_version(db, params["expected_version"])
    previous = db.execute("SELECT * FROM mcp25_question_preferences WHERE direction=? AND mode=? AND scope=?",
                          (direction, params["mode"], params["scope"])).fetchone()
    before = dict(previous) if previous else None
    updated_at = _iso(now)
    db.execute(
        "INSERT INTO mcp25_question_preferences VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(direction,mode,scope) DO UPDATE SET strength=excluded.strength,expires_at=excluded.expires_at,reason=excluded.reason,updated_at=excluded.updated_at",
        (direction, params["mode"], params["scope"], params["strength"], expiry, params.get("reason"), updated_at),
    )
    new_version = book._bump_preference_version(db, version)
    after = {"direction": direction, "mode": params["mode"], "scope": params["scope"],
             "strength": params["strength"], "expires_at": expiry, "reason": params.get("reason")}
    db.execute("INSERT INTO mcp25_question_preference_history VALUES (?,?,?,?,?,?,?,?)",
               (uuid.uuid4().hex, direction, params["mode"], params["scope"],
                json.dumps(before, ensure_ascii=False) if before else None,
                json.dumps(after, ensure_ascii=False), new_version, updated_at))
    return mcp_workflows.receipt(params, version=new_version, before=before, after=after)


def get_question_preferences(book, db, params, channel, validate, now, actor):
    direction = params.get("direction")
    version = db.execute("SELECT version FROM preference_meta WHERE singleton=1").fetchone()[0]
    rows = db.execute("SELECT * FROM mcp25_question_preferences" + (" WHERE direction IN ('*', ?)" if direction else "") +
                      " ORDER BY direction,mode,scope", (direction,) if direction else ()).fetchall()
    effective, _has_override = _question_preference_values(book, db, direction, now)
    result = {"version": version, "direction": direction, "items": [dict(row) for row in rows], "effective": effective}
    if params.get("include_history"):
        history_rows = db.execute(
            "SELECT * FROM mcp25_question_preference_history" + (" WHERE direction IN ('*', ?)" if direction else "") +
            " ORDER BY created_at DESC,event_id DESC LIMIT ?", (*((direction,) if direction else ()), params.get("limit", 20)),
        ).fetchall()
        result["history"] = [dict(row) for row in history_rows]
    return result


def end_question_preference(book, db, params, channel, validate, now, actor):
    direction = params.get("direction", "*")
    version = book._preference_version(db, params["expected_version"])
    row = db.execute("SELECT * FROM mcp25_question_preferences WHERE direction=? AND mode=? AND scope='recent'",
                     (direction, params["mode"])).fetchone()
    if row is None:
        return mcp_workflows.receipt(params, version=version, ended=False, before=None, after=None)
    before = dict(row)
    db.execute("DELETE FROM mcp25_question_preferences WHERE direction=? AND mode=? AND scope='recent'",
               (direction, params["mode"]))
    new_version = book._bump_preference_version(db, version)
    after = {"direction": direction, "mode": params["mode"], "scope": "recent", "ended": True}
    db.execute("INSERT INTO mcp25_question_preference_history VALUES (?,?,?,?,?,?,?,?)",
               (uuid.uuid4().hex, direction, params["mode"], "recent", json.dumps(before, ensure_ascii=False),
                json.dumps(after, ensure_ascii=False), new_version, _iso(now)))
    return mcp_workflows.receipt(params, version=new_version, ended=True, before=before, after=after)


WRITE_ACTIONS = {
    "set_session_conditions", "end_session_conditions", "set_prompt_family_relation",
    "pick_unfinished_prompt", "set_question_preference", "end_question_preference",
}

HANDLERS = {
    "get_session_conditions": get_session_conditions,
    "set_session_conditions": set_session_conditions,
    "end_session_conditions": end_session_conditions,
    "get_prompt_family": get_prompt_family,
    "set_prompt_family_relation": set_prompt_family_relation,
    "pick_unfinished_prompt": pick_unfinished_prompt,
    "list_unfinished_prompts": list_unfinished_prompts,
    "get_guide_history": get_guide_history,
    "set_question_preference": set_question_preference,
    "get_question_preferences": get_question_preferences,
    "end_question_preference": end_question_preference,
}


def handle(book, db, action, params, channel, validate, now, actor):
    return HANDLERS[action](book, db, params, channel, validate, now, actor)
