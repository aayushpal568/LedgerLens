import React from "react";
import { useApp } from "@/context/AppContext";
import { cn } from "@/lib/utils";
import {
  Building2, Users, FolderSearch, FileCheck2, ShieldAlert, Columns2, FileSpreadsheet, Lock, Activity,
} from "lucide-react";

const NAV = [
  { id: "dashboard", label: "Overview & Firm", icon: Building2 },
  { id: "clients", label: "Client Directory", icon: Users },
  { id: "scan_workspace", label: "Scan Workspace", icon: FolderSearch },
  { id: "checklists", label: "Checklist Templates", icon: FileCheck2 },
  { id: "review_center", label: "Exception Review", icon: ShieldAlert },
  { id: "file_compare", label: "File Diff Inspector", icon: Columns2 },
  { id: "reports", label: "Reports & Exports", icon: FileSpreadsheet },
  { id: "system_check", label: "System Health", icon: Activity },
];

export default function Sidebar() {
  const { tab, setTab, activeClient } = useApp();
  return (
    <aside className="w-60 shrink-0 border-r border-border bg-card flex flex-col">
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
              onClick={() => setTab(item.id)}
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
  );
}
