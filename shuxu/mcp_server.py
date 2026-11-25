from __future__ import annotations

import asyncio
import json
import logging
import sys
import urllib.error
import urllib.request
from base64 import b64encode
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import mcp_types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from shuxu import mcp24_contract
from shuxu.book import TOOL_SPECS, _WRITE_ACTIONS
from shuxu.server import WEB_DIR, ensure_server

LOGGER = logging.getLogger("shuxu.mcp")
HOST_PROMPTS = PROJECT_ROOT / "config" / "mcp-prompts"
HOST_INSTRUCTIONS = "\n\n".join((HOST_PROMPTS / name).read_text(encoding="utf-8") for name in (
    "01-host-role.txt", "02-tool-routing.txt", "03-materials-and-full-text.txt",
    "04-admin-operations.txt", "05-timeouts-and-recovery.txt", "06-host-acceptance.txt",
))
ICON_DATA = b64encode((WEB_DIR / "shuxu-icon.png").read_bytes()).decode("ascii")
SERVER_ICON = mcp_types.Icon(src=f"data:image/png;base64,{ICON_DATA}", mime_type="image/png", sizes=["64x64"])
FILE_WRITES = {"export_thought_bundle", "export_full_library", "create_backup", "restore_backup"}
WRITE_TOOLS = _WRITE_ACTIONS | FILE_WRITES
def tools_from_specs(specs):
    return [
        mcp_types.Tool(
            name=item["name"], description=item["description"], inputSchema=item["inputSchema"],
            annotations={"readOnlyHint": item["name"] not in WRITE_TOOLS,
                         "destructiveHint": item["name"] in {"restore_backup", "undo_action", "apply_actions", "remove_inspiration", "remove_clipping", "clear_resume_card", "apply_retention_cleanup"},
                         "idempotentHint": item["name"] in WRITE_TOOLS, "openWorldHint": False},
        )
        for item in specs
    ]


TOOLS = tools_from_specs(TOOL_SPECS)
TOOL_NAMES = {item["name"] for item in TOOL_SPECS}


def call_local_service(service: dict, name: str, arguments: dict) -> dict:
    body = json.dumps(arguments, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{service['port']}/api/{name}",
        data=body,
        headers={"Content-Type": "application/json", "X-Book-Bridge": service["bridge_token"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=65 if name in FILE_WRITES else 30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.headers.get_content_type() == "application/json":
            return json.load(error)
        raise


async def list_tools(ctx, params):
    return mcp_types.ListToolsResult(tools=TOOLS)


def result_payload(result, *, protocol_error=False):
    encoded = json.dumps(result, ensure_ascii=False)
    return mcp_types.CallToolResult(content=[mcp_types.TextContent(text=encoded)], structuredContent=result, isError=protocol_error)


async def call_tool(ctx, params):
    if params.name not in TOOL_NAMES:
        result = {"status": "not_found", "data": None, "error": {"code": "not_found", "message": "Tool not found."}}
        return result_payload(result, protocol_error=True)
    arguments = params.arguments or {}
    try:
        service = await asyncio.to_thread(ensure_server)
    except Exception as exc:
        LOGGER.error("Local service unavailable: %s", type(exc).__name__)
        return result_payload({"status": "service_unavailable", "error": {"code": "service_unavailable", "message": "未能连接书序本机服务。", "details": {"dispatched": False}}})
    try:
        result = await asyncio.to_thread(call_local_service, service, params.name, arguments)
    except Exception as exc:
        LOGGER.error("Local call failed for %s: %s", params.name, type(exc).__name__)
        if params.name in WRITE_TOOLS:
            return result_payload({"status": "submission_unknown", "error": {"code": "submission_unknown", "message": "暂时无法确认该操作是否提交。", "details": {"request_id": arguments.get("request_id"), "retry_with_same_arguments": True}}})
        return result_payload({"status": "service_unavailable", "error": {"code": "service_unavailable", "message": "无法读取书序本机结果。", "details": {}}})
    return result_payload(result)


async def run() -> None:
    await asyncio.to_thread(ensure_server)
    server = Server("book-sequence", version=mcp24_contract.SERVICE_VERSION, title="书序", instructions=HOST_INSTRUCTIONS,
                    icons=[SERVER_ICON], on_list_tools=list_tools, on_call_tool=call_tool)
    server.middleware.clear()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


async def run_gateway_stdio(client) -> None:
    handshake = await asyncio.to_thread(client.handshake)
    catalog = handshake["data"]["tool_directory"]
    tools = tools_from_specs(catalog)
    names = {item["name"] for item in catalog}

    async def remote_list_tools(ctx, params):
        return mcp_types.ListToolsResult(tools=tools)

    async def remote_call_tool(ctx, params):
        if params.name not in names:
            result = {"status": "not_found", "data": None, "error": {"code": "not_found", "message": "Tool not found."}}
            return result_payload(result, protocol_error=True)
        arguments = params.arguments or {}
        metadata = getattr(params, "meta", None) or getattr(params, "_meta", None) or {}
        upstream_message_id = metadata.get("upstream_message_id") if isinstance(metadata, dict) else None
        operation_slot = metadata.get("operation_slot") if isinstance(metadata, dict) else None
        try:
            result = await asyncio.to_thread(
                client.call, params.name, arguments, arguments.get("request_id"),
                upstream_message_id, operation_slot,
            )
        except Exception as exc:
            LOGGER.error("Gateway call failed for %s: %s", params.name, type(exc).__name__)
            if params.name in WRITE_TOOLS:
                return result_payload({"status": "submission_unknown", "error": {"code": "submission_unknown", "message": "暂时无法确认该操作是否提交。", "details": {"request_id": arguments.get("request_id"), "retry_with_same_arguments": True}}})
            return result_payload({"status": "service_unavailable", "error": {"code": "service_unavailable", "message": "无法读取书序本机结果。", "details": {}}})
        return result_payload(result)

    server = Server("book-sequence", version=mcp24_contract.SERVICE_VERSION, title="书序", instructions=HOST_INSTRUCTIONS,
                    icons=[SERVER_ICON], on_list_tools=remote_list_tools, on_call_tool=remote_call_tool)
    server.middleware.clear()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        LOGGER.error("MCP server stopped: %s", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
