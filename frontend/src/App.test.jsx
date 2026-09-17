import React, { act } from "react";
import { createRoot } from "react-dom/client";
import App from "@/App";
import { AppProvider, useApp } from "@/context/AppContext";
import Overview from "@/pages/Overview";
import Clients from "@/pages/Clients";
import ScanWorkspace from "@/pages/ScanWorkspace";
import Checklists from "@/pages/Checklists";
import ReviewCenter from "@/pages/ReviewCenter";
import FileCompare from "@/pages/FileCompare";
import Reports from "@/pages/Reports";

// Mock the API layer
jest.mock("./lib/api", () => ({
  __esModule: true,
  API: "http://127.0.0.1:8001/api",
  api: {
    getFirm: jest.fn().mockResolvedValue({ id: "firm-1", name: "Test Firm", contact_email: "test@firm.com", retention_note: "" }),
    updateFirm: jest.fn().mockResolvedValue({ id: "firm-1", name: "Updated Firm", contact_email: "test@firm.com", retention_note: "" }),
    listClients: jest.fn().mockResolvedValue([
      { id: "c-1", name: "Acme Corp", client_type: "Corporation", file_count: 5, last_scan: { total_findings: 2 } },
    ]),
    createClient: jest.fn().mockImplementation((c) => Promise.resolve({ id: "c-2", ...c, file_count: 0 })),
    deleteClient: jest.fn().mockResolvedValue({ status: "deleted" }),
    listFiles: jest.fn().mockResolvedValue([
      { id: "f-1", name: "invoice_101.pdf", ext: "pdf", size: 10240, sha256: "abc1234567890abcdef1234567890" },
      { id: "f-2", name: "ledger_2024.xlsx", ext: "xlsx", size: 20480, sha256: "def1234567890abcdef1234567890" },
    ]),
    uploadFiles: jest.fn().mockResolvedValue({ uploaded: 2, files: [] }),
    deleteFile: jest.fn().mockResolvedValue({ status: "deleted" }),
    listTemplates: jest.fn().mockResolvedValue([
      { id: "t-1", name: "Standard Tax Checklist", client_type: "Corporation", items: [] },
    ]),
    createTemplate: jest.fn().mockResolvedValue({ id: "t-2", name: "New Template" }),
    updateTemplate: jest.fn().mockResolvedValue({ id: "t-1", name: "Updated" }),
    deleteTemplate: jest.fn().mockResolvedValue({ status: "deleted" }),
    startScan: jest.fn().mockResolvedValue({ id: "s-1", status: "queued", progress: 0, total_files: 2, processed_files: 0 }),
    cancelScan: jest.fn().mockResolvedValue({ status: "cancelled" }),
    listScans: jest.fn().mockResolvedValue([
      { id: "s-1", status: "completed", started_at: "2026-01-01T00:00:00Z", total_findings: 2, total_files: 2, processed_files: 2 },
    ]),
    getScan: jest.fn().mockResolvedValue({ id: "s-1", status: "completed", total_findings: 2, total_files: 2, processed_files: 2 }),
    getFindings: jest.fn().mockResolvedValue([
      {
        id: "find-1",
        category: "exact_duplicate",
        title: "Exact Duplicate File",
        status: "unreviewed",
        confidence: 1.0,
        confidence_level: "high",
        files: [
          { id: "f-1", name: "inv1.pdf", ext: "pdf", size: 1024, sha256: "hash1" },
          { id: "f-2", name: "inv1_copy.pdf", ext: "pdf", size: 1024, sha256: "hash1" },
        ],
        evidence: { summary: "Exact hash match" },
      },
    ]),
    updateFinding: jest.fn().mockImplementation((id, data) => Promise.resolve({
      id,
      category: "exact_duplicate",
      title: "Exact Duplicate File",
      status: data.status || "keep",
      note: data.note || "",
      confidence: 1.0,
      confidence_level: "high",
      files: [],
      evidence: { summary: "Exact hash match" },
    })),
    reportUrl: jest.fn().mockReturnValue("http://127.0.0.1:8001/api/scans/s-1/report?format=pdf"),
    downloadReport: jest.fn().mockResolvedValue(new ArrayBuffer(10)),
  },
}));

let container = null;
let root = null;

beforeEach(() => {
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
    container = null;
  }
});

