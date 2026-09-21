"""Bounded async Claude agent execution loop for LedgerLens.

Architecture:
  POST /api/agent/messages
      ↓
  create queued run
      ↓
  start background agent run
      ↓
  load conversation (tenant-scoped)
      ↓
  Claude
      ↓
  validate JSON response
      ↓
  execute registered tool (execute_tool)
      ↓
  store tool call and tool result in agent_run_steps
      ↓
  send result back to Claude
      ↓
  repeat
      ↓
  final answer
      ↓
  store assistant message
      ↓
  mark run completed

Guarantees:
- Maximum 12 Claude turns per run.
- Maximum 10 tool calls per run.
- Every Claude turn requires exactly one valid JSON object (either tool call or final).
- Only executes tools registered in the tool registry.
- Enforces strict tenant isolation and AuthedUser context on every tool execution.
- Thoughts are strictly internal and NEVER exposed in user-facing assistant messages.
- Cancellation and idempotency: runs execute at most once; cancellation is checked before every turn and tool call.
"""
import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set

from auth_dep import AuthedUser
import agent.approvals as approvals
from agent.grounding import verify_grounding
from agent.registry import Tool, default_registry, execute_tool, validate_tool_arguments
import services

logger = logging.getLogger(__name__)

MAX_CLAUDE_TURNS = 12
MAX_TOOL_CALLS = 10
CLAUDE_DECISION_MAX_TOKENS = 384
CLAUDE_FINAL_ANSWER_MAX_TOKENS = 2048
CLAUDE_MAX_TOKENS = CLAUDE_FINAL_ANSWER_MAX_TOKENS  # Backward compatibility

# Concurrency & cancellation tracking
_ACTIVE_RUNS: Set[str] = set()
_CANCELLED_RUNS: Set[str] = set()
_RUN_LOCK = asyncio.Lock()

_global_llm_provider: Optional[Any] = None


def set_global_llm_provider(provider: Optional[Any]) -> None:
    """Set provider override (e.g. for testing to avoid live API calls)."""
    global _global_llm_provider
    _global_llm_provider = provider


def get_llm_provider() -> Any:
    """Acquire configured LLM provider, falling back to ClaudeOpusFalProvider."""
    global _global_llm_provider
    if _global_llm_provider is not None:
        return _global_llm_provider
    from engine.providers.llm import ClaudeOpusFalProvider
    return ClaudeOpusFalProvider()


def request_run_cancellation(run_id: str) -> None:
    """Signal cancellation for an active agent run."""
    _CANCELLED_RUNS.add(run_id)


def is_run_cancelled(run_id: str) -> bool:
    """Check if cancellation has been requested for run_id."""
    return run_id in _CANCELLED_RUNS


