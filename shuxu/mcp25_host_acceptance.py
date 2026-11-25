"""Repeatable, synthetic-data MCP stdio acceptance runner."""

from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import closing
import hashlib
import json
import os
import random
import secrets
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import PaginatedRequestParams

from . import mcp24_contract


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER_MODULE = "shuxu.mcp_server"
SERVICE_SCRIPT = PROJECT_ROOT / "shuxu" / "server.py"
IMAGE_CHUNK_BYTES = 65_536
EXPORT_CHUNK_BYTES = 65_536


class AcceptanceFailure(Exception):
    def __init__(self, phase: str, code: str, message: str):
        super().__init__(message)
        self.phase = phase
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _get(value, *names, default=None):
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _tool_schema(item) -> dict:
    schema = _get(item, "inputSchema", "input_schema", default={})
    return schema if isinstance(schema, dict) else {}


def _tool_name(item) -> str:
    return _get(item, "name", default="")


def _payload(result) -> dict:
    structured = _get(result, "structuredContent", "structured_content")
    if isinstance(structured, dict):
        return structured
    for item in _get(result, "content", default=[]) or []:
        text = _get(item, "text")
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {"status": "protocol_error", "data": None,
            "error": {"code": "invalid_tool_result", "message": "MCP returned no structured result."}}


def _safe_parameters(params: dict) -> dict:
    safe = dict(params)
    for key in ("image_data", "chunk_base64"):
        if isinstance(safe.get(key), str):
            encoded = safe.pop(key)
            value = {"redacted": True, "base64_length": len(encoded)}
            if key == "image_data":
                value["sha256"] = _sha256(encoded.encode("ascii"))
            safe[key] = value
    return safe


def _data_summary(data) -> dict:
    if not isinstance(data, dict):
        return {"type": type(data).__name__}
    keys = ("id", "prompt_id", "request_id", "version", "state", "committed", "export_id",
            "record_count", "attachment_count", "size_bytes", "sha256", "chunk_sha256",
            "next_offset", "eof", "total_count", "count", "complete", "next_cursor",
            "full_access", "directory_id", "directory_version", "compatible", "passed",
            "synthetic_only", "found", "error_code", "identity", "checks")
    summary = {key: data[key] for key in keys if key in data}
    for key in ("prompt", "record", "attachment"):
        nested = data.get(key)
        if isinstance(nested, dict):
            nested_summary = {name: nested[name] for name in ("id", "text", "version", "state", "kind", "sha256", "size_bytes") if name in nested}
            if "text" in nested_summary:
                nested_summary["text_sha256"] = _sha256(str(nested_summary.pop("text")).encode("utf-8"))
            summary[key] = nested_summary
    if isinstance(data.get("tools"), list):
        summary["tools_count"] = len(data["tools"])
        summary["tool_names"] = [_tool_name(item) for item in data["tools"]]
    return summary


def _result_summary(payload: dict, result=None) -> dict:
    error = payload.get("error") or {}
    envelope = payload.get("envelope") or {}
    receipt = envelope.get("receipt") if isinstance(envelope, dict) else None
    summary = {"status": payload.get("status"), "error_code": error.get("code"),
               "data": _data_summary(payload.get("data")), "receipt": receipt}
    if result is not None:
        summary["mcp_is_error"] = bool(_get(result, "isError", "is_error", default=False))
    return summary


def _record_step(report: dict, phase: str, name: str, params: dict, result_payload: dict,
                 started: float, result=None, expected: dict | None = None) -> None:
    step = {
        "phase": phase,
        "call": name,
        "parameters": _safe_parameters(params),
        "result": _result_summary(result_payload, result),
        "response_sha256": _sha256(_canonical(result_payload)),
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
    }
    if expected:
        step["expected"] = expected
    report["steps"].append(step)
    if result_payload.get("status") == "ok":
        report["last_successful_step"] = name


async def _call(session: ClientSession, report: dict, name: str, params: dict,
                *, phase: str | None = None, expected: dict | None = None) -> dict:
    started = time.monotonic()
    try:
        result = await session.call_tool(name, params)
        payload = _payload(result)
        _record_step(report, phase or name, name, params, payload, started, result, expected)
        return payload
    except Exception as exc:
        payload = {"status": "transport_error", "data": None,
                   "error": {"code": type(exc).__name__, "message": str(exc)}}
        _record_step(report, phase or name, name, params, payload, started)
        raise AcceptanceFailure(phase or name, type(exc).__name__, str(exc)) from exc


def _require_ok(payload: dict, phase: str) -> dict:
    if payload.get("status") != "ok" or not isinstance(payload.get("data"), dict):
        error = payload.get("error") or {}
        raise AcceptanceFailure(phase, error.get("code") or payload.get("status") or "invalid_result",
                                error.get("message") or f"{phase} did not return status=ok.")
    return payload["data"]


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _png_fixture(run_id: str) -> bytes:
    width = height = 256
    randomizer = random.Random(run_id)
    rows = bytearray()
    for _ in range(height):
        rows.append(0)
        rows.extend(randomizer.randbytes(width * 3))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")


