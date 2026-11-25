"""Local-only export, backup and restore for the MCP conversation tools."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import base64
from contextlib import closing
from pathlib import Path

from . import mcp24_ops, mcp_workflows as flow

_BACKUP_NAME = re.compile(r"backup-[0-9a-f]{24}")
_TABLES = ("prompts", "materials", "inspiration_images", "preference_reasons", "mcp_topic_projects", "mcp_claim_sources")
_FULL_EXPORT_BLOCK_SIZE = 65_536


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _hash_file(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _hash_file_blocks(path):
    full_hash = hashlib.sha256()
    blocks = []
    size_bytes = 0
    with path.open("rb") as source:
        while block := source.read(_FULL_EXPORT_BLOCK_SIZE):
            full_hash.update(block)
            blocks.append(hashlib.sha256(block).hexdigest())
            size_bytes += len(block)
    return full_hash.hexdigest(), blocks, size_bytes


def _request_key(action, params, channel):
    return _digest(f"{action}:{channel}:{params['request_id']}".encode("utf-8"))[:24]


def _params_hash(params):
    return _digest(json.dumps(params, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _folder(book, name):
    if book._uri:
        flow.resolve_error("not_available", "这个工具需要实际的本机 SQLite 文件。")
    parent = Path(book.db_path).parent
    path = parent / name
    if path.is_symlink() or not path.resolve().is_relative_to(parent):
        flow.resolve_error("forbidden", "无法验证书序本机目录。")
    path.mkdir(exist_ok=True)
    return path


def _safe_file(folder, filename):
    path = folder / filename
    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
        flow.resolve_error("forbidden", "无法验证本机文件。")
    return path


def _atomic_write(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".shuxu-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _json_file(path, data):
    _atomic_write(path, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _metadata(path):
    return {"local_path": str(path), "sha256": _hash_file(path), "bytes": path.stat().st_size}


def create_full_export(book, db, filters, actor, now, export_id):
    from . import mcp25_read

    folder = _folder(book, "mcp-full-exports")
    refs = mcp25_read._record_refs(book, db, {**filters, "state": None, "page_size": 100})
    selected_refs = set(refs)
    selected_prompt_ids = {identifier for kind, identifier in refs if kind == "prompt"}
    unfiltered_archive = (filters.get("kind", "all") == "all" and
                          not any(filters.get(key) for key in ("prompt_id", "direction", "topic", "created_from", "created_to")))
    descriptors = [(kind, identifier, "record", None) for kind, identifier in refs]
    include_prompt_related = filters.get("kind", "all") in {"all", "prompt"}
    topic_id = filters.get("topic")
    if topic_id:
        topic_id = book._resolve_topic(db, topic_id)["id"]

    if include_prompt_related and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mcp_thought_checkpoints'").fetchone():
        for row in db.execute("""SELECT id, source_conversation_id_public, prompt_id, prompt_version,
            prompt_text, direction, topic_id, cue, unanswered_question, progress, guide_step, created_at
            FROM mcp_thought_checkpoints ORDER BY id"""):
            if not unfiltered_archive and row["prompt_id"] not in selected_prompt_ids:
                continue
            if filters.get("prompt_id") and row["prompt_id"] != filters["prompt_id"]:
                continue
            if filters.get("direction") and row["direction"] != filters["direction"]:
                continue
            if topic_id and row["topic_id"] != topic_id:
                continue
            if filters.get("created_from") and row["created_at"] < filters["created_from"]:
                continue
            if filters.get("created_to") and row["created_at"] >= filters["created_to"]:
                continue
            descriptors.append(("checkpoint", row["id"], "checkpoint", None))

    if include_prompt_related and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mcp_claim_sources'").fetchone():
        for row in db.execute("SELECT prompt_id, text, clipping_id FROM mcp_claim_sources ORDER BY prompt_id, text, clipping_id"):
            if not unfiltered_archive and row["prompt_id"] not in selected_prompt_ids:
                continue
            if filters.get("prompt_id") and row["prompt_id"] != filters["prompt_id"]:
                continue
            prompt = db.execute("SELECT direction,topic_id FROM prompts WHERE id = ?", (row["prompt_id"],)).fetchone()
            if filters.get("direction") and (prompt is None or prompt["direction"] != filters["direction"]):
                continue
            if topic_id and (prompt is None or prompt["topic_id"] != topic_id):
                continue
            identifier = hashlib.sha256(f"{row['prompt_id']}\0{row['text']}\0{row['clipping_id']}".encode("utf-8")).hexdigest()[:32]
            descriptors.append(("claim_source", identifier, "claim_source", (row["prompt_id"], row["text"], row["clipping_id"])))

    if include_prompt_related and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mcp_topic_projects'").fetchone():
        for row in db.execute("SELECT id, title, items_json, version, updated_at FROM mcp_topic_projects ORDER BY id"):
            items = json.loads(row["items_json"])
            scoped_items = items if unfiltered_archive else [
                item for item in items if (item.get("kind"), item.get("id")) in selected_refs
            ]
            if not unfiltered_archive and not scoped_items:
                continue
            if filters.get("created_from") and row["updated_at"] < filters["created_from"]:
                continue
            if filters.get("created_to") and row["updated_at"] >= filters["created_to"]:
                continue
            descriptors.append(("topic_project", row["id"], "topic_project", None if unfiltered_archive else scoped_items))

    descriptors.sort(key=lambda item: (item[0], item[1]))
    attachment_count = sum(
        db.execute("SELECT 1 FROM inspiration_images WHERE material_id = ?", (identifier,)).fetchone() is not None
        for kind, identifier in refs if kind == "inspiration"
    )
    preference_version = db.execute("SELECT version FROM preference_meta WHERE singleton = 1").fetchone()[0]
    directions = [dict(row) for row in db.execute("SELECT * FROM directions ORDER BY id")]
    reasons = [dict(row) for row in db.execute("SELECT item_kind,item_id,scope,reason,updated_at FROM preference_reasons ORDER BY item_kind,item_id,scope")]
    manifest = {"type": "manifest", "format": "shuxu-full-library-v1", "filters": filters,
                "created_at": now.isoformat(timespec="seconds"), "snapshot_version": db.execute(
                    "SELECT revision FROM mcp_read_state WHERE singleton = 1").fetchone()[0],
                "preference_version": preference_version, "directions": directions,
                "preference_reasons": reasons, "record_count": len(descriptors),
                "attachment_count": attachment_count}
    stage_path = None
    with tempfile.NamedTemporaryFile(dir=folder, prefix=".shuxu-full-", suffix=".part", delete=False) as stream:
        stage_path = Path(stream.name)
        file_hash = hashlib.sha256()
        block_hashes = []
        block = bytearray()
        size_bytes = 0

        def write_bytes(raw):
            nonlocal size_bytes
            stream.write(raw)
            file_hash.update(raw)
            size_bytes += len(raw)
            position = 0
            while position < len(raw):
                length = min(_FULL_EXPORT_BLOCK_SIZE - len(block), len(raw) - position)
                block.extend(raw[position:position + length])
                position += length
                if len(block) == _FULL_EXPORT_BLOCK_SIZE:
                    block_hashes.append(hashlib.sha256(block).hexdigest())
                    block.clear()

        encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))

        def write_json_line(value):
            for fragment in encoder.iterencode(value):
                write_bytes(fragment.encode("utf-8"))
            write_bytes(b"\n")

        try:
            write_json_line(manifest)
            for kind, identifier, source, source_ref in descriptors:
                if source == "record":
                    record = mcp25_read._record(book, db, kind, identifier, actor, now)
                    if not unfiltered_archive:
                        for project in record.get("topic_project_progress", []):
                            progress = project["progress"]
                            members = [member for member in progress["members"]
                                       if (member["kind"], member["id"]) in selected_refs]
                            progress["members"] = members
                            progress["gaps"] = [gap for gap in progress["gaps"]
                                                if (gap["member_kind"], gap["member_id"]) in selected_refs]
                            clue = progress["next_clue"]
                            if clue:
                                basis = clue.get("basis", {})
                                selected_clue = ((basis.get("kind"), basis.get("id")) in selected_refs or
                                                 ("prompt", basis.get("prompt_id")) in selected_refs)
                                if not selected_clue:
                                    progress["next_clue"] = None
                            progress["total"] = len(members)
                            progress["next_offset"] = None
                            progress["project"]["total_members"] = len(members)
                            progress["project"]["next_offset"] = None
                    if kind == "inspiration":
                        image = db.execute("SELECT content_type, content FROM inspiration_images WHERE material_id = ?", (identifier,)).fetchone()
                        if image:
                            content = image["content"]
                            record["attachment"] = {"content_type": image["content_type"], "size_bytes": len(content),
                                                     "sha256": hashlib.sha256(content).hexdigest(),
                                                     "data_base64": base64.b64encode(content).decode("ascii")}
                elif source == "checkpoint":
                    record = dict(db.execute("""SELECT id, source_conversation_id_public, prompt_id, prompt_version,
                        prompt_text, direction, topic_id, cue, unanswered_question, progress, guide_step, created_at
                        FROM mcp_thought_checkpoints WHERE id = ?""", (identifier,)).fetchone())
                elif source == "claim_source":
                    prompt_id, text, clipping_id = source_ref
                    record = {"prompt_id": prompt_id, "text": text, "clipping_id": clipping_id}
                else:
                    row = db.execute("SELECT id, title, items_json, version, updated_at FROM mcp_topic_projects WHERE id = ?", (identifier,)).fetchone()
                    items = json.loads(row["items_json"]) if source_ref is None else source_ref
                    record = {"id": row["id"], "title": row["title"], "items": items,
                              "version": row["version"], "updated_at": row["updated_at"]}
                write_json_line({"type": "record", "kind": kind, "id": identifier, "data": record})
            if block:
                block_hashes.append(hashlib.sha256(block).hexdigest())
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            stage_path.unlink(missing_ok=True)
            raise
    return {"path": stage_path, "sha256": file_hash.hexdigest(), "block_hashes": block_hashes,
            "size_bytes": size_bytes, "record_count": len(descriptors), "attachment_count": attachment_count}


def _readonly(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def _copy_database(source, destination):
    deadline = time.monotonic() + 15
    job = mcp24_ops.current_job_context()

    def progress(status, remaining, total):
        if job:
            job.check_cancelled()
        if remaining and time.monotonic() >= deadline:
            flow.resolve_error("temporarily_unavailable", "数据库备份或恢复超时，请稍后核对。")

    source.backup(destination, pages=256, progress=progress, sleep=.05)


def _create_backup(book, folder, backup_id):
    destination = _safe_file(folder, backup_id + ".sqlite3")
    if not destination.is_file():
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".shuxu-backup-", suffix=".sqlite3", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            with closing(book._connect()) as source, closing(sqlite3.connect(temporary)) as target:
                _copy_database(source, target)
            job = mcp24_ops.current_job_context()
            if job:
                job.check_cancelled()
                job.begin_commit()
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        job = mcp24_ops.current_job_context()
        if job:
            job.check_cancelled()
            job.begin_commit()
    return destination


def _backup_path(folder, identifier):
    if not _BACKUP_NAME.fullmatch(identifier):
        flow.resolve_error("invalid_params", "请使用书序返回的备份 ID。")
    path = _safe_file(folder, identifier + ".sqlite3")
    if not path.is_file():
        flow.resolve_error("not_found", "未找到这份本机备份。")
    return path


def _backup_summary(path):
    with closing(_readonly(path)) as db:
        all_tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"directions", "prompts", "request_receipts"} <= all_tables or db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            flow.resolve_error("invalid_backup", "这个文件未通过书序数据库检查。")
        stats = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] if table in all_tables else 0 for table in _TABLES}
    return stats


def _inline(value):
    return " ".join(value.split())


def _export(book, params, channel, now):
    folder = _folder(book, "mcp-exports")
    key = _request_key("export", params, channel)
    artifact = _safe_file(folder, f"shuxu-{key}.md")
    manifest = _safe_file(folder, f"shuxu-{key}.json")
    details = _params_hash(params)
    if manifest.is_file():
        record = json.loads(manifest.read_text("utf-8"))
        if record["parameters"] != details:
            flow.resolve_error("idempotency_conflict", "该 request_id 已用于不同的导出内容。")
        if not artifact.is_file() or _hash_file(artifact) != record["result"]["sha256"]:
            flow.resolve_error("version_conflict", "原导出文件已被移动或更改，请使用新请求重新导出。")
        job = mcp24_ops.current_job_context()
        if job:
            job.check_cancelled()
            job.begin_commit()
        return record["result"]
    scoped = params.get("authorized_excerpts", [])
    refs = {(item["kind"], item["id"]) for item in params["items"]}
    if any((item["kind"], item["id"]) not in refs for item in scoped):
        flow.resolve_error("invalid_params", "授权片段必须属于本次点名的资料。")
    lines = ["# " + _inline(params["title"]), ""]
    omitted = []
    with closing(book._connect()) as db:
        for ref in params["items"]:
            identifier, kind = ref["id"], ref["kind"]
            matching = [item for item in scoped if item["id"] == identifier and item["kind"] == kind]
            lines.extend([f"## {dict(prompt='命题', inspiration='灵感', clipping='剪藏', resume='续接卡')[kind]}", "", f"记录 ID：{_inline(identifier)}", ""])
            if kind == "prompt":
                row = db.execute("SELECT * FROM prompts WHERE id = ?", (identifier,)).fetchone()
                if not row:
                    flow.resolve_error("not_found", "未找到导出的命题。", id=identifier)
                if row["visibility"] != "private" and not matching:
                    lines.extend([row["text"], ""])
                elif not matching:
                    omitted.append(ref)
            else:
                row = db.execute("SELECT * FROM materials WHERE id = ? AND collection = ?", (identifier, kind)).fetchone()
                if not row:
                    flow.resolve_error("not_found", "未找到导出的资料。", id=identifier)
                if kind == "clipping":
                    source = flow._clipping_source(row)
                    lines.extend([_inline(source["title"]), "", f"出处：{_inline(source['source'])} · {source['published_on']}", "", source["url"], ""])
                if not matching:
                    omitted.append(ref)
            for item in matching:
                excerpt = flow.share_excerpt(book, db, {**item, "authorized": True}, channel, None, now)["excerpt"]
                lines.extend([excerpt, ""])
    if omitted:
        lines.extend(["部分私密文字未获本次片段授权，详见导出回执。", ""])
    job = mcp24_ops.current_job_context()
    if job:
        job.check_cancelled()
        job.begin_commit()
    _atomic_write(artifact, "\n".join(lines).encode("utf-8"))
    result = flow.receipt(params, format="markdown", item_count=len(params["items"]), omitted_private_content=omitted, **_metadata(artifact))
    _json_file(manifest, {"parameters": details, "result": result})
    return result


def handle(book, action, params, channel, now):
    if action == "export_thought_bundle":
        return _export(book, params, channel, now)
    folder = _folder(book, "mcp-backups")
    if action == "create_backup":
        identifier = "backup-" + _request_key("backup", params, channel)
        path = _create_backup(book, folder, identifier)
        return flow.receipt(params, backup_id=identifier, includes="SQLite and local screenshots", **_metadata(path))
    if action == "preview_backup":
        identifier = params.get("backup_id")
        if identifier is None:
            items = [entry for entry in folder.glob("backup-*.sqlite3") if _BACKUP_NAME.fullmatch(entry.stem) and entry.is_file() and not entry.is_symlink()]
            if not items:
                flow.resolve_error("not_found", "没有找到本机备份。")
            identifier = max(items, key=lambda item: item.stat().st_mtime).stem
        path = _backup_path(folder, identifier)
        return {"backup_id": identifier, "fingerprint": _hash_file(path), "counts": _backup_summary(path),
                "restore_replaces_current": True, "creates_safety_backup": True,
                "next_call": flow.next_call("restore_backup", {"backup_id": identifier, "fingerprint": _hash_file(path)}, condition="向用户展示预览，收到明确恢复指令后填写 confirmed 和新 request_id")}
    if params["confirmed"] is not True:
        flow.resolve_error("needs_clarification", "恢复需要用户看过预览后的明确确认。", question="确定用这份备份替换当前数据库吗？")
    marker = _safe_file(folder, "restore-" + _request_key("restore", params, channel) + ".json")
    details = _params_hash(params)
    if marker.is_file():
        prior = json.loads(marker.read_text("utf-8"))
        if prior["parameters"] != details:
            flow.resolve_error("idempotency_conflict", "该 request_id 已用于不同的恢复操作。")
        if prior.get("result"):
            return prior["result"]
        flow.resolve_error("submission_unknown", "上次恢复结果需要核对，未再次执行。", safety_backup_id=prior["safety_backup_id"], retry_will_not_restore=True)
    path = _backup_path(folder, params["backup_id"])
    if _hash_file(path) != params["fingerprint"].casefold():
        flow.resolve_error("version_conflict", "备份内容与预览不一致，请重新预览。", next_call=flow.next_call("preview_backup", {"backup_id": params["backup_id"]}))
    counts = _backup_summary(path)
    safety_id = "backup-" + _request_key("safety", params, channel)
    _create_backup(book, folder, safety_id)
    _json_file(marker, {"parameters": details, "safety_backup_id": safety_id})
    with closing(_readonly(path)) as source, closing(book._connect()) as destination:
        _copy_database(source, destination)
    book._initialize()
    result = flow.receipt(params, backup_id=params["backup_id"], safety_backup_id=safety_id, restored_counts=counts)
    _json_file(marker, {"parameters": details, "result": result})
    return result

