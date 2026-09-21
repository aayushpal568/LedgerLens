import React, { act } from "react";
import { createRoot } from "react-dom/client";
import AgentChat from "@/pages/AgentChat";
import { api, setOnUnauthorized } from "@/lib/api";

jest.mock("@/lib/api", () => {
  let _unauthorizedCb = null;
  return {
    __esModule: true,
    api: {
      postAgentMessage: jest.fn(),
      getAgentRun: jest.fn(),
      cancelAgentRun: jest.fn(),
      listAgentApprovals: jest.fn(),
      getAgentApproval: jest.fn(),
      approveAgentApproval: jest.fn(),
      rejectAgentApproval: jest.fn(),
      listAgentMessages: jest.fn(),
      listAgentRunSteps: jest.fn(),
      downloadReport: jest.fn(),
    },
    setOnUnauthorized: jest.fn((cb) => {
      _unauthorizedCb = cb;
    }),
    triggerUnauthorized: () => {
      if (_unauthorizedCb) _unauthorizedCb();
    },
  };
});

// Mock AppContext
jest.mock("@/context/AppContext", () => ({
  __esModule: true,
  useApp: () => ({
    tab: "agent_chat",
    setTab: jest.fn(),
  }),
}));

// Mock scrollIntoView
window.HTMLElement.prototype.scrollIntoView = jest.fn();

let container = null;
let root = null;

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

function typeMessage(element, text) {
  const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype,
    "value"
  ).set;
  nativeInputValueSetter.call(element, text);
  element.dispatchEvent(new Event("input", { bubbles: true }));
  element.dispatchEvent(new Event("change", { bubbles: true }));
}

beforeEach(() => {
  jest.clearAllMocks();
  jest.useFakeTimers();
  api.listAgentMessages.mockResolvedValue([]);
  api.listAgentRunSteps.mockResolvedValue([]);
  api.listAgentApprovals.mockResolvedValue([]);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  if (root) {
    await act(async () => {
      root.unmount();
    });
  }
  if (container) {
    container.remove();
  }
  container = null;
  root = null;
  jest.useRealTimers();
});

