import React, { useState, useEffect } from "react";
import { api } from "@/lib/api";
import { Button, Card, Input, Select, Label, Badge, EmptyState } from "@/components/ui";
import { CLIENT_TYPES } from "@/lib/utils";
import { toast } from "sonner";
import { FileCheck2, Plus, Trash2, X, Save, Pencil, ListChecks } from "lucide-react";

const TYPE_OPTIONS = ["PDF", "JPG", "JPEG", "PNG", "TIFF", "DOCX", "XLSX", "CSV"];
const emptyItem = () => ({ name: "", aliases: [], allowed_types: [], rule: {} });

function ItemEditor({ item, onChange, onRemove }) {
  const [aliasText, setAliasText] = useState((item.aliases || []).join(", "));
  const toggleType = (t) => {
    const has = item.allowed_types.includes(t);
    onChange({ ...item, allowed_types: has ? item.allowed_types.filter((x) => x !== t) : [...item.allowed_types, t] });
  };
  return (
    <div className="rounded-lg border border-border p-4 space-y-3">
      <div className="flex items-center gap-2">
        <Input placeholder="Checklist item name (e.g. Bank Statement)" value={item.name}
          onChange={(e) => onChange({ ...item, name: e.target.value })} data-testid="item-name-input" />
        <button onClick={onRemove} className="h-9 w-9 shrink-0 rounded-md hover:bg-rose-50 hover:text-rose-600 flex items-center justify-center text-muted-foreground">
          <Trash2 className="h-4 w-4" />
        </button>
      </div>
      <div>
        <Label>Aliases / synonyms (comma separated)</Label>
        <Input className="mt-1" placeholder="bank stmt, statement, checking" value={aliasText}
          onChange={(e) => { setAliasText(e.target.value); onChange({ ...item, aliases: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) }); }} />
      </div>
      <div>
        <Label>Allowed file types</Label>
        <div className="flex flex-wrap gap-1.5 mt-1.5">
          {TYPE_OPTIONS.map((t) => (
            <button key={t} onClick={() => toggleType(t)}
              className={`px-2.5 py-1 rounded-md text-xs font-medium border transition-colors ${item.allowed_types.includes(t) ? "bg-primary text-primary-foreground border-primary" : "bg-card border-border text-muted-foreground hover:bg-secondary"}`}>
              {t}
            </button>
          ))}
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <Label>Vendor pattern (optional)</Label>
          <Input className="mt-1" placeholder="e.g. chase" value={item.rule?.vendor || ""}
            onChange={(e) => onChange({ ...item, rule: { ...item.rule, vendor: e.target.value } })} />
        </div>
        <label className="flex items-center gap-2 text-sm mt-6">
          <input type="checkbox" className="h-4 w-4 accent-teal-600" checked={!!item.rule?.requires_period}
            onChange={(e) => onChange({ ...item, rule: { ...item.rule, requires_period: e.target.checked } })} />
          Period-specific (month/year)
        </label>
      </div>
    </div>
  );
}