def _evidence_citations(turn_steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Derive safe evidence references from executed tool results (no raw payloads/secrets)."""
    citations: List[Dict[str, Any]] = []
    for s in turn_steps:
        if s.get("type") != "tool_result":
            continue
        tool_name = s.get("tool")
        result = s.get("result") or {}
        payload = result.get("result") if isinstance(result, dict) and "result" in result else result
        ids: List[str] = []
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    for key in ("id", "scan_id", "client_id"):
                        if item.get(key):
                            ids.append(str(item[key]))
        elif isinstance(payload, dict):
            for key in ("id", "scan_id", "client_id"):
                if payload.get(key):
                    ids.append(str(payload[key]))
        citations.append(
            {
                "tool": tool_name,
                "success": bool(result.get("success", True)) if isinstance(result, dict) else True,
                "count": len(payload) if isinstance(payload, list) else None,
                "entity_ids": sorted(set(ids))[:50],
            }
        )
    return citations


def _cap_tool_result(result: Any, max_chars: int) -> Any:
    """Bound the size of a tool result before persisting / feeding to Claude."""
    try:
        serialized = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        serialized = str(result)
    if len(serialized) <= max_chars:
        return result
    return {
        "success": (result.get("success") if isinstance(result, dict) else True),
        "tool": (result.get("tool") if isinstance(result, dict) else None),
        "truncated": True,
        "note": f"Tool result exceeded {max_chars} characters and was truncated for safety.",
        "excerpt": serialized[:max_chars],
    }


def extract_json(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Robustly extract and parse a single JSON object from LLM output."""
    if not text or not isinstance(text, str):
        return None

    cleaned = text.strip()

    # Strip markdown code fences if present (```json ... ``` or ``` ...)
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    # Direct JSON parse
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Substring search for outermost balanced JSON object
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return None


def build_system_prompt(tools: List[Tool]) -> str:
    """Generate the strict accounting agent system prompt with tool definitions and JSON protocol."""
    read_only_tools = [t for t in tools if t.read_only or not t.approval_required]
    action_tools = [t for t in tools if not t.read_only or t.approval_required]

    ro_descriptions = []
    for t in read_only_tools:
        ro_descriptions.append(
            f"- Tool '{t.name}': {t.description}\n"
            f"  Parameters schema: {json.dumps(t.parameters)}"
        )
    ro_block = "\n".join(ro_descriptions)

    action_descriptions = []
    for t in action_tools:
        action_descriptions.append(
            f"- Action Tool '{t.name}' (REQUIRES HUMAN APPROVAL): {t.description}\n"
            f"  Parameters schema: {json.dumps(t.parameters)}"
        )
    action_block = "\n".join(action_descriptions)

    return (
        "You are the LedgerLens AI Accounting Assistant, an expert CPA agent specialized in "
        "assisting accountants with client files, checklist templates, audit scans, and findings.\n\n"
        "AVAILABLE READ-ONLY INSPECTION TOOLS:\n"
        f"{ro_block}\n\n"
        "AVAILABLE ACTION TOOLS (REQUIRE HUMAN APPROVAL):\n"
        f"{action_block}\n\n"
        "STRICT OPERATIONAL RULES:\n"
        "1. Tools are your ONLY way to access or modify LedgerLens data. Never invent or hallucinate client names, "
        "files, periods, scans, or findings.\n"
        "2. Do NOT expose internal filesystem paths (such as storage paths), database identifiers (_id), "
        "or raw document OCR text to the user.\n"
        "3. Use tool results as the absolute source of truth. If information is missing or not found, "
        "explicitly inform the accountant.\n"
        "4. Action tools (run_scan, create_client, set_finding_review) require human approval. When you call an "
        "action tool, execution pauses and the user is asked to approve it. Propose actions when the user asks "
        "you to perform an action.\n"
        "5. Do NOT claim an action was performed unless confirmed by a successful tool result. If an action was "
        "rejected or failed, clearly explain that it was not performed.\n"
        "6. Maximum 1 'run_scan' call allowed per run.\n\n"
        "MANDATORY JSON RESPONSE PROTOCOL:\n"
        "For EVERY turn, you MUST respond STRICTLY with a single valid JSON object. Do not include any "
        "conversational text, markdown formatting, or preamble outside the JSON object.\n\n"
        "Your response must be EXACTLY ONE of the following two shapes:\n\n"
        "A) Tool Call:\n"
        "{\n"
        '  "thought": "short internal reasoning summary",\n'
        '  "tool": "<tool_name>",\n'
        '  "args": { ... }\n'
        "}\n\n"
        "B) Final Response:\n"
        "{\n"
        '  "thought": "short internal reasoning summary",\n'
        '  "final": "<clear, helpful answer for the accountant>"\n'
        "}\n\n"
        "Never include both 'tool' and 'final' in the same response. Never omit both."
    )


def format_turn_prompt(messages: List[Dict[str, Any]], steps: List[Dict[str, Any]]) -> str:
    """Format recent conversation history and current run step sequence for Claude."""
    lines = ["CONVERSATION HISTORY:"]
    for m in messages:
        role = m.get("role", "user").capitalize()
        lines.append(f"{role}: {m.get('text', '')}")

    if steps:
        lines.append("\nCURRENT RUN EXECUTION STEPS:")
        for s in steps:
            stype = s.get("type")
            if stype == "tool_call":
                lines.append(f"-> Executed Tool Call: {s.get('tool')}(args={json.dumps(s.get('args'))})")
            elif stype == "tool_result":
                lines.append(f"<- Tool Result: {json.dumps(s.get('result'))}")

    lines.append("\nRespond with your next action (either a tool call or final response) as a JSON object:")
    return "\n".join(lines)


async def run_agent_loop(
    user: AuthedUser,
    run_id: str,
    thread_id: str,
    llm_provider: Optional[Any] = None,
    registry: Optional[Any] = None,
    db=None,
    is_resume: bool = False,
) -> Dict[str, Any]:
    """Execute the bounded async agent loop for a queued or resuming agent run.

    Idempotent: Executes at most once per active run.
    """
    # 1. In-process fast-path guard (same worker must never run the same id twice)
    async with _RUN_LOCK:
        if run_id in _ACTIVE_RUNS:
            logger.warning(f"Agent run '{run_id}' is already active in this process. Ignoring duplicate.")
            return {"status": "already_active", "run_id": run_id}
        _ACTIVE_RUNS.add(run_id)

    tool_registry = registry or default_registry
    provider = llm_provider or get_llm_provider()

    # Durable server-side execution limits
    run_deadline = time.monotonic() + max(1, services.agent_run_timeout_seconds())
    max_output_tokens = services.agent_max_total_output_tokens()
    output_tokens_used = 0

    try:
        # 1b. Early in-process cancellation short-circuit (pre-dispatch)
        if is_run_cancelled(run_id):
            await services.update_agent_run(
                user,
                run_id,
                {"status": services.RUN_STATUS_CANCELLED, "completed_at": services.now_iso()},
                db=db,
            )
            return {"status": services.RUN_STATUS_CANCELLED}

        # 2. DURABLE ATOMIC CLAIM — authoritative cross-worker mutual exclusion.
        # Two workers can never both obtain a live lease on the same run. A cancelled or
        # terminal run is never claimable, so retries cannot resurrect a cancelled run.
        if is_resume:
            try:
                current_run = await services.get_agent_run(user, run_id, db=db)
            except Exception as e:
                logger.error(f"Cannot load agent run '{run_id}': {e}")
                return {"status": "error", "error": str(e)}
            allowed_resume = {
                services.RUN_STATUS_QUEUED,
                services.RUN_STATUS_RUNNING,
                services.RUN_STATUS_WAITING_FOR_APPROVAL,
            }
            if current_run.get("status") not in allowed_resume:
                return {"status": current_run.get("status"), "run_id": run_id}
        else:
            claimed = await services.claim_agent_run(user, run_id, db=db)
            if claimed is None:
                cur_status = "not_found"
                try:
                    cur = await services.get_agent_run(user, run_id, db=db)
                    cur_status = cur.get("status")
                except Exception:
                    pass
                logger.info(
                    f"Agent run '{run_id}' not claimed (held by another worker or terminal: {cur_status})."
                )
                return {"status": cur_status, "run_id": run_id, "claimed": False}

        # 2. Load conversation messages (tenant-scoped, bounded)
        raw_messages = await services.list_agent_messages(user, thread_id, db=db)
        bounded_messages = raw_messages[-10:] if raw_messages else []

        system_prompt = build_system_prompt(tool_registry.list_tools())

        # Load existing run steps from DB to reconstruct turn_steps
        existing_steps = await services.list_agent_run_steps(user, run_id, db=db)
        turn_steps: List[Dict[str, Any]] = []
        tool_call_count = 0
        run_scan_count = 0

        for s in existing_steps:
            stype = s.get("step_type")
            inp = s.get("input_data") or {}
            outp = s.get("output_data") or {}
            if stype == "tool_call":
                t_name = inp.get("tool")
                turn_steps.append({"type": "tool_call", "tool": t_name, "args": inp.get("args")})
                tool_call_count += 1
                if t_name == "run_scan":
                    run_scan_count += 1
            elif stype == "tool_result":
                turn_steps.append({"type": "tool_result", "tool": inp.get("tool"), "result": outp})

        turn_count = 0

        # 3. Agent execution loop
        while turn_count < MAX_CLAUDE_TURNS:
            turn_count += 1

            # --- Server-side guard before EVERY LLM/tool execution ---
            # (1) Wall-clock timeout
            if time.monotonic() > run_deadline:
                err_msg = f"Agent run exceeded maximum execution time of {services.agent_run_timeout_seconds()}s."
                await services.update_agent_run(
                    user,
                    run_id,
                    {"status": services.RUN_STATUS_FAILED, "error": err_msg, "completed_at": services.now_iso()},
                    db=db,
                )
                return {"status": services.RUN_STATUS_FAILED, "error": err_msg}

            # (2) Cancellation (in-process fast flag AND persisted DB status — cross-process safe)
            if await services.run_is_cancelled(run_id, user=user, db=db):
                await services.update_agent_run(
                    user,
                    run_id,
                    {"status": services.RUN_STATUS_CANCELLED, "completed_at": services.now_iso()},
                    db=db,
                )
                return {"status": services.RUN_STATUS_CANCELLED}

            # (3) Heartbeat: extend this worker's lease so a live run is never reclaimed
            try:
                await services.renew_agent_run_lease(user, run_id, db=db)
            except Exception:
                pass

            user_prompt = format_turn_prompt(bounded_messages, turn_steps)

            last_invocation_error = None

            async def _invoke_claude(prompt_text: str, token_budget: int) -> Optional[str]:
                nonlocal last_invocation_error, output_tokens_used
                # Cumulative output-token budget (server-side protection)
                if output_tokens_used + token_budget > max_output_tokens:
                    last_invocation_error = (
                        f"Agent exceeded maximum total output-token budget of {max_output_tokens} tokens."
                    )
                    logger.warning(f"Run '{run_id}' hit output-token budget: {last_invocation_error}")
                    return None
                output_tokens_used += token_budget
                try:
                    if asyncio.iscoroutinefunction(getattr(provider, "generate", None)):
                        return await provider.generate(
                            prompt_text,
                            system_prompt=system_prompt,
                            max_tokens=token_budget,
                            temperature=0.0,
                        )
                    else:
                        return await asyncio.to_thread(
                            provider.generate,
                            prompt_text,
                            system_prompt=system_prompt,
                            max_tokens=token_budget,
                            temperature=0.0,
                        )
                except Exception as e:
                    logger.error(f"Claude invocation error in run '{run_id}': {e}")
                    last_invocation_error = str(e)
                    return None

            # Initial call for turn decision (tool call or final answer intention)
            raw_response = await _invoke_claude(user_prompt, CLAUDE_DECISION_MAX_TOKENS)

            if not raw_response:
                if last_invocation_error:
                    detail_err = f"Claude provider execution failed: {last_invocation_error}"
                    safe_err = "The AI provider failed to produce a response. Please try again shortly."
                else:
                    detail_err = "The AI provider returned an empty or unavailable response."
                    safe_err = "The AI provider returned an empty response."
                logger.error(f"Agent provider failure in run '{run_id}': {detail_err}")
                await services.update_agent_run(
                    user,
                    run_id,
                    {
                        "status": services.RUN_STATUS_FAILED,
                        "error": safe_err,
                        "completed_at": services.now_iso(),
                        "lease_expires_at": None,
                        "worker_id": None,
                    },
                    db=db,
                )
                return {"status": services.RUN_STATUS_FAILED, "error": detail_err}

            # 4. Parse and validate JSON protocol with exactly one retry on malformed/ambiguous response
            parsed = extract_json(raw_response)
            has_tool = bool(parsed and isinstance(parsed, dict) and "tool" in parsed)
            has_final = bool(parsed and isinstance(parsed, dict) and "final" in parsed)
            is_valid_protocol = parsed and isinstance(parsed, dict) and ((has_tool and not has_final) or (has_final and not has_tool))

            if not is_valid_protocol:
                # Determine initial violation message
                if not parsed or not isinstance(parsed, dict):
                    initial_err = "Claude returned malformed response that could not be parsed into JSON."
                elif has_tool and has_final:
                    initial_err = "Ambiguous response: Claude provided both 'tool' and 'final' in a single turn."
                else:
                    initial_err = "Protocol violation: Claude response contained neither 'tool' nor 'final'."

                logger.warning(f"Agent received invalid protocol in run '{run_id}'. Retrying once with correction prompt.")
                correction_prompt = (
                    f"{user_prompt}\n\n"
                    "CORRECTION REQUIRED:\n"
                    "Your previous response was not valid according to the mandatory JSON protocol.\n"
                    "You must respond with ONLY a single valid JSON object containing EXACTLY ONE of:\n"
                    '1) Tool call: {"thought": "...", "tool": "<tool_name>", "args": {...}}\n'
                    '2) Final answer: {"thought": "...", "final": "<your answer here>"}\n'
                    "Do NOT return both 'tool' and 'final'. Do NOT return markdown commentary outside the JSON."
                )
                retry_raw = await _invoke_claude(correction_prompt, CLAUDE_FINAL_ANSWER_MAX_TOKENS)
                if retry_raw:
                    retry_parsed = extract_json(retry_raw)
                    r_has_tool = bool(retry_parsed and isinstance(retry_parsed, dict) and "tool" in retry_parsed)
                    r_has_final = bool(retry_parsed and isinstance(retry_parsed, dict) and "final" in retry_parsed)
                    if retry_parsed and isinstance(retry_parsed, dict) and ((r_has_tool and not r_has_final) or (r_has_final and not r_has_tool)):
                        parsed = retry_parsed
                        has_tool = r_has_tool
                        has_final = r_has_final
                        is_valid_protocol = True
                    else:
                        if not retry_parsed or not isinstance(retry_parsed, dict):
                            err_msg = "Claude returned malformed response that could not be parsed into JSON."
                        elif r_has_tool and r_has_final:
                            err_msg = "Ambiguous response: Claude provided both 'tool' and 'final' in a single turn."
                        else:
                            err_msg = "Protocol violation: Claude response contained neither 'tool' nor 'final'."
                else:
                    err_msg = initial_err

                if not is_valid_protocol:
                    logger.error(f"Claude JSON correction retry failed for run '{run_id}': {err_msg}")
                    await services.update_agent_run(
                        user,
                        run_id,
                        {
                            "status": services.RUN_STATUS_FAILED,
                            "error": err_msg,
                            "completed_at": services.now_iso(),
                        },
                        db=db,
                    )
                    return {"status": services.RUN_STATUS_FAILED, "error": err_msg}

            thought = str(parsed.get("thought") or "").strip()

            # 5. Case A: Final answer
            if has_final:
                final_text = str(parsed["final"]).strip()
                # If final answer was generated during decision phase and might have hit the smaller token limit or needs full budget:
                # If final_text appears truncated or short, or was parsed directly:
                if not final_text:
                    err_msg = "The agent generated an empty final answer."
                    await services.update_agent_run(
                        user,
                        run_id,
                        {
                            "status": services.RUN_STATUS_FAILED,
                            "error": err_msg,
                            "completed_at": services.now_iso(),
                        },
                        db=db,
                    )
                    return {"status": services.RUN_STATUS_FAILED, "error": err_msg}


                # Record thought step if present (internal only, not in user message)
                if thought:
                    await services.create_agent_run_step(
                        user,
                        run_id=run_id,
                        thread_id=thread_id,
                        step_type="thought",
                        input_data={"thought": thought},
                        status="completed",
                        db=db,
                    )

                # Deterministic Grounding Verification
                grounding_res = verify_grounding(final_text, turn_steps)
                final_text = grounding_res.safe_text

                # Persist an evidence reference step (final answer traceable to real tool results only)
                await services.create_agent_run_step(
                    user,
                    run_id=run_id,
                    thread_id=thread_id,
                    step_type="evidence",
                    input_data={"run_id": run_id},
                    output_data={
                        "grounded": grounding_res.is_grounded,
                        "violations": grounding_res.violations,
                        "citations": getattr(grounding_res, "citations", None) or _evidence_citations(turn_steps),
                        "tool_result_count": len([s for s in turn_steps if s.get("type") == "tool_result"]),
                    },
                    status="completed",
                    db=db,
                )

                # Idempotent assistant message: never duplicate on a durable re-execution.
                active_db = services._get_db(db)
                t_now = services.now_iso()
                assistant_msgs = await active_db.agent_messages.find(
                    {"firm_id": user.firm_id, "thread_id": thread_id, "role": "assistant"},
                    {"_id": 0},
                ).to_list(10000)
                existing_msg = next(
                    (m for m in assistant_msgs if (m.get("metadata") or {}).get("run_id") == run_id),
                    None,
                )
                if existing_msg:
                    msg_id = existing_msg["id"]
                else:
                    msg_id = services.new_id()
                    msg_doc = {
                        "id": msg_id,
                        "firm_id": user.firm_id,
                        "thread_id": thread_id,
                        "sender_id": "assistant",
                        "role": "assistant",
                        "text": final_text,
                        "created_at": t_now,
                        "metadata": {
                            "run_id": run_id,
                            "thought": thought if thought else None,
                            "is_grounded": grounding_res.is_grounded,
                            "violations": grounding_res.violations,
                        },
                    }
                    await active_db.agent_messages.insert_one(msg_doc)

                # Mark run completed and release the lease (terminal state is never re-claimed).
                await services.update_agent_run(
                    user,
                    run_id,
                    {
                        "status": services.RUN_STATUS_COMPLETED,
                        "completed_at": t_now,
                        "lease_expires_at": None,
                        "worker_id": None,
                    },
                    db=db,
                )
                return {"status": services.RUN_STATUS_COMPLETED, "message_id": msg_id}

            # 6. Case B: Tool call
            tool_name = str(parsed["tool"]).strip()
            args = parsed.get("args") or {}

            # Validate tool in registry
            tool = tool_registry.get(tool_name)
            if not tool:
                err_msg = f"Unknown tool '{tool_name}' requested by agent."
                await services.update_agent_run(
                    user,
                    run_id,
                    {
                        "status": services.RUN_STATUS_FAILED,
                        "error": err_msg,
                        "completed_at": services.now_iso(),
                    },
                    db=db,
                )
                return {"status": services.RUN_STATUS_FAILED, "error": err_msg}

            # Validate arguments against tool parameter schema
            is_valid_args, arg_err = validate_tool_arguments(tool, args)
            if not is_valid_args:
                err_msg = f"Invalid arguments for tool '{tool_name}': {arg_err}"
                await services.update_agent_run(
                    user,
                    run_id,
                    {
                        "status": services.RUN_STATUS_FAILED,
                        "error": err_msg,
                        "completed_at": services.now_iso(),
                    },
                    db=db,
                )
                return {"status": services.RUN_STATUS_FAILED, "error": err_msg}

            # Check tool call limit
            if tool_call_count >= MAX_TOOL_CALLS:
                err_msg = f"Agent exceeded maximum tool call limit of {MAX_TOOL_CALLS}."
                await services.update_agent_run(
                    user,
                    run_id,
                    {
                        "status": services.RUN_STATUS_FAILED,
                        "error": err_msg,
                        "completed_at": services.now_iso(),
                    },
                    db=db,
                )
                return {"status": services.RUN_STATUS_FAILED, "error": err_msg}

            # Limit: maximum 1 run_scan per run
            if tool_name == "run_scan" and run_scan_count >= 1:
                err_msg = "Agent exceeded maximum limit of 1 'run_scan' invocation per run."
                await services.update_agent_run(
                    user,
                    run_id,
                    {
                        "status": services.RUN_STATUS_FAILED,
                        "error": err_msg,
                        "completed_at": services.now_iso(),
                    },
                    db=db,
                )
                return {"status": services.RUN_STATUS_FAILED, "error": err_msg}

            # Check timeout & cancellation (in-process + persisted DB status) before tool execution
            if time.monotonic() > run_deadline:
                err_msg = f"Agent run exceeded maximum execution time of {services.agent_run_timeout_seconds()}s."
                await services.update_agent_run(
                    user,
                    run_id,
                    {"status": services.RUN_STATUS_FAILED, "error": err_msg, "completed_at": services.now_iso()},
                    db=db,
                )
                return {"status": services.RUN_STATUS_FAILED, "error": err_msg}

            if await services.run_is_cancelled(run_id, user=user, db=db):
                await services.update_agent_run(
                    user,
                    run_id,
                    {"status": services.RUN_STATUS_CANCELLED, "completed_at": services.now_iso()},
                    db=db,
                )
                return {"status": services.RUN_STATUS_CANCELLED}

            # 7. Action Tools: require human approval
            if tool.approval_required:
                approval = await approvals.create_approval(
                    user,
                    run_id=run_id,
                    thread_id=thread_id,
                    tool_name=tool_name,
                    proposed_args=args,
                    db=db,
                )
                await services.create_agent_run_step(
                    user,
                    run_id=run_id,
                    thread_id=thread_id,
                    step_type="tool_call",
                    input_data={
                        "tool": tool_name,
                        "args": args,
                        "thought": thought,
                        "approval_id": approval["id"],
                    },
                    status="waiting_for_approval",
                    db=db,
                )
                await services.update_agent_run(
                    user,
                    run_id,
                    {
                        "status": services.RUN_STATUS_WAITING_FOR_APPROVAL,
                        "metadata": {"approval_id": approval["id"], "tool": tool_name},
                    },
                    db=db,
                )
                return {
                    "status": services.RUN_STATUS_WAITING_FOR_APPROVAL,
                    "run_id": run_id,
                    "approval_id": approval["id"],
                }

            # 8. Read-only tools: execute immediately
            await services.create_agent_run_step(
                user,
                run_id=run_id,
                thread_id=thread_id,
                step_type="tool_call",
                input_data={"tool": tool_name, "args": args, "thought": thought},
                status="running",
                db=db,
            )

            # Execute tool through secure registry interface
            tool_result = await execute_tool(user, tool_name, args, registry=tool_registry, db=db)
            tool_call_count += 1
            if tool_name == "run_scan":
                run_scan_count += 1

            # Enforce maximum tool-result size (persisted + fed back to Claude)
            tool_result = _cap_tool_result(tool_result, services.agent_max_tool_result_chars())

            # Record tool result step
            await services.create_agent_run_step(
                user,
                run_id=run_id,
                thread_id=thread_id,
                step_type="tool_result",
                input_data={"tool": tool_name},
                output_data=tool_result,
                status="completed" if tool_result.get("success") else "failed",
                error=tool_result.get("error"),
                db=db,
            )

            # Append to turn steps for Claude's subsequent context
            turn_steps.append({"type": "tool_call", "tool": tool_name, "args": args})
            turn_steps.append({"type": "tool_result", "tool": tool_name, "result": tool_result})

        # Exceeded max turns without final response
        err_msg = f"Agent exceeded maximum turn limit of {MAX_CLAUDE_TURNS} without producing a final answer."
        await services.update_agent_run(
            user,
            run_id,
            {
                "status": services.RUN_STATUS_FAILED,
                "error": err_msg,
                "completed_at": services.now_iso(),
            },
            db=db,
        )
        return {"status": services.RUN_STATUS_FAILED, "error": err_msg}

    except Exception as e:
        logger.exception(f"Unexpected agent run failure for run '{run_id}': {e}")
        safe_err = "The agent run encountered an unexpected internal error and could not complete."
        try:
            await services.update_agent_run(
                user,
                run_id,
                {
                    "status": services.RUN_STATUS_FAILED,
                    "error": safe_err,
                    "completed_at": services.now_iso(),
                },
                db=db,
            )
        except Exception:
            pass
        return {"status": services.RUN_STATUS_FAILED, "error": safe_err}


    finally:
        async with _RUN_LOCK:
            _ACTIVE_RUNS.discard(run_id)
            _CANCELLED_RUNS.discard(run_id)


def start_agent_run_background(
    user: AuthedUser,
    run_id: str,
    thread_id: str,
    llm_provider: Optional[Any] = None,
    registry: Optional[Any] = None,
    db=None,
) -> asyncio.Task:
    """Schedule agent execution in the background using asyncio task."""
    task = asyncio.create_task(
        run_agent_loop(
            user,
            run_id,
            thread_id,
            llm_provider=llm_provider,
            registry=registry,
            db=db,
        )
    )
    return task


async def resume_agent_run(
    user: AuthedUser,
    approval_id: str,
    is_approved: bool,
    reason: Optional[str] = None,
    llm_provider: Optional[Any] = None,
    registry: Optional[Any] = None,
    db=None,
) -> Dict[str, Any]:
    """Resume a paused agent run after approval or rejection.

    SERVER-SIDE enforcement: the persisted approval status and run status are revalidated
    here — never trusting the `is_approved` flag alone — and execution is guarded by an
    atomic approved->executing claim so a consequential action runs exactly once.
    """
    approval = await approvals.get_approval(user, approval_id, db=db)  # 404 if not this firm
    run_id = approval["run_id"]
    thread_id = approval["thread_id"]
    tool_name = approval["tool_name"]
    proposed_args = approval["proposed_args"]

    tool_registry = registry or default_registry

    # Revalidate authorization at execution time: the run must belong to this tenant and
    # must not be a cancelled/failed terminal run (those may never execute a pending action).
    run_state = await services.get_agent_run(user, run_id, db=db)  # 404 if not this firm
    if run_state.get("status") in (services.RUN_STATUS_CANCELLED, services.RUN_STATUS_FAILED):
        return {"status": run_state.get("status"), "run_id": run_id, "reason": "run_not_resumable"}

    continue_loop = True

    if is_approved:
        if approval.get("status") == approvals.APPROVAL_STATUS_EXECUTED:
            # Idempotent: a previously executed approval is never run again.
            continue_loop = True
        else:
            won_claim = await services.claim_agent_approval_for_execution(user, approval_id, db=db)
            if not won_claim:
                # Not APPROVED (pending/expired/rejected) or another executor already holds
                # the claim. Do NOT execute; keep the run paused for the authoritative result.
                logger.info(
                    f"Approval '{approval_id}' not executed on resume (status={approval.get('status')}): "
                    "server-side authorization/exactly-once guard."
                )
                return {
                    "status": run_state.get("status") or services.RUN_STATUS_WAITING_FOR_APPROVAL,
                    "run_id": run_id,
                    "approval_id": approval_id,
                    "reason": "approval_not_executable",
                }
            # We hold the exclusive claim — execute the consequential tool with the exact
            # validated arguments stored at proposal time.
            try:
                tool_result = await execute_tool(
                    user,
                    tool_name,
                    proposed_args,
                    registry=tool_registry,
                    db=db,
                    is_approved=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Consequential tool '{tool_name}' failed on resume: {exc}")
                tool_result = {
                    "success": False,
                    "tool": tool_name,
                    "error": "Action execution failed.",
                    "error_type": "ExecutionError",
                }
            if tool_result.get("success"):
                await approvals.mark_approval_executed(user, approval_id, db=db)
                step_status = "completed"
            else:
                await services.mark_agent_approval_failed(
                    user, approval_id, error=tool_result.get("error"), db=db
                )
                step_status = "failed"
            await services.create_agent_run_step(
                user,
                run_id=run_id,
                thread_id=thread_id,
                step_type="tool_result",
                input_data={"tool": tool_name, "approval_id": approval_id},
                output_data=tool_result,
                status=step_status,
                error=tool_result.get("error"),
                db=db,
            )
    else:
        rejection_result = {
            "success": False,
            "tool": tool_name,
            "status": "rejected",
            "error": reason or "Action was rejected by user.",
            "error_type": "ActionRejected",
        }
        await services.create_agent_run_step(
            user,
            run_id=run_id,
            thread_id=thread_id,
            step_type="tool_result",
            input_data={"tool": tool_name, "approval_id": approval_id},
            output_data=rejection_result,
            status="rejected",
            error=rejection_result["error"],
            db=db,
        )

    if not continue_loop:
        return {"status": services.RUN_STATUS_WAITING_FOR_APPROVAL, "run_id": run_id, "approval_id": approval_id}

    # Set run status back to running (re-entrant lease for the resuming worker)
    await services.update_agent_run(
        user,
        run_id,
        {"status": services.RUN_STATUS_RUNNING},
        db=db,
    )

    # Continue loop
    return await run_agent_loop(
        user,
        run_id,
        thread_id,
        llm_provider=llm_provider,
        registry=registry,
        db=db,
        is_resume=True,
    )


def resume_agent_run_background(
    user: AuthedUser,
    approval_id: str,
    is_approved: bool,
    reason: Optional[str] = None,
    llm_provider: Optional[Any] = None,
    registry: Optional[Any] = None,
    db=None,
) -> asyncio.Task:
    """Schedule resuming a paused agent run in the background."""
    return asyncio.create_task(
        resume_agent_run(
            user,
            approval_id,
            is_approved=is_approved,
            reason=reason,
            llm_provider=llm_provider,
            registry=registry,
            db=db,
        )
    )
