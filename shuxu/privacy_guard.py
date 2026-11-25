"""Write-boundary checks for identifiable consultation material."""

from __future__ import annotations

import re
from collections.abc import Mapping


_CATEGORY_LABELS = {
    "name": "姓名",
    "phone": "电话",
    "email": "邮箱",
    "account": "账号",
    "case_id": "个案编号",
    "address": "地址",
    "identity_number": "证件号",
    "uninspectable_image": "图片文字（不可检查）",
}
_PLACEHOLDER = re.compile(
    r"^(?:(?:(?:某|匿名|匿名的)?(?:来访者|咨询者|个案|案主|患者|当事人|服务对象|客户)"
    r"(?:[a-z](?:\d+)?|[甲乙丙丁戊己庚辛壬癸]|[一二三四五六七八九十]号?)?)|"
    r"(?:[a-z](?:\d+)?|[甲乙丙丁戊己庚辛壬癸]|[一二三四五六七八九十]号?)|"
    r"某人|某某|匿名|已匿名|已脱敏|已去标识|已替换|隐去|n/a|(?:姓名|电话|邮箱|账号|个案编号|地址)(?:已|已经)?(?:替换|脱敏|匿名|隐去)|"
    r"姓名|电话|邮箱|账号|个案|"
    r"<\s*(?:name|phone|email|account|case|address)\s*>|"
    r"\[\s*(?:姓名|电话|邮箱|账号|个案编号|地址|已去标识|已替换)\s*\])$",
    re.IGNORECASE,
)
_FIELD_PATTERNS = {
    "name": re.compile(
        r"(?:姓名|名字|来访者姓名|咨询者姓名|当事人姓名|患者姓名|案主姓名|客户姓名|"
        r"[\"']?(?:full_name|client_name|patient_name|person_name|name)[\"']?)"
        r"\s*(?:是|为|[:：=]|\s)\s*[\"“‘「]?([^,，;；\n\r\t。\"'”’」}〔〕【】\[\]()（）\s]{1,40})",
        re.IGNORECASE,
    ),
    "phone": re.compile(
        r"(?:电话|手机|联系电话|联系号码|联系方式|phone(?:_number)?|mobile|tel)"
        r"\s*(?:是|为|[:：=])\s*([+＋\d()（）.\-\s]{7,24})",
        re.IGNORECASE,
    ),
    "email": re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-z]{2,}(?![\w.-])", re.IGNORECASE),
    "account": re.compile(
        r"(?:账号|帳号|账户|帳戶|微信号|QQ号|用户名|user(?:_?id|_?name)|account(?:_?id)?|login(?:_?id)?)"
        r"\s*(?:是|为|[:：=])\s*([\w@.\-]{3,64})",
        re.IGNORECASE,
    ),
    "case_id": re.compile(
        r"(?:个案|案例|病例|案号|case(?:_?(?:id|no|number))?)"
        r"\s*(?:编号|号码|号|id|no|number)?\s*(?:是|为|[:：=])\s*([\w/\-]{2,40})",
        re.IGNORECASE,
    ),
    "address": re.compile(
        r"(?:家庭地址|联系地址|居住地址|地址|住址|address|residence)"
        r"\s*(?:是|为|[:：=])\s*([^,，;；\n\r\t。|]{3,80})",
        re.IGNORECASE,
    ),
    "identity_number": re.compile(
        r"(?:身份证(?:号码|号)?|证件(?:号码|号)?|passport(?:_number)?|id_card(?:_number)?)"
        r"\s*(?:是|为|[:：=])\s*([\dXx\-]{8,24})",
        re.IGNORECASE,
    ),
}
_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_FIELD_KEYS = {
    "name": "name", "full_name": "name", "client_name": "name", "patient_name": "name", "person_name": "name",
    "phone": "phone", "phone_number": "phone", "mobile": "phone", "tel": "phone",
    "email": "email", "email_address": "email",
    "account": "account", "account_id": "account", "username": "account", "user_id": "account",
    "wechat_id": "account", "qq_id": "account",
    "case_id": "case_id", "case_no": "case_id", "case_number": "case_id",
    "address": "address", "home_address": "address", "residence": "address",
    "id_card": "identity_number", "identity_number": "identity_number", "passport_number": "identity_number",
}
_MATERIAL_ACTIONS = {
    "add_inspiration": ("inspiration", "create"), "update_inspiration": ("inspiration", "update"),
    "save_clipping": ("clipping", "create"), "update_clipping": ("clipping", "update"),
    "save_resume_card": ("resume", "create"), "update_resume_card": ("resume", "update"),
    "save_inspiration_image": ("inspiration", "image"),
}
_CONTENT_FIELDS = {
    "inspiration": ("text", "source", "url"),
    "clipping": ("title", "url", "published_on", "source", "note"),
    "resume": ("title", "note", "question"),
}


