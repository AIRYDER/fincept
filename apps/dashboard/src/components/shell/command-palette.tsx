"use client";

import { Command } from "cmdk";
import {
  ArrowRight,
  Power,
  ShieldAlert,
  Sparkles,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { create } from "zustand";

import { NAV_ITEMS } from "@/components/shell/sidebar";
import { Dialog, DialogContent } from "@/components/ui/dialog";

interface PaletteState {
  open: boolean;
  setOpen: (v: boolean) => void;
}

export const useCommandPalette = create<PaletteState>((set) => ({
  open: false,
  setOpen: (open) => set({ open }),
}));

/**
 * Bloomberg-style command palette: Cmd/Ctrl+K opens; type a mnemonic
 * (OV, PS, OR…) or page name to navigate; arrow/enter to select.
 *
 * v1 covers nav-only.  TASK-056 expands this to symbol-level mnemonics
 * (e.g. "BTC <Equity>") and inline market actions ("BTC POS",
 * "BTC ORD <buy> <50000>").
 */
export function CommandPalette() {
  const open = useCommandPalette((s) => s.open);
  const setOpen = useCommandPalette((s) => s.setOpen);
  const router = useRouter();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen(!open);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, setOpen]);

  const go = (href: string) => {
    setOpen(false);
    router.push(href);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="max-w-xl gap-0 overflow-hidden p-0">
        <Command
          loop
          className="bg-transparent"
          filter={(value, search) => {
            const v = value.toLowerCase();
            const s = search.toLowerCase();
            if (v.includes(s)) return 1;
            // Match mnemonics first character.
            const mnemonic = v.split("|")[1]?.trim();
            if (mnemonic && mnemonic.toLowerCase().startsWith(s)) return 1;
            return 0;
          }}
        >
          <Command.Input
            autoFocus
            placeholder="Type a page, mnemonic (OV/PS/OR…), or command…"
            className="h-12 w-full border-b border-border/60 bg-transparent px-4 text-sm outline-none placeholder:text-muted-foreground"
          />
          <Command.List className="max-h-96 overflow-y-auto p-2 scrollbar-thin">
            <Command.Empty className="px-3 py-6 text-center text-sm text-muted-foreground">
              No matches.
            </Command.Empty>
            <Command.Group heading="Navigate" className="text-xs uppercase text-muted-foreground">
              {NAV_ITEMS.map((item) => {
                const Icon = item.icon;
                return (
                  <Command.Item
                    key={item.href}
                    value={`${item.label}|${item.mnemonic}`}
                    onSelect={() => go(item.href)}
                    className="flex cursor-pointer items-center gap-2 rounded-md px-3 py-2 text-sm aria-selected:bg-accent"
                  >
                    <Icon className="h-4 w-4 text-muted-foreground" />
                    <span className="flex-1">{item.label}</span>
                    <kbd className="rounded border border-border/60 bg-background/60 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                      {item.mnemonic}
                    </kbd>
                    <ArrowRight className="h-3 w-3 text-muted-foreground" />
                  </Command.Item>
                );
              })}
            </Command.Group>

            <Command.Group heading="Actions" className="mt-2 text-xs uppercase text-muted-foreground">
              <Command.Item
                value="kill switch"
                onSelect={() => go("/risk")}
                className="flex cursor-pointer items-center gap-2 rounded-md px-3 py-2 text-sm aria-selected:bg-destructive/10"
              >
                <Power className="h-4 w-4 text-destructive" />
                <span className="flex-1">Kill switch</span>
                <kbd className="rounded border border-border/60 bg-background/60 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                  KS
                </kbd>
              </Command.Item>
              <Command.Item
                value="risk panel"
                onSelect={() => go("/risk")}
                className="flex cursor-pointer items-center gap-2 rounded-md px-3 py-2 text-sm aria-selected:bg-accent"
              >
                <ShieldAlert className="h-4 w-4 text-warn" />
                <span className="flex-1">Risk panel</span>
              </Command.Item>
              <Command.Item
                value="predictions"
                onSelect={() => go("/predictions")}
                className="flex cursor-pointer items-center gap-2 rounded-md px-3 py-2 text-sm aria-selected:bg-accent"
              >
                <Sparkles className="h-4 w-4 text-primary" />
                <span className="flex-1">Live predictions</span>
              </Command.Item>
            </Command.Group>
          </Command.List>
          <div className="flex items-center justify-between border-t border-border/60 px-3 py-2 text-[10px] text-muted-foreground">
            <span>
              <kbd className="rounded border border-border/60 bg-background/60 px-1 py-0.5 font-mono">↵</kbd>{" "}
              select
              {" · "}
              <kbd className="rounded border border-border/60 bg-background/60 px-1 py-0.5 font-mono">↑↓</kbd>{" "}
              move
              {" · "}
              <kbd className="rounded border border-border/60 bg-background/60 px-1 py-0.5 font-mono">esc</kbd>{" "}
              close
            </span>
            <span className="font-mono">Fincept · cmdk</span>
          </div>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
