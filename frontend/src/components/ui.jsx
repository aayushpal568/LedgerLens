import React from "react";
import { cn } from "@/lib/utils";
import { Loader2 } from "lucide-react";

export function Button({ variant = "primary", size = "md", className, children, ...props }) {
  const base =
    "inline-flex items-center justify-center gap-2 font-medium rounded-lg select-none whitespace-nowrap " +
    "transition-[background-color,color,box-shadow,transform] duration-150 active:scale-[0.98] " +
    "disabled:opacity-50 disabled:pointer-events-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
  const variants = {
    primary: "bg-primary text-primary-foreground hover:shadow-md hover:brightness-110",
    secondary: "bg-secondary text-secondary-foreground hover:bg-accent",
    outline: "border border-border bg-card hover:bg-secondary text-foreground",
    ghost: "hover:bg-secondary text-foreground",
    danger: "bg-destructive text-destructive-foreground hover:brightness-110",
  };
  const sizes = { sm: "h-8 px-3 text-xs", md: "h-10 px-4 text-sm", lg: "h-11 px-6 text-sm", icon: "h-9 w-9" };
  return (
    <button className={cn(base, variants[variant], sizes[size], className)} {...props}>
      {children}
    </button>
  );
}

export function Card({ className, children, ...props }) {
  return (
    <div className={cn("bg-card border border-border rounded-xl", className)} {...props}>
      {children}
    </div>
  );
}

export function Badge({ className, children, ...props }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs font-medium border",
        className
      )}
      {...props}
    >
      {children}
    </span>
  );
}

export function ConfidenceBadge({ level, value }) {
  const styles = {
    high: "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:border-emerald-900",
    medium: "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:border-amber-900",
    low: "bg-slate-100 text-slate-600 border-slate-300 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700",
  };
  return (
    <Badge className={cn(styles[level] || styles.low, "font-mono-data")} data-testid="confidence-badge">
      {value}% · {level}
    </Badge>
  );
}

export function Input({ className, ...props }) {
  return (
    <input
      className={cn(
        "h-10 w-full rounded-lg border border-input bg-card px-3 text-sm text-foreground",
        "placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring transition-shadow",
        className
      )}
      {...props}
    />
  );
}

export function Textarea({ className, ...props }) {
  return (
    <textarea
      className={cn(
        "w-full rounded-lg border border-input bg-card px-3 py-2 text-sm text-foreground",
        "placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring transition-shadow",
        className
      )}
      {...props}
    />
  );
}

export function Select({ className, children, ...props }) {
  return (
    <select
      className={cn(
        "h-10 w-full rounded-lg border border-input bg-card px-3 text-sm text-foreground",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring transition-shadow",
        className
      )}
      {...props}
    >
      {children}
    </select>
  );
}

export function Label({ className, children, ...props }) {
  return (
    <label className={cn("text-xs font-semibold text-muted-foreground uppercase tracking-wide", className)} {...props}>
      {children}
    </label>
  );
}

export function Spinner({ className }) {
  return <Loader2 className={cn("animate-spin", className)} />;
}

export function EmptyState({ icon: Icon, title, subtitle, action }) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-16 px-6">
      {Icon && (
        <div className="h-14 w-14 rounded-2xl bg-secondary flex items-center justify-center mb-4">
          <Icon className="h-7 w-7 text-muted-foreground" />
        </div>
      )}
      <h3 className="font-head text-lg font-semibold">{title}</h3>
      {subtitle && <p className="text-sm text-muted-foreground mt-1 max-w-md">{subtitle}</p>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}
