import React from "react";
import { useApp } from "@/context/AppContext";
import { ShieldCheck, Minus, Square, X, Moon, Sun, ChevronDown } from "lucide-react";
import { Badge } from "@/components/ui";

export default function Titlebar() {
  const { firm, dark, setDark, activeClient } = useApp();

  return (
    <div
      data-testid="app-titlebar"
      className="h-11 shrink-0 flex items-center justify-between px-3 border-b border-border bg-card/80 backdrop-blur-xl sticky top-0 z-30"
    >
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-1.5 pr-3 border-r border-border">
          <span className="h-3 w-3 rounded-full bg-rose-400" />
          <span className="h-3 w-3 rounded-full bg-amber-400" />
          <span className="h-3 w-3 rounded-full bg-emerald-400" />
        </div>
        <div className="flex items-center gap-2">
          <div className="h-6 w-6 rounded-md bg-primary flex items-center justify-center">
            <ShieldCheck className="h-4 w-4 text-primary-foreground" />
          </div>
          <span className="font-head font-bold text-sm tracking-tight">LedgerLens</span>
          <span className="text-xs text-muted-foreground hidden sm:inline">Document Checker</span>
        </div>
        {activeClient && (
          <div className="hidden md:flex items-center gap-1.5 pl-3 border-l border-border text-xs text-muted-foreground">
            <span>Active client</span>
            <ChevronDown className="h-3 w-3" />
            <span className="font-semibold text-foreground">{activeClient.name}</span>
          </div>
        )}
      </div>

      <div className="flex items-center gap-2">
        <Badge
          data-testid="privacy-status-badge"
          className="bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:border-emerald-900"
        >
          <ShieldCheck className="h-3.5 w-3.5" />
          <span className="hidden sm:inline">100% Local · No Cloud Decisions</span>
          <span className="sm:hidden">Local</span>
        </Badge>
        <button
          data-testid="theme-toggle"
          onClick={() => setDark(!dark)}
          className="h-8 w-8 rounded-md flex items-center justify-center hover:bg-secondary transition-colors"
        >
          {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </button>
        <div className="hidden sm:flex items-center gap-1 pl-1">
          <span className="h-7 w-7 rounded-md flex items-center justify-center hover:bg-secondary transition-colors"><Minus className="h-3.5 w-3.5" /></span>
          <span className="h-7 w-7 rounded-md flex items-center justify-center hover:bg-secondary transition-colors"><Square className="h-3 w-3" /></span>
          <span className="h-7 w-7 rounded-md flex items-center justify-center hover:bg-rose-500 hover:text-white transition-colors"><X className="h-3.5 w-3.5" /></span>
        </div>
      </div>
    </div>
  );
}
