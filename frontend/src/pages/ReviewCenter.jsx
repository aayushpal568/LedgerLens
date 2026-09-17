import React, { useState, useEffect, useCallback, useMemo } from "react";
import { useApp } from "@/context/AppContext";
import { api } from "@/lib/api";
import { Button, Card, Select, Input, Badge, Textarea, EmptyState, ConfidenceBadge, Spinner } from "@/components/ui";
import { CATEGORIES, CATEGORY_MAP, STATUS_OPTIONS, STATUS_MAP, formatBytes } from "@/lib/utils";
import { toast } from "sonner";
import * as Icons from "lucide-react";
import { ShieldAlert, Search, Columns2, FileText, CheckCircle2, Save } from "lucide-react";

const STATUS_STYLE = {
  unreviewed: "bg-secondary text-muted-foreground border-border",
  keep: "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:border-emerald-900",
  keep_both: "bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-950 dark:text-blue-300 dark:border-blue-900",
  ignore: "bg-slate-100 text-slate-500 border-slate-300 dark:bg-slate-800 dark:text-slate-400 dark:border-slate-700",
  review_later: "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:border-amber-900",
};

function CategoryChip({ cat }) {
  const meta = CATEGORY_MAP[cat];
  const Icon = Icons[meta.icon] || FileText;
  return <Badge className={meta.badge}><Icon className="h-3 w-3" /> {meta.label}</Badge>;
}

