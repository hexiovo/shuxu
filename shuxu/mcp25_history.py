"""Receipt-bound corrections for prompt state and snooze updates."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import mcp24_access, mcp_workflows, privacy_guard

ACTION = "correct_prompt_status"
TIMELINE_ACTION = "get_prompt_timeline"
REVIEW_ACTION = "review_prompt_events"
_SOURCE_ACTIONS = {
    "set_prompt_state", "snooze_prompt", "clear_snooze", "save_prompt",
    "apply_actions", ACTION,
}
_PROMPT_ACTIONS = {"set_prompt_state", "snooze_prompt", "clear_snooze", "save_prompt", ACTION}
_STATES = ("want_to_write", "in_progress", "written")
_EVENT_TYPES = ("created", "state_changed", "snoozed", "snooze_cleared", "corrected", "undone")
_PROMPT_STATE_ACTIONS = {"set_prompt_state", "snooze_prompt", "clear_snooze"}


def specs(tool, field, ident, direction, version, include_private):
    date_time = field("string", format="date-time")
    date_only = field("string", format="date")
    event_types = field("array", items=field("string", enum=list(_EVENT_TYPES)),
                        minItems=1, maxItems=len(_EVENT_TYPES))
    period = field("object", properties={
        "label": field("string", minLength=1, maxLength=40),
        "from_date": date_only, "through_date": date_only,
    }, required=["from_date", "through_date"], additionalProperties=False)
    return [
        tool(ACTION, "按原 request_id 或当前会话最近一项可核对的命题状态操作，原子更正状态和暂缓时间。", {
        "request_id": ident,
        "conversation_id": ident,
        "source_conversation_id": ident,
        "target_request_id": ident,
        "prompt_id": ident,
        "state": field("string", enum=list(_STATES)),
        "snoozed_until": field(["string", "null"], format="date-time", minLength=1),
        "reason": field("string", minLength=1, maxLength=240),
        }, ("request_id", "conversation_id")),
        tool(TIMELINE_ACTION, "读取一题或多题已记录的真实状态、暂缓、更正与撤销事件；返回覆盖起点、版本、来源和可查回引用。", {
            "prompt_ids": field("array", items=ident, minItems=1, maxItems=20),
            "from": date_time, "through": date_time,
            "event_types": event_types,
            "page_size": field("integer", minimum=1, maximum=50),
            "offset": field("integer", minimum=0),
        }, ("prompt_ids",)),
        tool(REVIEW_ACTION, "按实际事件时间回顾或比较一至两个本地日历区间，区分完成事件、重开事件和当前完成态。", {
            "periods": field("array", items=period, minItems=1, maxItems=2),
            "timezone": field("string", minLength=1, maxLength=64),
            "direction": direction, "topic": ident,
            "event_types": event_types,
            "page_size": field("integer", minimum=1, maximum=50),
            "offset": field("integer", minimum=0),
        }, ("periods", "timezone")),
    ]


def schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS mcp25_prompt_history_meta (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            coverage_start_at TEXT NOT NULL
        );
        INSERT OR IGNORE INTO mcp25_prompt_history_meta(singleton, coverage_start_at)
            VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
        CREATE TABLE IF NOT EXISTS mcp25_prompt_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            prompt_id TEXT NOT NULL,
            prompt_visibility TEXT NOT NULL,
            direction TEXT,
            topic_id TEXT,
            event_type TEXT NOT NULL,
            action TEXT NOT NULL,
            step_number INTEGER,
            request_id_internal TEXT,
            request_id_public TEXT,
            target_request_id_internal TEXT,
            target_request_id_public TEXT,
            conversation_id_internal TEXT,
            conversation_id_public TEXT,
            actor_host_id TEXT NOT NULL,
            actor_profile TEXT NOT NULL,
            authorization_revision INTEGER NOT NULL,
            channel TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            from_version INTEGER,
            to_version INTEGER,
            from_state TEXT,
            to_state TEXT,
            from_snoozed_until TEXT,
            to_snoozed_until TEXT,
            reason TEXT
        );
        CREATE INDEX IF NOT EXISTS mcp25_prompt_events_prompt_time
            ON mcp25_prompt_events(prompt_id, occurred_at, sequence);
        CREATE INDEX IF NOT EXISTS mcp25_prompt_events_scope_time
            ON mcp25_prompt_events(occurred_at, direction, topic_id, event_type);
        CREATE INDEX IF NOT EXISTS mcp25_prompt_events_request
            ON mcp25_prompt_events(request_id_internal, sequence);
    """)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(mcp25_prompt_events)")}
    if "reason" not in columns:
        db.execute("ALTER TABLE mcp25_prompt_events ADD COLUMN reason TEXT")