def _seed_synthetic_image(db_path: Path, run_id: str, direction: str) -> dict:
    image = _png_fixture(run_id)
    material_id = f"acceptance-image-{run_id[:48]}"
    now = _utc_now()
    record = {"kind": "screenshot", "text": "Synthetic host acceptance attachment",
              "source": "isolated acceptance fixture", "url": ""}
    with closing(sqlite3.connect(db_path, timeout=10)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("""INSERT INTO materials(id,collection,direction,topic_id,prompt_id,data_json,version,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (material_id, "inspiration", direction, None, None,
             json.dumps(record, ensure_ascii=False, sort_keys=True), 1, now, now))
        db.execute("INSERT INTO inspiration_images(material_id,content_type,content) VALUES(?,?,?)",
                   (material_id, "image/png", image))
        db.commit()
    return {"material_id": material_id, "image_size_bytes": len(image), "image_sha256": _sha256(image),
            "synthetic_only": True}


def _verify_write_evidence(result: dict, sqlite_receipt: bool, expected_id: str,
                           before_version: int | None, after_version: int | None) -> bool:
    receipt = ((result.get("envelope") or {}).get("receipt") or {})
    data = result.get("data") or {}
    prior = before_version or 0
    return (result.get("status") == "ok" and receipt.get("committed") is True and sqlite_receipt
            and data.get("id") == expected_id and isinstance(after_version, int)
            and data.get("version") == after_version and after_version > prior)


def _sqlite_prompt_evidence(db_path: Path, prompt_id: str, request_ids: tuple[str, ...], failed_request_id: str) -> dict:
    with closing(sqlite3.connect(db_path, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        prompt = db.execute("SELECT id,text,state,version,created_at,updated_at FROM prompts WHERE id=?", (prompt_id,)).fetchone()
        receipts = {}
        for request_id in request_ids:
            row = db.execute("SELECT request_id,action,created_at,response_json FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
            receipts[request_id] = None if row is None else {
                "request_id": row["request_id"], "action": row["action"], "created_at": row["created_at"],
                "response_sha256": _sha256(row["response_json"].encode("utf-8")),
            }
        failed = db.execute("SELECT state,result_status,error_code,committed FROM mcp24_request_observations WHERE request_id=?",
                            (failed_request_id,)).fetchone()
        return {"prompt": dict(prompt) if prompt else None, "receipts": receipts,
                "failed_observation": dict(failed) if failed else None}


def _sqlite_named_write_evidence(db_path: Path, prompt_id: str, request_id: str, actor: dict) -> dict:
    from . import mcp24_access

    internal_request_id = mcp24_access.namespace_id(actor, "request", request_id)
    with closing(sqlite3.connect(db_path, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        prompt = db.execute("SELECT id,state,version FROM prompts WHERE id=?", (prompt_id,)).fetchone()
        receipt = db.execute("SELECT request_id,action,created_at,response_json FROM request_receipts WHERE request_id=?",
                             (internal_request_id,)).fetchone()
        public_key = db.execute("SELECT 1 FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        return {
            "prompt": dict(prompt) if prompt else None,
            "receipt": None if receipt is None else {
                "action": receipt["action"], "created_at": receipt["created_at"],
                "response_sha256": _sha256(receipt["response_json"].encode("utf-8")),
            },
            "public_request_key_absent": public_key is None and internal_request_id != request_id,
        }


def _verify_named_write_evidence(result: dict, status_result: dict, sqlite_evidence: dict,
                                 request_id: str, prompt_id: str, before_version: int,
                                 after_version: int) -> bool:
    receipt = ((result.get("envelope") or {}).get("receipt") or {})
    result_data = result.get("data") or {}
    status_data = status_result.get("data") or {}
    prompt = sqlite_evidence.get("prompt") or {}
    stored_receipt = sqlite_evidence.get("receipt") or {}
    return (
        result.get("status") == "ok" and receipt.get("committed") is True
        and receipt.get("request_id") == request_id and receipt.get("action") == "set_prompt_state"
        and result_data.get("id") == prompt_id and result_data.get("version") == after_version
        and after_version > before_version
        and status_result.get("status") == "ok" and status_data.get("request_id") == request_id
        and status_data.get("action") == "set_prompt_state" and status_data.get("state") == "committed"
        and status_data.get("committed") is True
        and stored_receipt.get("action") == "set_prompt_state"
        and prompt.get("id") == prompt_id and prompt.get("version") == after_version
        and prompt.get("state") == "in_progress"
        and sqlite_evidence.get("public_request_key_absent") is True
    )


async def _named_host_session(report: dict, gateway_url: str, base_env: dict, host_id: str,
                              bearer: str, *, full_access: bool, prompt_id: str, prompt_text: str,
                              gateway_port: int, db_path: Path, actor: dict) -> None:
    relay_env = base_env.copy()
    relay_env.update({"SHUXU_GATEWAY_URL": gateway_url, "SHUXU_GATEWAY_HOST_ID": host_id,
                      "SHUXU_GATEWAY_BEARER": bearer})
    parameters = StdioServerParameters(
        command=sys.executable, args=["-X", "utf8", "-B", "-m", "shuxu.mcp24_transport", "stdio"],
        env=relay_env, cwd=PROJECT_ROOT,
    )
    phase_prefix = "named_full" if full_access else "named_limited"
    try:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream, read_timeout_seconds=65) as session:
                started = time.monotonic()
                initialize = await session.initialize()
                server_info = _get(initialize, "serverInfo", "server_info", default={})
                init_data = {"label": phase_prefix, "host_id": host_id,
                             "server_name": _get(server_info, "name", default=""),
                             "service_version": _get(server_info, "version", default=""),
                             "mcp_protocol_version": _get(initialize, "protocolVersion", "protocol_version", default=""),
                             "gateway_url": gateway_url, "gateway_port": gateway_port}
                _record_step(report, f"{phase_prefix}_initialize", "initialize", {}, {
                    "status": "ok", "data": init_data, "error": None,
                }, started)
                tools, schemas = await _list_tools(session, report)
                names = sorted(schemas)
                directory_tools, directory_data = await _list_directory(session, report)
                directory_schemas = {item["name"]: item["inputSchema"] for item in directory_tools}
                missing = sorted(set(schemas) - set(directory_schemas))
                unexpected = sorted(set(directory_schemas) - set(schemas))
                mismatches = sorted(name for name in set(schemas) & set(directory_schemas)
                                    if _canonical(schemas[name]) != _canonical(directory_schemas[name]))
                _expect(not missing and not unexpected and not mismatches,
                        f"{phase_prefix}_discovery", "tool_schema_mismatch",
                        "Named stdio tool schemas did not match the authenticated gateway directory.")
                identity = directory_data.get("identity") or {}
                _expect(identity.get("host_id") == host_id and directory_data.get("full_access") is full_access,
                        f"{phase_prefix}_identity", "host_identity_mismatch",
                        "The named stdio directory returned an unexpected host identity or grant.")
                section = {
                    "host_id": host_id, "profile": identity.get("profile"),
                    "authorization_revision": identity.get("authorization_revision"),
                    "full_access": directory_data.get("full_access"),
                    "tool_count": len(tools), "tool_names": names,
                    "directory_id": directory_data.get("directory_id"),
                    "directory_version": directory_data.get("directory_version"),
                    "schema_sha256": _schema_digest(schemas),
                    "directory_schema_sha256": _directory_digest(directory_tools),
                    "hidden_tools": sorted({"get_full_record"} - set(names)),
                }
                if full_access:
                    read = await _call(session, report, "get_full_record", {
                        "kind": "prompt", "id": prompt_id,
                    }, phase="named_full_read")
                    data = _require_ok(read, "named_full_read")
                    _expect(data.get("record", {}).get("text") == prompt_text
                            and data["record"].get("id") == prompt_id,
                            "named_full_read", "full_host_read_mismatch",
                            "The authenticated full host could not read the synthetic prompt.")
                    section["full_record_readback"] = True
                    prompt_record = data["record"]
                    before_version = prompt_record["version"]
                    _expect(prompt_record.get("state") == "written",
                            "named_full_write", "unexpected_named_write_baseline",
                            "The named full write must start from the synthetic prompt's actual written state.")
                    request_id = f"accept-named-state-{uuid.uuid4().hex}"
                    write_result = await _call(session, report, "set_prompt_state", {
                        "request_id": request_id, "prompt_id": prompt_id,
                        "expected_version": before_version, "state": "in_progress",
                        "reason": "synthetic named-host acceptance transition",
                    }, phase="named_full_write")
                    write_data = _require_ok(write_result, "named_full_write")
                    after_version = write_data.get("version")
                    status_result = await _call(session, report, "get_request_status", {
                        "request_id": request_id,
                    }, phase="named_full_receipt_readback")
                    status_data = _require_ok(status_result, "named_full_receipt_readback")
                    prompt_after = await _call(session, report, "get_prompt", {
                        "prompt_id": prompt_id, "detail": "full",
                    }, phase="named_full_prompt_readback")
                    prompt_after_data = _require_ok(prompt_after, "named_full_prompt_readback").get("prompt", {})
                    sqlite_named = _sqlite_named_write_evidence(db_path, prompt_id, request_id, actor)
                    named_write_passed = _verify_named_write_evidence(
                        write_result, status_result, sqlite_named, request_id, prompt_id,
                        before_version, after_version,
                    )
                    _expect(named_write_passed and prompt_after_data.get("id") == prompt_id
                            and prompt_after_data.get("version") == after_version
                            and prompt_after_data.get("state") == "in_progress",
                            "named_full_write", "named_write_readback_mismatch",
                            "The named full write receipt, request-status readback, prompt version, or SQLite record did not match.")
                    section["full_access_write"] = {
                        "tool": "set_prompt_state", "request_id": request_id, "prompt_id": prompt_id,
                        "before_version": before_version, "after_version": after_version,
                        "state": prompt_after_data["state"],
                        "receipt": ((write_result.get("envelope") or {}).get("receipt") or {}),
                        "request_status": {
                            "state": status_data.get("state"), "committed": status_data.get("committed"),
                            "error_code": status_data.get("error_code"),
                        },
                        "sqlite_receipt": sqlite_named["receipt"],
                        "sqlite_public_request_key_absent": sqlite_named["public_request_key_absent"],
                        "sqlite_prompt": sqlite_named["prompt"],
                        "passed": named_write_passed,
                    }
                else:
                    summary = await _call(session, report, "get_prompt", {
                        "prompt_id": prompt_id,
                    }, phase="named_limited_read")
                    summary_data = _require_ok(summary, "named_limited_read")
                    _expect(summary_data.get("prompt", {}).get("id") == prompt_id,
                            "named_limited_read", "limited_read_mismatch",
                            "The scoped named reader could not read its permitted prompt summary.")
                    denied = await _call(session, report, "get_prompt", {
                        "prompt_id": prompt_id, "detail": "full",
                    }, phase="named_limited_denial", expected={"status": "forbidden", "error_code": "forbidden"})
                    _read_error(denied, "forbidden")
                    hidden = await _call(session, report, "get_full_record", {
                        "kind": "prompt", "id": prompt_id,
                    }, phase="named_hidden_tool", expected={"status": "not_found", "error_code": "not_found"})
                    _read_error(hidden, "not_found")
                    section["scoped_summary_readback"] = True
                    section["full_prompt_read_denied"] = True
                    section["full_record_tool_hidden"] = True
                report.setdefault("named_gateway", {})[phase_prefix] = section
    except AcceptanceFailure:
        raise
    except Exception as exc:
        raise AcceptanceFailure(f"{phase_prefix}_stdio", type(exc).__name__, str(exc)) from exc


async def _run_named_gateway_checks(report: dict, db_path: Path, base_env: dict,
                                   prompt_id: str, prompt_text: str) -> None:
    from . import mcp24_admin

    full_host = f"accept-full-{uuid.uuid4().hex[:10]}"
    limited_host = f"accept-reader-{uuid.uuid4().hex[:10]}"
    full_bearer = secrets.token_urlsafe(48)
    limited_bearer = secrets.token_urlsafe(48)
    full = mcp24_admin.create_host(db_path, host_id=full_host, bearer=full_bearer,
                                   profile="acceptance_full", scopes=["tool:*"])
    limited = mcp24_admin.create_host(db_path, host_id=limited_host, bearer=limited_bearer,
                                      profile="acceptance_reader",
                                      scopes=["tool:get_mcp_directory", "tool:get_prompt"])
    report["named_gateway"] = {
        "transport": "loopback gateway to mcp24_transport stdio",
        "credentials_in_report": False,
        "credentials_passed_to_relay_process_environment": True,
        "full_host_setup": {key: full[key] for key in ("host_id", "profile", "scopes", "authorization_revision")},
        "limited_host_setup": {key: limited[key] for key in ("host_id", "profile", "scopes", "authorization_revision")},
    }
    gateway_port = _free_loopback_port()
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    started = time.monotonic()
    gateway = await asyncio.create_subprocess_exec(
        sys.executable, "-X", "utf8", "-B", "-m", "shuxu.mcp24_transport",
        "gateway", "--host", "127.0.0.1", "--port", str(gateway_port),
        cwd=PROJECT_ROOT, env=base_env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    report.setdefault("cleanup", {})["gateway_pid"] = gateway.pid
    try:
        startup_line = await asyncio.wait_for(gateway.stderr.readline(), timeout=8)
        _expect(bool(startup_line) and gateway.returncode is None,
                "gateway_start", "gateway_not_ready", "The isolated loopback gateway did not start.")
        report["steps"].append({"phase": "gateway_start", "call": "mcp24_transport.gateway",
                                "parameters": {"host": "127.0.0.1", "port": gateway_port},
                                "result": {"status": "ok", "listening": True, "pid": gateway.pid},
                                "response_sha256": _sha256(startup_line.strip()),
                                "elapsed_ms": round((time.monotonic() - started) * 1000, 3)})
        await _named_host_session(report, gateway_url, base_env, full_host, full_bearer,
                                  full_access=True, prompt_id=prompt_id, prompt_text=prompt_text,
                                  gateway_port=gateway_port, db_path=db_path, actor=full)
        await _named_host_session(report, gateway_url, base_env, limited_host, limited_bearer,
                                  full_access=False, prompt_id=prompt_id, prompt_text=prompt_text,
                                  gateway_port=gateway_port, db_path=db_path, actor=limited)
        report["named_gateway"]["full_access_read_write_and_scoped_reader_denial_passed"] = (
            report["named_gateway"]["named_full"]["full_access_write"]["passed"] is True)
    finally:
        started = time.monotonic()
        if gateway.returncode is None:
            gateway.terminate()
            try:
                await asyncio.wait_for(gateway.wait(), timeout=5)
            except asyncio.TimeoutError:
                gateway.kill()
                await gateway.wait()
        stopped = gateway.returncode is not None
        report.setdefault("cleanup", {})["gateway_stopped"] = stopped
        report["steps"].append({"phase": "gateway_cleanup", "call": "stop_isolated_gateway",
                                "parameters": {"pid": gateway.pid},
                                "result": {"status": "ok" if stopped else "failed", "stopped": stopped},
                                "response_sha256": _sha256(_canonical({"stopped": stopped})),
                                "elapsed_ms": round((time.monotonic() - started) * 1000, 3)})
        _expect(stopped, "gateway_cleanup", "gateway_not_stopped",
                "The isolated gateway subprocess did not stop.")


def _tool_map(tools) -> dict[str, dict]:
    return {_tool_name(item): _tool_schema(item) for item in tools}


def _schema_digest(schemas: dict[str, dict]) -> str:
    return _sha256(_canonical({name: schemas[name] for name in sorted(schemas)}))


def _directory_digest(tools: list[dict]) -> str:
    return _schema_digest({item["name"]: item["inputSchema"] for item in tools})


async def _list_tools(session: ClientSession, report: dict) -> tuple[list, dict[str, dict]]:
    items = []
    cursor = None
    while True:
        started = time.monotonic()
        result = await session.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
        page = list(_get(result, "tools", default=[]) or [])
        items.extend(page)
        next_cursor = _get(result, "nextCursor", "next_cursor")
        payload = {"status": "ok", "data": {"count": len(page), "tool_names": [_tool_name(item) for item in page],
                                             "schema_sha256": _schema_digest(_tool_map(page))}, "error": None}
        _record_step(report, "discovery", "tools/list", {"cursor": cursor}, payload, started,
                     expected={"next_cursor": next_cursor})
        if not next_cursor:
            break
        cursor = next_cursor
    return items, _tool_map(items)


async def _list_directory(session: ClientSession, report: dict) -> tuple[list[dict], dict]:
    entries = []
    offset = 0
    directory_data = {}
    while True:
        result = await _call(session, report, "get_mcp_directory", {"limit": 100, "offset": offset}, phase="directory")
        data = _require_ok(result, "directory")
        entries.extend(data.get("tools", []))
        directory_data = data
        if data.get("complete"):
            break
        next_offset = data.get("next_offset")
        if not isinstance(next_offset, int) or next_offset <= offset:
            raise AcceptanceFailure("directory", "invalid_pagination", "get_mcp_directory returned a non-advancing offset.")
        offset = next_offset
    return entries, directory_data


def _expect(condition: bool, phase: str, code: str, message: str) -> None:
    if not condition:
        raise AcceptanceFailure(phase, code, message)


def _read_error(payload: dict, expected_code: str) -> None:
    error = payload.get("error") or {}
    if payload.get("status") != expected_code or error.get("code") != expected_code:
        raise AcceptanceFailure("expected_error", "expected_error_missing",
                                f"Expected {expected_code}, received {error.get('code') or payload.get('status')}.")


async def _run_steps(report: dict, root: Path, port: int) -> None:
    data_dir = root / "data"
    runtime_dir = root / "runtime"
    data_dir.mkdir()
    runtime_dir.mkdir()
    database_path = data_dir / "shuxu.sqlite3"
    env = os.environ.copy()
    for key in ("SHUXU_GATEWAY_URL", "SHUXU_GATEWAY_HOST_ID", "SHUXU_GATEWAY_BEARER"):
        env.pop(key, None)
    env.update({"SHUXU_PORT": str(port), "SHUXU_DATA_DIR": str(data_dir),
                "SHUXU_RUNTIME_DIR": str(runtime_dir), "PYTHONUTF8": "1"})
    report["environment"] = {
        "isolated": True, "transport": "stdio", "service_transport": "loopback",
        "loopback_host": "127.0.0.1", "port": port, "data_dir": str(data_dir),
        "runtime_dir": str(runtime_dir), "database_path": str(database_path), "workspace": str(root),
        "python": sys.version.split()[0], "python_executable": sys.executable,
        "formal_data_dir_used": False, "real_cuteduck_used": False, "real_qq_used": False,
    }
    server_params = StdioServerParameters(
        command=sys.executable, args=["-X", "utf8", "-B", "-m", MCP_SERVER_MODULE],
        env=env, cwd=PROJECT_ROOT,
    )
    cleanup = {"stdio_child_closed": False, "service_stop_requested": False, "service_stopped": False}
    report["cleanup"] = cleanup
    try:
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream, read_timeout_seconds=65) as session:
                started = time.monotonic()
                initialize = await session.initialize()
                server_info = _get(initialize, "serverInfo", "server_info", default={})
                report["host"] = {
                    "label": report["host_label"], "kind": "isolated-mcp-stdio-client",
                    "server_name": _get(server_info, "name", default=""),
                    "service_version": _get(server_info, "version", default=""),
                    "mcp_protocol_version": _get(initialize, "protocolVersion", "protocol_version", default=""),
                }
                _record_step(report, "initialize", "initialize", {}, {"status": "ok", "data": report["host"], "error": None}, started)

                tools, mcp_schemas = await _list_tools(session, report)
                mcp_names = sorted(mcp_schemas)
                report["tool_catalog"] = {"mcp_tools_total": len(tools), "mcp_tool_names": mcp_names,
                                          "mcp_schema_sha256": _schema_digest(mcp_schemas)}
                _expect(len(tools) > 0 and len(mcp_names) == len(set(mcp_names)), "discovery", "invalid_tool_catalog",
                        "tools/list returned no tools or duplicate names.")
                _expect("get_mcp_directory" in mcp_schemas, "discovery", "missing_directory_tool",
                        "The discovered catalog does not contain get_mcp_directory.")

                directory_tools, directory_data = await _list_directory(session, report)
                directory_schemas = {item["name"]: item["inputSchema"] for item in directory_tools}
                missing = sorted(set(mcp_schemas) - set(directory_schemas))
                unexpected = sorted(set(directory_schemas) - set(mcp_schemas))
                schema_mismatches = sorted(name for name in set(mcp_schemas) & set(directory_schemas)
                                           if _canonical(mcp_schemas[name]) != _canonical(directory_schemas[name]))
                report["tool_catalog"].update({
                    "directory_tools_total": len(directory_tools), "directory_schema_sha256": _directory_digest(directory_tools),
                    "directory_id": directory_data.get("directory_id"), "directory_version": directory_data.get("directory_version"),
                    "directory_identity": directory_data.get("identity"), "full_access": directory_data.get("full_access"),
                    "missing_from_directory": missing, "unexpected_in_directory": unexpected,
                    "schema_mismatches": schema_mismatches,
                })
                _expect(not missing and not unexpected and not schema_mismatches,
                        "catalog_compare", "tool_schema_mismatch", "The actual tools/list catalog differs from get_mcp_directory.")

                expected_service = report["host"]["service_version"]
                diagnose = await _call(session, report, "diagnose_mcp_compatibility", {
                    "expected_protocol_version": mcp24_contract.PROTOCOL_VERSION,
                    "expected_service_version": expected_service,
                    "expected_directory_id": directory_data["directory_id"],
                }, phase="compatibility")
                diagnose_data = _require_ok(diagnose, "compatibility")
                _expect(diagnose_data.get("compatible") is True and diagnose_data.get("service_version") == expected_service,
                        "compatibility", "incompatible_service", "The loopback service reported an incompatible version or directory.")
                report["host"]["service_protocol_version"] = diagnose_data.get("protocol_version")
                report["host"]["service_directory_id"] = diagnose_data.get("directory_id")

                self_check = await _call(session, report, "run_mcp_self_check", {}, phase="self_check")
                self_check_data = _require_ok(self_check, "self_check")
                _expect(self_check_data.get("passed") is True and self_check_data.get("synthetic_only") is True
                        and self_check_data.get("writes_performed") is False,
                        "self_check", "self_check_failed", "The service self-check failed or was not synthetic-only.")

                direction = mcp_schemas["draft_prompt"].get("properties", {}).get("direction", {}).get("enum", [None])[0]
                _expect(isinstance(direction, str), "prompt_flow", "direction_schema_missing",
                        "draft_prompt has no usable direction enum.")
                fixture = _seed_synthetic_image(database_path, report["run_id"], direction)
                report["fixture"] = fixture
                report["steps"].append({"phase": "fixture", "call": "seed_synthetic_image", "parameters": {
                    "material_id": fixture["material_id"], "direction": direction,
                    "image_size_bytes": fixture["image_size_bytes"], "image_sha256": fixture["image_sha256"],
                }, "result": {"status": "ok", "synthetic_only": True},
                    "response_sha256": _sha256(_canonical(fixture)), "elapsed_ms": 0.0})
                report["last_successful_step"] = "seed_synthetic_image"

                draft_result = await _call(session, report, "draft_prompt", {
                    "direction": direction, "focus": f"isolated host acceptance {report['run_id']}", "depth": "light",
                }, phase="prompt_flow")
                draft = _require_ok(draft_result, "prompt_flow").get("prompt", {})
                _expect(bool(draft.get("text")) and draft.get("draft") is True,
                        "prompt_flow", "invalid_draft", "draft_prompt did not return an unsaved prompt.")
                save_request_id = f"accept-save-{uuid.uuid4().hex}"
                save_result = await _call(session, report, "save_prompt", {
                    "request_id": save_request_id, "text": draft["text"], "direction": direction,
                    "depth": "light", "state": "in_progress",
                }, phase="prompt_write")
                save_data = _require_ok(save_result, "prompt_write")
                prompt_id = save_data.get("id")
                save_receipt = ((save_result.get("envelope") or {}).get("receipt") or {})
                _expect(isinstance(prompt_id, str) and save_receipt.get("committed") is True,
                        "prompt_write", "missing_write_receipt", "The prompt save lacked its real committed receipt.")

                save_status = await _call(session, report, "get_request_status", {"request_id": save_request_id}, phase="receipt_readback")
                status_data = _require_ok(save_status, "receipt_readback")
                _expect(status_data.get("found") is True and status_data.get("committed") is True
                        and status_data.get("state") == "committed",
                        "receipt_readback", "receipt_not_found", "The saved request receipt could not be read back as committed.")

                before_version = int(save_data.get("version", 0))
                state_request_id = f"accept-state-{uuid.uuid4().hex}"
                state_result = await _call(session, report, "set_prompt_state", {
                    "request_id": state_request_id, "prompt_id": prompt_id,
                    "expected_version": before_version, "state": "written",
                    "reason": "synthetic acceptance state transition",
                }, phase="prompt_update")
                state_data = _require_ok(state_result, "prompt_update")
                state_receipt = ((state_result.get("envelope") or {}).get("receipt") or {})
                _expect(state_receipt.get("committed") is True and state_data.get("version") == before_version + 1,
                        "prompt_update", "version_not_increased",
                        "The state update lacked a committed receipt or did not increase the version.")

                prompt_read = await _call(session, report, "get_prompt", {"prompt_id": prompt_id, "detail": "full"}, phase="prompt_readback")
                prompt_data = _require_ok(prompt_read, "prompt_readback").get("prompt", {})
                _expect(prompt_data.get("id") == prompt_id and prompt_data.get("text") == draft["text"]
                        and prompt_data.get("version") == before_version + 1 and prompt_data.get("state") == "written",
                        "prompt_readback", "prompt_mismatch",
                        "The MCP prompt readback did not match the saved full record and version.")
                full_prompt = await _call(session, report, "get_full_record", {"kind": "prompt", "id": prompt_id}, phase="full_record")
                full_prompt_data = _require_ok(full_prompt, "full_record")
                _expect(full_prompt_data.get("record", {}).get("id") == prompt_id
                        and full_prompt_data["record"].get("version") == before_version + 1,
                        "full_record", "full_record_mismatch", "get_full_record did not return the saved prompt version.")
                state_status = await _call(session, report, "get_request_status", {"request_id": state_request_id}, phase="receipt_readback")
                state_status_data = _require_ok(state_status, "receipt_readback")
                _expect(state_status_data.get("committed") is True and state_status_data.get("state") == "committed",
                        "receipt_readback", "receipt_not_found", "The prompt update receipt could not be read back as committed.")

                image_record = await _call(session, report, "get_full_record", {
                    "kind": "inspiration", "id": fixture["material_id"],
                }, phase="attachment_record")
                image_record_data = _require_ok(image_record, "attachment_record")
                attachment_info = image_record_data.get("attachment") or {}
                _expect(attachment_info.get("sha256") == fixture["image_sha256"]
                        and attachment_info.get("size_bytes") == fixture["image_size_bytes"],
                        "attachment_record", "attachment_metadata_mismatch",
                        "The full record did not match the synthetic attachment metadata.")

                image_content = bytearray()
                image_chunks = []
                offset = 0
                while True:
                    chunk_result = await _call(session, report, "get_attachment_chunk", {
                        "id": fixture["material_id"], "offset": offset, "max_bytes": IMAGE_CHUNK_BYTES,
                    }, phase="attachment_chunks")
                    chunk_data = _require_ok(chunk_result, "attachment_chunks")
                    raw_chunk = base64.b64decode(chunk_data["chunk_base64"], validate=True)
                    _expect(chunk_data.get("chunk_sha256") == _sha256(raw_chunk)
                            and chunk_data.get("sha256") == fixture["image_sha256"],
                            "attachment_chunks", "chunk_checksum_mismatch",
                            "An original-image chunk checksum did not match.")
                    _expect(chunk_data.get("offset") == offset
                            and chunk_data.get("next_offset") == offset + len(raw_chunk),
                            "attachment_chunks", "chunk_offset_mismatch",
                            "The attachment chunk offsets were inconsistent.")
                    image_chunks.append({"offset": offset, "size_bytes": len(raw_chunk), "sha256": _sha256(raw_chunk)})
                    image_content.extend(raw_chunk)
                    offset = chunk_data["next_offset"]
                    if chunk_data.get("eof"):
                        break
                    _expect(bool(raw_chunk), "attachment_chunks", "empty_chunk", "An attachment read made no progress.")
                reassembled_sha = _sha256(bytes(image_content))
                _expect(offset == fixture["image_size_bytes"] and reassembled_sha == fixture["image_sha256"],
                        "attachment_chunks", "image_checksum_mismatch",
                        "The reassembled original image size or SHA-256 did not match.")
                report["attachment"] = {"id": fixture["material_id"], "size_bytes": offset,
                                        "original_sha256": fixture["image_sha256"],
                                        "reassembled_sha256": reassembled_sha, "chunks": image_chunks}

                export_request_id = f"accept-export-{uuid.uuid4().hex}"
                exported = await _call(session, report, "export_full_library", {
                    "request_id": export_request_id, "kind": "inspiration",
                }, phase="full_export")
                export_data = _require_ok(exported, "full_export")
                _expect(export_data.get("committed") is True and export_data.get("attachment_count") == 1,
                        "full_export", "export_not_committed",
                        "The synthetic attachment export lacked its commit receipt or attachment count.")
                export_receipt = ((exported.get("envelope") or {}).get("receipt") or {})
                _expect(export_receipt.get("committed") is True,
                        "full_export", "missing_export_receipt", "The full export lacked its real committed receipt.")
                info_result = await _call(session, report, "get_full_export_info", {"export_id": export_data["export_id"]}, phase="export_info")
                info = _require_ok(info_result, "export_info")
                _expect(info.get("sha256") == export_data.get("sha256")
                        and info.get("size_bytes") == export_data.get("size_bytes"),
                        "export_info", "export_info_mismatch", "The export info did not match the committed export.")

                export_bytes = bytearray()
                export_chunks = []
                export_offset = 0
                while True:
                    chunk_result = await _call(session, report, "read_full_export_chunk", {
                        "export_id": export_data["export_id"], "offset": export_offset,
                        "max_bytes": EXPORT_CHUNK_BYTES,
                    }, phase="export_chunks")
                    chunk_data = _require_ok(chunk_result, "export_chunks")
                    raw_chunk = base64.b64decode(chunk_data["chunk_base64"], validate=True)
                    _expect(chunk_data.get("chunk_sha256") == _sha256(raw_chunk),
                            "export_chunks", "export_chunk_checksum_mismatch",
                            "A full-export chunk checksum did not match.")
                    export_chunks.append({"offset": export_offset, "size_bytes": len(raw_chunk), "sha256": _sha256(raw_chunk)})
                    export_bytes.extend(raw_chunk)
                    export_offset = chunk_data["next_offset"]
                    if chunk_data.get("eof"):
                        break
                    _expect(bool(raw_chunk), "export_chunks", "empty_chunk", "A full-export chunk read made no progress.")
                export_sha = _sha256(bytes(export_bytes))
                _expect(export_sha == export_data["sha256"] and len(export_bytes) == export_data["size_bytes"],
                        "export_chunks", "export_checksum_mismatch",
                        "The reassembled full export failed size or SHA-256 validation.")
                export_lines = [json.loads(line) for line in export_bytes.decode("utf-8").splitlines() if line]
                exported_image = next((item for item in export_lines if item.get("id") == fixture["material_id"]), None)
                _expect(exported_image is not None, "export_contents", "attachment_not_exported",
                        "The full export did not contain the synthetic attachment record.")
                exported_image_bytes = base64.b64decode(exported_image["data"]["attachment"]["data_base64"], validate=True)
                exported_image_sha = _sha256(exported_image_bytes)
                _expect(exported_image_sha == fixture["image_sha256"], "export_contents", "exported_image_checksum_mismatch",
                        "The exported original image did not match the source image SHA-256.")
                report["export"] = {"export_id": export_data["export_id"], "request_id": export_request_id,
                                    "record_count": export_data["record_count"], "attachment_count": export_data["attachment_count"],
                                    "size_bytes": len(export_bytes), "sha256": export_sha,
                                    "image_sha256": exported_image_sha, "chunks": export_chunks}

                range_error = await _call(session, report, "get_prompt_timeline", {
                    "prompt_ids": [prompt_id], "from": "2030-01-02T00:00:00Z", "through": "2020-01-02T00:00:00Z",
                }, phase="range_error", expected={"status": "invalid_params", "error_code": "invalid_params"})
                _read_error(range_error, "invalid_params")

                failed_request_id = f"accept-fail-{uuid.uuid4().hex}"
                failed_write = await _call(session, report, "save_prompt", {
                    "request_id": failed_request_id, "text": "Synthetic expected failure",
                    "direction": "not-a-real-direction",
                }, phase="failure_receipt", expected={"status": "invalid_params", "error_code": "invalid_params"})
                _read_error(failed_write, "invalid_params")
                failure_status_result = await _call(session, report, "get_request_status", {
                    "request_id": failed_request_id,
                }, phase="failure_receipt_readback")
                failure_status = _require_ok(failure_status_result, "failure_receipt_readback")
                _expect(failure_status.get("found") is True and failure_status.get("committed") is False
                        and failure_status.get("state") == "failed"
                        and failure_status.get("error_code") == "invalid_params",
                        "failure_receipt_readback", "failure_status_mismatch",
                        "The failed write status did not retain the actual failure code without a commit.")

                sqlite_evidence = _sqlite_prompt_evidence(database_path, prompt_id,
                    (save_request_id, state_request_id, export_request_id), failed_request_id)
                report["sqlite"] = {"prompt": sqlite_evidence["prompt"], "receipts": sqlite_evidence["receipts"],
                                    "failed_observation": sqlite_evidence["failed_observation"]}
                _expect(sqlite_evidence["prompt"] is not None
                        and sqlite_evidence["prompt"]["version"] == before_version + 1
                        and sqlite_evidence["prompt"]["state"] == "written",
                        "sqlite_readback", "sqlite_prompt_mismatch",
                        "SQLite did not contain the expected final prompt version/state.")
                save_sqlite_receipt = sqlite_evidence["receipts"].get(save_request_id)
                state_sqlite_receipt = sqlite_evidence["receipts"].get(state_request_id)
                export_sqlite_receipt = sqlite_evidence["receipts"].get(export_request_id)
                _expect(save_sqlite_receipt and save_sqlite_receipt["action"] == "save_prompt"
                        and state_sqlite_receipt and state_sqlite_receipt["action"] == "set_prompt_state"
                        and export_sqlite_receipt and export_sqlite_receipt["action"] == "export_full_library",
                        "sqlite_receipt", "sqlite_receipt_missing", "The actual write receipts were absent from SQLite.")
                _expect(sqlite_evidence["failed_observation"] is not None
                        and sqlite_evidence["failed_observation"]["state"] == "failed"
                        and sqlite_evidence["failed_observation"]["committed"] == 0
                        and failed_request_id not in sqlite_evidence["receipts"],
                        "sqlite_failure", "failure_observation_mismatch",
                        "SQLite did not retain a failed, uncommitted observation.")

                save_gate = _verify_write_evidence(
                    save_result, bool(save_sqlite_receipt), prompt_id, 0, save_data.get("version"),
                )
                state_gate = _verify_write_evidence(
                    state_result, bool(state_sqlite_receipt), prompt_id, before_version,
                    sqlite_evidence["prompt"]["version"],
                )
                report["checks"] = {
                    "catalog_matches_directory": True,
                    "compatibility_passed": diagnose_data.get("compatible") is True,
                    "synthetic_self_check_passed": self_check_data.get("passed") is True,
                    "mcp_write_receipt_committed": save_gate and state_gate,
                    "sqlite_receipt_present": bool(save_sqlite_receipt),
                    "prompt_version_increased": before_version + 1 == sqlite_evidence["prompt"]["version"],
                    "full_record_readback": full_prompt_data["record"]["version"] == sqlite_evidence["prompt"]["version"],
                    "image_chunks_match_original": reassembled_sha == fixture["image_sha256"],
                    "export_chunks_and_image_match": export_sha == export_data["sha256"] and exported_image_sha == fixture["image_sha256"],
                    "real_range_error_reported": range_error.get("status") == "invalid_params",
                    "failed_request_status_readback": failure_status.get("state") == "failed",
                }
                await _run_named_gateway_checks(report, database_path, env, prompt_id, draft["text"])
                report["checks"]["named_full_and_limited_reader_access_passed"] = (
                    report["named_gateway"].get("full_access_read_write_and_scoped_reader_denial_passed") is True)
                fake_success = {"status": "ok", "data": {"id": prompt_id, "version": before_version + 1},
                                "envelope": {"receipt": {"committed": True}}}
                report["checks"]["fake_success_without_sqlite_receipt_or_version_growth_rejected"] = not _verify_write_evidence(
                    fake_success, False, prompt_id, before_version + 1, before_version + 1)
                _expect(all(report["checks"].values()), "acceptance_checks", "acceptance_check_failed",
                        "One or more isolated host acceptance checks did not pass.")
                report["status"] = "passed"
                report["summary"] = "Isolated stdio discovery, local and named-host writes/readbacks, receipts, full record, image chunks, export, and failure reporting passed."
    except AcceptanceFailure as exc:
        report["status"] = "failed"
        report["failure"] = {"phase": exc.phase, "error_code": exc.code, "message": str(exc),
                             "last_successful_step": report.get("last_successful_step")}
    except Exception as exc:
        report["status"] = "failed"
        report["failure"] = {"phase": "runner", "error_code": type(exc).__name__,
                             "message": str(exc), "last_successful_step": report.get("last_successful_step")}
    finally:
        cleanup["stdio_child_closed"] = True
        cleanup.update(await asyncio.to_thread(_stop_isolated_service, runtime_dir, port, env))


def _process_alive(pid: int) -> bool:
    from .server import process_is_alive

    return process_is_alive(pid)


def _stop_isolated_service(runtime_dir: Path, port: int, env: dict) -> dict:
    runtime_file = runtime_dir / "service.json"
    if not runtime_file.is_file():
        return {"service_stop_requested": False, "service_stopped": True, "service_cleanup": "not_started"}
    try:
        identity = json.loads(runtime_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"service_stop_requested": False, "service_stopped": False, "service_cleanup": "runtime_identity_unreadable"}
    if identity.get("port") != port or not isinstance(identity.get("pid"), int) or not identity.get("instance_id"):
        return {"service_stop_requested": False, "service_stopped": False, "service_cleanup": "runtime_identity_mismatch"}
    stopped = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", str(SERVICE_SCRIPT), "stop"],
        cwd=PROJECT_ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, timeout=8, check=False,
    )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and (runtime_file.exists() or _process_alive(identity["pid"])):
        time.sleep(0.1)
    if runtime_file.exists():
        try:
            current = json.loads(runtime_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            current = {}
        if current.get("instance_id") == identity["instance_id"] and current.get("pid") == identity["pid"]:
            health_request = Request(f"http://127.0.0.1:{port}/api/health",
                                    headers={"X-Book-Bridge": identity.get("bridge_token", "")})
            try:
                with urlopen(health_request, timeout=1) as response:
                    health = json.load(response)
                if health.get("data", {}).get("instance_id") == identity["instance_id"]:
                    os.kill(identity["pid"], signal.SIGTERM)
            except (OSError, ValueError, ProcessLookupError):
                pass
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and (runtime_file.exists() or _process_alive(identity["pid"])):
                time.sleep(0.1)
    service_stopped = not runtime_file.exists() and not _process_alive(identity["pid"])
    return {"service_stop_requested": stopped.returncode == 0, "service_stopped": service_stopped,
            "service_cleanup": "stopped" if service_stopped else "failed",
            "service_stop_return_code": stopped.returncode}


def run_acceptance(run_id: str, *, host_label: str = "isolated-local-stdio",
                   output_path: str | Path | None = None) -> dict:
    if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 160:
        raise ValueError("run_id must be a non-empty string of at most 160 characters")
    report = {"format_version": 1, "run_id": run_id, "host_label": host_label,
              "started_at": _utc_now(), "status": "running", "steps": [],
              "reproduction": {"command": "py -3.12 -X utf8 -B -m shuxu.mcp25_host_acceptance --run-id <RUN_ID> --output <REPORT.json>"}}
    root = Path(tempfile.mkdtemp(prefix="shuxu-host-acceptance-"))
    port = _free_loopback_port()
    report["environment"] = {"workspace": str(root)}
    try:
        asyncio.run(_run_steps(report, root, port))
    except Exception as exc:
        report["status"] = "failed"
        report["failure"] = {"phase": "runner", "error_code": type(exc).__name__, "message": str(exc),
                             "last_successful_step": report.get("last_successful_step")}
    finally:
        cleanup = report.get("cleanup", {})
        if cleanup.get("service_stopped") is True:
            import shutil
            shutil.rmtree(root)
            cleanup["workspace_removed"] = True
        else:
            cleanup["workspace_removed"] = False
            report["status"] = "failed"
            report["failure"] = {"phase": "cleanup", "error_code": "isolated_service_not_stopped",
                                 "message": "The isolated service could not be verified as stopped; its temporary workspace was preserved.",
                                 "last_successful_step": report.get("last_successful_step")}
    report["finished_at"] = _utc_now()
    report.pop("host_label", None)
    if output_path is not None:
        path = Path(output_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["report_path"] = str(path)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the synthetic isolated stdio host acceptance flow.")
    parser.add_argument("--run-id", required=True, help="RUN/task identifier copied into the report.")
    parser.add_argument("--host-label", default="isolated-local-stdio",
                        help="Report label; does not connect an external host.")
    parser.add_argument("--output", type=Path, help="Optional path for the complete JSON report.")
    args = parser.parse_args(argv)
    try:
        report = run_acceptance(args.run_id, host_label=args.host_label, output_path=args.output)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "failed", "error_code": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