function TemplateEditor({ initial, onClose, onSaved }) {
  const [name, setName] = useState(initial?.name || "");
  const [clientType, setClientType] = useState(initial?.client_type || "Small Business");
  const [items, setItems] = useState(initial?.items?.length ? initial.items.map((i) => ({ aliases: [], allowed_types: [], rule: {}, ...i })) : [emptyItem()]);
  const [busy, setBusy] = useState(false);

  const save = async () => {
    if (!name.trim()) return toast.error("Template name is required");
    const clean = items.filter((i) => i.name.trim());
    if (!clean.length) return toast.error("Add at least one checklist item");
    setBusy(true);
    try {
      const body = { name, client_type: clientType, items: clean };
      if (initial?.id) await api.updateTemplate(initial.id, body);
      else await api.createTemplate(body);
      toast.success("Template saved");
      onSaved();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm" onClick={onClose}>
      <Card className="w-full max-w-2xl max-h-[88vh] flex flex-col" onClick={(e) => e.stopPropagation()} data-testid="template-editor-modal">
        <div className="flex items-center justify-between p-5 border-b border-border">
          <h2 className="font-head text-lg font-semibold">{initial?.id ? "Edit" : "New"} Checklist Template</h2>
          <button onClick={onClose} className="h-8 w-8 rounded-md hover:bg-secondary flex items-center justify-center"><X className="h-4 w-4" /></button>
        </div>
        <div className="p-5 overflow-y-auto space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>Template name</Label>
              <Input className="mt-1.5" value={name} onChange={(e) => setName(e.target.value)} data-testid="template-name-input" placeholder="e.g. Small Business Package" />
            </div>
            <div>
              <Label>Client type</Label>
              <Select className="mt-1.5" value={clientType} onChange={(e) => setClientType(e.target.value)} data-testid="template-type-select">
                {CLIENT_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
              </Select>
            </div>
          </div>
          <div className="space-y-3">
            <Label>Checklist items</Label>
            {items.map((it, i) => (
              <ItemEditor key={i} item={it}
                onChange={(v) => setItems(items.map((x, idx) => (idx === i ? v : x)))}
                onRemove={() => setItems(items.filter((_, idx) => idx !== i))} />
            ))}
            <Button variant="outline" size="sm" onClick={() => setItems([...items, emptyItem()])} data-testid="add-item-button">
              <Plus className="h-4 w-4" /> Add item
            </Button>
          </div>
        </div>
        <div className="flex justify-end gap-2 p-5 border-t border-border">
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button onClick={save} disabled={busy} data-testid="save-template-button"><Save className="h-4 w-4" /> Save Template</Button>
        </div>
      </Card>
    </div>
  );
}

import { TwoStepDeleteDialog } from "@/components/TwoStepDeleteDialog";

export default function Checklists() {
  const [templates, setTemplates] = useState([]);
  const [editing, setEditing] = useState(null);
  const [showNew, setShowNew] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState(null);

  const load = async () => setTemplates(await api.listTemplates());
  useEffect(() => { load(); }, []);

  const promptRemove = (t) => {
    setDeleteTarget(t);
  };

  const handleConfirmDelete = async () => {
    if (!deleteTarget) return;
    try {
      await api.deleteTemplate(deleteTarget.id);
      await load();
      toast.success(`Template "${deleteTarget.name}" deleted`);
    } catch (err) {
      toast.error("Failed to delete template");
    } finally {
      setDeleteTarget(null);
    }
  };

  return (
    <div className="max-w-6xl mx-auto p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-head text-2xl font-bold tracking-tight flex items-center gap-2">
            <FileCheck2 className="h-6 w-6 text-primary" /> Checklist Templates
          </h1>
          <p className="text-sm text-muted-foreground mt-1">Define expected documents per client type — matched by name, aliases, and rules.</p>
        </div>
        <Button data-testid="new-template-button" onClick={() => setShowNew(true)}><Plus className="h-4 w-4" /> New Template</Button>
      </div>

      {templates.length === 0 ? (
        <Card><EmptyState icon={ListChecks} title="No templates" subtitle="Create a checklist template to enable missing-document detection." /></Card>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {templates.map((t) => (
            <Card key={t.id} className="p-5" data-testid={`template-card-${t.id}`}>
              <div className="flex items-start justify-between">
                <div>
                  <div className="font-head font-semibold">{t.name}</div>
                  <Badge className="mt-1.5 bg-secondary text-secondary-foreground border-border">{t.client_type}</Badge>
                </div>
                <div className="flex gap-1">
                  <button onClick={() => setEditing(t)} data-testid={`edit-template-${t.id}`} className="h-8 w-8 rounded-md hover:bg-secondary flex items-center justify-center text-muted-foreground"><Pencil className="h-4 w-4" /></button>
                  <button onClick={() => promptRemove(t)} data-testid={`delete-template-${t.id}`} className="h-8 w-8 rounded-md hover:bg-rose-50 hover:text-rose-600 flex items-center justify-center text-muted-foreground" title="Delete template"><Trash2 className="h-4 w-4" /></button>
                </div>
              </div>
              <div className="mt-4 space-y-1.5">
                {t.items.map((it, i) => (
                  <div key={i} className="flex items-center gap-2 text-sm">
                    <ListChecks className="h-3.5 w-3.5 text-primary shrink-0" />
                    <span className="truncate">{it.name}</span>
                    {it.allowed_types?.length > 0 && (
                      <span className="ml-auto text-xs text-muted-foreground font-mono-data">{it.allowed_types.join("/")}</span>
                    )}
                  </div>
                ))}
              </div>
            </Card>
          ))}
        </div>
      )}

      {(showNew || editing) && (
        <TemplateEditor initial={editing}
          onClose={() => { setShowNew(false); setEditing(null); }}
          onSaved={async () => { setShowNew(false); setEditing(null); await load(); }} />
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
