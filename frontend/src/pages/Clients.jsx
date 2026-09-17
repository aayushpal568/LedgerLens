import React, { useState, useEffect } from "react";
import { useApp } from "@/context/AppContext";
import { api } from "@/lib/api";
import { Button, Card, Input, Select, Label, Badge, EmptyState, Textarea } from "@/components/ui";
import { CLIENT_TYPES } from "@/lib/utils";
import { toast } from "sonner";
import { Users, Plus, Trash2, FolderSearch, ShieldAlert, X, FileStack } from "lucide-react";

function AddClientModal({ onClose, onCreated }) {
  const [form, setForm] = useState({ name: "", client_type: "Small Business", notes: "" });
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (!form.name.trim()) return toast.error("Client name is required");
    setBusy(true);
    try {
      const c = await api.createClient(form);
      toast.success(`Client "${c.name}" created`);
      onCreated(c);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm" onClick={onClose}>
      <Card className="w-full max-w-md p-6" onClick={(e) => e.stopPropagation()} data-testid="add-client-modal">
        <div className="flex items-center justify-between mb-5">
          <h2 className="font-head text-lg font-semibold">New Client</h2>
          <button onClick={onClose} className="h-8 w-8 rounded-md hover:bg-secondary flex items-center justify-center"><X className="h-4 w-4" /></button>
        </div>
        <div className="space-y-4">
          <div>
            <Label>Client name</Label>
            <Input data-testid="client-name-input" autoFocus className="mt-1.5" value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="e.g. Acme LLC" />
          </div>
          <div>
            <Label>Client type</Label>
            <Select data-testid="client-type-select" className="mt-1.5" value={form.client_type}
              onChange={(e) => setForm({ ...form, client_type: e.target.value })}>
              {CLIENT_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </Select>
          </div>
          <div>
            <Label>Notes (optional)</Label>
            <Textarea className="mt-1.5" rows={2} value={form.notes}
              onChange={(e) => setForm({ ...form, notes: e.target.value })} />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="outline" onClick={onClose}>Cancel</Button>
            <Button data-testid="client-create-submit" onClick={submit} disabled={busy}>
              <Plus className="h-4 w-4" /> Create
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}

import { TwoStepDeleteDialog } from "@/components/TwoStepDeleteDialog";

export default function Clients() {
  const { clients, refreshClients, setActiveClient, setTab, activeClient } = useApp();
  const [showAdd, setShowAdd] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState(null);

  useEffect(() => { refreshClients(); }, [refreshClients]);

  const openScan = (c) => { setActiveClient(c); setTab("scan_workspace"); };
  const openReview = (c) => { setActiveClient(c); setTab("review_center"); };

  const promptRemove = (c, e) => {
    e.stopPropagation();
    setDeleteTarget(c);
  };

  const handleConfirmDelete = async () => {
    if (!deleteTarget) return;
    try {
      await api.deleteClient(deleteTarget.id);
      if (activeClient?.id === deleteTarget.id) setActiveClient(null);
      await refreshClients();
      toast.success(`Client "${deleteTarget.name}" deleted`);
    } catch (err) {
      toast.error("Failed to delete client");
    } finally {
      setDeleteTarget(null);
    }
  };

  return (
    <div className="max-w-6xl mx-auto p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-head text-2xl font-bold tracking-tight flex items-center gap-2">
            <Users className="h-6 w-6 text-primary" /> Client Directory
          </h1>
          <p className="text-sm text-muted-foreground mt-1">Create clients and open a folder workspace to scan.</p>
        </div>
        <Button data-testid="add-client-button" onClick={() => setShowAdd(true)}>
          <Plus className="h-4 w-4" /> Add Client
        </Button>
      </div>

      {clients.length === 0 ? (
        <Card>
          <EmptyState icon={Users} title="No clients yet"
            subtitle="Add your first client to create a document workspace and run a scan."
            action={<Button data-testid="empty-add-client" onClick={() => setShowAdd(true)}><Plus className="h-4 w-4" /> Add Client</Button>} />
        </Card>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {clients.map((c) => (
            <Card key={c.id} data-testid={`client-card-${c.id}`}
              className="p-5 hover:shadow-md transition-shadow cursor-pointer group"
              onClick={() => openScan(c)}>
              <div className="flex items-start justify-between">
                <div className="min-w-0">
                  <div className="font-head font-semibold truncate">{c.name}</div>
                  <Badge className="mt-1.5 bg-secondary text-secondary-foreground border-border">{c.client_type}</Badge>
                </div>
                <button data-testid={`delete-client-${c.id}`} onClick={(e) => promptRemove(c, e)}
                  className="h-8 w-8 rounded-md hover:bg-rose-50 hover:text-rose-600 flex items-center justify-center text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity"
                  title="Delete client">
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
              <div className="flex items-center gap-4 mt-4 text-xs text-muted-foreground">
                <span className="flex items-center gap-1"><FileStack className="h-3.5 w-3.5" /> {c.file_count || 0} files</span>
                {c.last_scan && (
                  <span className="flex items-center gap-1"><ShieldAlert className="h-3.5 w-3.5" /> {c.last_scan.total_findings ?? 0} flags</span>
                )}
              </div>
              <div className="flex gap-2 mt-4" onClick={(e) => e.stopPropagation()}>
                <Button size="sm" variant="outline" className="flex-1" data-testid={`scan-client-${c.id}`} onClick={() => openScan(c)}>
                  <FolderSearch className="h-3.5 w-3.5" /> Workspace
                </Button>
                <Button size="sm" variant="ghost" className="flex-1" disabled={!c.last_scan} data-testid={`review-client-${c.id}`} onClick={() => openReview(c)}>
                  <ShieldAlert className="h-3.5 w-3.5" /> Review
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}

      {showAdd && (
        <AddClientModal onClose={() => setShowAdd(false)}
          onCreated={async (c) => { setShowAdd(false); await refreshClients(); setActiveClient(c); }} />
      )}

      <TwoStepDeleteDialog
        open={!!deleteTarget}
        onOpenChange={(isOpen) => { if (!isOpen) setDeleteTarget(null); }}
        onConfirm={handleConfirmDelete}
        itemName={deleteTarget ? deleteTarget.name : ""}
      />
    </div>
  );
}
