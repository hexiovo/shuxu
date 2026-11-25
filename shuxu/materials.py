"""Local inspiration, source clippings and continuation notes."""

import base64
import binascii
import json
import sqlite3
import uuid
from datetime import date
from urllib.parse import urlsplit

from . import privacy_guard

MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_FORMATS = {
    "image/png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
    "image/gif": lambda data: data.startswith((b"GIF87a", b"GIF89a")),
    "image/webp": lambda data: data.startswith(b"RIFF") and data[8:12] == b"WEBP",
}

COLLECTIONS = {
    "inspiration": ("add_inspiration", "search_inspirations", "get_inspiration", "update_inspiration", "remove_inspiration"),
    "clipping": ("save_clipping", "list_clippings", "get_clipping", "update_clipping", "remove_clipping"),
    "resume": ("save_resume_card", "list_resume_cards", "get_resume_card", "update_resume_card", "clear_resume_card"),
}
ACTION_MAP = {
    name: (collection, operation)
    for collection, names in COLLECTIONS.items()
    for name, operation in zip(names, ("create", "list", "get", "update", "remove"), strict=True)
}
LOCAL_ACTIONS = {"save_inspiration_image", "get_inspiration_image", "remove_inspiration_image"}
WRITE_ACTIONS = {name for name, (_, operation) in ACTION_MAP.items() if operation in {"create", "update", "remove"}}
WRITE_ACTIONS.update(LOCAL_ACTIONS - {"get_inspiration_image"})


class MaterialError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details


