import React, { useEffect, useState, useCallback, useMemo } from "react";
import { useApp } from "@/context/AppContext";
import { api } from "@/lib/api";
import { Button, Card, Select, Badge, EmptyState } from "@/components/ui";
import { CATEGORIES, CATEGORY_MAP, STATUS_OPTIONS, STATUS_MAP } from "@/lib/utils";
import * as Icons from "lucide-react";
import { FileSpreadsheet, FileText, FileType, ShieldCheck } from "lucide-react";
import { toast } from "sonner";

export default function Reports() {
  const { activeClient, activeScan, setActiveScan, setTab } = useApp();
  const [scans, setScans] = useState([]);
  const [findings, setFindings] = useState([]);
  const scanId = activeScan?.id;

  const loadScans = useCallback(async () => {
    if (!activeClient) return;
    try {
      const s = await api.listScans(activeClient.id);
      const list = Array.isArray(s) ? s : [];
      const completed = list.filter((x) => x.status === "completed");
      setScans(completed);
      if (!activeScan && completed.length) setActiveScan(completed[0]);
    } catch {
      setScans([]);
    }
  }, [activeClient, activeScan, setActiveScan]);

  useEffect(() => { loadScans(); }, [loadScans]);
  useEffect(() => {
    if (scanId && api.getFindings) {
      Promise.resolve(api.getFindings(scanId))
        .then((data) => setFindings(Array.isArray(data) ? data : []))
        .catch(() => setFindings([]));
    }
  }, [scanId]);

  const statusCounts = useMemo(() => {
    const c = {};
    const list = Array.isArray(findings) ? findings : [];
    STATUS_OPTIONS.forEach((s) => { c[s.id] = list.filter((f) => f.status === s.id).length; });
    return c;
  }, [findings]);

  const download = async (fmt) => {
    if (!scanId) return;
    try {
      window.open(api.reportUrl(scanId, fmt), "_blank");
    } catch (error) {
      toast.error(`Export failed: ${error?.message || "Unable to save report"}`);
    }
  };

  if (!activeClient) {
    return <div className="p-8 max-w-3xl mx-auto"><Card><EmptyState icon={FileSpreadsheet} title="No client selected" subtitle="Choose a client to view and export reports." action={<Button onClick={() => setTab("clients")}>Go to Clients</Button>} /></Card></div>;
  }

  return (
    <div className="max-w-5xl mx-auto p-8 space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="font-head text-2xl font-bold tracking-tight flex items-center gap-2">
            <FileSpreadsheet className="h-6 w-6 text-primary" /> Reports &amp; Exports
          </h1>
          <p className="text-sm text-muted-foreground mt-1">Internal review artifact for {activeClient.name}. No client is contacted automatically.</p>
        </div>
        {scans.length > 0 && (
          <Select className="w-56" data-testid="report-scan-select" value={scanId || ""} onChange={(e) => setActiveScan(scans.find((s) => s.id === e.target.value))}>
            {scans.map((s) => <option key={s.id} value={s.id}>{new Date(s.started_at).toLocaleString()}</option>)}
          </Select>
        )}
      </div>

      {!scanId ? (
        <Card><EmptyState icon={FileSpreadsheet} title="No completed scan" subtitle="Run a scan to generate a report." action={<Button onClick={() => setTab("scan_workspace")}>Go to Scan Workspace</Button>} /></Card>
      ) : (
        <>
          {/* Export buttons */}
          <Card className="p-6">
            <h2 className="font-head font-semibold mb-4">Export {findings.length} exceptions</h2>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <Button variant="outline" className="h-auto py-4 flex-col gap-2" data-testid="export-csv-button" onClick={() => download("csv")}>
                <FileText className="h-6 w-6 text-teal-600" /><span>CSV</span>
              </Button>
              <Button variant="outline" className="h-auto py-4 flex-col gap-2" data-testid="export-xlsx-button" onClick={() => download("xlsx")}>
                <FileSpreadsheet className="h-6 w-6 text-emerald-600" /><span>Excel (.xlsx)</span>
              </Button>
              <Button variant="outline" className="h-auto py-4 flex-col gap-2" data-testid="export-pdf-button" onClick={() => download("pdf")}>
                <FileType className="h-6 w-6 text-rose-600" /><span>PDF</span>
              </Button>
            </div>
          </Card>

          {/* Summary */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <Card className="p-6">
              <h2 className="font-head font-semibold mb-4">By category</h2>
              <div className="space-y-2">
                {CATEGORIES.map((cat) => {
                  const Icon = Icons[cat.icon] || FileText;
                  const n = findings.filter((f) => f.category === cat.id).length;
                  return (
                    <div key={cat.id} className="flex items-center gap-3">
                      <span className={`h-7 w-7 rounded-lg flex items-center justify-center ${cat.badge}`}><Icon className="h-3.5 w-3.5" /></span>
                      <span className="text-sm flex-1">{cat.label}</span>
                      <span className="font-head font-bold tabular-nums">{n}</span>
                    </div>
                  );
                })}
              </div>
            </Card>

            <Card className="p-6">
              <h2 className="font-head font-semibold mb-4">Review progress</h2>
              <div className="space-y-2">
                {STATUS_OPTIONS.map((s) => (
                  <div key={s.id} className="flex items-center gap-3">
                    <span className="text-sm flex-1">{s.label}</span>
                    <div className="w-32 h-2 rounded-full bg-secondary overflow-hidden">
                      <div className="h-full bg-primary" style={{ width: findings.length ? `${(statusCounts[s.id] / findings.length) * 100}%` : "0%" }} />
                    </div>
                    <span className="font-head font-bold tabular-nums w-6 text-right">{statusCounts[s.id]}</span>
                  </div>
                ))}
              </div>
            </Card>
          </div>

          <Card className="p-4 flex items-center gap-3 bg-emerald-50 border-emerald-200 dark:bg-emerald-950/40 dark:border-emerald-900">
            <ShieldCheck className="h-5 w-5 text-emerald-600 shrink-0" />
            <p className="text-sm text-emerald-800 dark:text-emerald-300">
              Reports are for your internal review only. No tax, accounting, or compliance decisions are made by this app.
            </p>
          </Card>
        </>
      )}
    </div>
  );
}