def _source_row(db, params, channel, actor=None):
    target_request_id = params.get("target_request_id")
    source_conversation_id = params.get("source_conversation_id")
    if target_request_id:
        row = db.execute(
            """SELECT u.request_id, u.conversation_id, u.channel, u.changes_json,
                      u.created_at AS undo_created_at, u.undone_at,
                      r.action, r.created_at AS receipt_created_at, r.response_json, r.channel AS receipt_channel
               FROM mcp_undo_log AS u JOIN request_receipts AS r ON r.request_id = u.request_id
               WHERE u.request_id = ? AND u.channel = ?""",
            (target_request_id, channel),
        ).fetchone()
        if row is None or row["receipt_channel"] not in (None, channel):
            mcp_workflows.resolve_error("not_found", "没有找到这条已提交且可更正的原回执。",
                                         target_request_id=_safe_request_id(db, target_request_id, actor))
        if source_conversation_id and row["conversation_id"] != source_conversation_id:
            mcp_workflows.resolve_error("not_found", "原回执不属于指定的来源会话。",
                                         target_request_id=_safe_request_id(db, target_request_id, actor),
                                         source_conversation_id=_safe_conversation_id(db, source_conversation_id, actor))
        return row

    source_conversation_id = source_conversation_id or params.get("conversation_id")
    row = db.execute(
        """SELECT u.request_id, u.conversation_id, u.channel, u.changes_json,
                  u.created_at AS undo_created_at, u.undone_at,
                  r.action, r.created_at AS receipt_created_at, r.response_json, r.channel AS receipt_channel
           FROM mcp_undo_log AS u JOIN request_receipts AS r ON r.request_id = u.request_id
           WHERE u.conversation_id = ? AND u.channel = ?
           ORDER BY u.rowid DESC LIMIT 1""",
        (source_conversation_id, channel),
    ).fetchone()
    if row is None or row["receipt_channel"] not in (None, channel):
        mcp_workflows.resolve_error(
            "needs_clarification", "本会话没有可核对的最近写入回执。",
            question="请提供要更正的 target_request_id，或确认来源会话。",
        )
    return row


def _receipt_data(row):
    return json.loads(row["response_json"])


def _is_committed(response):
    data = response.get("data") or {}
    return (
        response.get("status") == "ok"
        or (response.get("status") == "receipt_payload_pruned"
            and data.get("committed") is True)
    )


def _prompt_changes(row, response):
    if row["action"] not in _SOURCE_ACTIONS or not _is_committed(response):
        return []
    try:
        changes = json.loads(row["changes_json"])
    except (TypeError, json.JSONDecodeError):
        return []
    return [change for change in changes
            if change.get("table") == "prompts" and change.get("after_exists", True)]


def _receipt_matches_prompt(row, response, prompt_id):
    if response.get("status") == "receipt_payload_pruned" or response.get("receipt_payload_pruned") is True:
        return None
    data = response.get("data") or {}
    if row["action"] == "apply_actions":
        steps = data.get("steps") or []
        matches = [step.get("data") or {} for step in steps
                   if step.get("tool") in _PROMPT_ACTIONS
                   and (step.get("data") or {}).get("id") == prompt_id]
        if len(matches) == 1:
            return matches[0]
        return None
    if row["action"] in _PROMPT_ACTIONS:
        identifier = data.get("prompt_id") or data.get("id")
        return data if identifier == prompt_id else None
    return None


def _visible_prompt(book, db, prompt_id, channel, actor):
    row = db.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
    if row is None or (channel == "mcp" and row["visibility"] == "private"
                       and not mcp24_access.has_full_access(actor)):
        return None
    return book._find_prompt_for_actor(db, prompt_id, channel, actor, {})


def _candidate(book, db, change, channel, actor):
    prompt_id = change["id"]
    row = _visible_prompt(book, db, prompt_id, channel, actor)
    if row is None:
        return None
    return {
        "prompt_id": prompt_id,
        "text": row["text"][:160],
        "state": row["state"],
        "snoozed_until": row["snoozed_until"],
        "version": row["version"],
    }


