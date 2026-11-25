"""Time-limited file transfer and opt-in MCP gateway transport."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import sqlite3
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import materials, mcp_workflows, privacy_guard

LOGGER = logging.getLogger("shuxu.mcp24_transport")

PROTOCOL_VERSION = 1
ARTIFACT_CHUNK_BYTES = 64 * 1024
UPLOAD_CHUNK_BYTES = 64 * 1024
MAX_ARTIFACT_TTL_SECONDS = 3600
MAX_UPLOAD_TTL_SECONDS = 24 * 60 * 60
MIN_REFERENCE_TTL_SECONDS = 60
MAX_EXPORT_ARTIFACT_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = 4 * ((materials.MAX_IMAGE_BYTES + 2) // 3) + 32_768
MAX_RESPONSE_BYTES = MAX_REQUEST_BYTES
SERVICE_TIMEOUT_SECONDS = 65
CLIENT_TIMEOUT_SECONDS = 65

_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_EXPORT_KEY = re.compile(r"[0-9a-f]{24}\Z")
_NAMESPACED_REQUEST_ID = re.compile(r"([0-9a-f]{24}):request:([0-9a-f]{32})\Z")
_GATEWAY_URL_ENV = "SHUXU_GATEWAY_URL"
_GATEWAY_HOST_ENV = "SHUXU_GATEWAY_HOST_ID"
_GATEWAY_BEARER_ENV = "SHUXU_GATEWAY_BEARER"
_UPSTREAM_MESSAGE_HEADER = "X-Shuxu-Upstream-Message-ID"
_OPERATION_SLOT_HEADER = "X-Shuxu-Operation-Slot"
_REQUEST_ID_HEADER = "X-Shuxu-Request-ID"


def _field(field_type, **extra):
    return {"type": field_type, **extra}


def specs(tool, field, ident, direction, version, include_private):
    sha = field("string", minLength=64, maxLength=64, pattern="^[0-9a-fA-F]{64}$")
    ttl = field("integer", minimum=MIN_REFERENCE_TTL_SECONDS, maximum=MAX_UPLOAD_TTL_SECONDS)
    artifact_id = ident
    upload_id = ident
    return [
        tool("create_export_artifact_reference", "为已完成的本机 Markdown 导出回执创建限时、按宿主绑定的文件引用；不接受文件路径。", {
            "request_id": ident,
            "export_request_id": ident,
            "source_channel": field("string", enum=["local", "mcp"]),
            "expires_in_seconds": field("integer", minimum=MIN_REFERENCE_TTL_SECONDS, maximum=MAX_ARTIFACT_TTL_SECONDS),
        }, ("request_id", "export_request_id", "expires_in_seconds")),
        tool("get_export_artifact_info", "查看当前宿主限时导出文件引用的大小、SHA-256 与分块上限。", {
            "artifact_id": artifact_id,
        }, ("artifact_id",)),
        tool("read_export_artifact_chunk", "按字节偏移读取本机导出 Markdown 的一个有界 base64 分块。", {
            "artifact_id": artifact_id,
            "offset": field("integer", minimum=0),
            "max_bytes": field("integer", minimum=1, maximum=ARTIFACT_CHUNK_BYTES),
        }, ("artifact_id", "offset", "max_bytes")),
        tool("start_attachment_upload", "为用户明确指定的图片建立按宿主绑定的续传会话；声明图片类型、大小、SHA-256 和有效期。", {
            "request_id": ident,
            "image_type": field("string", enum=list(materials.IMAGE_FORMATS)),
            "size_bytes": field("integer", minimum=1, maximum=materials.MAX_IMAGE_BYTES),
            "sha256": sha,
            "expires_in_seconds": ttl,
            "note": field("string", maxLength=500),
            "source": field("string", maxLength=160),
            "prompt_id": ident,
            "direction": direction,
            "topic": ident,
        }, ("request_id", "image_type", "size_bytes", "sha256", "expires_in_seconds")),
        tool("put_attachment_upload_chunk", "按固定的零起始分段编号续传图片；同编号同 SHA 幂等，不同内容冲突。", {
            "request_id": ident,
            "upload_id": upload_id,
            "part_number": field("integer", minimum=0),
            "part_sha256": sha,
            "chunk_base64": field("string", minLength=1, maxLength=4 * ((UPLOAD_CHUNK_BYTES + 2) // 3)),
        }, ("request_id", "upload_id", "part_number", "part_sha256", "chunk_base64")),
        tool("get_attachment_upload_status", "查询当前宿主的上传状态、已收分段和待传分段，不返回已传图片内容。", {
            "upload_id": upload_id,
        }, ("upload_id",)),
        tool("finish_attachment_upload", "校验完整图片的大小、SHA-256 和类型后，复用原截图 SQLite 保存流程。", {
            "request_id": ident,
            "upload_id": upload_id,
        }, ("request_id", "upload_id")),
        tool("cancel_attachment_upload", "取消当前宿主未完成的截图上传并清除已传分段内容。", {
            "request_id": ident,
            "upload_id": upload_id,
        }, ("request_id", "upload_id")),
    ]


WRITE_ACTIONS = {
    "create_export_artifact_reference",
    "start_attachment_upload",
    "put_attachment_upload_chunk",
    "finish_attachment_upload",
    "cancel_attachment_upload",
}


def schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS mcp24_export_artifact_refs (
            artifact_id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            profile TEXT NOT NULL,
            authorization_revision INTEGER NOT NULL,
            request_id TEXT NOT NULL,
            params_hash TEXT NOT NULL,
            export_request_id TEXT NOT NULL,
            source_channel TEXT NOT NULL CHECK (source_channel IN ('local','mcp')),
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            UNIQUE(host_id, request_id)
        );
        CREATE INDEX IF NOT EXISTS mcp24_export_refs_expiry ON mcp24_export_artifact_refs(expires_at);
        CREATE TABLE IF NOT EXISTS mcp24_attachment_uploads (
            upload_id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            profile TEXT NOT NULL,
            authorization_revision INTEGER NOT NULL,
            request_id TEXT NOT NULL,
            params_hash TEXT NOT NULL,
            image_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
            sha256 TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('uploading','complete','cancelled')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            result_json TEXT,
            UNIQUE(host_id, request_id)
        );
        CREATE INDEX IF NOT EXISTS mcp24_attachment_uploads_expiry ON mcp24_attachment_uploads(expires_at, status);
        CREATE TABLE IF NOT EXISTS mcp24_attachment_chunks (
            upload_id TEXT NOT NULL REFERENCES mcp24_attachment_uploads(upload_id) ON DELETE CASCADE,
            part_number INTEGER NOT NULL CHECK (part_number >= 0),
            part_sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
            content BLOB,
            PRIMARY KEY(upload_id, part_number)
        );
    """)


