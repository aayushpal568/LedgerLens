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
    # 1. Idempotency and run state check
    async with _RUN_LOCK:
        if run_id in _ACTIVE_RUNS:
            logger.warning(f"Agent run '{run_id}' is already active. Ignoring duplicate invocation.")
            return {"status": "already_active", "run_id": run_id}

        # Check existing DB run status
        try:
            current_run = await services.get_agent_run(user, run_id, db=db)
        except Exception as e:
            logger.error(f"Cannot load agent run '{run_id}': {e}")
            return {"status": "error", "error": str(e)}

        allowed_statuses = (
            {services.RUN_STATUS_QUEUED}
            if not is_resume
            else {
                services.RUN_STATUS_QUEUED,
                services.RUN_STATUS_RUNNING,
                services.RUN_STATUS_WAITING_FOR_APPROVAL,
            }
        )
        if current_run.get("status") not in allowed_statuses:
            logger.warning(
                f"Agent run '{run_id}' status is '{current_run.get('status')}', expected one of {allowed_statuses}. Skipping."
            )
            return {"status": current_run.get("status"), "run_id": run_id}

        _ACTIVE_RUNS.add(run_id)

    tool_registry = registry or default_registry
    provider = llm_provider or get_llm_provider()

    try:
        # Check initial cancellation
        if is_run_cancelled(run_id):
            await services.update_agent_run(
                user,
                run_id,
                {"status": services.RUN_STATUS_CANCELLED, "completed_at": services.now_iso()},
                db=db,
            )
            return {"status": services.RUN_STATUS_CANCELLED}

        # Mark run running
        await services.update_agent_run(
            user,
            run_id,
            {"status": services.RUN_STATUS_RUNNING, "started_at": services.now_iso()},
            db=db,
        )

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

            # Check cancellation before calling Claude (checks both in-memory set and DB)
            cancelled = is_run_cancelled(run_id)
            if not cancelled and db is not None:
                try:
                    db_run = await services.get_agent_run(user, run_id, db=db)
                    if db_run.get("status") == services.RUN_STATUS_CANCELLED:
                        cancelled = True
                except Exception:
                    pass

            if cancelled:
                await services.update_agent_run(
                    user,
                    run_id,
                    {"status": services.RUN_STATUS_CANCELLED, "completed_at": services.now_iso()},
                    db=db,
                )
                return {"status": services.RUN_STATUS_CANCELLED}

            user_prompt = format_turn_prompt(bounded_messages, turn_steps)

            last_invocation_error = None

            async def _invoke_claude(prompt_text: str, token_budget: int) -> Optional[str]:
                nonlocal last_invocation_error
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
                    err_msg = f"Claude provider execution failed: {last_invocation_error}"
                else:
                    err_msg = "Claude provider returned an empty response or is unavailable."
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

                # Store visible assistant message (NEVER expose thought in message text)
                msg_id = services.new_id()
                t_now = services.now_iso()
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
                active_db = services._get_db(db)
                await active_db.agent_messages.insert_one(msg_doc)

                # Mark run completed
                await services.update_agent_run(
                    user,
                    run_id,
                    {"status": services.RUN_STATUS_COMPLETED, "completed_at": t_now},
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

            # Check cancellation before tool execution
            if is_run_cancelled(run_id):
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
    """Resume a paused agent run after approval or rejection."""
    approval = await approvals.get_approval(user, approval_id, db=db)
    run_id = approval["run_id"]
    thread_id = approval["thread_id"]
    tool_name = approval["tool_name"]
    proposed_args = approval["proposed_args"]

    tool_registry = registry or default_registry

    if is_approved:
        # Execute tool with exact proposed arguments and approved flag
        tool_result = await execute_tool(
            user,
            tool_name,
            proposed_args,
            registry=tool_registry,
            db=db,
            is_approved=True,
        )
        await approvals.mark_approval_executed(user, approval_id, db=db)
        await services.create_agent_run_step(
            user,
            run_id=run_id,
            thread_id=thread_id,
            step_type="tool_result",
            input_data={"tool": tool_name, "approval_id": approval_id},
            output_data=tool_result,
            status="completed" if tool_result.get("success") else "failed",
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

    # Set run status back to running
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