def _resolve_prompt(book, db, source, response, params, channel, actor):
    changes = _prompt_changes(source, response)
    target_request_id = _safe_request_id(db, source["request_id"], actor)
    requested_id = params.get("prompt_id")
    if requested_id:
        matches = [change for change in changes if change.get("id") == requested_id]
        if len(matches) != 1:
            mcp_workflows.resolve_error("not_found", "原回执中没有这道题的状态更改。",
                                         prompt_id=requested_id,
                                         target_request_id=target_request_id)
        selected = matches[0]
    elif len(changes) == 1:
        selected = changes[0]
    elif changes:
        candidates = [item for item in (_candidate(book, db, change, channel, actor) for change in changes) if item]
        mcp_workflows.resolve_error(
            "needs_clarification", "原回执更改了多道题，无法唯一确定目标。",
            question="你要更正哪一道题？请提供 prompt_id。", candidates=candidates,
            target_request_id=target_request_id,
        )
    else:
        mcp_workflows.resolve_error(
            "needs_clarification", "这条回执没有可更正的命题状态记录。",
            question="请提供一条写作状态或暂缓操作的 target_request_id。",
            target_request_id=target_request_id, action=source["action"],
        )

    prompt_id = selected["id"]
    receipt_data = _receipt_matches_prompt(source, response, prompt_id)
    after_version = selected.get("after_version")
    if (receipt_data is not None and receipt_data.get("version") is not None
            and after_version is not None and receipt_data["version"] != after_version):
        mcp_workflows.resolve_error(
            "version_conflict", "原回执版本与撤销快照不一致，不能安全更正。",
            prompt_id=prompt_id, target_request_id=target_request_id,
            receipt_version=receipt_data["version"], expected_version=after_version,
        )

    current = _visible_prompt(book, db, prompt_id, channel, actor)
    if current is None:
        raw = db.execute("SELECT visibility FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
        if (raw is not None and channel == "mcp" and raw["visibility"] == "private"
                and not mcp24_access.has_full_access(actor)):
            mcp_workflows.resolve_error("not_found", "未找到可访问的命题。", prompt_id=prompt_id)
    if current is None or current["version"] != after_version or source["undone_at"] is not None:
        current_values = None if current is None else {
            "version": current["version"], "state": current["state"],
            "snoozed_until": current["snoozed_until"],
        }
        mcp_workflows.resolve_error(
            "version_conflict", "目标回执之后这道题已经发生变化，未覆盖当前状态。",
            prompt_id=prompt_id, target_request_id=target_request_id,
            expected_version=after_version,
            actual_version=current["version"] if current is not None else None,
            current=current_values,
            next_call=mcp_workflows.next_call("get_prompt", {"prompt_id": prompt_id}),
        )
    before = {
        "version": current["version"], "state": current["state"],
        "snoozed_until": current["snoozed_until"],
    }
    return prompt_id, before, current


def _public_reference(db, request_id, actor):
    if not request_id:
        return None
    from .book import _named_actor

    if not _named_actor(actor):
        return request_id
    row = db.execute(
        "SELECT request_id_public FROM mcp25_prompt_events "
        "WHERE request_id_internal = ? AND request_id_public IS NOT NULL "
        "AND actor_host_id = ? AND actor_profile = ? AND authorization_revision = ? "
        "ORDER BY sequence DESC LIMIT 1",
        (request_id, actor["host_id"], actor["profile"], actor["authorization_revision"]),
    ).fetchone()
    if row is not None:
        return row["request_id_public"]
    row = db.execute(
        "SELECT target_request_id_public FROM mcp25_prompt_events "
        "WHERE target_request_id_internal = ? AND target_request_id_public IS NOT NULL "
        "AND actor_host_id = ? AND actor_profile = ? AND authorization_revision = ? "
        "ORDER BY sequence DESC LIMIT 1",
        (request_id, actor["host_id"], actor["profile"], actor["authorization_revision"]),
    ).fetchone()
    return row["target_request_id_public"] if row else None


def _public_conversation_reference(db, conversation_id, actor, public_params):
    if not conversation_id:
        return None
    from .book import _named_actor, _namespaced

    if not _named_actor(actor):
        return conversation_id
    for key in ("source_conversation_id", "conversation_id"):
        value = public_params.get(key)
        if value and _namespaced(actor, "conversation", value) == conversation_id:
            return value
    row = db.execute(
        "SELECT conversation_id_public FROM mcp25_prompt_events "
        "WHERE conversation_id_internal = ? AND conversation_id_public IS NOT NULL "
        "AND actor_host_id = ? AND actor_profile = ? AND authorization_revision = ? "
        "ORDER BY sequence DESC LIMIT 1",
        (conversation_id, actor["host_id"], actor["profile"], actor["authorization_revision"]),
    ).fetchone()
    return row["conversation_id_public"] if row else None


def _sanitize_original_data(db, value, source, actor, public_params):
    public_request_id = _public_reference(db, source["request_id"], actor)
    public_conversation_id = _public_conversation_reference(
        db, source["conversation_id"], actor, public_params,
    )
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            if key in {"request_id", "target_request_id"} and isinstance(item, str):
                output[key] = _public_reference(db, item, actor)
            elif key in {"conversation_id", "source_conversation_id"} and item == source["conversation_id"]:
                output[key] = public_conversation_id
            else:
                output[key] = _sanitize_original_data(db, item, source, actor, public_params)
        return output
    if isinstance(value, list):
        return [_sanitize_original_data(db, item, source, actor, public_params) for item in value]
    return value


def _original_receipt(db, source, response, prompt_id, actor, public_params):
    public_request_id = _public_reference(db, source["request_id"], actor)
    common = {
        "request_id": public_request_id,
        "action": source["action"],
        "created_at": source["receipt_created_at"],
        "committed": True,
    }
    if response.get("status") == "receipt_payload_pruned" or response.get("receipt_payload_pruned") is True:
        return {**common, "payload_pruned": True}
    data = response.get("data") or {}
    if source["action"] == "apply_actions":
        steps = []
        for step in data.get("steps") or []:
            step_data = step.get("data") or {}
            if step.get("tool") in _PROMPT_ACTIONS and step_data.get("id") == prompt_id:
                steps.append({
                    "step": step["step"], "tool": step["tool"],
                    "data": {key: value for key, value in step_data.items() if key != "request_id"},
                })
        return {**common, "data": _sanitize_original_data(db, {"steps": steps}, source, actor, public_params)}
    return {**common, "data": _sanitize_original_data(db, data, source, actor, public_params)}


def _correct_prompt_status(book, db, params, channel, validate, now, actor):
    if "state" not in params and "snoozed_until" not in params:
        mcp_workflows.resolve_error("invalid_params", "请提供要更正的状态或暂缓时间。",
                                     fields=["state", "snoozed_until"])
    source = _source_row(db, params, channel, actor)
    response = _receipt_data(source)
    prompt_id, before, current = _resolve_prompt(book, db, source, response, params, channel, actor)
    privacy_guard.check_action(db, ACTION, {**params, "prompt_id": prompt_id})

    from .book import _namespaced

    internal_request_id = _namespaced(actor, "request", params["request_id"])
    target_request_id_public = params.get("target_request_id") or _public_reference(
        db, source["request_id"], actor,
    )
    if not target_request_id_public:
        from .book import _named_actor
        if not _named_actor(actor):
            target_request_id_public = source["request_id"]
    original_after_version = current["version"]
    correction_snapshot = {"table": "prompts", "before": dict(current)}
    version = original_after_version
    position = 0
    if "state" in params:
        position += 1
        arguments = {
            "request_id": mcp_workflows._child_request(internal_request_id, position),
            "prompt_id": prompt_id, "expected_version": version, "state": params["state"],
        }
        validate("set_prompt_state", arguments, channel)
        result = book._run(db, "set_prompt_state", arguments, channel, actor, now)["data"]
        version = result["version"]

    if "snoozed_until" in params:
        position += 1
        action = "clear_snooze" if params["snoozed_until"] is None else "snooze_prompt"
        arguments = {
            "request_id": mcp_workflows._child_request(internal_request_id, position),
            "prompt_id": prompt_id, "expected_version": version,
        }
        if action == "snooze_prompt":
            arguments["snoozed_until"] = params["snoozed_until"]
        validate(action, arguments, channel)
        result = book._run(db, action, arguments, channel, actor, now)["data"]
        version = result["version"]

    after_row = _visible_prompt(book, db, prompt_id, channel, actor)
    after = {
        "version": after_row["version"], "state": after_row["state"],
        "snoozed_until": after_row["snoozed_until"],
    }
    changed = (before["state"], before["snoozed_until"]) != (after["state"], after["snoozed_until"])
    undo_available = False
    if changed:
        undo_change = mcp_workflows.capture_after(db, correction_snapshot, {"id": prompt_id})
        undo_available = mcp_workflows.log_undo(db, {
            "request_id": internal_request_id,
            "conversation_id": params.get("conversation_id"),
        }, [undo_change], channel, now)

    internal_params = {**params, "request_id": internal_request_id}
    return mcp_workflows.receipt(
        internal_params,
        target_request_id=target_request_id_public,
        original_receipt=_original_receipt(db, source, response, prompt_id, actor, params),
        prompt_id=prompt_id,
        before=before,
        after=after,
        correction_receipt={
            "request_id": internal_request_id, "action": ACTION,
            "state": "committed", "committed": True,
        },
        undo_available=undo_available,
        next_call=mcp_workflows.next_call("get_prompt", {"prompt_id": prompt_id}),
    )


def _captured_prompt(db, prompt_id, channel, actor):
    row = db.execute(
        "SELECT id, visibility, direction, topic_id, version, state, snoozed_until FROM prompts WHERE id = ?",
        (prompt_id,),
    ).fetchone()
    if row is None or (channel == "mcp" and row["visibility"] == "private"
                       and not mcp24_access.has_full_access(actor)):
        return None
    return dict(row)


def _undo_source(db, params, channel):
    target_request_id = params.get("target_request_id")
    conversation_id = params.get("conversation_id")
    if target_request_id:
        row = db.execute(
            "SELECT * FROM mcp_undo_log WHERE request_id = ? AND channel = ?",
            (target_request_id, channel),
        ).fetchone()
        if row is not None and conversation_id and row["conversation_id"] not in (None, conversation_id):
            return None
        return row
    if not conversation_id:
        return None
    return db.execute(
        "SELECT * FROM mcp_undo_log WHERE conversation_id = ? AND channel = ? AND undone_at IS NULL "
        "ORDER BY rowid DESC LIMIT 1",
        (conversation_id, channel),
    ).fetchone()


def capture_dispatch(db, action, execution_params, public_params, channel, actor):
    if action in _PROMPT_STATE_ACTIONS:
        return {"kind": "prompt", "prompt": _captured_prompt(
            db, execution_params["prompt_id"], channel, actor)}
    if action == "save_prompt":
        return {"kind": "create_prompt"}
    if action == "save_prompt_material_relations" and "prompt" in execution_params:
        return {"kind": "create_prompt"}
    if action == "apply_actions":
        return {"kind": "batch"}
    if action == "undo_action":
        source = _undo_source(db, execution_params, channel)
        if source is None:
            return {"kind": "undo", "source_request_id_internal": None, "prompts": []}
        prompts = []
        for change in json.loads(source["changes_json"]):
            if change["table"] == "prompts":
                current = _captured_prompt(db, change["id"], channel, actor)
                prompts.append({"prompt_id": change["id"], "prompt": current})
        return {"kind": "undo", "source_request_id_internal": source["request_id"],
                "prompts": prompts}
    if action == ACTION:
        if "state" not in execution_params and "snoozed_until" not in execution_params:
            return {"kind": "correction", "source_request_id_internal": None}
        source = _source_row(db, execution_params, channel, actor)
        return {"kind": "correction", "source_request_id_internal": source["request_id"]}
    return None


def _internal_request_id(execution_params, public_params, actor):
    from .book import _namespaced

    request_id = public_params.get("request_id")
    return _namespaced(actor, "request", request_id) if request_id else execution_params.get("request_id")


def _public_request_id(db, request_id_internal, actor):
    if not request_id_internal:
        return None
    row = db.execute(
        "SELECT request_id_public, actor_host_id, actor_profile, authorization_revision "
        "FROM mcp25_prompt_events WHERE request_id_internal = ? AND request_id_public IS NOT NULL "
        "ORDER BY sequence DESC LIMIT 1",
        (request_id_internal,),
    ).fetchone()
    if (row is None or row["actor_host_id"] != actor["host_id"]
            or row["actor_profile"] != actor["profile"]
            or row["authorization_revision"] != actor["authorization_revision"]):
        return None
    return row["request_id_public"]


def _safe_request_id(db, request_id_internal, actor):
    if not request_id_internal:
        return None
    from .book import _named_actor

    return _public_reference(db, request_id_internal, actor) if _named_actor(actor) else request_id_internal


def _safe_conversation_id(db, conversation_id_internal, actor):
    if not conversation_id_internal:
        return None
    from .book import _named_actor

    return (_public_conversation_reference(db, conversation_id_internal, actor, {})
            if _named_actor(actor) else conversation_id_internal)


def _event_type(action, before, after):
    if action == "save_prompt":
        return "created"
    if action == "set_prompt_state":
        return "state_changed"
    if action == "snooze_prompt":
        return "snoozed"
    if action == "clear_snooze":
        return "snooze_cleared"
    if action == ACTION:
        return "corrected"
    if action == "undo_action":
        return "undone"
    if before is None and after is not None:
        return "created"
    return "state_changed"


def _insert_event(db, *, prompt_id, before, after, action, event_type, step_number,
                  execution_params, public_params, channel, actor, occurred_at,
                  target_request_id_internal=None, target_request_id_public=None, reason=None):
    if before is None and after is None:
        return
    if before is not None and after is not None and (
        before.get("state"), before.get("snoozed_until")
    ) == (
        after.get("state"), after.get("snoozed_until")
    ):
        return
    snapshot = after or before
    request_id_public = public_params.get("request_id")
    request_id_internal = _internal_request_id(execution_params, public_params, actor)
    conversation_id_internal = execution_params.get("conversation_id")
    conversation_id_public = public_params.get("conversation_id")
    db.execute(
        """INSERT INTO mcp25_prompt_events
           (event_id, prompt_id, prompt_visibility, direction, topic_id, event_type, action, step_number,
            request_id_internal, request_id_public, target_request_id_internal, target_request_id_public,
            conversation_id_internal, conversation_id_public, actor_host_id, actor_profile,
            authorization_revision, channel, occurred_at, from_version, to_version, from_state, to_state,
            from_snoozed_until, to_snoozed_until, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uuid.uuid4().hex, prompt_id, snapshot["visibility"], snapshot["direction"], snapshot["topic_id"],
         event_type, action, step_number, request_id_internal, request_id_public,
         target_request_id_internal, target_request_id_public, conversation_id_internal,
         conversation_id_public, actor["host_id"], actor["profile"], actor["authorization_revision"],
         channel, occurred_at, before.get("version") if before else None,
         after.get("version") if after else None, before.get("state") if before else None,
         after.get("state") if after else None, before.get("snoozed_until") if before else None,
         after.get("snoozed_until") if after else None, reason),
    )


def _event_row(db, prompt_id):
    row = db.execute(
        "SELECT id, visibility, direction, topic_id, version, state, snoozed_until FROM prompts WHERE id = ?",
        (prompt_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _batch_events(db, result_data, execution_params, public_params, channel, actor, occurred_at):
    undo = db.execute(
        "SELECT changes_json FROM mcp_undo_log WHERE request_id = ? AND channel = ?",
        (execution_params.get("request_id"), channel),
    ).fetchone()
    if undo is None:
        return
    changes = {change["id"]: change for change in json.loads(undo["changes_json"])
               if change["table"] == "prompts"}
    for step in result_data.get("steps", []):
        action = step["tool"]
        if action not in (*_PROMPT_STATE_ACTIONS, "save_prompt"):
            continue
        step_data = step.get("data") or {}
        prompt_id = step_data.get("id")
        change = changes.get(prompt_id)
        if change is None:
            continue
        before = change.get("before")
        after = _event_row(db, prompt_id)
        _insert_event(
            db, prompt_id=prompt_id, before=before, after=after, action=action,
            event_type=_event_type(action, before, after), step_number=step.get("step"),
            execution_params=execution_params, public_params=public_params, channel=channel,
            actor=actor, occurred_at=occurred_at,
            reason=(execution_params.get("actions", [])[step["step"] - 1]["arguments"].get("reason")),
        )


def record_dispatch(db, action, execution_params, public_params, result_data, channel, actor,
                    occurred_at, *, before):
    if action == "save_prompt" or (
        action == "save_prompt_material_relations" and result_data.get("prompt_created")
    ):
        prompt_id = result_data.get("id") if action == "save_prompt" else result_data.get("prompt_id")
        after = _event_row(db, prompt_id) if prompt_id else None
        _insert_event(db, prompt_id=prompt_id, before=None, after=after, action=action,
                      event_type="created", step_number=None, execution_params=execution_params,
                      public_params=public_params, channel=channel, actor=actor, occurred_at=occurred_at,
                      reason=execution_params.get("reason"))
        return
    if action in _PROMPT_STATE_ACTIONS:
        old = (before or {}).get("prompt")
        prompt_id = result_data.get("id")
        after = _event_row(db, prompt_id) if prompt_id else None
        _insert_event(db, prompt_id=prompt_id, before=old, after=after, action=action,
                      event_type=_event_type(action, old, after), step_number=None,
                      execution_params=execution_params, public_params=public_params,
                      channel=channel, actor=actor, occurred_at=occurred_at,
                      reason=execution_params.get("reason"))
        return
    if action == "apply_actions":
        _batch_events(db, result_data, execution_params, public_params, channel, actor, occurred_at)
        return
    if action == ACTION:
        prompt_id = result_data["prompt_id"]
        old = {**_event_row(db, prompt_id), **result_data["before"]}
        after = _event_row(db, prompt_id)
        target_internal = (before or {}).get("source_request_id_internal") or execution_params.get("target_request_id")
        target_public = public_params.get("target_request_id")
        if target_internal and target_public is None:
            target_public = _public_request_id(db, target_internal, actor)
        if target_internal and target_public is None:
            from .book import _named_actor
            if not _named_actor(actor):
                target_public = target_internal
        _insert_event(db, prompt_id=prompt_id, before=old, after=after, action=action,
                      event_type="corrected", step_number=None, execution_params=execution_params,
                      public_params=public_params, channel=channel, actor=actor,
                      occurred_at=occurred_at, target_request_id_internal=target_internal,
                      target_request_id_public=target_public, reason=execution_params.get("reason"))
        return
    if action == "undo_action":
        undo_before = before or {}
        target_internal = execution_params.get("target_request_id") or undo_before.get("source_request_id_internal")
        target_public = public_params.get("target_request_id")
        if target_internal and target_public is None:
            target_public = _public_request_id(db, target_internal, actor)
        for item in undo_before.get("prompts", []):
            prompt_id = item["prompt_id"]
            old = item["prompt"]
            after = _event_row(db, prompt_id)
            _insert_event(db, prompt_id=prompt_id, before=old, after=after, action=action,
                          event_type="undone", step_number=None, execution_params=execution_params,
                          public_params=public_params, channel=channel, actor=actor,
                          occurred_at=occurred_at, target_request_id_internal=target_internal,
                          target_request_id_public=target_public, reason=execution_params.get("reason"))


def _parse_datetime(value, field_name):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        mcp_workflows.resolve_error("invalid_params", f"{field_name} 必须是带时区的 ISO 日期时间。", field=field_name)
    if parsed.tzinfo is None:
        mcp_workflows.resolve_error("invalid_params", f"{field_name} 必须是带时区的 ISO 日期时间。", field=field_name)
    return mcp_workflows.iso(parsed)


def _visible_event(row, channel, actor):
    return not (channel == "mcp" and row["prompt_visibility"] == "private"
                and not mcp24_access.has_full_access(actor))


def _event_view(db, row, actor):
    same_actor = (row["actor_host_id"] == actor["host_id"]
                  and row["actor_profile"] == actor["profile"]
                  and row["authorization_revision"] == actor["authorization_revision"])
    full_access = mcp24_access.has_full_access(actor)
    before = {"version": row["from_version"], "state": row["from_state"],
              "snoozed_until": row["from_snoozed_until"]}
    after = {"version": row["to_version"], "state": row["to_state"],
             "snoozed_until": row["to_snoozed_until"]}
    cards = []
    if same_actor and row["conversation_id_internal"]:
        versions = sorted({value for value in (row["from_version"], row["to_version"]) if value is not None})
        if versions:
            placeholders = ",".join("?" for _ in versions)
            cards = [dict(item) for item in db.execute(
                f"SELECT id, prompt_version, created_at FROM mcp_thought_checkpoints "
                f"WHERE prompt_id = ? AND source_conversation_id = ? "
                f"AND prompt_version IN ({placeholders}) AND created_at <= ? ORDER BY created_at, id",
                (row["prompt_id"], row["conversation_id_internal"], *versions, row["occurred_at"]),
            )]
    return {
        "event_id": row["event_id"], "prompt_id": row["prompt_id"],
        "event_type": row["event_type"], "action": row["action"],
        "step_number": row["step_number"], "occurred_at": row["occurred_at"],
        "before": before, "after": after,
        "source": {"channel": row["channel"],
                   "request_id": row["request_id_public"] if same_actor else None,
                   "target_request_id": row["target_request_id_public"] if same_actor else None,
                   "conversation_id": row["conversation_id_public"] if same_actor else None},
        "reason": row["reason"] if same_actor or full_access else None,
        "continuation_cards": cards,
        "next_call": mcp_workflows.next_call(TIMELINE_ACTION, {"prompt_ids": [row["prompt_id"]]}),
    }


def _coverage(db, prompt_id, coverage_start_at):
    prompt = db.execute("SELECT created_at FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
    created_at = prompt["created_at"] if prompt else None
    first_event = db.execute(
        "SELECT occurred_at, event_type FROM mcp25_prompt_events WHERE prompt_id = ? ORDER BY sequence LIMIT 1",
        (prompt_id,),
    ).fetchone()
    coverage_dt = datetime.fromisoformat(coverage_start_at.replace("Z", "+00:00"))
    created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00")) if created_at else None
    created_before_coverage = created_dt is not None and created_dt < coverage_dt
    if created_dt:
        available_from = max(coverage_dt, created_dt)
    elif first_event and first_event["event_type"] == "created":
        available_from = datetime.fromisoformat(first_event["occurred_at"].replace("Z", "+00:00"))
    else:
        available_from = coverage_dt
    return {
        "prompt_id": prompt_id,
        "available_from": mcp_workflows.iso(available_from),
        "first_recorded_event": first_event["occurred_at"] if first_event else None,
        "gap_before_available_from": created_before_coverage or first_event is None or
                                    (created_dt is None and first_event["event_type"] != "created"),
        "older_changes_reconstructible": False,
        "note": "状态事件仅从历史账本启用后记录；此前的状态变化没有可重建证据。",
    }


def _page_events(db, prompt_ids, *, start=None, end=None, event_types=None, page_size=20, offset=0,
                 channel, actor):
    clauses = ["prompt_id IN (" + ",".join("?" for _ in prompt_ids) + ")"]
    args = list(prompt_ids)
    if start:
        clauses.append("occurred_at >= ?")
        args.append(start)
    if end:
        clauses.append("occurred_at < ?")
        args.append(end)
    if event_types:
        clauses.append("event_type IN (" + ",".join("?" for _ in event_types) + ")")
        args.extend(event_types)
    clauses.append("(prompt_visibility != 'private' OR ? = 'local' OR ? = 1)")
    args.extend((channel, int(mcp24_access.has_full_access(actor))))
    where = " AND ".join(clauses)
    total = db.execute(f"SELECT count(*) FROM mcp25_prompt_events WHERE {where}", args).fetchone()[0]
    rows = db.execute(
        f"SELECT * FROM mcp25_prompt_events WHERE {where} ORDER BY occurred_at, sequence LIMIT ? OFFSET ?",
        (*args, page_size, offset),
    ).fetchall()
    end_offset = offset + page_size
    return [_event_view(db, row, actor) for row in rows], total, (end_offset if end_offset < total else None)


def _get_prompt_timeline(book, db, params, channel, validate, now, actor):
    prompt_ids = params["prompt_ids"]
    if len(prompt_ids) != len(set(prompt_ids)):
        mcp_workflows.resolve_error("invalid_params", "prompt_ids 不能重复。", field="prompt_ids")
    for prompt_id in prompt_ids:
        current = db.execute("SELECT id FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
        if current:
            book._find_prompt_for_actor(db, prompt_id, channel, actor, {})
        else:
            historical = db.execute(
                "SELECT prompt_visibility FROM mcp25_prompt_events WHERE prompt_id = ? ORDER BY sequence",
                (prompt_id,),
            ).fetchall()
            if not historical or (channel == "mcp" and any(row["prompt_visibility"] == "private" for row in historical)
                                  and not mcp24_access.has_full_access(actor)):
                mcp_workflows.resolve_error("not_found", "没有找到可访问的命题历史。", prompt_id=prompt_id)
    start = _parse_datetime(params["from"], "from") if "from" in params else None
    end = _parse_datetime(params["through"], "through") if "through" in params else None
    if start and end and start >= end:
        mcp_workflows.resolve_error("invalid_params", "from 必须早于 through（through 不包含）。", fields=["from", "through"])
    page_size, offset = params.get("page_size", 20), params.get("offset", 0)
    events, total, next_offset = _page_events(
        db, prompt_ids, start=start, end=end, event_types=params.get("event_types"),
        page_size=page_size, offset=offset, channel=channel, actor=actor,
    )
    coverage_start = db.execute(
        "SELECT coverage_start_at FROM mcp25_prompt_history_meta WHERE singleton = 1"
    ).fetchone()[0]
    return {
        "events": events, "total": total, "page_size": page_size, "offset": offset,
        "next_offset": next_offset,
        "coverage": [_coverage(db, prompt_id, coverage_start) for prompt_id in prompt_ids],
        "sort": ["occurred_at", "sequence"],
    }


def _local_period(period, zone):
    try:
        start_date = date.fromisoformat(period["from_date"])
        through_date = date.fromisoformat(period["through_date"])
        if start_date > through_date:
            raise ValueError
        start = datetime.combine(start_date, time.min, zone)
        end = datetime.combine(through_date + timedelta(days=1), time.min, zone)
    except (ValueError, OverflowError):
        mcp_workflows.resolve_error("invalid_params", "日期区间无效；from_date 必须不晚于 through_date。",
                                     fields=["from_date", "through_date"])
    return mcp_workflows.iso(start), mcp_workflows.iso(end)


def _review_prompt_events(book, db, params, channel, validate, now, actor):
    try:
        zone = ZoneInfo(params["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        mcp_workflows.resolve_error("invalid_params", "timezone 必须是有效的 IANA 时区。", field="timezone")
    topic = book._resolve_topic(db, params.get("topic"))
    if "topic" in params and topic is None:
        mcp_workflows.resolve_error("not_found", "未找到指定主题。", topic=params["topic"])
    event_types = params.get("event_types")
    page_size, offset = params.get("page_size", 20), params.get("offset", 0)
    coverage_start = db.execute(
        "SELECT coverage_start_at FROM mcp25_prompt_history_meta WHERE singleton = 1"
    ).fetchone()[0]
    current_rows = [row for row in book._visible_rows(db, channel, actor)
                    if (not params.get("direction") or row["direction"] == params["direction"])
                    and (topic is None or row["topic_id"] == topic["id"])]
    current_written_ids = {row["id"] for row in current_rows if row["state"] == "written"}
    periods = []
    for period in params["periods"]:
        start, end = _local_period(period, zone)
        clauses = ["occurred_at >= ?", "occurred_at < ?"]
        args: list[object] = [start, end]
        if params.get("direction"):
            clauses.append("direction = ?")
            args.append(params["direction"])
        if topic:
            clauses.append("topic_id = ?")
            args.append(topic["id"])
        if event_types:
            clauses.append("event_type IN (" + ",".join("?" for _ in event_types) + ")")
            args.extend(event_types)
        where = " AND ".join(clauses)
        rows = db.execute(
            f"SELECT * FROM mcp25_prompt_events WHERE {where} ORDER BY occurred_at, sequence",
            args,
        ).fetchall()
        rows = [row for row in rows if _visible_event(row, channel, actor)]
        data = [_event_view(db, row, actor) for row in rows]
        prompt_ids = {row["prompt_id"] for row in rows}
        completed = {row["prompt_id"] for row in rows
                     if row["event_type"] in {"state_changed", "corrected"}
                     and row["from_state"] != "written" and row["to_state"] == "written"}
        reopened = [row for row in rows if row["event_type"] in {"state_changed", "corrected"}
                    and row["from_state"] == "written" and row["to_state"] != "written"]
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        coverage_dt = datetime.fromisoformat(coverage_start.replace("Z", "+00:00"))
        covered_from = min(coverage_dt, end_dt)
        page = data[offset:offset + page_size]
        next_offset = offset + page_size if offset + page_size < len(data) else None
        periods.append({
            "label": period.get("label"), "from_date": period["from_date"],
            "through_date": period["through_date"], "timezone": params["timezone"],
            "from": start, "through_exclusive": end,
            "coverage": {"traceable_from": coverage_start,
                         "covers_entire_period": start_dt >= coverage_dt,
                         "uncovered_interval": None if start_dt >= coverage_dt
                         else {"from": start, "through_exclusive": mcp_workflows.iso(covered_from)}},
            "counts": {
                "events": len(rows),
                "completed_events": sum(1 for row in rows if row["event_type"] in {"state_changed", "corrected"}
                                         and row["from_state"] != "written" and row["to_state"] == "written"),
                "reopened_events": len(reopened),
                "completed_prompts_currently_written": len(completed & current_written_ids),
                "prompts_with_period_events_currently_written": len(prompt_ids & current_written_ids),
                "currently_written_in_scope": len(current_written_ids),
            },
            "formula": {
                "completed_events": "state_changed/corrected event with before != written and after = written",
                "reopened_events": "state_changed/corrected event with before = written and after != written",
                "completed_prompts_currently_written": "distinct prompt IDs with a completion event in this interval whose current state is written",
                "currently_written_in_scope": "all currently visible prompts matching direction/topic with state=written; not date-filtered",
            },
            "records": page, "total": len(data), "page_size": page_size,
            "offset": offset, "next_offset": next_offset,
        })
    return {"periods": periods, "comparison": len(periods) == 2,
            "event_types": event_types or list(_EVENT_TYPES),
            "filters": {"direction": params.get("direction"), "topic_id": topic["id"] if topic else None}}


HANDLERS = {
    ACTION: _correct_prompt_status,
    TIMELINE_ACTION: _get_prompt_timeline,
    REVIEW_ACTION: _review_prompt_events,
}
WRITE_ACTIONS = frozenset({ACTION})


def handle(book, db, action, params, channel, validate, now, actor):
    return HANDLERS[action](book, db, params, channel, validate, now, actor)