def _params_hash(params):
    stable = {key: value for key, value in params.items() if key != "request_id"}
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _actor_identity(actor):
    host_id = actor["host_id"]
    profile = actor["profile"]
    revision = actor["authorization_revision"]
    if not isinstance(host_id, str) or not host_id or not isinstance(profile, str) or not isinstance(revision, int):
        mcp_workflows.resolve_error("unauthorized", "当前连接没有可用于传输的宿主身份。")
    return host_id, profile, revision


def _namespace_request_id(actor, external_id):
    from . import mcp24_access

    if actor["host_id"] in {mcp24_access.LEGACY_HOST_ID, mcp24_access.LOCAL_HOST_ID}:
        return external_id
    return mcp24_access.namespace_id(actor, "request", external_id)


def _owned_export_request_id(actor, export_request_id):
    from . import mcp24_access

    if actor["host_id"] in {mcp24_access.LEGACY_HOST_ID, mcp24_access.LOCAL_HOST_ID}:
        return export_request_id
    match = _NAMESPACED_REQUEST_ID.fullmatch(export_request_id)
    namespace = mcp24_access.host_namespace(actor)
    if match:
        if match.group(1) != namespace:
            mcp_workflows.resolve_error("not_found", "没有找到该宿主的本机 Markdown 导出回执。")
        return export_request_id
    return mcp24_access.namespace_id(actor, "request", export_request_id)


