import React, { useState, useEffect, useRef, useCallback } from "react";
import { api } from "@/lib/api";
import { useApp } from "@/context/AppContext";
import { Button, Card, Badge } from "@/components/ui";
import {
  Bot,
  Send,
  Loader2,
  AlertCircle,
  CheckCircle2,
  XCircle,
  Sparkles,
  RotateCcw,
  ShieldCheck,
  FileSpreadsheet,
  ExternalLink,
} from "lucide-react";

const TOOL_STATUS_MAP = {
  list_clients: "Checking your clients...",
  list_files: "Reviewing client documents...",
  list_templates: "Checking checklist templates...",
  run_scan: "Initiating document audit scan...",
  get_scan_status: "Inspecting scan progress...",
  get_findings: "Querying audit findings...",
  summarize_findings: "Preparing audit findings summary...",
  create_client: "Creating client record...",
  set_finding_review: "Updating finding review disposition...",
  get_agent_run_status: "Checking agent status...",
};

const ACTION_TITLE_MAP = {
  run_scan: "Start Document Audit Scan",
  create_client: "Create New Client Entity",
  set_finding_review: "Update Finding Review",
};

export default function AgentChat({ initialThreadId = null, initialMessages = [] } = {}) {
  const { setTab } = useApp?.() || {};

  const [messages, setMessages] = useState(initialMessages);
  const [inputText, setInputText] = useState("");
  const [threadId, setThreadId] = useState(initialThreadId);
  const [activeRunId, setActiveRunId] = useState(null);
  const [runStatus, setRunStatus] = useState(null);
  const [statusMessage, setStatusMessage] = useState(null);
  const [pendingApproval, setPendingApproval] = useState(null);
  const [approvalActionStatus, setApprovalActionStatus] = useState(null);
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState(null);

  const messagesEndRef = useRef(null);
  const pollingRef = useRef(null);
  const isPollingRef = useRef(false);

  // Auto-scroll to bottom of messages
  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, statusMessage, pendingApproval, scrollToBottom]);

  // Load existing messages if threadId changes
  useEffect(() => {
    let isMounted = true;
    if (threadId && !activeRunId && api?.listAgentMessages) {
      Promise.resolve(api.listAgentMessages(threadId))
        .then((data) => {
          if (isMounted && Array.isArray(data)) {
            setMessages(data);
          }
        })
        .catch(() => {});
    }
    return () => {
      isMounted = false;
    };
  }, [threadId, activeRunId]);

  // Handle active run polling
  useEffect(() => {
    if (!activeRunId) {
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
      return;
    }

    // Prevent duplicate polling intervals
    if (pollingRef.current) {
      return;
    }

    const pollRun = async () => {
      if (isPollingRef.current) return;
      isPollingRef.current = true;

      try {
        const run = await api.getAgentRun(activeRunId);
        const status = run.status;
        setRunStatus(status);

        if (status === "waiting_for_approval") {
          setStatusMessage("Waiting for your approval...");
          // Fetch pending approval details
          const approvals = await api.listAgentApprovals({
            run_id: activeRunId,
            status: "pending",
          });
          if (approvals && approvals.length > 0) {
            setPendingApproval(approvals[0]);
          }
        } else if (status === "queued") {
          setStatusMessage("Agent run is queued...");
        } else if (status === "running") {
          setPendingApproval(null);
          // Try to fetch latest step for user-friendly progress
          try {
            const steps = await api.listAgentRunSteps(activeRunId);
            if (steps && steps.length > 0) {
              const lastStep = steps[steps.length - 1];
              const toolName = lastStep.input_data?.tool;
              setStatusMessage(
                TOOL_STATUS_MAP[toolName] || "Analyzing accounting records..."
              );
            } else {
              setStatusMessage("Agent is reviewing accounting data...");
            }
          } catch {
            setStatusMessage("Agent is thinking...");
          }
        } else if (status === "completed") {
          // Terminal state: completed
          if (pollingRef.current) {
            clearInterval(pollingRef.current);
            pollingRef.current = null;
          }
          setActiveRunId(null);
          setStatusMessage(null);
          setPendingApproval(null);

          // Fetch final conversation message stream
          const currentThreadId = run.thread_id || threadId;
          if (currentThreadId) {
            const freshMsgs = await api.listAgentMessages(currentThreadId);
            if (Array.isArray(freshMsgs)) {
              setMessages(freshMsgs);
            }
          }
        } else if (status === "failed") {
          // Terminal state: failed
          if (pollingRef.current) {
            clearInterval(pollingRef.current);
            pollingRef.current = null;
          }
          setActiveRunId(null);
          setStatusMessage(null);
          setPendingApproval(null);
          setError(run.error || "The agent run could not be completed.");
        } else if (status === "cancelled") {
          // Terminal state: cancelled
          if (pollingRef.current) {
            clearInterval(pollingRef.current);
            pollingRef.current = null;
          }
          setActiveRunId(null);
          setStatusMessage("Agent run was cancelled.");
          setPendingApproval(null);
        }
      } catch (err) {
        if (err?.response?.status === 401) {
          // Handled by global auth interceptor
          return;
        }
        setError("Could not update agent status. Will retry...");
      } finally {
        isPollingRef.current = false;
      }
    };

    // Run immediate check then interval
    pollRun();
    pollingRef.current = setInterval(pollRun, 1000);

    return () => {
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
    };
  }, [activeRunId, threadId]);

  // Send a user message
  const handleSendMessage = async (e) => {
    if (e) e.preventDefault();
    const text = inputText.trim();
    if (!text || isSending || activeRunId) return;

    setIsSending(true);
    setError(null);

    // Optimistic UI message
    const tempUserMsg = {
      id: `temp-${Date.now()}`,
      role: "user",
      text,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, tempUserMsg]);
    setInputText("");

    try {
      const payload = {
        text,
        thread_id: threadId || undefined,
      };
      const res = await api.postAgentMessage(payload);

      setThreadId(res.thread_id);
      setActiveRunId(res.run_id);
      setRunStatus("queued");
      setStatusMessage("Agent is starting...");
      setPendingApproval(null);
      setApprovalActionStatus(null);
    } catch (err) {
      if (err?.response?.status === 401) return;
      const msg =
        err?.response?.data?.detail ||
        err?.response?.data?.message ||
        "Failed to send message. Please try again.";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    } finally {
      setIsSending(false);
    }
  };

  // Keyboard navigation for sending: Enter sends, Shift+Enter newlines
  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSendMessage();
    }
  };

  // Approve action
  const handleApprove = async () => {
    if (!pendingApproval || approvalActionStatus) return;
    try {
      setApprovalActionStatus("approved");
      setStatusMessage("Approved. Executing action...");
      await api.approveAgentApproval(pendingApproval.id);
    } catch (err) {
      if (err?.response?.status === 401) return;
      const msg = err?.response?.data?.detail || "Could not approve action.";
      setError(typeof msg === "string" ? msg : "Approval error occurred.");
      setApprovalActionStatus(null);
    }
  };

  // Reject action
  const handleReject = async () => {
    if (!pendingApproval || approvalActionStatus) return;
    try {
      setApprovalActionStatus("rejected");
      setStatusMessage("Action rejected. Continuing agent...");
      await api.rejectAgentApproval(pendingApproval.id, {
        reason: "Rejected by user in chat interface",
      });
    } catch (err) {
      if (err?.response?.status === 401) return;
      const msg = err?.response?.data?.detail || "Could not reject action.";
      setError(typeof msg === "string" ? msg : "Rejection error occurred.");
      setApprovalActionStatus(null);
    }
  };

  // Cancel running run
  const handleCancelRun = async () => {
    if (!activeRunId) return;
    try {
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
      await api.cancelAgentRun(activeRunId);
      setRunStatus("cancelled");
      setStatusMessage("Agent run was cancelled.");
      setActiveRunId(null);
      setPendingApproval(null);
    } catch (err) {
      if (err?.response?.status === 401) return;
      setError("Could not cancel run.");
    }
  };

  // Start a fresh conversation
  const handleNewConversation = () => {
    if (activeRunId && pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
    setThreadId(null);
    setActiveRunId(null);
    setRunStatus(null);
    setStatusMessage(null);
    setMessages([]);
    setPendingApproval(null);
    setApprovalActionStatus(null);
    setError(null);
    setInputText("");
  };

  const isWorking =
    activeRunId && ["queued", "running", "waiting_for_approval"].includes(runStatus);

  return (
    <div
      className="flex flex-col h-full bg-background text-foreground"
      data-testid="agent-chat-page"
    >
      {/* Header */}
      <header className="px-6 py-4 border-b border-border bg-card/60 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-xl bg-primary/10 text-primary">
            <Sparkles className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-base font-semibold tracking-tight text-foreground flex items-center gap-2">
              LedgerLens AI
              <Badge variant="outline" className="text-[10px] font-normal py-0">
                Assistant
              </Badge>
            </h1>
            <p className="text-xs text-muted-foreground">
              Ask about your clients, documents, scans, and audit findings.
            </p>
          </div>
        </div>

        <Button
          variant="outline"
          size="sm"
          onClick={handleNewConversation}
          disabled={isSending}
          data-testid="new-conversation-btn"
          className="text-xs gap-1.5"
        >
          <RotateCcw className="h-3.5 w-3.5" />
          New Conversation
        </Button>
      </header>

      {/* Messages Area */}
      <div
        className="flex-1 overflow-y-auto p-6 space-y-4 min-h-0"
        data-testid="messages-container"
      >
        {messages.length === 0 && !activeRunId && (
          <div className="h-full flex flex-col items-center justify-center text-center p-8 max-w-md mx-auto space-y-4 text-muted-foreground">
            <div className="w-12 h-12 rounded-2xl bg-secondary flex items-center justify-center text-primary shadow-sm">
              <Bot className="h-6 w-6" />
            </div>
            <div>
              <h2 className="text-sm font-semibold text-foreground">
                How can LedgerLens AI help today?
              </h2>
              <p className="text-xs mt-1 text-muted-foreground">
                Ask about client files, run automated audit scans, or review exception
                findings across your firm.
              </p>
            </div>
            <div className="grid grid-cols-1 gap-2 w-full text-xs text-left">
              <button
                type="button"
                onClick={() => setInputText("List all clients in my firm")}
                className="p-2.5 rounded-lg border border-border bg-card hover:bg-secondary transition-colors text-foreground"
              >
                "List all clients in my firm"
              </button>
              <button
                type="button"
                onClick={() => setInputText("What templates are available?")}
                className="p-2.5 rounded-lg border border-border bg-card hover:bg-secondary transition-colors text-foreground"
              >
                "What templates are available?"
              </button>
              <button
                type="button"
                onClick={() =>
                  setInputText("Check findings for my recent audit scan")
                }
                className="p-2.5 rounded-lg border border-border bg-card hover:bg-secondary transition-colors text-foreground"
              >
                "Check findings for my recent audit scan"
              </button>
            </div>
          </div>
        )}

        {messages.map((msg) => {
          const isUser = msg.role === "user";
          return (
            <div
              key={msg.id}
              data-testid={isUser ? "user-message" : "assistant-message"}
              className={`flex gap-3 max-w-3xl ${
                isUser ? "ml-auto justify-end" : "mr-auto justify-start"
              }`}
            >
              {!isUser && (
                <div className="w-8 h-8 rounded-lg bg-primary/10 text-primary flex items-center justify-center shrink-0 mt-0.5">
                  <Bot className="h-4 w-4" />
                </div>
              )}

              <div
                className={`rounded-2xl px-4 py-3 text-sm leading-relaxed ${
                  isUser
                    ? "bg-primary text-primary-foreground shadow-sm"
                    : "bg-card border border-border text-foreground shadow-sm"
                }`}
              >
                {/* Message Text (Strictly user-facing, never raw JSON or thought) */}
                <div className="whitespace-pre-wrap break-words">{msg.text}</div>

                {/* Helpful Action Links if the assistant refers to reviews or reports */}
                {!isUser &&
                  (msg.text.toLowerCase().includes("finding") ||
                    msg.text.toLowerCase().includes("scan completed")) &&
                  setTab && (
                    <div className="mt-3 pt-2.5 border-t border-border/60 flex items-center gap-2">
                      <Button
                        variant="secondary"
                        size="sm"
                        className="text-xs h-7 gap-1"
                        onClick={() => setTab("review_center")}
                      >
                        <ShieldCheck className="h-3.5 w-3.5" />
                        View Findings
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-xs h-7 gap-1 text-muted-foreground"
                        onClick={() => setTab("reports")}
                      >
                        <FileSpreadsheet className="h-3.5 w-3.5" />
                        Reports
                      </Button>
                    </div>
                  )}
              </div>
            </div>
          );
        })}

        {/* Approval Card (when agent is waiting for human approval) */}
        {runStatus === "waiting_for_approval" && pendingApproval && (
          <div className="max-w-xl mx-auto my-3" data-testid="approval-card">
            <Card className="p-4 border-amber-300 dark:border-amber-900 bg-amber-50/50 dark:bg-amber-950/20 shadow-md">
              <div className="flex items-start gap-3">
                <div className="p-2 rounded-lg bg-amber-100 dark:bg-amber-900/60 text-amber-700 dark:text-amber-300 shrink-0">
                  <ShieldCheck className="h-5 w-5" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between">
                    <h3 className="text-sm font-semibold text-amber-950 dark:text-amber-100">
                      Approval Required
                    </h3>
                    <Badge className="bg-amber-100 dark:bg-amber-900 text-amber-800 dark:text-amber-200 border-amber-300">
                      Pending Action
                    </Badge>
                  </div>

                  <p className="text-xs text-amber-800 dark:text-amber-300/80 mt-1">
                    The agent is requesting authorization to perform the following action:
                  </p>

                  <div className="my-3 p-2.5 rounded-lg bg-background/80 border border-amber-200 dark:border-amber-900/60 space-y-1.5 text-xs">
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">Action:</span>
                      <span className="font-semibold text-foreground">
                        {ACTION_TITLE_MAP[pendingApproval.tool_name] ||
                          pendingApproval.tool_name}
                      </span>
                    </div>

                    {pendingApproval.proposed_args?.name && (
                      <div className="flex justify-between">
                        <span className="text-muted-foreground">Client Name:</span>
                        <span className="font-medium text-foreground">
                          {pendingApproval.proposed_args.name}
                        </span>
                      </div>
                    )}

                    {pendingApproval.proposed_args?.client_id && (
                      <div className="flex justify-between">
                        <span className="text-muted-foreground">Client ID:</span>
                        <span className="font-mono text-foreground">
                          {pendingApproval.proposed_args.client_id}
                        </span>
                      </div>
                    )}

                    {pendingApproval.proposed_args?.expected_period && (
                      <div className="flex justify-between">
                        <span className="text-muted-foreground">Audit Period:</span>
                        <span className="font-medium text-foreground">
                          {pendingApproval.proposed_args.expected_period}
                        </span>
                      </div>
                    )}

                    {pendingApproval.proposed_args?.review_status && (
                      <div className="flex justify-between">
                        <span className="text-muted-foreground">Disposition:</span>
                        <span className="font-semibold text-foreground">
                          {pendingApproval.proposed_args.review_status}
                        </span>
                      </div>
                    )}

                    {pendingApproval.proposed_args?.notes && (
                      <div className="flex justify-between">
                        <span className="text-muted-foreground">Notes:</span>
                        <span className="italic text-foreground">
                          {pendingApproval.proposed_args.notes}
                        </span>
                      </div>
                    )}
                  </div>

                  {/* Actions */}
                  <div className="flex items-center gap-2 mt-3">
                    <Button
                      size="sm"
                      onClick={handleApprove}
                      disabled={!!approvalActionStatus}
                      data-testid="approval-approve-btn"
                      className="text-xs h-8"
                    >
                      <CheckCircle2 className="h-3.5 w-3.5" />
                      Approve
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleReject}
                      disabled={!!approvalActionStatus}
                      data-testid="approval-reject-btn"
                      className="text-xs h-8 text-destructive hover:bg-destructive/10 hover:text-destructive"
                    >
                      <XCircle className="h-3.5 w-3.5" />
                      Reject
                    </Button>

                    {approvalActionStatus && (
                      <span className="text-xs font-medium ml-2 text-muted-foreground capitalize">
                        {approvalActionStatus}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            </Card>
          </div>
        )}

        {/* Working / Status Indicator (Non-technical) */}
        {(isWorking || runStatus === "cancelled") && (
          <div
            className="flex items-center justify-between p-3 rounded-xl bg-secondary/80 border border-border/80 text-xs text-muted-foreground max-w-md mx-auto"
            data-testid="agent-status-indicator"
          >
            <div className="flex items-center gap-2">
              {isWorking ? (
                <Loader2 className="h-4 w-4 animate-spin text-primary" />
              ) : (
                <XCircle className="h-4 w-4 text-muted-foreground" />
              )}
              <span>{statusMessage || (runStatus === "cancelled" ? "Agent run was cancelled." : "Agent is processing...")}</span>
            </div>

            {isWorking && (
              <Button
                variant="ghost"
                size="sm"
                onClick={handleCancelRun}
                data-testid="cancel-run-btn"
                className="text-xs h-6 px-2 text-muted-foreground hover:text-destructive"
              >
                Cancel
              </Button>
            )}
          </div>
        )}

        {/* Error notification */}
        {error && (
          <div
            className="p-3 rounded-xl bg-destructive/10 border border-destructive/20 text-destructive text-xs flex items-center justify-between max-w-xl mx-auto"
            data-testid="chat-error"
          >
            <div className="flex items-center gap-2">
              <AlertCircle className="h-4 w-4 shrink-0" />
              <span>{error}</span>
            </div>
            <Button
              variant="ghost"
              size="sm"
              className="text-xs h-6 px-2"
              onClick={() => setError(null)}
            >
              Dismiss
            </Button>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Input Area */}
      <footer className="p-4 border-t border-border bg-card/40 shrink-0">
        <form onSubmit={handleSendMessage} className="max-w-3xl mx-auto flex gap-2">
          <textarea
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask LedgerLens AI... (Enter to send, Shift+Enter for new line)"
            disabled={isSending || !!activeRunId}
            rows={1}
            data-testid="chat-input"
            className="flex-1 min-h-[42px] max-h-32 px-3.5 py-2.5 rounded-xl border border-border bg-background text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-ring resize-none disabled:opacity-50"
          />
          <Button
            type="submit"
            onClick={handleSendMessage}
            disabled={!inputText.trim() || isSending || !!activeRunId}
            data-testid="send-message-btn"
            className="h-[42px] px-4 rounded-xl shrink-0"
          >
            {isSending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Send className="h-4 w-4" />
            )}
          </Button>
        </form>
      </footer>
    </div>
  );
}
