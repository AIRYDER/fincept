"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Command } from "cmdk";
import {
  ArrowRight,
  AlertTriangle,
  CheckSquare,
  Database,
  FileJson,
  HeartPulse,
  Power,
  RefreshCw,
  Search,
  ShieldAlert,
  Sparkles,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useMemo } from "react";
import { create } from "zustand";

import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";

import {
  buildEntityResults,
  COMMANDS,
  type EntitySearchResult,
  type PaletteCommand,
} from "./command-registry";

interface PaletteState {
  open: boolean;
  setOpen: (v: boolean) => void;
}

export const useCommandPalette = create<PaletteState>((set) => ({
  open: false,
  setOpen: (open) => set({ open }),
}));

// ---------------------------------------------------------------------------
// Icon map (lucide-react name → component)
// ---------------------------------------------------------------------------

const ICON_MAP: Record<string, React.ComponentType<{ className?: string }>> = {
  LayoutDashboard: Sparkles,
  Briefcase: ShieldAlert,
  ScrollText: FileJson,
  GitCompareArrows: Database,
  Bot: Sparkles,
  PieChart: Sparkles,
  Newspaper: Search,
  Microscope: Search,
  FlaskConical: Sparkles,
  Activity: Sparkles,
  BarChart3: Sparkles,
  Brain: Sparkles,
  FileJson: FileJson,
  ShieldAlert: ShieldAlert,
  Power: Power,
  Search: Search,
  CheckSquare: CheckSquare,
  HeartPulse: HeartPulse,
  Database: Database,
  RefreshCw: RefreshCw,
  Play: ArrowRight,
  Square: ShieldAlert,
  ArrowUpCircle: ArrowRight,
  Send: ArrowRight,
};

function CommandIcon({ name, className }: { name: string; className?: string }) {
  const Comp = ICON_MAP[name] ?? Search;
  return <Comp className={className ?? "h-4 w-4 text-muted-foreground"} />;
}

// ---------------------------------------------------------------------------
// Entity type badge
// ---------------------------------------------------------------------------

function entityTypeBadge(type: EntitySearchResult["type"]) {
  const variants: Record<EntitySearchResult["type"], React.ComponentProps<typeof Badge>["variant"]> = {
    symbol: "secondary",
    strategy: "outline",
    model: "default",
  };
  return <Badge variant={variants[type]}>{type}</Badge>;
}

// ---------------------------------------------------------------------------
// Command Palette v2
// ---------------------------------------------------------------------------

/**
 * Bloomberg-style command palette 2.0: Cmd/Ctrl+K opens; type a mnemonic,
 * page name, symbol, strategy, or command to navigate or act.
 *
 * Dangerous commands never execute directly — they route to confirmation
 * surfaces with query params.
 */
export function CommandPalette() {
  const open = useCommandPalette((s) => s.open);
  const setOpen = useCommandPalette((s) => s.setOpen);
  const router = useRouter();
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();

  // Fetch entities for search
  const positionsQ = useQuery({
    queryKey: ["positions", "palette"],
    queryFn: () => api.positions(token, true),
    enabled: !!token && open,
    staleTime: 30_000,
  });
  const strategiesQ = useQuery({
    queryKey: ["strategies", "palette"],
    queryFn: () => api.strategies(token),
    enabled: !!token && open,
    staleTime: 30_000,
  });

  // Build entity index
  const entities = useMemo<EntitySearchResult[]>(() => {
    const symbols = Array.from(
      new Set(
        (positionsQ.data ?? []).map((p: { symbol: string }) => p.symbol),
      ),
    );
    const strategyIds = (strategiesQ.data ?? []).map(
      (s: { strategy_id: string }) => s.strategy_id,
    );
    return buildEntityResults(symbols, strategyIds, []);
  }, [positionsQ.data, strategiesQ.data]);

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
    if (href === "#refresh") {
      queryClient.invalidateQueries();
      return;
    }
    router.push(href);
  };

  // Group commands by group
  const grouped = useMemo(() => {
    const map = new Map<string, PaletteCommand[]>();
    for (const cmd of COMMANDS) {
      const list = map.get(cmd.group) ?? [];
      list.push(cmd);
      map.set(cmd.group, list);
    }
    return map;
  }, []);

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
            return 0;
          }}
        >
          <Command.Input
            autoFocus
            placeholder="Type a page, mnemonic (OV/PS/OR…), symbol, strategy, or command…"
            className="h-12 w-full border-b border-border/60 bg-transparent px-4 text-sm outline-none placeholder:text-muted-foreground"
          />
          <Command.List className="max-h-96 overflow-y-auto p-2 scrollbar-thin">
            <Command.Empty className="px-3 py-6 text-center text-sm text-muted-foreground">
              No matches.
            </Command.Empty>

            {/* Entity search results */}
            {entities.length > 0 ? (
              <Command.Group
                heading="Entities"
                className="text-xs uppercase text-muted-foreground"
              >
                {entities.map((entity) => (
                  <Command.Item
                    key={entity.id}
                    value={`${entity.label}|${entity.keywords.join(" ")}`}
                    onSelect={() => go(entity.href)}
                    className="flex cursor-pointer items-center gap-2 rounded-md px-3 py-2 text-sm aria-selected:bg-accent"
                  >
                    <Search className="h-4 w-4 text-primary" />
                    <span className="flex-1 font-mono">{entity.label}</span>
                    {entityTypeBadge(entity.type)}
                    <ArrowRight className="h-3 w-3 text-muted-foreground" />
                  </Command.Item>
                ))}
              </Command.Group>
            ) : null}

            {/* Static commands by group */}
            {Array.from(grouped.entries()).map(([group, cmds]) => (
              <Command.Group
                key={group}
                heading={group}
                className="text-xs uppercase text-muted-foreground"
              >
                {cmds.map((cmd) => (
                  <Command.Item
                    key={cmd.id}
                    value={`${cmd.label}|${cmd.mnemonic ?? ""}|${cmd.keywords.join(" ")}`}
                    onSelect={() => go(cmd.href)}
                    className={`flex cursor-pointer items-center gap-2 rounded-md px-3 py-2 text-sm ${
                      cmd.safety === "confirm"
                        ? "aria-selected:bg-destructive/10"
                        : "aria-selected:bg-accent"
                    }`}
                  >
                    <CommandIcon
                      name={cmd.icon}
                      className={
                        cmd.safety === "confirm"
                          ? "h-4 w-4 text-destructive"
                          : cmd.safety === "readonly"
                            ? "h-4 w-4 text-primary"
                            : "h-4 w-4 text-muted-foreground"
                      }
                    />
                    <span className="flex-1">{cmd.label}</span>
                    {cmd.safety === "confirm" && (
                      <Badge variant="destructive" className="text-[9px]">
                        CONFIRM
                      </Badge>
                    )}
                    {cmd.safety === "readonly" && (
                      <Badge variant="secondary" className="text-[9px]">
                        READ-ONLY
                      </Badge>
                    )}
                    {cmd.mnemonic && (
                      <kbd className="rounded border border-border/60 bg-background/60 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                        {cmd.mnemonic}
                      </kbd>
                    )}
                    <ArrowRight className="h-3 w-3 text-muted-foreground" />
                  </Command.Item>
                ))}
              </Command.Group>
            ))}
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
            <span className="font-mono">
              {entities.length} entities · {COMMANDS.length} commands
            </span>
          </div>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