def _now_value(now):
    if isinstance(now, str):
        return datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(timezone.utc)
    return now.astimezone(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_sha(value, field_name):
    if not _SHA256.fullmatch(value):
        mcp_workflows.resolve_error("invalid_params", f"{field_name} 必须是 SHA-256 十六进制值。", field=field_name)
    return value.casefold()


def _export_paths(book, export_request_id, source_channel):
    if book._uri:
        mcp_workflows.resolve_error("not_available", "文件引用只适用于本机 SQLite 数据库。")
    parent = Path(book.db_path).parent.resolve()
    folder = parent / "mcp-exports"
    if folder.is_symlink() or not folder.is_dir() or not folder.resolve().is_relative_to(parent):
        mcp_workflows.resolve_error("not_found", "没有找到该本机导出回执。")
    key = hashlib.sha256(f"export:{source_channel}:{export_request_id}".encode("utf-8")).hexdigest()[:24]
    if not _EXPORT_KEY.fullmatch(key):
        mcp_workflows.resolve_error("not_found", "没有找到该本机导出回执。")
    artifact = folder / f"shuxu-{key}.md"
    manifest = folder / f"shuxu-{key}.json"
    for path in (artifact, manifest):
        if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
            mcp_workflows.resolve_error("forbidden", "无法验证该本机导出文件。")
    return artifact, manifest


def _verified_export(book, export_request_id, source_channel):
    artifact, manifest = _export_paths(book, export_request_id, source_channel)
    if not artifact.is_file() or not manifest.is_file():
        mcp_workflows.resolve_error("not_found", "没有找到该本机导出回执。")
    try:
        receipt = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        mcp_workflows.resolve_error("version_conflict", "本机导出回执不可读取。")
    data = receipt.get("result", {})
    if data.get("request_id") != export_request_id or data.get("format") != "markdown":
        mcp_workflows.resolve_error("not_found", "没有找到该本机 Markdown 导出回执。")
    digest = data.get("sha256")
    size = data.get("bytes")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or not isinstance(size, int) or size < 0:
        mcp_workflows.resolve_error("version_conflict", "本机导出回执缺少有效文件校验信息。")
    if size > MAX_EXPORT_ARTIFACT_BYTES:
        mcp_workflows.resolve_error("invalid_params", "本机导出文件超过可分块取回上限。", max_bytes=MAX_EXPORT_ARTIFACT_BYTES)
    actual_size = artifact.stat().st_size
    if actual_size != size:
        mcp_workflows.resolve_error("version_conflict", "本机导出文件与已提交回执不一致。")
    try:
        with artifact.open("rb") as stream:
            actual_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        mcp_workflows.resolve_error("not_found", "本机导出文件暂时不可读取。")
    if actual_digest != digest.casefold():
        mcp_workflows.resolve_error("version_conflict", "本机导出文件与已提交回执不一致。")
    return artifact, digest.casefold(), size


def _resolve_export(book, export_request_id, source_channel=None):
    if source_channel is not None:
        artifact, digest, size = _verified_export(book, export_request_id, source_channel)
        return artifact, digest, size, source_channel
    matches = []
    for candidate_channel in ("mcp", "local"):
        artifact, manifest = _export_paths(book, export_request_id, candidate_channel)
        if artifact.is_file() and manifest.is_file():
            digest_artifact = _verified_export(book, export_request_id, candidate_channel)
            matches.append((*digest_artifact, candidate_channel))
    if not matches:
        mcp_workflows.resolve_error("not_found", "没有找到该本机导出回执。")
    if len(matches) != 1:
        mcp_workflows.resolve_error("needs_clarification", "该导出 ID 对应多个本机回执，请明确来源通道。",
                                    source_channels=[item[3] for item in matches])
    return matches[0]


def _reference_row(db, actor, artifact_id, now):
    host_id, profile, revision = _actor_identity(actor)
    row = db.execute(
        "SELECT * FROM mcp24_export_artifact_refs WHERE artifact_id = ? AND host_id = ? AND profile = ? AND authorization_revision = ?",
        (artifact_id, host_id, profile, revision),
    ).fetchone()
    if row is None:
        mcp_workflows.resolve_error("not_found", "没有找到当前宿主的文件引用。")
    if row["expires_at"] <= _iso(_now_value(now)):
        mcp_workflows.resolve_error("expired", "该本机文件引用已过期。")
    return row


def _artifact_path(book, row):
    artifact, digest, size = _verified_export(book, row["export_request_id"], row["source_channel"])
    if digest != row["sha256"] or size != row["size_bytes"]:
        mcp_workflows.resolve_error("version_conflict", "本机导出文件与文件引用不一致。")
    return artifact


def _create_export_artifact_reference(book, db, params, channel, validate, now, actor):
    host_id, profile, revision = _actor_identity(actor)
    request_id = _namespace_request_id(actor, params["request_id"])
    details = _params_hash(params)
    existing = db.execute(
        "SELECT * FROM mcp24_export_artifact_refs WHERE host_id = ? AND request_id = ?",
        (host_id, request_id),
    ).fetchone()
    if existing:
        if existing["params_hash"] != details or existing["profile"] != profile or existing["authorization_revision"] != revision:
            mcp_workflows.resolve_error("idempotency_conflict", "request_id 已用于不同的文件引用。", request_id=params["request_id"])
        if existing["expires_at"] <= _iso(_now_value(now)):
            mcp_workflows.resolve_error("expired", "该文件引用已过期；请使用新的 request_id。")
        _artifact_path(book, existing)
        return {"request_id": params["request_id"], "artifact_id": existing["artifact_id"], "format": "markdown",
                "size_bytes": existing["size_bytes"], "sha256": existing["sha256"], "expires_at": existing["expires_at"],
                "max_chunk_bytes": ARTIFACT_CHUNK_BYTES, "committed": True}
    export_request_id = _owned_export_request_id(actor, params["export_request_id"])
    artifact, digest, size, source_channel = _resolve_export(book, export_request_id, params.get("source_channel"))
    artifact_id = uuid.uuid4().hex
    created = _now_value(now)
    expires = created + timedelta(seconds=params["expires_in_seconds"])
    db.execute(
        "INSERT INTO mcp24_export_artifact_refs(artifact_id,host_id,profile,authorization_revision,request_id,params_hash,export_request_id,source_channel,sha256,size_bytes,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (artifact_id, host_id, profile, revision, request_id, details, export_request_id, source_channel,
         digest, size, _iso(created), _iso(expires)),
    )
    return {"request_id": params["request_id"], "artifact_id": artifact_id, "format": "markdown",
            "size_bytes": size, "sha256": digest, "expires_at": _iso(expires),
            "max_chunk_bytes": ARTIFACT_CHUNK_BYTES, "committed": True}


def _get_export_artifact_info(book, db, params, channel, validate, now, actor):
    row = _reference_row(db, actor, params["artifact_id"], now)
    _artifact_path(book, row)
    return {"artifact_id": row["artifact_id"], "format": "markdown", "size_bytes": row["size_bytes"],
            "sha256": row["sha256"], "max_chunk_bytes": ARTIFACT_CHUNK_BYTES, "expires_at": row["expires_at"]}


