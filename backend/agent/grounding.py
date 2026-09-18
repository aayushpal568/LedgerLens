"""Deterministic factual grounding validator for LedgerLens Claude Agent.

Guarantees:
- Validates assertions in agent final answers against actual structured tool outputs.
- Checks action execution results (e.g. preventing claims of success for rejected actions).
- Verifies referenced entities (clients, files, scans, finding counts).
- Pure deterministic validation: NO secondary LLM calls, zero added latency, no raw OCR leaks.
"""
from dataclasses import dataclass, field
import re
from typing import Any, Dict, List, Optional, Set

logger = logging = __import__("logging").getLogger(__name__)


@dataclass
class GroundingResult:
    is_grounded: bool
    violations: List[str] = field(default_factory=list)
    safe_text: str = ""
    citations: List[str] = field(default_factory=list)


def extract_grounded_facts(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract authoritative facts from tool execution steps recorded in the run."""
    facts: Dict[str, Any] = {
        "known_client_names": set(),
        "known_client_ids": set(),
        "created_client_names": set(),
        "created_client_ids": set(),
        "known_file_names": set(),
        "known_file_ids": set(),
        "known_scan_ids": set(),
        "scan_statuses": {},
        "finding_counts": set(),
        "successful_tools": set(),
        "rejected_tools": set(),
        "failed_tools": set(),
    }

    for step in steps:
        stype = step.get("type") or step.get("step_type")
        if stype != "tool_result":
            continue

        result_obj = step.get("result") or step.get("output_data") or {}
        tool_name = step.get("tool") or (step.get("input_data") or {}).get("tool") or result_obj.get("tool")

        # Check rejection / failure
        if result_obj.get("status") == "rejected" or result_obj.get("error_type") == "ActionRejected":
            if tool_name:
                facts["rejected_tools"].add(tool_name)
            continue

        if not result_obj.get("success", True) and result_obj.get("error"):
            if tool_name:
                facts["failed_tools"].add(tool_name)
            continue

        if tool_name:
            facts["successful_tools"].add(tool_name)

        payload = result_obj.get("result") if "result" in result_obj else result_obj

        # Extract per tool
        if tool_name == "list_clients" and isinstance(payload, list):
            for c in payload:
                if isinstance(c, dict):
                    if c.get("name"):
                        facts["known_client_names"].add(str(c["name"]).strip().lower())
                    if c.get("id"):
                        facts["known_client_ids"].add(str(c["id"]).strip())

        elif tool_name == "create_client" and isinstance(payload, dict):
            c_name = payload.get("name")
            c_id = payload.get("client_id") or payload.get("id")
            if c_name:
                facts["known_client_names"].add(str(c_name).strip().lower())
                facts["created_client_names"].add(str(c_name).strip().lower())
            if c_id:
                facts["known_client_ids"].add(str(c_id).strip())
                facts["created_client_ids"].add(str(c_id).strip())

        elif tool_name == "list_files" and isinstance(payload, list):
            for f in payload:
                if isinstance(f, dict):
                    if f.get("filename"):
                        facts["known_file_names"].add(str(f["filename"]).strip().lower())
                    if f.get("id"):
                        facts["known_file_ids"].add(str(f["id"]).strip())

        elif tool_name == "run_scan" and isinstance(payload, dict):
            scan_id = payload.get("scan_id") or payload.get("id")
            if scan_id:
                facts["known_scan_ids"].add(str(scan_id).strip())
            if payload.get("client_name"):
                facts["known_client_names"].add(str(payload["client_name"]).strip().lower())
            if payload.get("status"):
                facts["scan_statuses"][scan_id] = payload.get("status")

        elif tool_name == "get_scan_status" and isinstance(payload, dict):
            scan_id = payload.get("scan_id") or payload.get("id")
            if scan_id:
                facts["known_scan_ids"].add(str(scan_id).strip())
            if payload.get("client_id"):
                facts["known_client_ids"].add(str(payload["client_id"]).strip())
            if payload.get("client_name"):
                facts["known_client_names"].add(str(payload["client_name"]).strip().lower())
            if payload.get("status"):
                facts["scan_statuses"][scan_id] = payload.get("status")
            if payload.get("total_findings") is not None:
                facts["finding_counts"].add(int(payload["total_findings"]))

        elif tool_name == "summarize_findings" and isinstance(payload, dict):
            scan_id = payload.get("scan_id")
            if scan_id:
                facts["known_scan_ids"].add(str(scan_id).strip())
            if payload.get("client_id"):
                facts["known_client_ids"].add(str(payload["client_id"]).strip())
            if payload.get("client_name"):
                facts["known_client_names"].add(str(payload["client_name"]).strip().lower())
            # Accept both 'total' and 'total_findings'
            total = payload.get("total_findings") if payload.get("total_findings") is not None else payload.get("total")
            if total is not None:
                facts["finding_counts"].add(int(total))
            by_cat = payload.get("by_category") or {}
            for cnt in by_cat.values():
                if isinstance(cnt, (int, float)):
                    facts["finding_counts"].add(int(cnt))
            by_status = payload.get("by_status") or {}
            for cnt in by_status.values():
                if isinstance(cnt, (int, float)):
                    facts["finding_counts"].add(int(cnt))

            # Also collect files from important_findings
            for imp in payload.get("important_findings") or []:
                for fid in imp.get("file_ids") or []:
                    facts["known_file_ids"].add(str(fid).strip())
                for fn in imp.get("filenames") or []:
                    facts["known_file_names"].add(str(fn).strip().lower())

        elif tool_name == "get_findings" and isinstance(payload, list):
            facts["finding_counts"].add(len(payload))
            for fnd in payload:
                if isinstance(fnd, dict):
                    if fnd.get("scan_id"):
                        facts["known_scan_ids"].add(str(fnd["scan_id"]).strip())
                    if fnd.get("client_id"):
                        facts["known_client_ids"].add(str(fnd["client_id"]).strip())
                    for fid in fnd.get("file_ids") or []:
                        facts["known_file_ids"].add(str(fid).strip())
                    for fn in fnd.get("filenames") or []:
                        facts["known_file_names"].add(str(fn).strip().lower())

        elif tool_name == "export_report" and isinstance(payload, dict):
            if payload.get("scan_id"):
                facts["known_scan_ids"].add(str(payload["scan_id"]).strip())

    return facts


UUID_REGEX = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)


def verify_grounding(final_text: str, steps: List[Dict[str, Any]]) -> GroundingResult:
    """Verify that claims in the assistant's final text are supported by the execution steps."""
    if not final_text:
        return GroundingResult(is_grounded=True, safe_text="")

    facts = extract_grounded_facts(steps)
    violations: List[str] = []
    text_lower = final_text.lower()

    # Rule 1: Check rejected actions
    # If an action was rejected, the assistant must NOT claim it was successfully executed
    if "create_client" in facts["rejected_tools"]:
        if re.search(r"\b(successfully created|has been created|i created)\b", text_lower):
            violations.append("Claimed client creation succeeded when create_client was rejected by user.")

    if "run_scan" in facts["rejected_tools"]:
        if re.search(r"\b(scan (has been|was) (started|initiated|run)|i (started|ran) the scan)\b", text_lower):
            violations.append("Claimed scan was initiated when run_scan was rejected by user.")

    if "set_finding_review" in facts["rejected_tools"]:
        if re.search(r"\b(updated the finding|reviewed the finding|set finding review)\b", text_lower):
            violations.append("Claimed finding review updated when set_finding_review was rejected by user.")

    # Rule 2: Client creation existence claims
    created_matches = re.findall(r"client\s+['\"]?([A-Za-z0-9_\- ]+?)['\"]?\s+(?:was|has been)?\s*created", text_lower)
    for match in created_matches:
        client_cand = match.strip().lower()
        if client_cand and facts["created_client_names"] and client_cand not in facts["created_client_names"]:
            violations.append(f"Referenced created client '{match}' which was not returned by create_client.")

    # Rule 3: Entity UUID verification
    # All explicit UUIDs mentioned in the final answer must match a known client, scan, or file ID
    all_known_ids = facts["known_client_ids"] | facts["known_scan_ids"] | facts["known_file_ids"]
    if all_known_ids:
        for uuid_found in UUID_REGEX.findall(final_text):
            if uuid_found not in all_known_ids:
                violations.append(f"Referenced unsupported entity ID '{uuid_found}' not present in tool results.")

    # Rule 4: Claimed finding count verification
    # e.g., "found X findings" or "detected X exceptions" or "total of X findings"
    count_matches = re.findall(r"\b(?:found|detected|total of|identified)\s+(\d+)\s+(?:findings?|exceptions?|issues?)\b", text_lower)
    if count_matches and facts["finding_counts"]:
        for cnt_str in count_matches:
            cnt_val = int(cnt_str)
            if cnt_val not in facts["finding_counts"]:
                violations.append(f"Claimed finding count '{cnt_val}' does not match verified finding counts {sorted(facts['finding_counts'])}.")


    is_grounded = len(violations) == 0
    safe_text = final_text

    if not is_grounded:
        logger.warning(f"Grounding violations detected: {violations}")
        # Safe amendment: append factual correction or disclaimer
        disclaimer = "\n\n*(Note: The user rejected one or more proposed actions; unapproved actions were not executed.)*"
        if "rejected by user" in " ".join(violations).lower() and "rejected" not in text_lower:
            safe_text = final_text + disclaimer
        else:
            safe_text = final_text

    return GroundingResult(
        is_grounded=is_grounded,
        violations=violations,
        safe_text=safe_text,
    )