export default function ReviewCenter() {
  const { activeClient, activeScan, setActiveScan, setTab, goCompare } = useApp();
  const [scans, setScans] = useState([]);
  const [findings, setFindings] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(null);
  const [note, setNote] = useState("");
  const [filterCat, setFilterCat] = useState("all");
  const [filterStatus, setFilterStatus] = useState("all");
  const [search, setSearch] = useState("");

  const scanId = activeScan?.id;

  const loadScans = useCallback(async () => {
    if (!activeClient) return;
    const s = await api.listScans(activeClient.id);
    setScans(s);
    const completed = s.filter((x) => x.status === "completed");
    if (!activeScan && completed.length) setActiveScan(completed[0]);
  }, [activeClient, activeScan, setActiveScan]);

  const loadFindings = useCallback(async () => {
    if (!scanId) { setLoading(false); return; }
    setLoading(true);
    const data = await api.getFindings(scanId);
    setFindings(data);
    setLoading(false);
  }, [scanId]);

  useEffect(() => { loadScans(); }, [loadScans]);
  useEffect(() => { loadFindings(); }, [loadFindings]);
  useEffect(() => { setNote(selected?.note || ""); }, [selected]);

  const counts = useMemo(() => {
    const c = {};
    CATEGORIES.forEach((cat) => { c[cat.id] = findings.filter((f) => f.category === cat.id).length; });
    return c;
  }, [findings]);

  const filtered = useMemo(() => findings.filter((f) => {
    if (filterCat !== "all" && f.category !== filterCat) return false;
    if (filterStatus !== "all" && f.status !== filterStatus) return false;
    if (search && !f.title.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  }), [findings, filterCat, filterStatus, search]);

  const setStatus = async (finding, status) => {
    const updated = await api.updateFinding(finding.id, { status });
    setFindings((fs) => fs.map((f) => (f.id === finding.id ? updated : f)));
    if (selected?.id === finding.id) setSelected(updated);
    toast.success(`Marked "${STATUS_MAP[status]}"`);
  };

  const saveNote = async () => {
    if (!selected) return;
    const updated = await api.updateFinding(selected.id, { note });
    setFindings((fs) => fs.map((f) => (f.id === selected.id ? updated : f)));
    setSelected(updated);
    toast.success("Note saved");
  };

  if (!activeClient) {
    return <div className="p-8 max-w-3xl mx-auto"><Card><EmptyState icon={ShieldAlert} title="No client selected" subtitle="Choose a client from the directory." action={<Button onClick={() => setTab("clients")}>Go to Clients</Button>} /></Card></div>;
  }

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="p-6 pb-4 border-b border-border">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <h1 className="font-head text-2xl font-bold tracking-tight flex items-center gap-2">
              <ShieldAlert className="h-6 w-6 text-primary" /> Exception Review
            </h1>
            <p className="text-sm text-muted-foreground mt-1">{activeClient.name} · {findings.length} exceptions to review</p>
          </div>
          <div className="flex items-center gap-2">
            {scans.filter((s) => s.status === "completed").length > 0 && (
              <Select className="w-56" data-testid="review-scan-select" value={scanId || ""}
                onChange={(e) => setActiveScan(scans.find((s) => s.id === e.target.value))}>
                {scans.filter((s) => s.status === "completed").map((s) => (
                  <option key={s.id} value={s.id}>{new Date(s.started_at).toLocaleString()} · {s.total_findings} flags</option>
                ))}
              </Select>
            )}
            <Button variant="outline" data-testid="review-export-button" onClick={() => setTab("reports")}>Export Report</Button>
          </div>
        </div>

        {/* Category cards */}
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 mt-5">
          {CATEGORIES.map((cat) => {
            const Icon = Icons[cat.icon] || FileText;
            const active = filterCat === cat.id;
            return (
              <button key={cat.id} data-testid={`exception-category-${cat.id}`}
                onClick={() => setFilterCat(active ? "all" : cat.id)}
                className={`text-left rounded-xl border p-3 transition-all hover:shadow-sm ${active ? "border-primary ring-2 ring-ring/40" : "border-border"}`}>
                <div className="flex items-center justify-between">
                  <span className={`h-7 w-7 rounded-lg flex items-center justify-center ${cat.badge}`}><Icon className="h-3.5 w-3.5" /></span>
                  <span className="text-2xl font-head font-bold tabular-nums">{counts[cat.id]}</span>
                </div>
                <div className="text-xs text-muted-foreground mt-1.5 truncate">{cat.label}</div>
              </button>
            );
          })}
        </div>
      </div>

      {/* Body */}
      {!scanId ? (
        <Card className="m-6"><EmptyState icon={ShieldAlert} title="No completed scan" subtitle="Run a scan in the workspace to see exceptions here." action={<Button onClick={() => setTab("scan_workspace")}>Go to Scan Workspace</Button>} /></Card>
      ) : loading ? (
        <div className="flex-1 flex items-center justify-center"><Spinner className="h-6 w-6 text-primary" /></div>
      ) : (
        <div className="flex-1 flex min-h-0">
          {/* List */}
          <div className="w-full lg:w-[46%] border-r border-border flex flex-col min-h-0">
            <div className="flex items-center gap-2 p-3 border-b border-border">
              <div className="relative flex-1">
                <Search className="h-4 w-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
                <Input data-testid="review-search" className="pl-9 h-9" placeholder="Search issues…" value={search} onChange={(e) => setSearch(e.target.value)} />
              </div>
              <Select className="w-36 h-9" data-testid="review-status-filter" value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)}>
                <option value="all">All statuses</option>
                {STATUS_OPTIONS.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
              </Select>
            </div>
            <div className="flex-1 overflow-y-auto p-3 space-y-2" data-testid="findings-list">
              {filtered.length === 0 ? (
                <EmptyState icon={CheckCircle2} title="Nothing here" subtitle="No exceptions match the current filters." />
              ) : filtered.map((f) => (
                <button key={f.id} data-testid="exception-card-item" onClick={() => setSelected(f)}
                  className={`w-full text-left rounded-lg border p-3 transition-colors ${selected?.id === f.id ? "border-primary bg-accent" : "border-border hover:bg-secondary"}`}>
                  <div className="flex items-center justify-between gap-2">
                    <CategoryChip cat={f.category} />
                    <ConfidenceBadge level={f.confidence_level} value={f.confidence} />
                  </div>
                  <p className="text-sm font-medium mt-2 leading-snug">{f.title}</p>
                  <div className="flex items-center justify-between mt-2">
                    <Badge className={STATUS_STYLE[f.status]}>{STATUS_MAP[f.status]}</Badge>
                    {f.files?.length > 0 && <span className="text-xs text-muted-foreground">{f.files.length} file(s)</span>}
                  </div>
                </button>
              ))}
            </div>
          </div>

          {/* Inspector */}
          <div className="hidden lg:flex flex-1 flex-col min-h-0" data-testid="evidence-inspector">
            {!selected ? (
              <EmptyState icon={FileText} title="Select an exception" subtitle="Choose an item to view evidence and take a review action." />
            ) : (
              <div className="flex-1 overflow-y-auto p-6 space-y-5">
                <div>
                  <div className="flex items-center gap-2"><CategoryChip cat={selected.category} /><ConfidenceBadge level={selected.confidence_level} value={selected.confidence} /></div>
                  <h2 className="font-head text-lg font-semibold mt-3 leading-snug">{selected.title}</h2>
                </div>

                <Card className="p-4 bg-secondary/50">
                  <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-1">Evidence</div>
                  <p className="text-sm">{selected.evidence?.summary}</p>
                  {selected.evidence?.sha256 && <p className="text-xs font-mono-data text-muted-foreground mt-2 break-all">SHA-256: {selected.evidence.sha256}</p>}
                  {selected.evidence?.aliases?.length > 0 && <p className="text-xs text-muted-foreground mt-2">Also looked for: {selected.evidence.aliases.join(", ")}</p>}
                </Card>

                {selected.files?.length > 0 && (
                  <div>
                    <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-2">Files</div>
                    <div className="space-y-1.5">
                      {selected.files.map((f, i) => (
                        <div key={i} className="flex items-center gap-3 rounded-lg border border-border px-3 py-2">
                          <FileText className="h-4 w-4 text-primary shrink-0" />
                          <span className="text-sm truncate flex-1">{f.name}</span>
                          <span className="text-xs font-mono-data text-muted-foreground uppercase">{f.ext}</span>
                          <span className="text-xs text-muted-foreground">{formatBytes(f.size)}</span>
                        </div>
                      ))}
                    </div>
                    {selected.files.length >= 2 && (
                      <Button variant="outline" size="sm" className="mt-3" data-testid="compare-view-toggle" onClick={() => goCompare(selected)}>
                        <Columns2 className="h-4 w-4" /> Compare side by side
                      </Button>
                    )}
                  </div>
                )}

                {/* Actions */}
                <div>
                  <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-2">Review decision</div>
                  <div className="grid grid-cols-2 gap-2">
                    <Button variant={selected.status === "keep" ? "primary" : "outline"} data-testid="mark-keep-button" onClick={() => setStatus(selected, "keep")}>Keep</Button>
                    <Button variant={selected.status === "keep_both" ? "primary" : "outline"} data-testid="mark-keep-both-button" onClick={() => setStatus(selected, "keep_both")}>Keep Both</Button>
                    <Button variant={selected.status === "ignore" ? "primary" : "outline"} data-testid="mark-ignore-button" onClick={() => setStatus(selected, "ignore")}>Ignore</Button>
                    <Button variant={selected.status === "review_later" ? "primary" : "outline"} data-testid="mark-review-later-button" onClick={() => setStatus(selected, "review_later")}>Review Later</Button>
                  </div>
                </div>

                <div>
                  <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-2">Reviewer note</div>
                  <Textarea rows={3} data-testid="finding-note-input" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Add context for this decision…" />
                  <Button size="sm" variant="secondary" className="mt-2" data-testid="save-note-button" onClick={saveNote}><Save className="h-4 w-4" /> Save note</Button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