def _read_export_artifact_chunk(book, db, params, channel, validate, now, actor):
    row = _reference_row(db, actor, params["artifact_id"], now)
    artifact = _artifact_path(book, row)
    offset = params["offset"]
    max_bytes = params["max_bytes"]
    if offset > row["size_bytes"]:
        mcp_workflows.resolve_error("invalid_params", "offset 超出导出文件大小。", size_bytes=row["size_bytes"])
    with artifact.open("rb") as stream:
        stream.seek(offset)
        content = stream.read(max_bytes)
    next_offset = offset + len(content)
    return {"artifact_id": row["artifact_id"], "offset": offset, "size_bytes": row["size_bytes"],
            "chunk_base64": base64.b64encode(content).decode("ascii"), "chunk_sha256": hashlib.sha256(content).hexdigest(),
            "next_offset": next_offset, "eof": next_offset >= row["size_bytes"]}


def _upload_row(db, actor, upload_id):
    host_id, profile, revision = _actor_identity(actor)
    row = db.execute(
        "SELECT * FROM mcp24_attachment_uploads WHERE upload_id = ? AND host_id = ? AND profile = ? AND authorization_revision = ?",
        (upload_id, host_id, profile, revision),
    ).fetchone()
    if row is None:
        mcp_workflows.resolve_error("not_found", "没有找到当前宿主的附件上传。")
    return row


def _require_unexpired(row, now):
    if row["expires_at"] <= _iso(_now_value(now)):
        mcp_workflows.resolve_error("expired", "该附件上传已过期。")


def _total_parts(size_bytes):
    return (size_bytes + UPLOAD_CHUNK_BYTES - 1) // UPLOAD_CHUNK_BYTES


def _upload_status(db, row):
    total = _total_parts(row["size_bytes"])
    parts = [item[0] for item in db.execute(
        "SELECT part_number FROM mcp24_attachment_chunks WHERE upload_id = ? ORDER BY part_number",
        (row["upload_id"],),
    )]
    present = set(parts)
    missing = [number for number in range(total) if number not in present]
    result = json.loads(row["result_json"]) if row["result_json"] else None
    return {"upload_id": row["upload_id"], "status": row["status"], "image_type": row["image_type"],
            "size_bytes": row["size_bytes"], "sha256": row["sha256"], "chunk_bytes": UPLOAD_CHUNK_BYTES,
            "total_parts": total, "received_parts": parts, "missing_parts": missing,
            "expires_at": row["expires_at"], "result": result}


def _start_attachment_upload(book, db, params, channel, validate, now, actor):
    privacy_guard.check_action(db, "start_attachment_upload", params)
    host_id, profile, revision = _actor_identity(actor)
    request_id = _namespace_request_id(actor, params["request_id"])
    expected_sha = _validate_sha(params["sha256"], "sha256")
    cleaned = {key: value for key, value in params.items() if key != "request_id"}
    cleaned["sha256"] = expected_sha
    details = _params_hash({"request_id": params["request_id"], **cleaned})
    existing = db.execute(
        "SELECT * FROM mcp24_attachment_uploads WHERE host_id = ? AND request_id = ?",
        (host_id, request_id),
    ).fetchone()
    if existing:
        if existing["params_hash"] != details or existing["profile"] != profile or existing["authorization_revision"] != revision:
            mcp_workflows.resolve_error("idempotency_conflict", "request_id 已用于不同的附件内容。", request_id=params["request_id"])
        return _upload_status(db, existing)
    if params["size_bytes"] > materials.MAX_IMAGE_BYTES:
        mcp_workflows.resolve_error("invalid_params", "图片不能超过 5 MB。", max_bytes=materials.MAX_IMAGE_BYTES)
    created = _now_value(now)
    expires = created + timedelta(seconds=params["expires_in_seconds"])
    metadata = {key: params[key] for key in ("note", "source", "prompt_id", "direction", "topic") if key in params}
    upload_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO mcp24_attachment_uploads(upload_id,host_id,profile,authorization_revision,request_id,params_hash,image_type,size_bytes,sha256,metadata_json,status,created_at,updated_at,expires_at,result_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
        (upload_id, host_id, profile, revision, request_id, details, params["image_type"], params["size_bytes"], expected_sha,
         json.dumps(metadata, ensure_ascii=False, sort_keys=True), "uploading", _iso(created), _iso(created), _iso(expires)),
    )
    row = db.execute("SELECT * FROM mcp24_attachment_uploads WHERE upload_id = ?", (upload_id,)).fetchone()
    return _upload_status(db, row)


