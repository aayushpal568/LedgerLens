import { clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs) {
  return twMerge(clsx(inputs));
}

export function formatBytes(bytes = 0) {
  if (!bytes) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

export const CATEGORIES = [
  { id: "exact_duplicate", label: "Exact Duplicates", icon: "Copy", dot: "bg-rose-500",
    badge: "bg-rose-50 text-rose-700 border-rose-200 dark:bg-rose-950 dark:text-rose-300 dark:border-rose-900" },
  { id: "possible_duplicate", label: "Possible Duplicates", icon: "CopyPlus", dot: "bg-amber-500",
    badge: "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:border-amber-900" },
  { id: "missing_doc", label: "Missing Docs", icon: "FileQuestion", dot: "bg-blue-500",
    badge: "bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-950 dark:text-blue-300 dark:border-blue-900" },
  { id: "wrong_period", label: "Wrong Period", icon: "CalendarClock", dot: "bg-violet-500",
    badge: "bg-violet-50 text-violet-700 border-violet-200 dark:bg-violet-950 dark:text-violet-300 dark:border-violet-900" },
  { id: "wrong_type", label: "Wrong Type", icon: "FileWarning", dot: "bg-pink-500",
    badge: "bg-pink-50 text-pink-700 border-pink-200 dark:bg-pink-950 dark:text-pink-300 dark:border-pink-900" },
  { id: "unreadable", label: "Unreadable", icon: "FileX2", dot: "bg-slate-500",
    badge: "bg-slate-100 text-slate-700 border-slate-300 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700" },
];

export const CATEGORY_MAP = Object.fromEntries(CATEGORIES.map((c) => [c.id, c]));

export const STATUS_OPTIONS = [
  { id: "unreviewed", label: "Unreviewed" },
  { id: "keep", label: "Keep" },
  { id: "keep_both", label: "Keep Both" },
  { id: "ignore", label: "Ignore" },
  { id: "review_later", label: "Review Later" },
];
export const STATUS_MAP = Object.fromEntries(STATUS_OPTIONS.map((s) => [s.id, s.label]));

export const CLIENT_TYPES = ["Individual", "Small Business", "Corporation", "Non-Profit", "Partnership"];
