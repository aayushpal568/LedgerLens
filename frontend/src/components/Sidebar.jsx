import React from "react";
import { useApp } from "@/context/AppContext";
import { cn } from "@/lib/utils";
import {
  Building2, Users, FolderSearch, FileCheck2, ShieldAlert, Columns2, FileSpreadsheet, Lock, Sparkles,
} from "lucide-react";

const NAV = [
  { id: "dashboard", label: "Overview & Firm", icon: Building2 },
  { id: "agent_chat", label: "LedgerLens AI", icon: Sparkles },
  { id: "clients", label: "Client Directory", icon: Users },
  { id: "scan_workspace", label: "Scan Workspace", icon: FolderSearch },
  { id: "checklists", label: "Checklist Templates", icon: FileCheck2 },
  { id: "review_center", label: "Exception Review", icon: ShieldAlert },
  { id: "file_compare", label: "File Diff Inspector", icon: Columns2 },
  { id: "reports", label: "Reports & Exports", icon: FileSpreadsheet },
];

export default function Sidebar({ open = false, onClose }) {
  const { tab, setTab, activeClient } = useApp();
  const go = (id) => {
    setTab(id);
    if (onClose) onClose(); // auto-close the mobile drawer after navigating
  };
  return (
    <>
      {/* Mobile backdrop */}
      {open && (
        <div
          className="fixed inset-0 z-30 bg-black/40 md:hidden"
          data-testid="nav-backdrop"
          onClick={onClose}
          aria-hidden="true"
        />
      )}
      <aside
        data-testid="sidebar"
        data-open={open ? "true" : "false"}
        className={cn(
          "w-60 shrink-0 border-r border-border bg-card flex flex-col",
          // Off-canvas on mobile; a normal static column on md+
          "fixed inset-y-0 left-0 z-40 transition-transform duration-200 md:static md:z-auto md:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full md:translate-x-0"
        )}
      >
        <nav className="flex-1 p-3 space-y-1" data-testid="main-nav">
          {NAV.map((item) => {
            const Icon = item.icon;
            const active = tab === item.id;
            const disabled = ["review_center", "file_compare", "reports", "scan_workspace"].includes(item.id) && !activeClient;
            return (
              <button
                key={item.id}
                data-testid={`nav-${item.id}`}
                disabled={disabled}
                onClick={() => go(item.id)}
                className={cn(
                  "w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors text-left",
                  active ? "bg-primary text-primary-foreground shadow-sm" : "text-muted-foreground hover:bg-secondary hover:text-foreground",
                  disabled && "opacity-40 pointer-events-none"
                )}
              >
                <Icon className="h-4 w-4 shrink-0" />
                <span className="truncate">{item.label}</span>
              </button>
            );
          })}
        </nav>
        <div className="p-3 border-t border-border">
          <div className="flex items-start gap-2 text-xs text-muted-foreground rounded-lg bg-secondary p-3">
            <Lock className="h-3.5 w-3.5 mt-0.5 shrink-0 text-primary" />
            <span>Read-only engine. Never deletes, renames, moves, or emails. All actions need human review.</span>
          </div>
        </div>
      </aside>
    </>
  );
}
