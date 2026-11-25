"""Render the registered Shuxu MCP schemas and workflow links into the guide.

The MCP module builds its tool list in memory during import. This script never
calls ``ensure_server``, opens SQLite, or inspects a host's grants.
"""

from __future__ import annotations

import json
import sys
import argparse
from pathlib import Path


DOCS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DOCS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shuxu import mcp24_contract, mcp_workflows
from shuxu.mcp_server import TOOLS


# Product-spec numbering is separate from the legacy workflow IDs in
# mcp_workflows.ROUTES. Item 17 is a local acceptance CLI, not an MCP tool.
PRODUCT_CAPABILITIES = [
    {"product_spec_id": "01", "tools": ["get_mcp_directory", "diagnose_mcp_compatibility"]},
    {"product_spec_id": "02", "tools": ["plan_mcp_calls", "get_workflow_guide"]},
    {"product_spec_id": "03", "tools": ["save_thought_checkpoint", "continue_thought", "guide_thought"]},
    {"product_spec_id": "04", "tools": ["next_prompt"]},
    {"product_spec_id": "05", "tools": ["get_session_conditions", "set_session_conditions", "end_session_conditions", "next_prompt"]},
    {"product_spec_id": "06", "tools": ["get_prompt_family", "set_prompt_family_relation", "next_prompt"]},
    {"product_spec_id": "07", "tools": ["pick_unfinished_prompt", "list_unfinished_prompts"]},
    {"product_spec_id": "08", "tools": ["guide_thought", "get_guide_history"]},
    {"product_spec_id": "09", "tools": ["set_question_preference", "get_question_preferences", "end_question_preference", "guide_thought"]},
    {"product_spec_id": "10", "tools": ["correct_prompt_status", "get_prompt"]},
    {"product_spec_id": "11", "tools": ["get_prompt_timeline"]},
    {"product_spec_id": "12", "tools": ["review_prompt_events"]},
    {"product_spec_id": "13", "tools": ["draft_prompt_from_materials"]},
    {"product_spec_id": "14", "tools": ["save_prompt_material_relations", "remove_prompt_material_relation", "read_entity_relations"]},
    {"product_spec_id": "15", "tools": ["write_claim_evidence", "read_prompt_evidence"]},
    {"product_spec_id": "16", "tools": ["get_topic_project_progress"]},
    {"product_spec_id": "17", "tools": [], "runner": "py -3.12 -X utf8 -B -m shuxu.mcp25_host_acceptance"},
    {"product_spec_id": "18", "tools": ["get_full_record", "list_full_records", "get_attachment_chunk",
                                             "export_full_library", "get_full_export_info", "read_full_export_chunk"]},
    {"product_spec_id": "19", "tools": ["compare_mcp_directories"]},
    {"product_spec_id": "20", "tools": ["restart_service", "get_service_restart", "retry_service_restart"]},
]


def build_catalog() -> dict:
    route_ids = [route[0] for route in mcp_workflows.ROUTES]
    if len(route_ids) != len(set(route_ids)):
        raise SystemExit("legacy workflow route IDs are not unique")
    routes_by_tool: dict[str, list[str]] = {}
    workflows = []
    for scenario, intent, tools, guidance in mcp_workflows.ROUTES:
        workflows.append({
            "scenario": scenario,
            "intent": intent,
            "tools": tools,
            "guidance": guidance,
        })
        for name in tools:
            routes_by_tool.setdefault(name, []).append(scenario)

    tools = []
    for registered_tool in TOOLS:
        spec = registered_tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        spec["workflow_scenarios"] = routes_by_tool.get(spec["name"], [])
        tools.append(spec)
    names = [item["name"] for item in tools]
    if len(names) != len(set(names)):
        raise SystemExit("registered MCP tool names are not unique")
    registered_names = set(names)
    unknown_product_tools = sorted({
        name for item in PRODUCT_CAPABILITIES for name in item["tools"] if name not in registered_names
    })
    if unknown_product_tools:
        raise SystemExit("product capability map references unregistered tools: " + ", ".join(unknown_product_tools))
    return {
        "catalog_format": "shuxu-mcp-tool-catalog/v1",
        "protocol_version": mcp24_contract.PROTOCOL_VERSION,
        "service_version": mcp24_contract.SERVICE_VERSION,
        "registry_source": "shuxu.book.TOOL_SPECS; consumed by shuxu.mcp_server.tools_from_specs",
        "visibility": "Full registered definitions; named-host tools/list is filtered by that host's grants.",
        "tool_count": len(tools),
        "workflow_count": len(workflows),
        "product_capability_map": PRODUCT_CAPABILITIES,
        "workflows": workflows,
        "tools": tools,
    }