describe("Cloud LedgerLens Frontend Verification", () => {
  test("1. Shell renders successfully with Titlebar and Sidebar", async () => {
    await act(async () => {
      root.render(<App />);
    });

    const titlebar = container.querySelector("[data-testid='app-titlebar']");
    expect(titlebar).not.toBeNull();
    expect(titlebar.textContent).toContain("LedgerLens");
    expect(titlebar.textContent).toContain("Cloud Accounting AI");
    expect(titlebar.textContent).toContain("Secure Cloud Processing");

    // Verify desktop window controls are completely gone
    expect(container.querySelector(".bg-rose-400")).toBeNull();
    expect(container.querySelector(".bg-amber-400")).toBeNull();

    // Verify System Health is NOT in Sidebar
    const nav = container.querySelector("[data-testid='main-nav']");
    expect(nav.textContent).not.toContain("System Health");
    expect(container.querySelector("[data-testid='nav-system_check']")).toBeNull();
  });

  test("2. Overview Page renders without errors", async () => {
    await act(async () => {
      root.render(
        <AppProvider>
          <Overview />
        </AppProvider>
      );
    });

    expect(container.textContent).toContain("Surface only the exceptions worth your attention");
    expect(container.textContent).toContain("Firm Setup");
    expect(container.querySelector("[data-testid='firm-name-input']")).not.toBeNull();
  });

  test("3. Clients Page renders and lists clients", async () => {
    await act(async () => {
      root.render(
        <AppProvider>
          <Clients />
        </AppProvider>
      );
    });

    expect(container.textContent).toContain("Client Directory");
    expect(container.querySelector("[data-testid='add-client-button']")).not.toBeNull();
  });

  test("4. Scan Workspace renders cloud upload dropzone without desktop card", async () => {
    function TestScanWorkspaceWrapper() {
      const { setActiveClient } = useApp();
      React.useEffect(() => {
        setActiveClient({ id: "c-1", name: "Acme Corp", client_type: "Corporation" });
      }, [setActiveClient]);
      return <ScanWorkspace />;
    }

    await act(async () => {
      root.render(
        <AppProvider>
          <TestScanWorkspaceWrapper />
        </AppProvider>
      );
    });

    // Cloud upload dropzone must be present
    expect(container.querySelector("[data-testid='upload-dropzone']")).not.toBeNull();
    expect(container.textContent).toContain("Drag a folder or files here");
    expect(container.querySelector("[data-testid='browse-files-button']")).not.toBeNull();
    expect(container.querySelector("[data-testid='browse-folder-button']")).not.toBeNull();

    // Start Scan button must be present
    expect(container.querySelector("[data-testid='start-scan-button']")).not.toBeNull();

    // Desktop local folder card MUST NOT be present
    expect(container.querySelector("[data-testid='local-folder-card']")).toBeNull();
    expect(container.querySelector("[data-testid='pick-folder-button']")).toBeNull();
    expect(container.querySelector("[data-testid='start-local-scan-button']")).toBeNull();
  });

  test("5. Checklists Page renders templates", async () => {
    await act(async () => {
      root.render(
        <AppProvider>
          <Checklists />
        </AppProvider>
      );
    });

    expect(container.textContent).toContain("Checklist Templates");
    expect(container.querySelector("[data-testid='new-template-button']")).not.toBeNull();
  });

  test("6. Review Center Page renders exception review UI", async () => {
    function TestReviewCenterWrapper() {
      const { setActiveClient, setActiveScan } = useApp();
      React.useEffect(() => {
        setActiveClient({ id: "c-1", name: "Acme Corp", client_type: "Corporation" });
        setActiveScan({ id: "s-1", status: "completed", total_findings: 1 });
      }, [setActiveClient, setActiveScan]);
      return <ReviewCenter />;
    }

    await act(async () => {
      root.render(
        <AppProvider>
          <TestReviewCenterWrapper />
        </AppProvider>
      );
    });

    expect(container.textContent).toContain("Exception Review");
    expect(container.querySelector("[data-testid='review-export-button']")).not.toBeNull();
  });

  test("7. File Compare Page renders comparison UI", async () => {
    function TestFileCompareWrapper() {
      const { setCompareFinding } = useApp();
      React.useEffect(() => {
        setCompareFinding({
          id: "find-1",
          category: "exact_duplicate",
          confidence: 1.0,
          confidence_level: "high",
          evidence: { summary: "Exact hash match between invoice copies" },
          files: [
            { id: "f-1", name: "inv_copy_1.pdf", ext: "pdf", size: 1024, sha256: "abc1234567890abcdef1234567890" },
            { id: "f-2", name: "inv_copy_2.pdf", ext: "pdf", size: 1024, sha256: "abc1234567890abcdef1234567890" },
          ],
        });
      }, [setCompareFinding]);
      return <FileCompare />;
    }

    await act(async () => {
      root.render(
        <AppProvider>
          <TestFileCompareWrapper />
        </AppProvider>
      );
    });

    expect(container.textContent).toContain("File Diff Inspector");
    expect(container.querySelector("[data-testid='compare-panes']")).not.toBeNull();
    expect(container.textContent).toContain("inv_copy_1.pdf");
    expect(container.textContent).toContain("inv_copy_2.pdf");
  });

  test("8. Reports Page renders export triggers and download options", async () => {
    function TestReportsWrapper() {
      const { setActiveClient, setActiveScan } = useApp();
      React.useEffect(() => {
        setActiveClient({ id: "c-1", name: "Acme Corp", client_type: "Corporation" });
        setActiveScan({ id: "s-1", status: "completed", started_at: "2026-01-01T00:00:00Z" });
      }, [setActiveClient, setActiveScan]);
      return <Reports />;
    }

    await act(async () => {
      root.render(
        <AppProvider>
          <TestReportsWrapper />
        </AppProvider>
      );
    });

    expect(container.textContent).toContain("Reports & Exports");
    expect(container.querySelector("[data-testid='export-pdf-button']")).not.toBeNull();
    expect(container.querySelector("[data-testid='export-xlsx-button']")).not.toBeNull();
    expect(container.querySelector("[data-testid='export-csv-button']")).not.toBeNull();
  });
});