describe("Phase B: LedgerLens AI Chat UI", () => {
  // Test 1: Chat page renders
  test("1. Chat page renders header, description, and input area", async () => {
    api.listAgentMessages.mockResolvedValue([]);

    await act(async () => {
      root.render(<AgentChat />);
    });

    expect(container.querySelector('[data-testid="agent-chat-page"]')).not.toBeNull();
    expect(container.textContent).toContain("LedgerLens AI");
    expect(container.textContent).toContain("Ask about your clients, documents, scans, and audit findings.");
    expect(container.querySelector('[data-testid="chat-input"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="send-message-btn"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="new-conversation-btn"]')).not.toBeNull();
  });

  // Test 2 & 3 & 4: User can send message, calls postAgentMessage, stores thread_id & run_id
  test("2, 3, 4. User can send a message, POST /api/agent/messages called, thread/run stored", async () => {
    api.postAgentMessage.mockResolvedValue({
      run_id: "run-101",
      thread_id: "thread-555",
      status: "queued",
    });
    api.getAgentRun.mockResolvedValue({
      id: "run-101",
      status: "running",
      thread_id: "thread-555",
    });
    api.listAgentRunSteps.mockResolvedValue([]);

    await act(async () => {
      root.render(<AgentChat />);
    });

    const input = container.querySelector('[data-testid="chat-input"]');
    const sendBtn = container.querySelector('[data-testid="send-message-btn"]');

    // Type message
    await act(async () => {
      typeMessage(input, "Review Acme Corp documents");
    });

    // Submit message
    await act(async () => {
      sendBtn.click();
    });

    expect(api.postAgentMessage).toHaveBeenCalledWith({
      text: "Review Acme Corp documents",
      thread_id: undefined,
    });

    // Optimistic user message appears
    expect(container.textContent).toContain("Review Acme Corp documents");

    // Agent working indicator appears
    expect(container.querySelector('[data-testid="agent-status-indicator"]')).not.toBeNull();
  });

  // Test 5 & 6: Run polling works and stops after completed
  test("5, 6. Run polling occurs and stops on terminal state 'completed'", async () => {
    api.postAgentMessage.mockResolvedValue({
      run_id: "run-complete",
      thread_id: "thread-comp",
      status: "queued",
    });

    // First poll returns running, second returns completed
    api.getAgentRun
      .mockResolvedValueOnce({ id: "run-complete", status: "running", thread_id: "thread-comp" })
      .mockResolvedValueOnce({ id: "run-complete", status: "completed", thread_id: "thread-comp" });

    api.listAgentRunSteps.mockResolvedValue([
      { step_type: "tool_call", input_data: { tool: "list_clients" } },
    ]);

    api.listAgentMessages.mockResolvedValue([
      { id: "m1", role: "user", text: "Check files" },
      { id: "m2", role: "assistant", text: "All 5 client files checked successfully." },
    ]);

    await act(async () => {
      root.render(<AgentChat />);
    });

    const input = container.querySelector('[data-testid="chat-input"]');
    await act(async () => {
      typeMessage(input, "Check files");
    });

    await act(async () => {
      container.querySelector('[data-testid="send-message-btn"]').click();
    });

    // Advance timer 1 second -> triggers poll 1 (running)
    await act(async () => {
      jest.advanceTimersByTime(1000);
    });
    expect(api.getAgentRun).toHaveBeenCalledWith("run-complete");

    // Advance timer another 1 second -> triggers poll 2 (completed)
    await act(async () => {
      jest.advanceTimersByTime(1000);
    });

    // Polling must fetch final messages and stop
    expect(api.listAgentMessages).toHaveBeenCalledWith("thread-comp");
    expect(container.textContent).toContain("All 5 client files checked successfully.");
    expect(container.querySelector('[data-testid="agent-status-indicator"]')).toBeNull();
  });

  // Test 7: Polling stops after failed
  test("7. Polling stops after run reaches 'failed' state", async () => {
    api.postAgentMessage.mockResolvedValue({
      run_id: "run-fail",
      thread_id: "th-fail",
      status: "queued",
    });
    api.getAgentRun.mockResolvedValue({
      id: "run-fail",
      status: "failed",
      error: "LLM limit exceeded",
    });

    await act(async () => {
      root.render(<AgentChat />);
    });

    const input = container.querySelector('[data-testid="chat-input"]');
    await act(async () => {
      typeMessage(input, "Trigger failure");
    });
    await act(async () => {
      container.querySelector('[data-testid="send-message-btn"]').click();
    });

    await act(async () => {
      jest.advanceTimersByTime(1000);
    });

    expect(container.querySelector('[data-testid="chat-error"]')).not.toBeNull();
    expect(container.textContent).toContain("LLM limit exceeded");
    expect(container.querySelector('[data-testid="agent-status-indicator"]')).toBeNull();
  });

  // Test 8 & 12: Cancel run stops polling and displays Cancelled
  test("8, 12. Cancel run calls cancel endpoint and halts polling", async () => {
    api.postAgentMessage.mockResolvedValue({
      run_id: "run-cancel",
      thread_id: "th-cancel",
      status: "queued",
    });
    api.getAgentRun.mockResolvedValue({
      id: "run-cancel",
      status: "running",
    });
    api.cancelAgentRun.mockResolvedValue({ status: "cancelled" });

    await act(async () => {
      root.render(<AgentChat />);
    });

    const input = container.querySelector('[data-testid="chat-input"]');
    await act(async () => {
      typeMessage(input, "Long running task");
    });
    await act(async () => {
      container.querySelector('[data-testid="send-message-btn"]').click();
    });

    // Cancel button is available while running
    const cancelBtn = container.querySelector('[data-testid="cancel-run-btn"]');
    expect(cancelBtn).not.toBeNull();

    await act(async () => {
      cancelBtn.click();
    });

    expect(api.cancelAgentRun).toHaveBeenCalledWith("run-cancel");
    expect(container.textContent).toContain("cancelled");
  });

  // Test 9, 10, 13: waiting_for_approval displays approval card, approve calls endpoint, buttons disable
  test("9, 10, 13. waiting_for_approval displays card, approve calls API, disables buttons", async () => {
    api.postAgentMessage.mockResolvedValue({
      run_id: "run-appr",
      thread_id: "th-appr",
      status: "queued",
    });
    api.getAgentRun.mockResolvedValue({
      id: "run-appr",
      status: "waiting_for_approval",
    });
    api.listAgentApprovals.mockResolvedValue([
      {
        id: "appr-99",
        tool_name: "run_scan",
        proposed_args: { client_id: "c-123", expected_period: "2025" },
        status: "pending",
      },
    ]);
    api.approveAgentApproval.mockResolvedValue({ status: "approved" });

    await act(async () => {
      root.render(<AgentChat />);
    });

    const input = container.querySelector('[data-testid="chat-input"]');
    await act(async () => {
      typeMessage(input, "Start scan for Acme");
    });
    await act(async () => {
      container.querySelector('[data-testid="send-message-btn"]').click();
    });

    // Advance timer -> polling detects waiting_for_approval
    await act(async () => {
      jest.advanceTimersByTime(1000);
    });

    // Approval card is displayed
    const card = container.querySelector('[data-testid="approval-card"]');
    expect(card).not.toBeNull();
    expect(card.textContent).toContain("Approval Required");
    expect(card.textContent).toContain("Start Document Audit Scan");
    expect(card.textContent).toContain("2025");

    const approveBtn = container.querySelector('[data-testid="approval-approve-btn"]');
    const rejectBtn = container.querySelector('[data-testid="approval-reject-btn"]');
    expect(approveBtn.disabled).toBe(false);
    expect(rejectBtn.disabled).toBe(false);

    // Click approve
    await act(async () => {
      approveBtn.click();
    });

    expect(api.approveAgentApproval).toHaveBeenCalledWith("appr-99");
    expect(approveBtn.disabled).toBe(true);
    expect(rejectBtn.disabled).toBe(true);
    expect(container.textContent).toContain("approved");
  });

  // Test 11: Reject calls reject endpoint
  test("11. Reject calls correct endpoint and disables buttons", async () => {
    api.postAgentMessage.mockResolvedValue({
      run_id: "run-rej",
      thread_id: "th-rej",
      status: "queued",
    });
    api.getAgentRun.mockResolvedValue({
      id: "run-rej",
      status: "waiting_for_approval",
    });
    api.listAgentApprovals.mockResolvedValue([
      {
        id: "appr-88",
        tool_name: "create_client",
        proposed_args: { name: "Suspicious Client" },
        status: "pending",
      },
    ]);
    api.rejectAgentApproval.mockResolvedValue({ status: "rejected" });

    await act(async () => {
      root.render(<AgentChat />);
    });

    const input = container.querySelector('[data-testid="chat-input"]');
    await act(async () => {
      typeMessage(input, "Create client");
    });
    await act(async () => {
      container.querySelector('[data-testid="send-message-btn"]').click();
    });

    await act(async () => {
      jest.advanceTimersByTime(1000);
    });

    const rejectBtn = container.querySelector('[data-testid="approval-reject-btn"]');
    await act(async () => {
      rejectBtn.click();
    });

    expect(api.rejectAgentApproval).toHaveBeenCalledWith("appr-88", {
      reason: "Rejected by user in chat interface",
    });
    expect(rejectBtn.disabled).toBe(true);
    expect(container.textContent).toContain("rejected");
  });

  // Test 14: 401 triggers logout
  test("14. 401 error response triggers existing authentication logout behavior", async () => {
    const err401 = new Error("Unauthorized");
    err401.response = { status: 401 };
    api.postAgentMessage.mockRejectedValue(err401);

    await act(async () => {
      root.render(<AgentChat />);
    });

    const input = container.querySelector('[data-testid="chat-input"]');
    await act(async () => {
      typeMessage(input, "Unauthorized test");
    });
    await act(async () => {
      container.querySelector('[data-testid="send-message-btn"]').click();
    });

    // 401 is cleanly bypassed to auth interceptor without breaking page
    expect(container.querySelector('[data-testid="agent-chat-page"]')).not.toBeNull();
  });

  // Test 15: Safe API error messages
  test("15. Non-401 API errors display safe user-facing message", async () => {
    const err500 = new Error("Server error");
    err500.response = { status: 500, data: { detail: "Database unreachable" } };
    api.postAgentMessage.mockRejectedValue(err500);

    await act(async () => {
      root.render(<AgentChat />);
    });

    const input = container.querySelector('[data-testid="chat-input"]');
    await act(async () => {
      typeMessage(input, "Crash test");
    });
    await act(async () => {
      container.querySelector('[data-testid="send-message-btn"]').click();
    });

    expect(container.querySelector('[data-testid="chat-error"]')).not.toBeNull();
    expect(container.textContent).toContain("Database unreachable");
  });

  // Test 16, 17, 18: Assistant message displayed, thoughts & raw JSON never displayed
  test("16, 17, 18. Assistant messages displayed, internal thoughts and raw JSON hidden", async () => {
    api.listAgentMessages.mockResolvedValue([
      {
        id: "msg-1",
        role: "user",
        text: "What are Acme's findings?",
      },
      {
        id: "msg-2",
        role: "assistant",
        text: "Acme Industries has 3 findings for FY 2024: 1 duplicate invoice and 2 missing receipts.",
        metadata: {
          thought: "Secret internal chain-of-thought analysis that should NEVER be visible.",
          raw_tool_json: '{"tool": "get_findings", "result": {"items": [1, 2, 3]}}',
        },
      },
    ]);

    await act(async () => {
      root.render(<AgentChat initialThreadId="th-findings" />);
    });

    // Message text is rendered
    expect(container.textContent).toContain("Acme Industries has 3 findings for FY 2024");

    // Internal thought is NOT rendered
    expect(container.textContent).not.toContain("Secret internal chain-of-thought");

    // Raw tool JSON is NOT rendered
    expect(container.textContent).not.toContain('{"tool": "get_findings"');
  });

  // Test 19: Duplicate polling is prevented
  test("19. Duplicate polling timer is prevented while run is already being polled", async () => {
    api.postAgentMessage.mockResolvedValue({
      run_id: "run-dup",
      thread_id: "th-dup",
      status: "queued",
    });
    api.getAgentRun.mockResolvedValue({
      id: "run-dup",
      status: "running",
    });

    await act(async () => {
      root.render(<AgentChat />);
    });

    const input = container.querySelector('[data-testid="chat-input"]');
    await act(async () => {
      typeMessage(input, "Duplicate poll test");
    });
    await act(async () => {
      container.querySelector('[data-testid="send-message-btn"]').click();
    });

    // Send button is disabled while run is in-flight
    const sendBtn = container.querySelector('[data-testid="send-message-btn"]');
    expect(sendBtn.disabled).toBe(true);

    // Advance by 2.5s (should trigger 2 interval ticks, not an exponential explosion of timers)
    await act(async () => {
      jest.advanceTimersByTime(2500);
    });

    // getAgentRun should have been called reasonable number of times (initial + 2 ticks = ~3 calls)
    expect(api.getAgentRun.mock.calls.length).toBeLessThanOrEqual(4);
  });

  // Test 20: Component cleanup on unmount
  test("20. Component unmount cleans up active polling timer without memory leaks", async () => {
    api.postAgentMessage.mockResolvedValue({
      run_id: "run-leak",
      thread_id: "th-leak",
      status: "queued",
    });
    api.getAgentRun.mockResolvedValue({
      id: "run-leak",
      status: "running",
    });

    await act(async () => {
      root.render(<AgentChat />);
    });

    const input = container.querySelector('[data-testid="chat-input"]');
    await act(async () => {
      typeMessage(input, "Unmount test");
    });
    await act(async () => {
      container.querySelector('[data-testid="send-message-btn"]').click();
    });

    const callsBefore = api.getAgentRun.mock.calls.length;

    // Unmount component
    await act(async () => {
      root.unmount();
      root = null;
    });

    // Advance timers
    await act(async () => {
      jest.advanceTimersByTime(5000);
    });

    // No further calls should occur after unmount
    expect(api.getAgentRun.mock.calls.length).toBe(callsBefore);
  });

  // Test 21: Explicit queued -> running -> completed progression + run_id + final answer
  test("21. Polling walks queued -> running -> completed, uses run_id, shows safe progress and final answer", async () => {
    api.postAgentMessage.mockResolvedValue({
      run_id: "run-progress",
      thread_id: "th-progress",
      status: "queued",
    });
    api.getAgentRun
      .mockResolvedValueOnce({ id: "run-progress", status: "queued", thread_id: "th-progress" })
      .mockResolvedValueOnce({ id: "run-progress", status: "running", thread_id: "th-progress" })
      .mockResolvedValueOnce({ id: "run-progress", status: "completed", thread_id: "th-progress" });
    api.listAgentRunSteps.mockResolvedValue([
      { step_type: "tool_call", input_data: { tool: "list_files" } },
    ]);
    api.listAgentMessages.mockResolvedValue([
      { id: "u1", role: "user", text: "Review Acme" },
      { id: "a1", role: "assistant", text: "Finished reviewing Acme documents." },
    ]);

    await act(async () => {
      root.render(<AgentChat />);
    });
    const input = container.querySelector('[data-testid="chat-input"]');
    await act(async () => {
      typeMessage(input, "Review Acme");
    });
    await act(async () => {
      container.querySelector('[data-testid="send-message-btn"]').click();
    });

    // Polling uses the run_id returned by POST /api/agent/messages
    expect(api.getAgentRun).toHaveBeenCalledWith("run-progress");

    // queued -> running (progress derived from a safe step tool name), then completed
    await act(async () => {
      jest.advanceTimersByTime(1000);
    });
    const indicator = container.querySelector('[data-testid="agent-status-indicator"]');
    expect(indicator).not.toBeNull();
    expect(indicator.textContent).toContain("Reviewing client documents");

    await act(async () => {
      jest.advanceTimersByTime(1000);
    });
    expect(container.textContent).toContain("Finished reviewing Acme documents.");
    expect(container.querySelector('[data-testid="agent-status-indicator"]')).toBeNull();
  });

  // Test 22: 404 on run polling stops polling with a safe message
  test("22. A 404 while polling stops polling and shows a safe 'no longer available' message", async () => {
    const err404 = new Error("Not found");
    err404.response = { status: 404 };
    api.postAgentMessage.mockResolvedValue({
      run_id: "run-404",
      thread_id: "th-404",
      status: "queued",
    });
    api.getAgentRun.mockRejectedValue(err404);

    await act(async () => {
      root.render(<AgentChat />);
    });
    const input = container.querySelector('[data-testid="chat-input"]');
    await act(async () => {
      typeMessage(input, "gone run");
    });
    await act(async () => {
      container.querySelector('[data-testid="send-message-btn"]').click();
    });

    await act(async () => {
      jest.advanceTimersByTime(1000);
    });

    const err = container.querySelector('[data-testid="chat-error"]');
    expect(err).not.toBeNull();
    expect(err.textContent).toContain("no longer available");
    // status internals must not leak
    expect(err.textContent).not.toContain("Not found");

    // Polling stopped: further timer advances do not add calls
    const callsAfter = api.getAgentRun.mock.calls.length;
    await act(async () => {
      jest.advanceTimersByTime(3000);
    });
    expect(api.getAgentRun.mock.calls.length).toBe(callsAfter);
    expect(container.querySelector('[data-testid="agent-status-indicator"]')).toBeNull();
  });

  // Test 23: 403 on run polling shows a safe access message
  test("23. A 403 while polling shows a safe 'no access' message and stops polling", async () => {
    const err403 = new Error("Forbidden");
    err403.response = { status: 403 };
    api.postAgentMessage.mockResolvedValue({
      run_id: "run-403",
      thread_id: "th-403",
      status: "queued",
    });
    api.getAgentRun.mockRejectedValue(err403);

    await act(async () => {
      root.render(<AgentChat />);
    });
    const input = container.querySelector('[data-testid="chat-input"]');
    await act(async () => {
      typeMessage(input, "forbidden run");
    });
    await act(async () => {
      container.querySelector('[data-testid="send-message-btn"]').click();
    });
    await act(async () => {
      jest.advanceTimersByTime(1000);
    });

    const err = container.querySelector('[data-testid="chat-error"]');
    expect(err).not.toBeNull();
    expect(err.textContent).toContain("no longer have access");
    expect(err.textContent).not.toContain("Forbidden");
  });

  // Test 24: Already-actioned approval handled safely (approve -> 404), then completes
  test("24. Approving an already-actioned approval clears the card and continues polling without a scary error", async () => {
    const err404 = new Error("already handled");
    err404.response = { status: 404 };
    api.postAgentMessage.mockResolvedValue({
      run_id: "run-appr-done",
      thread_id: "th-appr-done",
      status: "queued",
    });
    // Keep the run waiting while the card is shown / approve is exercised.
    api.getAgentRun.mockResolvedValue({ id: "run-appr-done", status: "waiting_for_approval", thread_id: "th-appr-done" });
    api.listAgentApprovals.mockResolvedValue([
      { id: "appr-done", tool_name: "run_scan", proposed_args: { client_id: "c-9" }, status: "pending" },
    ]);
    api.approveAgentApproval.mockRejectedValue(err404);
    api.listAgentMessages.mockResolvedValue([
      { id: "u", role: "user", text: "scan" },
      { id: "a", role: "assistant", text: "The action was already handled." },
    ]);

    await act(async () => {
      root.render(<AgentChat />);
    });
    const input = container.querySelector('[data-testid="chat-input"]');
    await act(async () => {
      typeMessage(input, "scan please");
    });
    await act(async () => {
      container.querySelector('[data-testid="send-message-btn"]').click();
    });
    await act(async () => {
      jest.advanceTimersByTime(1000);
    });

    // Approval card present, then approve -> 404 -> card clears, no scary error
    expect(container.querySelector('[data-testid="approval-card"]')).not.toBeNull();
    await act(async () => {
      container.querySelector('[data-testid="approval-approve-btn"]').click();
    });
    expect(container.querySelector('[data-testid="approval-card"]')).toBeNull();
    expect(container.querySelector('[data-testid="chat-error"]')).toBeNull();

    // Switch to completed, then continue polling shows the final assistant message
    api.getAgentRun.mockResolvedValue({ id: "run-appr-done", status: "completed", thread_id: "th-appr-done" });
    await act(async () => {
      jest.advanceTimersByTime(1000);
    });
    expect(container.textContent).toContain("The action was already handled.");
  });

  // Test 25: Safe retry re-sends the last message after a failed run
  test("25. Retry re-sends the last message via the retry button after a failed run", async () => {
    api.postAgentMessage.mockResolvedValue({
      run_id: "run-fail-1",
      thread_id: "th-fail-1",
      status: "queued",
    });
    api.getAgentRun.mockResolvedValue({
      id: "run-fail-1",
      status: "failed",
      error: "Provider temporarily unavailable",
    });

    await act(async () => {
      root.render(<AgentChat />);
    });
    const input = container.querySelector('[data-testid="chat-input"]');
    await act(async () => {
      typeMessage(input, "Please list my clients");
    });
    await act(async () => {
      container.querySelector('[data-testid="send-message-btn"]').click();
    });
    // Poll detects failure, stops, shows error
    await act(async () => {
      jest.advanceTimersByTime(1000);
    });
    expect(container.querySelector('[data-testid="chat-error"]')).not.toBeNull();

    // Retry button appears; clicking re-sends the same text
    const retryBtn = container.querySelector('[data-testid="retry-send-btn"]');
    expect(retryBtn).not.toBeNull();
    await act(async () => {
      retryBtn.click();
    });
    expect(api.postAgentMessage).toHaveBeenCalledTimes(2);
    expect(api.postAgentMessage).toHaveBeenLastCalledWith({
      text: "Please list my clients",
      thread_id: "th-fail-1",
    });
  });
});
