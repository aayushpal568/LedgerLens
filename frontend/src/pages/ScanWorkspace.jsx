import React, { useState, useEffect, useRef, useCallback } from "react";
import { useApp } from "@/context/AppContext";
import { api } from "@/lib/api";
import { Button, Card, Select, Label, Badge, EmptyState, Spinner } from "@/components/ui";
import { formatBytes } from "@/lib/utils";
import { startScanPolling } from "@/lib/scanPolling";
import { toast } from "sonner";
import {
  FolderSearch, UploadCloud, FileText, Trash2, Play, ShieldAlert, CheckCircle2,
  AlertTriangle, Radar, FileStack, Info,
} from "lucide-react";
import { TwoStepDeleteDialog } from "@/components/TwoStepDeleteDialog";

const SUPPORTED = ["pdf", "jpg", "jpeg", "png", "tiff", "tif", "docx", "xlsx", "csv"];

export default function ScanWorkspace() {
  const { clients, activeClient, setActiveClient, activeScan, setActiveScan, setTab } = useApp();
  const [files, setFiles] = useState([]);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [templates, setTemplates] = useState([]);
  const [templateId, setTemplateId] = useState("");
  const [period, setPeriod] = useState(new Date().getFullYear());
  const [uploading, setUploading] = useState(false);
  const [uploadPct, setUploadPct] = useState(0);
  const [scan, setScan] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef();
  const folderRef = useRef();
  const pollRef = useRef();

  const loadFiles = useCallback(async (cid) => {
    try {
      const res = await api.listFiles(cid);
      setFiles(Array.isArray(res) ? res : []);
    } catch {
      setFiles([]);
    }
  }, []);

  useEffect(() => {
    if (api.listTemplates) {
      Promise.resolve(api.listTemplates())
        .then((res) => setTemplates(Array.isArray(res) ? res : []))
        .catch(() => setTemplates([]));
    }
  }, []);
  useEffect(() => {
    if (activeClient) loadFiles(activeClient.id);
    setScan(null);
  }, [activeClient, loadFiles]);

  const pollScan = useCallback((scanId) => {
    if (pollRef.current) pollRef.current.stop();
    pollRef.current = startScanPolling({
      scanId,
      getScan: api.getScan,
      onState: (s) => setScan(s),
      onDone: (s) => {
        setActiveScan(s);
        if (s.status === "completed") toast.success(`Scan complete — ${s.total_findings} exceptions found`);
        else if (s.status === "cancelled") toast("Scan cancelled");
        else toast.error("Scan failed");
      },
      onError: (msg) => {
        // Give up only after a run of consecutive transient failures (see scanPolling).
        // Drop the "running" spinner state so the UI is not left stuck.
        setScan((prev) => (prev ? { ...prev, status: "error" } : prev));
        toast.error(msg);
      },
    });
  }, [setActiveScan]);

  useEffect(() => () => { if (pollRef.current) pollRef.current.stop(); }, []);

  const doUpload = async (fileList) => {
    if (!activeClient) return toast.error("Select a client first");
    const arr = Array.from(fileList);
    if (!arr.length) return;
    if (arr.length > 20) return toast.error("Maximum 20 files allowed per upload batch.");

    // Validate supported extensions
    const invalid = arr.filter((f) => {
      const ext = f.name.split(".").pop().toLowerCase();
      return !SUPPORTED.includes(ext);
    });
    if (invalid.length > 0) {
      return toast.error(
        `Unsupported file(s): ${invalid.map((f) => f.name).join(", ")}. Supported formats: ${SUPPORTED.join(", ")}`
      );
    }

    // Check individual file size limit (50MB)
    const oversized = arr.filter((f) => f.size > 50 * 1024 * 1024);
    if (oversized.length > 0) {
      return toast.error(`File(s) exceed 50MB limit: ${oversized.map((f) => f.name).join(", ")}`);
    }

    // Check total batch size limit (100MB)
    const totalBatch = arr.reduce((sum, f) => sum + f.size, 0);
    if (totalBatch > 100 * 1024 * 1024) {
      return toast.error("Total batch size exceeds 100MB limit.");
    }

    const fd = new FormData();
    arr.forEach((f) => fd.append("files", f));
    setUploading(true);
    setUploadPct(0);
    try {
      const res = await api.uploadFiles(activeClient.id, fd, (e) => {
        if (e.total) setUploadPct(Math.round((e.loaded / e.total) * 100));
      });
      toast.success(`${res.uploaded} file(s) added`);
      await loadFiles(activeClient.id);
    } catch (err) {
      const detail = err?.response?.data?.detail;
      toast.error(typeof detail === "string" ? detail : "Upload failed");
    } finally {
      setUploading(false);
      setUploadPct(0);
    }
  };

  const promptRemoveFile = (f) => {
    setDeleteTarget(f);
  };

  const handleConfirmDeleteFile = async () => {
    if (!deleteTarget || !activeClient) return;
    try {
      await api.deleteFile(activeClient.id, deleteTarget.id);
      await loadFiles(activeClient.id);
      toast.success(`File "${deleteTarget.name}" removed from workspace`);
    } catch {
      toast.error("Failed to remove file");
    } finally {
      setDeleteTarget(null);
    }
  };

  const startScan = async () => {
    if (!activeClient) return;
    if (!files.length) return toast.error("Upload files before scanning");
    const s = await api.startScan(activeClient.id, {
      template_id: templateId || null,
      expected_period: period ? parseInt(period) : null,
    });
    setScan(s);
    pollScan(s.id);
    toast("Scan queued", { description: "Scanning uploaded documents…" });
  };

  const handleCancelScan = async () => {
    if (!scan || !scan.id) return;
    // Optimistic update: immediately show cancelling state in the UI
    // so the button becomes disabled without waiting for next poll.
    setScan((prev) => prev ? { ...prev, status: "cancelling" } : prev);
    try {
      await api.cancelScan(scan.id);
      toast("Cancelling scan… please wait");
    } catch {
      // Revert optimistic state on failure
      setScan((prev) => prev ? { ...prev, status: "scanning" } : prev);
      toast.error("Failed to cancel scan");
    }
  };

  const supportedCount = files.filter((f) => SUPPORTED.includes(f.ext)).length;
  const running = scan && ["queued", "scanning", "cancelling"].includes(scan.status);

  if (!activeClient) {
    return (
      <div className="max-w-3xl mx-auto p-8">
        <Card>
          <EmptyState icon={FolderSearch} title="Select a client to scan"
            subtitle="Pick a client workspace to upload documents and run detection."
            action={
              clients.length ? (
                <Select data-testid="workspace-client-picker" className="w-64" defaultValue=""
                  onChange={(e) => setActiveClient(clients.find((c) => c.id === e.target.value))}>
                  <option value="" disabled>Choose a client…</option>
                  {clients.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </Select>
              ) : <Button onClick={() => setTab("clients")}>Add a client first</Button>
            } />
        </Card>
      </div>
    );
  }

  return (
    <div className="max-w-6xl mx-auto p-8 space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="font-head text-2xl font-bold tracking-tight flex items-center gap-2">
            <FolderSearch className="h-6 w-6 text-primary" /> Scan Workspace
          </h1>
          <p className="text-sm text-muted-foreground mt-1">{activeClient.name} · {activeClient.client_type}</p>
        </div>
        <Select data-testid="workspace-client-switch" className="w-56" value={activeClient.id}
          onChange={(e) => setActiveClient((clients || []).find((c) => c.id === e.target.value))}>
          {(clients || []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </Select>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-6">
          {/* Upload */}
          <Card className="p-6">
            <div
              data-testid="upload-dropzone"
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => { e.preventDefault(); setDragOver(false); doUpload(e.dataTransfer.files); }}
              className={`rounded-xl border-2 border-dashed p-8 text-center transition-colors ${dragOver ? "border-primary bg-accent" : "border-border"}`}
            >
              <div className="h-12 w-12 rounded-2xl bg-secondary flex items-center justify-center mx-auto mb-3">
                <UploadCloud className="h-6 w-6 text-primary" />
              </div>
              <p className="font-medium text-sm">Drag a folder or files here</p>
              <p className="text-xs text-muted-foreground mt-1">PDF, JPG, JPEG, PNG, TIFF, DOCX, XLSX, CSV</p>
              <div className="flex items-center justify-center gap-2 mt-4">
                <Button size="sm" variant="outline" data-testid="browse-files-button" onClick={() => fileRef.current.click()} disabled={uploading}>
                  Select Files
                </Button>
                <Button size="sm" variant="outline" data-testid="browse-folder-button" onClick={() => folderRef.current.click()} disabled={uploading}>
                  Select Folder
                </Button>
              </div>
              {uploading && (
                <div className="mt-4">
                  <div className="h-1.5 rounded-full bg-secondary overflow-hidden">
                    <div className="h-full bg-primary transition-[width] duration-200" style={{ width: `${uploadPct}%` }} />
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">Uploading… {uploadPct}%</p>
                </div>
              )}
              <input ref={fileRef} type="file" multiple hidden onChange={(e) => doUpload(e.target.files)} />
              <input ref={folderRef} type="file" multiple hidden webkitdirectory="" directory="" onChange={(e) => doUpload(e.target.files)} />
            </div>

            {/* File list */}
            <div className="mt-5">
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2 text-sm font-semibold"><FileStack className="h-4 w-4" /> Documents ({files.length})</div>
                <span className="text-xs text-muted-foreground">{supportedCount} supported · {files.length - supportedCount} unsupported</span>
              </div>
              {files.length === 0 ? (
                <p className="text-sm text-muted-foreground py-6 text-center">No documents yet.</p>
              ) : (
                <div className="max-h-72 overflow-y-auto space-y-1.5 pr-1" data-testid="file-list">
                  {files.map((f) => {
                    const ok = SUPPORTED.includes(f.ext);
                    return (
                      <div key={f.id} className="flex items-center gap-3 rounded-lg border border-border px-3 py-2 group">
                        <FileText className={`h-4 w-4 shrink-0 ${ok ? "text-primary" : "text-muted-foreground"}`} />
                        <span className="text-sm truncate flex-1">{f.name}</span>
                        <span className="text-xs text-muted-foreground font-mono-data uppercase">{f.ext || "?"}</span>
                        <span className="text-xs text-muted-foreground w-16 text-right">{formatBytes(f.size)}</span>
                        {!ok && <Badge className="bg-slate-100 text-slate-600 border-slate-300 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700">skip</Badge>}
                        <button onClick={() => promptRemoveFile(f)} data-testid={`remove-file-${f.id}`}
                          className="h-7 w-7 rounded-md hover:bg-rose-50 hover:text-rose-600 flex items-center justify-center text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity"
                          title="Remove file from workspace">
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </Card>

          {/* Progress */}
          {scan && (
            <Card className="p-6" data-testid="scan-progress-panel">
              <div className="flex items-center gap-2 mb-4">
                {running ? <Radar className="h-5 w-5 text-primary scan-pulse" /> :
                  scan.status === "completed" ? <CheckCircle2 className="h-5 w-5 text-emerald-500" /> :
                  scan.status === "cancelled" ? <AlertTriangle className="h-5 w-5 text-amber-500" /> :
                  <AlertTriangle className="h-5 w-5 text-rose-500" />}
                <h2 className="font-head font-semibold">
                  {running ? "Scanning…" : scan.status === "completed" ? "Scan complete" : scan.status === "cancelled" ? "Scan cancelled" : "Scan error"}
                </h2>
                <span className="ml-auto text-sm font-mono-data text-muted-foreground">
                  {scan.processed_files}/{scan.total_files} files
                </span>
                {running && (
                  <Button
                    variant="outline"
                    size="sm"
                    className="ml-2 text-xs text-rose-600 hover:text-rose-700 hover:bg-rose-50 dark:hover:bg-rose-950/40"
                    data-testid="cancel-scan-button"
                    onClick={handleCancelScan}
                  >
                    Cancel Scan
                  </Button>
                )}
              </div>
              <div className="h-2 rounded-full bg-secondary overflow-hidden" data-testid="scan-progress-bar">
                <div className="h-full bg-primary transition-[width] duration-300" style={{ width: `${scan.progress || 0}%` }} />
              </div>
              {scan.skipped_files?.length > 0 && (
                <div className="mt-4 rounded-lg bg-amber-50 border border-amber-200 p-3 dark:bg-amber-950/40 dark:border-amber-900" data-testid="skipped-files-alert">
                  <div className="flex items-center gap-2 text-sm font-medium text-amber-700 dark:text-amber-300">
                    <AlertTriangle className="h-4 w-4" /> {scan.skipped_files.length} file(s) safely skipped
                  </div>
                  <ul className="mt-1.5 text-xs text-amber-700/80 dark:text-amber-300/80 space-y-0.5">
                    {scan.skipped_files.slice(0, 5).map((s, i) => <li key={i}>· {s.name} — {s.reason}</li>)}
                  </ul>
                </div>
              )}
              {scan.status === "completed" && (
                <Button className="mt-4 w-full" data-testid="go-to-review-button" onClick={() => setTab("review_center")}>
                  <ShieldAlert className="h-4 w-4" /> Review {scan.total_findings} Exceptions
                </Button>
              )}
            </Card>
          )}
        </div>

        {/* Scan config */}
        <div className="space-y-6">
          <Card className="p-6">
            <h2 className="font-head font-semibold mb-4">Scan Settings</h2>
            <div className="space-y-4">
              <div>
                <Label>Checklist template</Label>
                <Select data-testid="scan-template-select" className="mt-1.5" value={templateId} onChange={(e) => setTemplateId(e.target.value)}>
                  <option value="">No checklist (skip missing-doc check)</option>
                  {templates.map((t) => <option key={t.id} value={t.id}>{t.name} ({t.client_type})</option>)}
                </Select>
              </div>
              <div>
                <Label>Expected period (year)</Label>
                <Select data-testid="scan-period-select" className="mt-1.5" value={period} onChange={(e) => setPeriod(e.target.value)}>
                  <option value="">Any period</option>
                  {Array.from({ length: 8 }, (_, i) => new Date().getFullYear() - i).map((y) => (
                    <option key={y} value={y}>{y}</option>
                  ))}
                </Select>
              </div>
              <Button className="w-full" data-testid="start-scan-button" onClick={startScan} disabled={running || !files.length}>
                {running ? <Spinner className="h-4 w-4" /> : <Play className="h-4 w-4" />}
                {running ? "Scanning…" : "Start Scan"}
              </Button>
            </div>
          </Card>

          <Card className="p-5">
            <div className="flex gap-2 text-xs text-muted-foreground">
              <Info className="h-4 w-4 shrink-0 text-primary" />
              <span>Large folders are queued and partially scanned. Locked or inaccessible files are skipped safely — the scan never freezes.</span>
            </div>
          </Card>
        </div>
      </div>

      <TwoStepDeleteDialog
        open={!!deleteTarget}
        onOpenChange={(isOpen) => { if (!isOpen) setDeleteTarget(null); }}
        onConfirm={handleConfirmDeleteFile}
        itemName={deleteTarget ? deleteTarget.name : ""}
      />
    </div>
  );
}
