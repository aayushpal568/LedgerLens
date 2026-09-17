import React from "react";
import { useApp } from "@/context/AppContext";
import { ShieldCheck, Moon, Sun, ChevronDown, Cloud } from "lucide-react";
import { Badge } from "@/components/ui";

export default function Titlebar() {
  const { dark, setDark, activeClient } = useApp();

  return (
    <div
      data-testid="app-titlebar"
      className="h-11 shrink-0 flex items-center justify-between px-4 border-b border-border bg-card/80 backdrop-blur-xl sticky top-0 z-30"
    >
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2">
          <div className="h-6 w-6 rounded-md bg-primary flex items-center justify-center">
            <ShieldCheck className="h-4 w-4 text-primary-foreground" />
          </div>
          <span className="font-head font-bold text-sm tracking-tight">LedgerLens</span>
          <span className="text-xs text-muted-foreground hidden sm:inline">Cloud Accounting AI</span>
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
          <Cloud className="h-3.5 w-3.5 mr-1" />
          <span className="hidden sm:inline">Secure Cloud Processing</span>
          <span className="sm:hidden">Cloud</span>
        </Badge>
        <button
          data-testid="theme-toggle"
          onClick={() => setDark(!dark)}
          className="h-8 w-8 rounded-md flex items-center justify-center hover:bg-secondary transition-colors"
        >
          {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </button>
      </div>
    </div>
  );
}