def _put_attachment_upload_chunk(book, db, params, channel, validate, now, actor):
    row = _upload_row(db, actor, params["upload_id"])
    _require_unexpired(row, now)
    privacy_guard.check_action(db, "start_attachment_upload", json.loads(row["metadata_json"]))
    try:
        content = base64.b64decode(params["chunk_base64"], validate=True)
    except (ValueError, binascii.Error):
        mcp_workflows.resolve_error("invalid_params", "分段 base64 内容无效。")
    part_sha = _validate_sha(params["part_sha256"], "part_sha256")
    actual_sha = hashlib.sha256(content).hexdigest()
    if actual_sha != part_sha:
        mcp_workflows.resolve_error("integrity_conflict", "分段内容与声明的 SHA-256 不一致。", part_number=params["part_number"])
    total = _total_parts(row["size_bytes"])
    part_number = params["part_number"]
    if part_number >= total:
        mcp_workflows.resolve_error("invalid_params", "分段编号超出附件范围。", total_parts=total)
    expected_size = min(UPLOAD_CHUNK_BYTES, row["size_bytes"] - part_number * UPLOAD_CHUNK_BYTES)
    if len(content) != expected_size:
        mcp_workflows.resolve_error("invalid_params", "分段大小不符合固定分段边界。", part_number=part_number,
                                    expected_bytes=expected_size)
    if row["status"] == "cancelled":
        mcp_workflows.resolve_error("conflict", "该附件上传已取消。")
    existing = db.execute(
        "SELECT part_sha256,size_bytes FROM mcp24_attachment_chunks WHERE upload_id = ? AND part_number = ?",
        (row["upload_id"], part_number),
    ).fetchone()
    if existing:
        if existing["part_sha256"] != actual_sha or existing["size_bytes"] != len(content):
            mcp_workflows.resolve_error("idempotency_conflict", "同一分段编号已保存不同内容。", part_number=part_number)
        return {"request_id": params["request_id"], "upload_id": row["upload_id"], "part_number": part_number,
                "part_sha256": actual_sha, "size_bytes": len(content), "already_saved": True, "committed": True}
    if row["status"] == "complete":
        mcp_workflows.resolve_error("conflict", "已完成的附件上传不再接受新分段。")
    db.execute(
        "INSERT INTO mcp24_attachment_chunks(upload_id,part_number,part_sha256,size_bytes,content) VALUES(?,?,?,?,?)",
        (row["upload_id"], part_number, actual_sha, len(content), sqlite3.Binary(content)),
    )
    db.execute("UPDATE mcp24_attachment_uploads SET updated_at = ? WHERE upload_id = ?", (_iso(_now_value(now)), row["upload_id"]))
    return {"request_id": params["request_id"], "upload_id": row["upload_id"], "part_number": part_number,
            "part_sha256": actual_sha, "size_bytes": len(content), "already_saved": False, "committed": True}


def _get_attachment_upload_status(book, db, params, channel, validate, now, actor):
    row = _upload_row(db, actor, params["upload_id"])
    return _upload_status(db, row)


def _finish_attachment_upload(book, db, params, channel, validate, now, actor):
    row = _upload_row(db, actor, params["upload_id"])
    if row["status"] == "complete":
        return {"request_id": params["request_id"], "upload_id": row["upload_id"], "status": "complete",
                "result": json.loads(row["result_json"]), "committed": True}
    _require_unexpired(row, now)
    privacy_guard.check_action(db, "start_attachment_upload", json.loads(row["metadata_json"]))
    if row["status"] == "cancelled":
        mcp_workflows.resolve_error("conflict", "该附件上传已取消。")
    parts = db.execute(
        "SELECT part_number,part_sha256,size_bytes,content FROM mcp24_attachment_chunks WHERE upload_id = ? ORDER BY part_number",
        (row["upload_id"],),
    ).fetchall()
    total = _total_parts(row["size_bytes"])
    received = {item["part_number"] for item in parts}
    missing = [number for number in range(total) if number not in received]
    if missing:
        mcp_workflows.resolve_error("upload_incomplete", "附件尚未收齐所有分段。", missing_parts=missing)
    content = b"".join(item["content"] for item in parts)
    actual_sha = hashlib.sha256(content).hexdigest()
    if len(content) != row["size_bytes"] or actual_sha != row["sha256"]:
        mcp_workflows.resolve_error("integrity_conflict", "完整附件与开始时声明的大小或 SHA-256 不一致。",
                                    expected_size_bytes=row["size_bytes"], actual_size_bytes=len(content),
                                    expected_sha256=row["sha256"], actual_sha256=actual_sha)
    if not materials.IMAGE_FORMATS[row["image_type"]](content):
        mcp_workflows.resolve_error("invalid_params", "图片内容与声明的 PNG、JPEG、GIF 或 WebP 类型不一致。")
    metadata = json.loads(row["metadata_json"])
    save_request_id = _namespace_request_id(actor, params["request_id"])
    save_params = {"request_id": save_request_id, "image_type": row["image_type"],
                   "image_data": base64.b64encode(content).decode("ascii"), **metadata}
    result = mcp_workflows.save_chat_screenshot(book, db, save_params, channel, validate, now)
    db.execute(
        "UPDATE mcp24_attachment_uploads SET status='complete',updated_at=?,result_json=? WHERE upload_id=?",
        (_iso(_now_value(now)), json.dumps(result, ensure_ascii=False, sort_keys=True), row["upload_id"]),
    )
    db.execute("UPDATE mcp24_attachment_chunks SET content=NULL WHERE upload_id=?", (row["upload_id"],))
    return {"request_id": params["request_id"], "upload_id": row["upload_id"], "status": "complete",
            "result": result, "committed": True}


def _cancel_attachment_upload(book, db, params, channel, validate, now, actor):
    row = _upload_row(db, actor, params["upload_id"])
    if row["status"] == "complete":
        mcp_workflows.resolve_error("version_conflict", "已完成的截图保存不可取消。", result=json.loads(row["result_json"]))
    if row["status"] == "cancelled":
        return {"request_id": params["request_id"], "upload_id": row["upload_id"], "status": "cancelled", "committed": True}
    db.execute("DELETE FROM mcp24_attachment_chunks WHERE upload_id=?", (row["upload_id"],))
    db.execute(
        "UPDATE mcp24_attachment_uploads SET status='cancelled',updated_at=? WHERE upload_id=?",
        (_iso(_now_value(now)), row["upload_id"]),
    )
    return {"request_id": params["request_id"], "upload_id": row["upload_id"], "status": "cancelled", "committed": True}


