"use client";

import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bot,
  Brain,
  Briefcase,
  CircuitBoard,
  Coins,
  FlaskConical,
  FileJson,
  LayoutDashboard,
  Newspaper,
  PieChart,
  ScrollText,
  ShieldAlert,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  /** Single-letter Bloomberg-style mnemonic for the command palette. */
  mnemonic: string;
}

export const NAV_ITEMS: NavItem[] = [
  { href: "/", label: "Overview", icon: LayoutDashboard, mnemonic: "OV" },
  { href: "/positions", label: "Positions", icon: Briefcase, mnemonic: "PS" },
  { href: "/orders", label: "Orders", icon: ScrollText, mnemonic: "OR" },
  { href: "/strategies", label: "Strategies", icon: Bot, mnemonic: "ST" },
  { href: "/portfolio-builder", label: "Optimizer", icon: PieChart, mnemonic: "PO" },
  { href: "/news", label: "News", icon: Newspaper, mnemonic: "NW" },
  { href: "/news-impact-lab", label: "News Lab", icon: FlaskConical, mnemonic: "NL" },
  { href: "/predictions", label: "Predictions", icon: Activity, mnemonic: "PR" },
  { href: "/markets", label: "Markets", icon: BarChart3, mnemonic: "MK" },
  { href: "/backtest", label: "Backtest", icon: FlaskConical, mnemonic: "BT" },
  { href: "/models", label: "Models", icon: Brain, mnemonic: "ML" },
  { href: "/receipts", label: "Receipts", icon: FileJson, mnemonic: "PF" },
  { href: "/risk", label: "Risk", icon: ShieldAlert, mnemonic: "RK" },
];

export function Sidebar() {
  const pathname = usePathname();
  return (
    <aside className="flex h-screen w-60 shrink-0 flex-col border-r border-border/60 bg-card/30 backdrop-blur">
      <div className="flex h-14 items-center gap-2 border-b border-border/60 px-4">
        <div className="flex h-8 w-8 items-center justify-center rounded-md bg-primary/15 text-primary">
          <CircuitBoard className="h-4 w-4" />
        </div>
        <div className="flex flex-col leading-tight">
          <span className="text-sm font-semibold">Fincept Terminal</span>
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Operator
          </span>
        </div>
      </div>

      <nav className="flex-1 overflow-y-auto p-3 scrollbar-thin">
        <ul className="space-y-0.5">
          {NAV_ITEMS.map((item) => {
            const active =
              item.href === "/"
                ? pathname === "/"
                : pathname?.startsWith(item.href);
            const Icon = item.icon;
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  className={cn(
                    "group flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
                    active
                      ? "bg-primary/10 text-foreground"
                      : "text-muted-foreground hover:bg-accent hover:text-foreground",
                  )}
                >
                  <Icon
                    className={cn(
                      "h-4 w-4",
                      active ? "text-primary" : "text-muted-foreground group-hover:text-foreground",
                    )}
                  />
                  <span className="flex-1">{item.label}</span>
                  <kbd className="rounded border border-border/60 bg-background/60 px-1 py-0 font-mono text-[10px] text-muted-foreground">
                    {item.mnemonic}
                  </kbd>
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>

      <div className="border-t border-border/60 p-3 text-[11px] leading-relaxed text-muted-foreground">
        <div className="flex items-center gap-2">
          <Coins className="h-3.5 w-3.5" />
          <span>Paper trading mode</span>
        </div>
        <div className="mt-1 flex items-center gap-2">
          <AlertTriangle className="h-3.5 w-3.5 text-warn" />
          <span>No live capital at risk</span>
        </div>
      </div>
    </aside>
  );
}
