/**
 * Regression test for the Review Center note-clobber bug.
 *
 * Bug: clicking a finding's status button (Keep/Ignore/...) calls
 * updateFinding({status}) then setSelected(updated). The old effect
 *   useEffect(() => setNote(selected?.note || ""), [selected])
 * re-ran on that identity change and reset the note textarea to the (empty)
 * saved note — silently discarding a note the reviewer had just typed but not
 * yet saved. (Observed in a real Playwright run: report row had status=Keep but
 * an empty Notes column.)
 *
 * Fix: the note box is re-seeded only when the selected finding *id* changes.
 *
 * Uses the repo's createRoot+act style (no @testing-library available here).
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";

jest.mock("@/context/AppContext", () => {
  const value = {
    activeClient: { id: "client-1", name: "Acme Ltd" },
    activeScan: { id: "scan-1" },
    setActiveScan: jest.fn(),
    setTab: jest.fn(),
    goCompare: jest.fn(),
    clients: [{ id: "client-1", name: "Acme Ltd" }],
  };
  return { useApp: () => value };
});

jest.mock("@/lib/api", () => ({
  api: {
    listScans: jest.fn(),
    getFindings: jest.fn(),
    updateFinding: jest.fn(),
  },
}));

jest.mock("sonner", () => ({ toast: Object.assign(jest.fn(), { success: jest.fn(), error: jest.fn() }) }));

import ReviewCenter from "@/pages/ReviewCenter";
import { api } from "@/lib/api";

let mockUpdateCalls = [];

function typeInto(el, value) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
  setter.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

let container, root;
beforeEach(() => {
  mockUpdateCalls = [];
  const finding = {
    id: "f-1", category: "possible_duplicate", title: "Two similar invoices",
    status: "unreviewed", confidence: 85, confidence_level: "high",
    files: [{ file_id: "x", name: "a.pdf" }], note: "", evidence: { summary: "85% similar" },
  };
  api.listScans.mockResolvedValue([
    { id: "scan-1", status: "completed", total_findings: 1, started_at: new Date().toISOString() },
  ]);
  api.getFindings.mockResolvedValue([finding]);
  api.updateFinding.mockImplementation(async (id, body) => {
    mockUpdateCalls.push({ id, body });
    // New object identity, echoing only the sent field — the exact shape that
    // used to clobber the unsaved note via the [selected] effect.
    return { ...finding, ...body, id };
  });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

test("typing a note then clicking a status button does NOT wipe the note", async () => {
  await act(async () => { root.render(<ReviewCenter />); });
  for (let i = 0; i < 4; i++) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }

  const card = container.querySelector('[data-testid="exception-card-item"]');
  expect(card).toBeTruthy();
  await act(async () => { card.dispatchEvent(new MouseEvent("click", { bubbles: true })); });

  const noteBox = container.querySelector('[data-testid="finding-note-input"]');
  expect(noteBox).toBeTruthy();

  // Reviewer types a note (unsaved), then clicks Keep.
  await act(async () => { typeInto(noteBox, "verified with client CFO"); });
  const keep = container.querySelector('[data-testid="mark-keep-button"]');
  expect(keep).toBeTruthy();
  await act(async () => { keep.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
  for (let i = 0; i < 2; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });

  // THE GUARD: typed note survives the status toggle.
  const after = container.querySelector('[data-testid="finding-note-input"]');
  expect(after.value).toBe("verified with client CFO");

  // Saving now must persist the typed note (not an empty string).
  const save = container.querySelector('[data-testid="save-note-button"]');
  await act(async () => { save.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
  for (let i = 0; i < 2; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  const noteSave = mockUpdateCalls.find((c) => c.body && c.body.note !== undefined);
  expect(noteSave).toBeTruthy();
  expect(noteSave.body.note).toBe("verified with client CFO");
});
