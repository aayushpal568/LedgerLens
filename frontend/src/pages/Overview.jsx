import React, { useState, useEffect } from "react";
import { useApp } from "@/context/AppContext";
import { api } from "@/lib/api";
import { Button, Card, Input, Textarea, Label, Badge } from "@/components/ui";
import { toast } from "sonner";
import {
  ShieldCheck, Building2, Users, FileStack, ShieldAlert, Save, ArrowRight,
  Ban, FileCheck2,
} from "lucide-react";

const PILLARS = [
  { icon: Ban, title: "Never destructive", text: "No deleting, renaming, moving or reorganizing your files. Ever." },
  { icon: ShieldCheck, title: "No auto-decisions", text: "Nothing acts on AI confidence alone. Every flag waits for you." },
  { icon: FileCheck2, title: "Review, not action", text: "Reports are internal review artifacts. Clients are never contacted." },
];

function Stat({ icon: Icon, label, value, tint }) {
  return (
    <Card className="p-5 flex items-center gap-4">
      <div className={`h-11 w-11 rounded-xl flex items-center justify-center ${tint}`}>
        <Icon className="h-5 w-5" />
      </div>
      <div>
        <div className="text-2xl font-head font-bold tabular-nums">{value}</div>
        <div className="text-xs text-muted-foreground">{label}</div>
      </div>
    </Card>
  );
}

export default function Overview() {
  const { firm, refreshFirm, clients, setTab } = useApp();
  const [form, setForm] = useState({ name: "", contact_email: "", retention_note: "" });
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (firm) setForm({ name: firm.name || "", contact_email: firm.contact_email || "", retention_note: firm.retention_note || "" });
  }, [firm]);

  const totalFiles = clients.reduce((s, c) => s + (c.file_count || 0), 0);
  const totalExceptions = clients.reduce((s, c) => s + (c.last_scan?.total_findings || 0), 0);

  const save = async () => {
    setSaving(true);
    try {
      await api.updateFirm(form);
      await refreshFirm();
      toast.success("Firm settings saved");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="max-w-6xl mx-auto p-8 space-y-8">
      {/* Privacy hero */}
      <Card className="relative overflow-hidden p-7 grid-bg">
        <div className="absolute inset-0 bg-gradient-to-r from-card via-card/90 to-transparent" />
        <div className="relative">
          <Badge className="bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:border-emerald-900 mb-3">
            <ShieldCheck className="h-3.5 w-3.5" /> Privacy-first
          </Badge>
          <h1 className="font-head text-3xl font-extrabold tracking-tight">
            Surface only the exceptions worth your attention.
          </h1>
          <p className="text-muted-foreground mt-2 max-w-2xl text-sm leading-relaxed">
            LedgerLens scans your client document folders and flags duplicates, missing items, wrong-period and
            unreadable files — without ever moving, renaming, or acting on anything. You stay in control.
          </p>
          <div className="flex flex-wrap gap-3 mt-5">
            <Button data-testid="overview-add-client" onClick={() => setTab("clients")}>
              <Users className="h-4 w-4" /> Manage Clients
            </Button>
            <Button variant="outline" data-testid="overview-start-scan" onClick={() => setTab("scan_workspace")}>
              Start a Scan <ArrowRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </Card>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <Stat icon={Users} label="Clients" value={clients.length} tint="bg-blue-50 text-blue-600 dark:bg-blue-950 dark:text-blue-400" />
        <Stat icon={FileStack} label="Documents tracked" value={totalFiles} tint="bg-teal-50 text-teal-600 dark:bg-teal-950 dark:text-teal-400" />
        <Stat icon={ShieldAlert} label="Open exceptions" value={totalExceptions} tint="bg-amber-50 text-amber-600 dark:bg-amber-950 dark:text-amber-400" />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Firm setup */}
        <Card className="lg:col-span-2 p-6">
          <div className="flex items-center gap-2 mb-5">
            <Building2 className="h-5 w-5 text-primary" />
            <h2 className="font-head text-lg font-semibold">Firm Setup</h2>
          </div>
          <div className="space-y-4">
            <div>
              <Label>Firm name</Label>
              <Input data-testid="firm-name-input" className="mt-1.5" value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="e.g. Riverside Tax & Accounting" />
            </div>
            <div>
              <Label>Contact email</Label>
              <Input data-testid="firm-email-input" className="mt-1.5" value={form.contact_email}
                onChange={(e) => setForm({ ...form, contact_email: e.target.value })} placeholder="office@firm.com" />
            </div>
            <div>
              <Label>Privacy / retention note</Label>
              <Textarea data-testid="firm-retention-input" className="mt-1.5" rows={3} value={form.retention_note}
                onChange={(e) => setForm({ ...form, retention_note: e.target.value })} />
            </div>
            <Button data-testid="firm-save-button" onClick={save} disabled={saving}>
              <Save className="h-4 w-4" /> {saving ? "Saving…" : "Save Settings"}
            </Button>
          </div>
        </Card>

        {/* Boundaries */}
        <Card className="p-6">
          <h2 className="font-head text-lg font-semibold mb-4">What this app will never do</h2>
          <div className="space-y-4">
            {PILLARS.map((p) => {
              const Icon = p.icon;
              return (
                <div key={p.title} className="flex gap-3">
                  <div className="h-9 w-9 rounded-lg bg-secondary flex items-center justify-center shrink-0">
                    <Icon className="h-4 w-4 text-primary" />
                  </div>
                  <div>
                    <div className="text-sm font-semibold">{p.title}</div>
                    <div className="text-xs text-muted-foreground leading-relaxed">{p.text}</div>
                  </div>
                </div>
              );
            })}
          </div>
        </Card>
      </div>
    </div>
  );
}