HANDLERS = {
    "create_export_artifact_reference": _create_export_artifact_reference,
    "get_export_artifact_info": _get_export_artifact_info,
    "read_export_artifact_chunk": _read_export_artifact_chunk,
    "start_attachment_upload": _start_attachment_upload,
    "put_attachment_upload_chunk": _put_attachment_upload_chunk,
    "get_attachment_upload_status": _get_attachment_upload_status,
    "finish_attachment_upload": _finish_attachment_upload,
    "cancel_attachment_upload": _cancel_attachment_upload,
}


def handle(book, db, action, params, channel, validate, now, actor):
    return HANDLERS[action](book, db, params, channel, validate, now, actor)


class GatewayError(RuntimeError):
    """A transport failure or version mismatch that must not be retried automatically."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _is_loopback_host(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _valid_base_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("gateway URL must be an origin without credentials, path, query, or fragment")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("gateway URL port is invalid") from error
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise ValueError("gateway URL must use HTTPS unless its literal IP address is loopback")
    return f"{parsed.scheme}://{parsed.netloc}"


def _header_token(value, name, *, maximum=200):
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value or len(value) > maximum or not value.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise GatewayError(f"gateway {name} is invalid")
    return value


class GatewayClient:
    """Authenticated client for a single pinned Shuxu service instance."""

    def __init__(self, url, host_id, bearer):
        self.base_url = _valid_base_url(url)
        if not isinstance(host_id, str) or not host_id or not host_id.isascii() or len(host_id) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in host_id):
            raise ValueError("gateway host id is invalid")
        if not isinstance(bearer, str) or not bearer or not bearer.isascii() or len(bearer) > 4096 or any(char.isspace() for char in bearer):
            raise ValueError("gateway bearer credential is invalid")
        self.host_id = host_id
        self._bearer = bearer
        self.identity = None
        proxy_handler = urllib.request.ProxyHandler() if self.base_url.startswith("https://") else urllib.request.ProxyHandler({})
        self._opener = urllib.request.build_opener(proxy_handler, _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    @classmethod
    def from_env(cls):
        values = [os.environ.get(_GATEWAY_URL_ENV, "").strip(), os.environ.get(_GATEWAY_HOST_ENV, "").strip(), os.environ.get(_GATEWAY_BEARER_ENV, "").strip()]
        if not any(values):
            return None
        if not all(values):
            raise ValueError("all SHUXU_GATEWAY_URL, SHUXU_GATEWAY_HOST_ID, and SHUXU_GATEWAY_BEARER values are required")
        return cls(*values)

    def _request(self, method, path, payload=None, request_id=None, upstream_message_id=None, operation_slot=None, timeout=30, if_none_match=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "X-Shuxu-Host-ID": self.host_id,
            "Authorization": f"Bearer {self._bearer}",
        }
        if self.identity:
            headers["X-Shuxu-Expected-Instance-ID"] = self.identity["instance_id"]
        if request_id is not None:
            request_id = str(request_id)
            if not request_id or len(request_id) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in request_id):
                raise GatewayError("gateway request id is invalid")
            if request_id.isascii():
                headers[_REQUEST_ID_HEADER] = request_id
        if upstream_message_id is not None:
            headers[_UPSTREAM_MESSAGE_HEADER] = _header_token(upstream_message_id, "upstream message id")
        if operation_slot is not None:
            headers[_OPERATION_SLOT_HEADER] = _header_token(operation_slot, "operation slot", maximum=128)
        if if_none_match is not None:
            headers["If-None-Match"] = _header_token(if_none_match, "ETag", maximum=512)
        if body is not None:
            if len(body) > MAX_REQUEST_BYTES:
                raise GatewayError("gateway request exceeds the configured size limit")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                status = response.status
                etag = response.headers.get("ETag")
                content = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            etag = error.headers.get("ETag")
            content = b"" if status == HTTPStatus.NOT_MODIFIED else error.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise GatewayError(f"gateway request failed: {type(error).__name__}") from None
        if 300 <= status < 400 and status != HTTPStatus.NOT_MODIFIED:
            raise GatewayError("gateway redirects are not followed")
        if status == HTTPStatus.NOT_MODIFIED:
            return status, {"status": "not_modified", "data": None, "error": None, **({"etag": etag} if etag else {})}
        if len(content) > MAX_RESPONSE_BYTES:
            raise GatewayError("gateway response exceeds the configured size limit")
        try:
            result = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GatewayError("gateway returned a non-JSON response") from None
        if not isinstance(result, dict):
            raise GatewayError("gateway response must be a JSON object")
        if etag:
            result.setdefault("etag", etag)
        return status, result

    def handshake(self):
        from . import mcp24_contract

        status, result = self._request("GET", "/api/mcp24/handshake", timeout=15)
        envelope = result.get("envelope")
        if status != 200 or result.get("status") != "ok" or not isinstance(envelope, dict):
            raise GatewayError("gateway handshake was rejected")
        required = ("protocol_version", "service_version", "instance_id", "directory_id", "authorization_revision")
        if any(key not in envelope for key in required):
            raise GatewayError("gateway handshake omitted service identity fields")
        if envelope["protocol_version"] != mcp24_contract.PROTOCOL_VERSION or envelope["service_version"] != mcp24_contract.SERVICE_VERSION or not all(isinstance(envelope[key], str) and envelope[key] for key in ("service_version", "instance_id", "directory_id")):
            raise GatewayError("gateway protocol or service identity is incompatible")
        self.identity = {key: envelope[key] for key in required}
        return result

    def _check_envelope(self, result):
        envelope = result.get("envelope")
        if not isinstance(envelope, dict):
            raise GatewayError("gateway response omitted the protocol envelope")
        for key, expected in self.identity.items():
            if envelope.get(key) != expected:
                raise GatewayError(f"gateway service identity changed: {key}")

    def call(self, method, params, request_id=None, upstream_message_id=None, operation_slot=None, if_none_match=None):
        from . import mcp_server

        if method not in mcp_server.TOOL_NAMES:
            raise GatewayError("gateway method is not allowlisted")
        if self.identity is None:
            self.handshake()
        timeout = CLIENT_TIMEOUT_SECONDS if method in mcp_server.WRITE_TOOLS else 30
        if method in mcp_server.WRITE_TOOLS:
            body_request_id = params.get("request_id")
            if request_id is not None and body_request_id is not None and str(request_id) != str(body_request_id):
                raise GatewayError("gateway request id must match the tool request_id")
            request_id = body_request_id if body_request_id is not None else request_id
        else:
            request_id = None
        status, result = self._request(
            "POST", f"/api/{method}", params, request_id,
            upstream_message_id=upstream_message_id, operation_slot=operation_slot,
            timeout=timeout, if_none_match=if_none_match,
        )
        if 300 <= status < 400 and status != HTTPStatus.NOT_MODIFIED:
            raise GatewayError("gateway redirects are not followed")
        if status < 400 and status != HTTPStatus.NOT_MODIFIED:
            self._check_envelope(result)
        return result


def _allowed_methods():
    from . import mcp_server

    return mcp_server.TOOL_NAMES


class GatewayRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ShuxuGateway/1"
    sys_version = ""

    def log_message(self, format, *args):
        return

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, status, code, message):
        self._send_json(status, {"status": code, "data": None, "error": {"code": code, "message": message}})

    def _credentials(self, *, require_instance):
        if len(self.headers.get_all("X-Shuxu-Host-ID", [])) != 1 or len(self.headers.get_all("Authorization", [])) != 1:
            self._reject(HTTPStatus.BAD_REQUEST, "invalid_params", "Exactly one named host credential is required.")
            return None
        host_id = self.headers.get("X-Shuxu-Host-ID", "")
        authorization = self.headers.get("Authorization", "")
        if not host_id or not host_id.isascii() or len(host_id) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in host_id) or not authorization.startswith("Bearer ") or not authorization[7:] or not authorization[7:].isascii() or len(authorization) > 4103 or any(char.isspace() for char in authorization[7:]):
            self._reject(HTTPStatus.UNAUTHORIZED, "unauthorized", "Named host credentials are required.")
            return None
        expected = self.headers.get("X-Shuxu-Expected-Instance-ID", "")
        if require_instance and not expected:
            self._reject(HTTPStatus.PRECONDITION_REQUIRED, "version_conflict", "Complete the service handshake before calling a tool.")
            return None
        for name, maximum in ((_UPSTREAM_MESSAGE_HEADER, 200), (_OPERATION_SLOT_HEADER, 128), (_REQUEST_ID_HEADER, 200), ("X-Shuxu-Expected-Instance-ID", 128), ("If-None-Match", 512)):
            values = self.headers.get_all(name, [])
            if len(values) > 1 or (values and (not values[0] or len(values[0]) > maximum or not values[0].isascii() or any(ord(char) < 32 or ord(char) == 127 for char in values[0]))):
                self._reject(HTTPStatus.BAD_REQUEST, "invalid_params", "A gateway metadata header is invalid.")
                return None
        return {
            "X-Shuxu-Host-ID": host_id,
            "Authorization": authorization,
            **({"X-Shuxu-Expected-Instance-ID": expected} if expected else {}),
            **({name: self.headers[name] for name in (_UPSTREAM_MESSAGE_HEADER, _OPERATION_SLOT_HEADER, _REQUEST_ID_HEADER) if name in self.headers}),
            **({"If-None-Match": self.headers["If-None-Match"]} if "If-None-Match" in self.headers else {}),
        }

    def _forward(self, method, path, body, forwarded_headers):
        info = self.server.service_info
        local_url = f"http://127.0.0.1:{info['port']}{path}"
        headers = {"X-Book-Bridge": info["bridge_token"], **forwarded_headers}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(local_url, data=body, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=SERVICE_TIMEOUT_SECONDS) as response:
                status = response.status
                etag = response.headers.get("ETag")
                content = b"" if status == HTTPStatus.NOT_MODIFIED else response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            etag = error.headers.get("ETag")
            content = b"" if status == HTTPStatus.NOT_MODIFIED else error.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError):
            self._reject(HTTPStatus.SERVICE_UNAVAILABLE, "service_unavailable", "The local service could not be reached.")
            return
        if len(content) > MAX_RESPONSE_BYTES:
            self._reject(HTTPStatus.BAD_GATEWAY, "response_too_large", "The local service response is too large.")
            return
        if status != HTTPStatus.NOT_MODIFIED:
            try:
                json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._reject(HTTPStatus.BAD_GATEWAY, "invalid_upstream_response", "The local service returned an invalid response.")
                return
        self.send_response(status)
        if status != HTTPStatus.NOT_MODIFIED:
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/api/mcp24/handshake" or parsed.query or parsed.fragment:
            self._reject(HTTPStatus.NOT_FOUND, "not_found", "Gateway route not found.")
            return
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1 or self.headers.get("Content-Length", "0") != "0":
            self.close_connection = True
            self._reject(HTTPStatus.BAD_REQUEST, "invalid_params", "Handshake requests cannot contain a body.")
            return
        headers = self._credentials(require_instance=False)
        if headers is not None:
            self._forward("GET", parsed.path, None, headers)

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        parts = parsed.path.split("/")
        if parsed.query or parsed.fragment or len(parts) != 3 or parts[1] != "api" or parts[2] not in _allowed_methods():
            self._reject(HTTPStatus.NOT_FOUND, "not_found", "Gateway route not found.")
            return
        headers = self._credentials(require_instance=True)
        if headers is None:
            return
        if self.headers.get_content_type() != "application/json":
            self._reject(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "invalid_params", "Content-Type must be application/json.")
            return
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
            self.close_connection = True
            self._reject(HTTPStatus.BAD_REQUEST, "invalid_params", "Only one Content-Length header is supported.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reject(HTTPStatus.BAD_REQUEST, "invalid_params", "Content-Length must be an integer.")
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self.close_connection = True
            self._reject(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "invalid_params", "Request body is too large.")
            return
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._reject(HTTPStatus.BAD_REQUEST, "invalid_params", "Request body must be valid JSON.")
            return
        if not isinstance(payload, dict):
            self._reject(HTTPStatus.BAD_REQUEST, "invalid_params", "Request body must be a JSON object.")
            return
        self._forward("POST", parsed.path, body, headers)

    def do_HEAD(self):
        self._reject(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Gateway method not allowed.")

    def do_PUT(self):
        self._reject(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Gateway method not allowed.")

    def do_DELETE(self):
        self._reject(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Gateway method not allowed.")


class GatewayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service_info):
        self.address_family = socket.AF_INET6 if isinstance(address[0], str) and ":" in address[0] else socket.AF_INET
        self.service_info = service_info
        super().__init__(address, GatewayRequestHandler)


def _service_info():
    from . import server

    info = server.read_runtime()
    if info is None or not server.verify_runtime(info):
        raise RuntimeError("A verified local service must already be running before the gateway starts.")
    return info


def _run_gateway(args):
    host_ip = ipaddress.ip_address(args.host)
    has_cert = bool(args.certfile)
    has_key = bool(args.keyfile)
    if has_cert != has_key:
        raise ValueError("--certfile and --keyfile must be supplied together")
    if not host_ip.is_loopback and not has_cert:
        raise ValueError("non-loopback gateway listeners require --certfile and --keyfile")
    info = _service_info()
    gateway = GatewayHTTPServer((args.host, args.port), info)
    if has_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.certfile, args.keyfile)
        gateway.socket = context.wrap_socket(gateway.socket, server_side=True)
    scheme = "https" if has_cert else "http"
    display_host = f"[{args.host}]" if ":" in args.host else args.host
    print(f"Gateway listening at {scheme}://{display_host}:{gateway.server_address[1]}", file=sys.stderr)
    try:
        gateway.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        gateway.server_close()
    return 0


def _parse_gateway_args(argv):
    parser = argparse.ArgumentParser(prog="python -m shuxu.mcp24_transport gateway")
    parser.add_argument("--host", required=True, help="literal IP to bind; non-loopback requires TLS")
    parser.add_argument("--port", type=int, default=17832)
    parser.add_argument("--certfile")
    parser.add_argument("--keyfile")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    return parser, args


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "gateway":
        parser, options = _parse_gateway_args(args[1:])
        try:
            return _run_gateway(options)
        except (OSError, ValueError, RuntimeError) as error:
            parser.error(str(error))
    if args and args[0] == "stdio":
        args = args[1:]
    if args:
        raise SystemExit("usage: python -m shuxu.mcp24_transport [stdio|gateway ...]")
    try:
        client = GatewayClient.from_env()
    except ValueError as error:
        LOGGER.error("Gateway configuration is incomplete or invalid: %s", error)
        return 2
    if client is None:
        LOGGER.error("Remote stdio relay requires SHUXU_GATEWAY_URL, SHUXU_GATEWAY_HOST_ID, and SHUXU_GATEWAY_BEARER.")
        return 2
    import asyncio
    from . import mcp_server

    try:
        asyncio.run(mcp_server.run_gateway_stdio(client))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        LOGGER.error("Remote stdio relay stopped: %s", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