class PrivacyViolation(Exception):
    def __init__(self, code: str, categories: set[str] | list[str] | tuple[str, ...], *, image: bool = False):
        self.code = code
        self.details = {"categories": sorted(categories)}
        labels = "、".join(_CATEGORY_LABELS[item] for item in self.details["categories"])
        if image:
            message = "咨询或反思截图中的文字无法检查，已拒绝保存图片；请改提交已去标识的文字内容。"
            if labels:
                message += f" 附带文字含有可识别信息类别：{labels}。"
        elif code == "privacy_scope_required":
            message = (
                f"待保存内容含有可识别信息类别：{labels}。目前无法从方向、主题或关联命题判断用途；"
                "请先补充明确的非咨询范围，或在来源端去标识后重新提交。"
            )
        else:
            message = (
                f"咨询或反思内容含有可识别信息类别：{labels}，已拒绝保存。"
                "请先在来源端将相应内容替换为〔来访者A〕、〔个案B〕等去标识占位语后重新提交。"
            )
        super().__init__(message)


def _clean_value(value: str) -> str:
    return value.strip(" \t\r\n:：=,，.;；。\"'“”‘’「」【】〔〕[]{}()（）")


def _is_placeholder(value: str, category: str) -> bool:
    value = _clean_value(value)
    if not value or _PLACEHOLDER.fullmatch(value):
        return True
    if category == "name" and (re.fullmatch(r"[\u4e00-\u9fff]{1,2}某(?:某)?", value) or value.casefold() in {"client a", "patient a"}):
        return True
    if category == "case_id" and re.fullmatch(r"[a-f]|[甲乙丙丁戊己庚辛壬癸]", value, re.IGNORECASE):
        return True
    if category == "phone" and re.fullmatch(r"[+＋\d()（）.\-\s]*[xX*＊×]{2,}[\dXx*＊×()（）.\-\s]*", value):
        return True
    if category in {"account", "case_id", "address"} and re.search(r"[*＊×]{2,}", value):
        return True
    if category == "address" and re.search(r"(?:某市|某区|某县|某镇|某街道|某路|某号|某小区|某栋|某单元)", value):
        return True
    if re.fullmatch(r"[xX*＊×._-]{2,}", value):
        return True
    return False


def _scan_text(value: str) -> set[str]:
    found = set()
    for category, pattern in _FIELD_PATTERNS.items():
        for match in pattern.finditer(value):
            candidate = match.group(1) if match.lastindex else match.group(0)
            if not _is_placeholder(candidate, category):
                found.add(category)
    if _MOBILE.search(value):
        found.add("phone")
    return found