def render_reference(catalog: dict) -> str:
    lines = [
        "<!-- BEGIN GENERATED MCP TOOL SCHEMAS -->",
        "<!-- Generated from shuxu.book.TOOL_SPECS; do not edit this section by hand. -->",
        "## Registered tool schemas",
        "",
        f"Registry snapshot: service `{catalog['service_version']}`, protocol `{catalog['protocol_version']}`, "
        f"{catalog['tool_count']} registered tools and {catalog['workflow_count']} route scenarios.",
        "This is the complete registered source definition set. Compare it with the running instance handshake and actual `tools/list`; a named host's list is filtered by that host's grants.",
        "MCP currently registers input schemas here; it does not publish result schemas. A schema keyword `default` is included if registered; runtime defaults not encoded in schema are documented with the corresponding handler behavior. Result and error behavior is documented in the relevant guide sections.",
        "Regenerate this section after changing registered tools with `py -3.12 -B docs/generate_mcp_api_reference.py --update-guide`; this reads the in-memory registry and does not call a running service.",
        "",
        "### Legacy workflow routes",
        "",
        "| Scenario | User intent | Tools | Guidance |",
        "| --- | --- | --- | --- |",
    ]
    for route in catalog["workflows"]:
        intent = route["intent"].replace("|", "\\|")
        guidance = route["guidance"].replace("|", "\\|")
        tools = ", ".join(f"`{name}`" for name in route["tools"])
        lines.append(f"| {route['scenario']} | {intent} | {tools} | {guidance} |")
    lines.extend([
        "",
        "### Product-spec capability mapping",
        "",
        "These product numbers are independent of the legacy workflow IDs above. Item 17 is a local CLI runner and does not appear in MCP tools/list. The map records the documented path; a running host may expose only a permission-filtered subset.",
        "",
        "| Product item | Registered MCP tools in the documented path | Local runner |",
        "| --- | --- | --- |",
    ])
    for capability in catalog["product_capability_map"]:
        tools = ", ".join(f"`{name}`" for name in capability["tools"])
        runner = f"`{capability['runner']}`" if capability.get("runner") else "—"
        lines.append(f"| {capability['product_spec_id']} | {tools or '—'} | {runner} |")
    lines.extend(["", "### Full input schemas", ""])
    for tool in catalog["tools"]:
        lines.extend([
            f"#### `{tool['name']}`",
            "",
            tool["description"],
            "",
            "Route scenarios: " + (", ".join(tool["workflow_scenarios"]) or "none declared in `mcp_workflows.ROUTES`"),
            "",
            "`inputSchema` as registered:",
            "",
            "```json",
            json.dumps(tool["inputSchema"], ensure_ascii=False, indent=2),
            "```",
            "",
            "MCP registration annotations:",
            "",
            "```json",
            json.dumps(tool.get("annotations", {}), ensure_ascii=False, indent=2),
            "```",
            "",
        ])
    lines.append("<!-- END GENERATED MCP TOOL SCHEMAS -->")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the registered MCP schema appendix without starting Shuxu or reading a database.")
    parser.add_argument("--update-guide", action="store_true", help="replace the marked generated section in docs/mcp24-host-guide.md")
    parser.add_argument("--evidence-json", type=Path, help="write a machine-readable schema snapshot to an external evidence path")
    args = parser.parse_args()
    if not args.update_guide and args.evidence_json is None:
        parser.error("choose --update-guide, --evidence-json, or both")
    if args.evidence_json is not None:
        if not args.evidence_json.is_absolute():
            parser.error("--evidence-json must be an absolute RUN/evidence path")
        if args.evidence_json.resolve().is_relative_to(PROJECT_ROOT.resolve()):
            parser.error("--evidence-json must stay outside the project tree")
        if args.evidence_json.exists():
            parser.error("refusing to overwrite existing evidence")

    catalog = build_catalog()
    if args.update_guide:
        guide = DOCS_DIR / "mcp24-host-guide.md"
        text = guide.read_text(encoding="utf-8")
        start = "<!-- BEGIN GENERATED MCP TOOL SCHEMAS -->"
        end = "<!-- END GENERATED MCP TOOL SCHEMAS -->"
        if text.count(start) != 1 or text.count(end) != 1:
            raise SystemExit("mcp24-host-guide.md must contain exactly one generated-section marker pair")
        before, remainder = text.split(start, 1)
        _, after = remainder.split(end, 1)
        guide.write_text(before + render_reference(catalog) + after, encoding="utf-8")
    if args.evidence_json is not None:
        encoded = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
        args.evidence_json.write_text(encoded, encoding="utf-8")


if __name__ == "__main__":
    main()
