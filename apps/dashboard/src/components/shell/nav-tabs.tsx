"use client";

/**
 * Bloomberg-terminal horizontal primary navigation.
 *
 * Replaces the left sidebar.  ALL CAPS tab names, orange underline on
 * the active tab, bottom border separates the strip from the page
 * body.  Each label has a two-letter mnemonic rendered as a subscript,
 * so power users can see `OV`/`PS`/`OR`/… without opening the palette.
 */

import { Command, Search } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useCommandPalette } from "@/components/shell/command-palette";
import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  label: string;
  mnemonic: string;
}

export const NAV_ITEMS: NavItem[] = [
  { href: "/", label: "OVERVIEW", mnemonic: "OV" },
  { href: "/positions", label: "POSITIONS", mnemonic: "PS" },
  { href: "/orders", label: "ORDERS", mnemonic: "OR" },
  { href: "/reconciliation", label: "RECON", mnemonic: "RC" },
  { href: "/strategies", label: "STRATEGIES", mnemonic: "ST" },
  { href: "/portfolio-builder", label: "OPTIMIZER", mnemonic: "PO" },
  { href: "/news", label: "NEWS", mnemonic: "NW" },
  { href: "/research", label: "RESEARCH", mnemonic: "RX" },
  { href: "/news-impact-lab", label: "NEWS LAB", mnemonic: "NL" },
  { href: "/predictions", label: "PREDICTIONS", mnemonic: "PR" },
  { href: "/markets", label: "MARKETS", mnemonic: "MK" },
  { href: "/models", label: "MODELS", mnemonic: "ML" },
  { href: "/receipts", label: "RECEIPTS", mnemonic: "PF" },
  { href: "/risk", label: "RISK", mnemonic: "RK" },
  { href: "/system", label: "SYSTEM", mnemonic: "SY" },
];

export function NavTabs() {
  const pathname = usePathname();
  const open = useCommandPalette((s) => s.setOpen);

  return (
    <nav className="flex h-9 shrink-0 items-center border-b border-border bg-background">
      <div className="flex items-stretch overflow-x-auto">
        {NAV_ITEMS.map((item) => {
          const active =
            item.href === "/"
              ? pathname === "/"
              : pathname?.startsWith(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "group relative flex items-center gap-2 border-r border-border px-4 text-[11px] font-semibold uppercase tracking-wider transition-colors",
                active
                  ? "bg-primary/10 text-primary"
                  : "text-muted-foreground hover:bg-accent hover:text-foreground",
              )}
            >
              {item.label}
              <span
                className={cn(
                  "text-[9px] font-normal",
                  active
                    ? "text-primary/60"
                    : "text-border group-hover:text-muted-foreground",
                )}
              >
                {item.mnemonic}
              </span>
              {active && (
                <span className="pointer-events-none absolute inset-x-0 -bottom-px h-[2px] bg-primary" />
              )}
            </Link>
          );
        })}
      </div>

      <div className="flex-1" />

      {/* Command palette trigger */}
      <button
        onClick={() => open(true)}
        className="mr-2 flex h-6 items-center gap-2 border border-border bg-background px-2 text-[10px] uppercase tracking-wider text-muted-foreground hover:border-primary hover:text-foreground"
      >
        <Search className="h-3 w-3" />
        Search · Command
        <span className="ml-2 inline-flex items-center gap-[2px] border border-border bg-card px-1 text-[9px] text-muted-foreground">
          <Command className="h-2.5 w-2.5" />
          K
        </span>
      </button>
    </nav>
  );
}