def _scan(value) -> set[str]:
    found = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            category = _FIELD_KEYS.get(str(key).casefold())
            if category and isinstance(item, str) and not _is_placeholder(item, category):
                found.add(category)
            found.update(_scan(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_scan(item))
    elif isinstance(value, str):
        found.update(_scan_text(value))
    return found


def _direction_state(db, value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.casefold() in {"consultation_reflection", "咨询反思"}:
        return True
    row = db.execute(
        "SELECT id FROM directions WHERE id = ? COLLATE NOCASE OR name = ? COLLATE NOCASE LIMIT 1",
        (value, value),
    ).fetchone()
    if row is None:
        return None
    return row["id"] == "consultation_reflection"


def _topic_states(db, value):
    if not isinstance(value, str) or not value.strip():
        return []
    rows = db.execute(
        "SELECT direction FROM topics WHERE id = ? OR name = ? COLLATE NOCASE LIMIT 4",
        (value, value),
    ).fetchall()
    return [_direction_state(db, row["direction"]) for row in rows]


def _prompt_states(db, prompt_id):
    if not isinstance(prompt_id, str) or not prompt_id:
        return []
    row = db.execute("SELECT direction, topic_id FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
    if row is None:
        return []
    return [
        _direction_state(db, row["direction"]),
        *_topic_states(db, row["topic_id"]),
    ]


def _scope(db, *, direction=None, topic=None, topic_id=None, prompt_id=None, parent_id=None, old_row=None, items=None):
    states = []
    direct = _direction_state(db, direction)
    if direct is not None:
        states.append(direct)
    for value in (topic, topic_id):
        states.extend(state for state in _topic_states(db, value) if state is not None)
    for value in (prompt_id, parent_id):
        states.extend(state for state in _prompt_states(db, value) if state is not None)
    if old_row is not None:
        old_direction = old_row["direction"]
        state = _direction_state(db, old_direction)
        if state is not None:
            states.append(state)
        states.extend(state for state in _topic_states(db, old_row["topic_id"]) if state is not None)
        states.extend(state for state in _prompt_states(db, old_row["prompt_id"]) if state is not None)
    for item in items or ():
        kind, identifier = item.get("kind"), item.get("id")
        if kind == "prompt" and isinstance(identifier, str):
            states.extend(state for state in _prompt_states(db, identifier) if state is not None)
        elif kind in {"inspiration", "clipping", "resume"} and isinstance(identifier, str):
            row = db.execute(
                "SELECT direction, topic_id, prompt_id FROM materials WHERE id = ? AND collection = ?",
                (identifier, kind),
            ).fetchone()
            if row is not None:
                state = _direction_state(db, row["direction"])
                if state is not None:
                    states.append(state)
                states.extend(state for state in _topic_states(db, row["topic_id"]) if state is not None)
                states.extend(state for state in _prompt_states(db, row["prompt_id"]) if state is not None)
    if True in states:
        return "consultation"
    if False in states:
        return "non_consultation"
    return "unknown"


def _reject_if_needed(db, content, *, direction=None, topic=None, topic_id=None, prompt_id=None,
                      parent_id=None, old_row=None, items=None, image=False):
    categories = _scan(content)
    scope = _scope(
        db, direction=direction, topic=topic, topic_id=topic_id, prompt_id=prompt_id,
        parent_id=parent_id, old_row=old_row, items=items,
    )
    if image and scope == "consultation":
        raise PrivacyViolation("privacy_rejected", categories | {"uninspectable_image"}, image=True)
    if categories and scope == "consultation":
        raise PrivacyViolation("privacy_rejected", categories)
    if categories and scope == "unknown":
        raise PrivacyViolation("privacy_scope_required", categories)


def check_material_write(db, *, content, direction, topic_id, prompt_id, old_row=None, image=False):
    _reject_if_needed(
        db, content, direction=direction, topic_id=topic_id, prompt_id=prompt_id,
        old_row=old_row, image=image,
    )


def _old_material(db, collection, identifier):
    if not isinstance(identifier, str) or not identifier:
        return None
    return db.execute(
        "SELECT direction, topic_id, prompt_id FROM materials WHERE id = ? AND collection = ?",
        (identifier, collection),
    ).fetchone()


def _content_fields(params, fields):
    return {key: params[key] for key in fields if key in params}


def check_action(db, action, params):
    if action == "submit_async_job":
        if params.get("action"):
            check_action(db, params["action"], params.get("arguments", {}))
        return
    if action == "apply_actions":
        for step in params.get("actions", []):
            check_action(db, step["tool"], step.get("arguments", {}))
        return
    material_action = _MATERIAL_ACTIONS.get(action)
    if material_action:
        collection, operation = material_action
        if operation == "create" and collection == "clipping" and not params.get("direction"):
            direction = "social_topics"
        else:
            direction = params.get("direction")
        old_row = _old_material(db, collection, params.get("id")) if operation == "update" or "id" in params else None
        content = _content_fields(params, _CONTENT_FIELDS[collection])
        _reject_if_needed(
            db, content, direction=direction, topic=params.get("topic"),
            prompt_id=params.get("prompt_id"), old_row=old_row,
            image=action == "save_inspiration_image",
        )
        return
    if action == "save_prompt":
        _reject_if_needed(
            db, {"text": params.get("text", "")}, direction=params.get("direction"),
            topic=params.get("topic"), parent_id=params.get("parent_id"),
        )
    elif action == "record_feedback":
        _reject_if_needed(
            db, {"feedback": params.get("feedback", "")}, direction=params.get("direction"),
            topic=params.get("topic"), prompt_id=params.get("prompt_id"),
        )
    elif action == "guide_thought":
        prompt_id = params.get("prompt_id")
        if not prompt_id and params.get("conversation_id"):
            row = db.execute(
                "SELECT prompt_id FROM mcp_conversation_entries WHERE conversation_id = ? "
                "AND prompt_id IS NOT NULL ORDER BY rowid DESC LIMIT 1",
                (params["conversation_id"],),
            ).fetchone()
            prompt_id = row["prompt_id"] if row else None
        prompt = db.execute(
            "SELECT text, direction, topic_id FROM prompts WHERE id = ?", (prompt_id,),
        ).fetchone() if prompt_id else None
        _reject_if_needed(
            db, {"focus": params.get("focus", ""), **({"prompt": prompt["text"]} if prompt else {})},
            direction=prompt["direction"] if prompt else None,
            topic_id=prompt["topic_id"] if prompt else None, prompt_id=prompt_id,
        )
    elif action in {"set_prompt_state", "snooze_prompt", "clear_snooze", "correct_prompt_status"}:
        _reject_if_needed(db, {"reason": params.get("reason", "")}, prompt_id=params.get("prompt_id"))
    elif action == "set_prompt_family_relation":
        _reject_if_needed(
            db, {"reason": params.get("reason", "")}, prompt_id=params.get("prompt_id"),
            items=[{"kind": "prompt", "id": params.get("prompt_id")},
                   {"kind": "prompt", "id": params.get("related_prompt_id")}],
        )
    elif action == "set_question_preference":
        _reject_if_needed(db, {"reason": params.get("reason", "")},
                          direction=params.get("direction"))
    elif action == "capture_quotes":
        _reject_if_needed(
            db, {"quotes": params.get("quotes", []), "source": params.get("source", "")},
            direction=params.get("direction"), topic=params.get("topic"), prompt_id=params.get("prompt_id"),
        )
    elif action == "save_chat_screenshot":
        _reject_if_needed(
            db, {"note": params.get("note", ""), "source": params.get("source", "")},
            direction=params.get("direction"), topic=params.get("topic"), prompt_id=params.get("prompt_id"),
            image=True,
        )
    elif action == "start_attachment_upload":
        _reject_if_needed(
            db, {"note": params.get("note", ""), "source": params.get("source", "")},
            direction=params.get("direction"), topic=params.get("topic"), prompt_id=params.get("prompt_id"),
            image=True,
        )
    elif action == "merge_clipping":
        _reject_if_needed(
            db, _content_fields(params, ("title", "url", "published_on", "source", "note")),
            direction=params.get("direction"), topic=params.get("topic"), prompt_id=params.get("prompt_id"),
            old_row=_old_material(db, "clipping", params.get("id")),
        )
    elif action == "remember_claim_sources":
        _reject_if_needed(db, {"claims": params.get("claims", [])}, prompt_id=params.get("prompt_id"))
    elif action == "write_claim_evidence":
        claims = [{
            "text": item.get("text", ""),
            "evidence": [{key: source.get(key, "") for key in ("location", "excerpt", "conclusion")}
                         for source in item.get("evidence", [])],
        } for item in params.get("claims", [])]
        _reject_if_needed(db, {"claims": claims}, prompt_id=params.get("prompt_id"))
    elif action == "save_prompt_material_relations":
        prompt = params.get("prompt") or {}
        content = {"prompt": {"text": prompt.get("text", "")},
                   "relations": [{"description": item.get("description", "")}
                                 for item in params.get("materials", [])]}
        _reject_if_needed(
            db, content, direction=prompt.get("direction", params.get("direction")),
            topic=prompt.get("topic", params.get("topic")),
            prompt_id=params.get("prompt_id"), items=params.get("materials", []),
        )
    elif action == "save_topic_project":
        _reject_if_needed(db, {"title": params.get("title", "")}, items=params.get("items", []))
    elif action == "export_thought_bundle":
        _reject_if_needed(db, {"title": params.get("title", "")}, items=params.get("items", []))
