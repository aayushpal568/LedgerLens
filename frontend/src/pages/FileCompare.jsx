import React from "react";
import { useApp } from "@/context/AppContext";
import { Button, Card, Badge, EmptyState, ConfidenceBadge } from "@/components/ui";
import { CATEGORY_MAP, formatBytes } from "@/lib/utils";
import * as Icons from "lucide-react";
import { Columns2, FileText, ArrowLeft, Fingerprint, Calendar, HardDrive, FileType2 } from "lucide-react";

function FilePane({ file, other }) {
  const sameHash = other && file.sha256 && file.sha256 === other.sha256;
  const rows = [
    { icon: FileType2, label: "Type", value: file.ext },
    { icon: HardDrive, label: "Size", value: formatBytes(file.size), diff: other && other.size !== file.size },
    { icon: Calendar, label: "Detected period", value: file.period?.year || "—", diff: other && (other.period?.year || null) !== (file.period?.year || null) },
    { icon: Fingerprint, label: "SHA-256", value: file.sha256 ? `${file.sha256.slice(0, 20)}…` : "—", mono: true, match: sameHash },
  ];
  return (
    <Card className="p-5 flex-1 min-w-0">
      <div className="flex items-center gap-2">
        <div className="h-10 w-10 rounded-lg bg-secondary flex items-center justify-center shrink-0"><FileText className="h-5 w-5 text-primary" /></div>
        <div className="min-w-0">
          <div className="font-semibold truncate">{file.name}</div>
          <div className="text-xs text-muted-foreground font-mono-data uppercase">{file.ext}</div>
        </div>
      </div>
      <div className="mt-4 space-y-2">
        {rows.map((r) => {
          const Icon = r.icon;
          return (
            <div key={r.label} className={`flex items-center gap-2 rounded-lg px-3 py-2 text-sm ${r.diff ? "bg-amber-50 dark:bg-amber-950/40" : r.match ? "bg-emerald-50 dark:bg-emerald-950/40" : "bg-secondary/50"}`}>
              <Icon className="h-4 w-4 text-muted-foreground shrink-0" />
              <span className="text-xs uppercase tracking-wide text-muted-foreground w-28 shrink-0">{r.label}</span>
              <span className={`truncate ${r.mono ? "font-mono-data text-xs" : ""}`}>{r.value}</span>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

export default function FileCompare() {
  const { compareFinding, setTab } = useApp();

  if (!compareFinding || !compareFinding.files?.length) {
    return (
      <div className="p-8 max-w-3xl mx-auto">
        <Card>
          <EmptyState icon={Columns2} title="Nothing to compare"
            subtitle="Open an exception with two or more files in the Review Center, then choose “Compare side by side”."
            action={<Button onClick={() => setTab("review_center")}><ArrowLeft className="h-4 w-4" /> Go to Review</Button>} />
        </Card>
      </div>
    );
  }

  const meta = CATEGORY_MAP[compareFinding.category];
  const Icon = Icons[meta.icon] || FileText;
  const [a, b] = compareFinding.files;

  return (
    <div className="max-w-5xl mx-auto p-8 space-y-6">
      <Button variant="ghost" size="sm" onClick={() => setTab("review_center")} data-testid="compare-back-button"><ArrowLeft className="h-4 w-4" /> Back to Review</Button>
      <div className="flex items-center gap-2 flex-wrap">
        <Badge className={meta.badge}><Icon className="h-3 w-3" /> {meta.label}</Badge>
        <ConfidenceBadge level={compareFinding.confidence_level} value={compareFinding.confidence} />
      </div>
      <h1 className="font-head text-2xl font-bold tracking-tight flex items-center gap-2">
        <Columns2 className="h-6 w-6 text-primary" /> File Diff Inspector
      </h1>

      <Card className="p-4 bg-secondary/50">
        <p className="text-sm">{compareFinding.evidence?.summary}</p>
      </Card>

      <div className="flex flex-col md:flex-row gap-4" data-testid="compare-panes">
        <FilePane file={a} other={b} />
        <div className="hidden md:flex items-center"><div className="h-10 w-10 rounded-full bg-primary/10 flex items-center justify-center text-primary font-head font-bold text-xs">VS</div></div>
        <FilePane file={b} other={a} />
      </div>

      <Card className="p-4">
        <p className="text-xs text-muted-foreground">
          This is a read-only comparison. LedgerLens never deletes or merges files — use the Review Center to record your decision.
        </p>
      </Card>
    </div>
  );
}