def tool_specs(tool, field, identifier, direction, version, include_private):
    text = field("string", maxLength=500)
    short = field("string", maxLength=160)
    url = field("string", maxLength=2048)
    optional_id = field(["string", "null"], minLength=1, maxLength=200)
    links = {"direction": field(["string", "null"], enum=[*direction["enum"], None]), "topic": optional_id, "prompt_id": optional_id}
    fields = {
        "inspiration": {
            "kind": field("string", enum=["thought", "quote", "link", "screenshot"]),
            "text": text, "source": short, "url": url,
        },
        "clipping": {"title": short, "url": url, "published_on": field("string", maxLength=10), "source": short, "note": text},
        "resume": {"title": short, "note": text, "question": text},
    }
    labels = {"inspiration": "灵感", "clipping": "社会话题剪藏", "resume": "思考续接卡"}
    required = {"inspiration": ("kind",), "clipping": ("title", "url", "published_on", "source"), "resume": ("title", "note", "question")}
    specs = []
    for collection, (create, search, get, update, remove) in COLLECTIONS.items():
        label = labels[collection]
        record = {**links, **fields[collection]}
        specs.extend((
            tool(create, f"仅在用户主动要求时保存{label}。", {"request_id": identifier, **record}, ("request_id", *required[collection])),
            tool(search, f"按本轮指定线索查找{label}。MCP 查询须提供关键词、主题、命题或方向。", {
                "query": field("string", maxLength=160),
                "direction": direction, "topic": identifier, "prompt_id": identifier,
                "limit": field("integer", minimum=1, maximum=50),
                "offset": field("integer", minimum=0),
            }),
            tool(get, f"按精确 ID 查看{label}。MCP 仅在本轮获准读取该条文字时设置 include_private。", {
                "id": identifier, "include_private": include_private,
            }, ("id",)),
            tool(update, f"按明确 ID 和当前版本修改{label}。", {
                "request_id": identifier, "id": identifier, "expected_version": version, **record,
            }, ("request_id", "id", "expected_version")),
            tool(remove, f"仅在用户要求时按 ID 和当前版本删除{label}。", {
                "request_id": identifier, "id": identifier, "expected_version": version,
            }, ("request_id", "id", "expected_version")),
        ))
    image_fields = {"image_type": field("string", enum=list(IMAGE_FORMATS)), "image_data": field("string", minLength=1, maxLength=4 * ((MAX_IMAGE_BYTES + 2) // 3))}
    locals_only = [
        tool("save_inspiration_image", "从本机网页原子保存灵感和截图，或给已有灵感换图。", {
            "request_id": identifier, "id": identifier, "expected_version": version,
            **links, **fields["inspiration"], **image_fields,
        }, ("request_id", *image_fields)),
        tool("get_inspiration_image", "从本机网页读取截图。", {"id": identifier}, ("id",)),
        tool("remove_inspiration_image", "移除灵感截图。", {"request_id": identifier, "id": identifier, "expected_version": version}, ("request_id", "id", "expected_version")),
    ]
    return specs, locals_only


def schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS materials (
            id TEXT PRIMARY KEY,
            collection TEXT NOT NULL,
            direction TEXT REFERENCES directions(id),
            topic_id TEXT REFERENCES topics(id) ON DELETE SET NULL,
            prompt_id TEXT REFERENCES prompts(id) ON DELETE SET NULL,
            data_json TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS materials_collection_date ON materials(collection, updated_at DESC);
        CREATE TABLE IF NOT EXISTS inspiration_images (
            material_id TEXT PRIMARY KEY REFERENCES materials(id) ON DELETE CASCADE,
            content_type TEXT NOT NULL,
            content BLOB NOT NULL
        );
    """)


def _web_url(value):
    try:
        parts = urlsplit(value)
        if parts.scheme in ("http", "https") and parts.hostname and not parts.username and not parts.password and not any(char.isspace() for char in value):
            return value
    except ValueError:
        pass
    raise MaterialError("invalid_params", "链接必须是有效的 HTTP 或 HTTPS 地址。")


def _row(db, collection, identifier):
    row = db.execute("SELECT * FROM materials WHERE id = ? AND collection = ?", (identifier, collection)).fetchone()
    if row is None:
        raise MaterialError("not_found", "未找到这条记录。")
    return row


def _view(db, row, *, content, channel):
    result = {key: row[key] for key in ("id", "direction", "topic_id", "prompt_id", "version", "created_at", "updated_at")}
    result["collection"] = row["collection"]
    if channel == "mcp" and result["prompt_id"]:
        prompt = db.execute("SELECT visibility FROM prompts WHERE id = ?", (result["prompt_id"],)).fetchone()
        if prompt and prompt["visibility"] == "private":
            result["prompt_id"] = None
    payload = json.loads(row["data_json"])
    if content:
        result.update(payload)
    elif row["collection"] == "inspiration":
        result["kind"] = payload["kind"]
    elif row["collection"] == "clipping":
        result["published_on"] = payload["published_on"]
    if channel == "local" and row["collection"] == "inspiration":
        result["has_image"] = db.execute("SELECT 1 FROM inspiration_images WHERE material_id = ?", (row["id"],)).fetchone() is not None
    return result


def _links(db, params, previous, find_topic, find_prompt, channel, collection):
    prompt_id = params.get("prompt_id", previous["prompt_id"] if previous else None)
    topic_value = params.get("topic", previous["topic_id"] if previous else None)
    direction = params.get("direction", previous["direction"] if previous else None)
    topic = find_topic(db, topic_value)
    if topic_value and topic is None:
        raise MaterialError("not_found", "未找到指定主题。")
    prompt = find_prompt(db, prompt_id, channel, {}) if prompt_id else None
    direction = direction or (topic["direction"] if topic else prompt["direction"] if prompt else None)
    if collection == "clipping":
        direction = direction or "social_topics"
    if (topic and topic["direction"] != direction) or (prompt and prompt["direction"] != direction):
        raise MaterialError("invalid_params", "方向与关联的主题或命题不一致。")
    topic_id = topic["id"] if topic else None
    return direction, topic_id, prompt_id


def _payload(collection, params, previous, *, has_image):
    fields = {
        "inspiration": ("kind", "text", "source", "url"),
        "clipping": ("title", "url", "published_on", "source", "note"),
        "resume": ("title", "note", "question"),
    }[collection]
    result = {key: params.get(key, previous.get(key, "")).strip() for key in fields}
    if collection == "inspiration" and result["kind"] == "quote":
        quote = params.get("text", previous.get("text", ""))
        result["text"] = quote if quote.strip() else ""
    if collection == "clipping" and "annotations" in previous:
        result["annotations"] = previous["annotations"]
    if result.get("url"):
        result["url"] = _web_url(result["url"])
    if collection == "inspiration":
        kind = result["kind"]
        if kind == "screenshot" and not has_image:
            raise MaterialError("invalid_params", "截图灵感需要从本机网页选择或粘贴图像。")
        if kind == "link" and not result["url"]:
            raise MaterialError("invalid_params", "链接灵感需要原始链接。")
        if kind in ("thought", "quote") and not result["text"]:
            raise MaterialError("invalid_params", "请填写灵感或摘句。")
    elif collection == "clipping":
        if not all(result[key] for key in ("title", "url", "published_on", "source")):
            raise MaterialError("invalid_params", "剪藏需要标题、链接、发布日期和出处。")
        try:
            if date.fromisoformat(result["published_on"]).isoformat() != result["published_on"]:
                raise ValueError
        except ValueError:
            raise MaterialError("invalid_params", "发布日期请使用有效的 YYYY-MM-DD。") from None
    elif not all(result.values()):
        raise MaterialError("invalid_params", "续接卡需要标题、留给自己的一句话和一个待想问题。")
    return result


def _list(db, collection, params, channel, find_topic, find_prompt):
    query = params.get("query", "").strip()
    if channel == "mcp" and not (len(query) >= 2 or any(key in params for key in ("direction", "topic", "prompt_id"))):
        raise MaterialError("invalid_params", "请按明确方向、主题、命题或至少两个字符的关键词查找。")
    if channel == "mcp" and params.get("offset"):
        raise MaterialError("invalid_params", "请缩小查找范围。")
    filters, args = ["collection = ?"], [collection]
    for key in ("direction", "prompt_id"):
        if key in params:
            if key == "prompt_id":
                find_prompt(db, params[key], channel, {})
            filters.append(f"{key} = ?")
            args.append(params[key])
    if "topic" in params:
        topic = find_topic(db, params["topic"])
        if topic is None:
            return {"items": [], "has_more": False}
        filters.append("topic_id = ?")
        args.append(topic["id"])
    if query:
        keys = {
            "inspiration": ("text", "source", "url"),
            "clipping": ("title", "source", "url", "note"),
            "resume": ("title", "note", "question"),
        }[collection]
        filters.append("(" + " OR ".join(f"instr(lower(json_extract(data_json, '$.{key}')), lower(?)) > 0" for key in keys) + ")")
        args.extend([query] * len(keys))
    limit = min(params.get("limit", 20), 12 if channel == "mcp" else 50)
    rows = db.execute(
        "SELECT * FROM materials WHERE " + " AND ".join(filters) + " ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
        (*args, limit + 1, params.get("offset", 0)),
    ).fetchall()
    if channel == "mcp":
        # A scoped search returns pointers only; private content requires a separate exact-ID request.
        return {"items": [_view(db, row, content=False, channel=channel) for row in rows[:limit]], "has_more": len(rows) > limit}
    return {"items": [_view(db, row, content=True, channel=channel) for row in rows[:limit]], "has_more": len(rows) > limit}


def _decode_image(params):
    image_type = params["image_type"]
    try:
        image = base64.b64decode(params["image_data"], validate=True)
    except (ValueError, binascii.Error):
        raise MaterialError("invalid_params", "截图数据无效。") from None
    if len(image) > MAX_IMAGE_BYTES or not IMAGE_FORMATS[image_type](image):
        raise MaterialError("invalid_params", "请选择不超过 5 MB 的 PNG、JPEG、GIF 或 WebP 图片。")
    return image_type, image


def handle(db, action, params, channel, now, find_topic, find_prompt):
    collection, operation = ACTION_MAP.get(action, ("inspiration", "image"))
    if operation == "list":
        return _list(db, collection, params, channel, find_topic, find_prompt)
    if operation == "get":
        row = _row(db, collection, params["id"])
        return _view(db, row, content=channel == "local" or params.get("include_private") is True, channel=channel)
    if action == "get_inspiration_image":
        row = db.execute("SELECT content_type, content FROM inspiration_images WHERE material_id = ?", (params["id"],)).fetchone()
        if not row:
            raise MaterialError("not_found", "这条灵感没有截图。")
        return {"image_type": row["content_type"], "image_data": base64.b64encode(row["content"]).decode("ascii")}

    row = _row(db, collection, params["id"]) if "id" in params else None
    if row is not None and row["version"] != params.get("expected_version"):
        raise MaterialError("version_conflict", "这条记录已更新，请刷新后重试。")
    if channel == "mcp" and row is not None:
        if row["prompt_id"]:
            find_prompt(db, row["prompt_id"], channel, {})
        if collection == "inspiration" and db.execute("SELECT 1 FROM inspiration_images WHERE material_id = ?", (row["id"],)).fetchone():
            raise MaterialError("forbidden", "带截图的灵感请在本机网页维护。")
    identifier = row["id"] if row else str(uuid.uuid4())
    version = row["version"] + 1 if row else 1
    receipt = {"id": identifier, "version": version, "request_id": params["request_id"]}
    if operation == "remove":
        db.execute("DELETE FROM materials WHERE id = ?", (identifier,))
        return {**receipt, "removed": True}

    previous = json.loads(row["data_json"]) if row else {}
    has_image = bool(row and collection == "inspiration" and db.execute("SELECT 1 FROM inspiration_images WHERE material_id = ?", (identifier,)).fetchone())
    if action == "remove_inspiration_image":
        db.execute("DELETE FROM inspiration_images WHERE material_id = ?", (identifier,))
        if previous["kind"] == "screenshot":
            if not previous.get("text") and not previous.get("url"):
                raise MaterialError("invalid_params", "这条仅含截图的灵感，请删除整条或先补充文字或链接。")
            previous["kind"] = "thought" if previous.get("text") else "link"
        db.execute("UPDATE materials SET data_json = ?, version = ?, updated_at = ? WHERE id = ?", (json.dumps(previous, ensure_ascii=False), version, now, identifier))
        return receipt

    if action == "save_inspiration_image" and not row and "kind" not in params:
        params = {**params, "kind": "screenshot"}
    if action == "save_inspiration_image" and row is None and "expected_version" in params:
        raise MaterialError("invalid_params", "新增截图不接受已有版本。")
    direction, topic_id, prompt_id = _links(db, params, row, find_topic, find_prompt, channel, collection)
    image = action == "save_inspiration_image"
    if image:
        privacy_guard.check_material_write(
            db, content=_payload(collection, params, previous, has_image=True),
            direction=direction, topic_id=topic_id, prompt_id=prompt_id,
            old_row=row, image=True,
        )
        image = _decode_image(params)
    data = _payload(collection, params, previous, has_image=bool(image) or has_image)
    privacy_guard.check_material_write(
        db, content=data, direction=direction, topic_id=topic_id,
        prompt_id=prompt_id, old_row=row,
    )
    encoded = json.dumps(data, ensure_ascii=False)
    if row is None:
        db.execute(
            "INSERT INTO materials(id, collection, direction, topic_id, prompt_id, data_json, version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (identifier, collection, direction, topic_id, prompt_id, encoded, version, now, now),
        )
    else:
        db.execute(
            "UPDATE materials SET direction = ?, topic_id = ?, prompt_id = ?, data_json = ?, version = ?, updated_at = ? WHERE id = ?",
            (direction, topic_id, prompt_id, encoded, version, now, identifier),
        )
    if image:
        db.execute("INSERT OR REPLACE INTO inspiration_images(material_id, content_type, content) VALUES (?, ?, ?)", (identifier, *image))
    return receipt

