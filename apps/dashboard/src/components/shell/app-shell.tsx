"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { CommandPalette } from "@/components/shell/command-palette";
import { NavTabs } from "@/components/shell/nav-tabs";
import { SafetyStateBar } from "@/components/shell/safety-state-bar";
import { StatusBar } from "@/components/shell/status-bar";

import { StreamInvalidator } from "@/components/shell/stream-invalidator";
import { TitleBar } from "@/components/shell/title-bar";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useAuth } from "@/lib/auth";

/**
 * Bloomberg-terminal authenticated shell.
 *
 *   ┌ TitleBar  (branding · clock · API/WS pills · kill · logout) ┐
 *   ├ NavTabs   (OVERVIEW · POSITIONS · ORDERS · … · search/cmd) ┤
 *   │                                                             │
 *   │                  main (widget grid pages)                   │
 *   │                                                             │
 *   ├ StatusBar (version · session · feeds · mem · latency · rdy) ┤
 *
 * All four strips are single-line, monospace, uppercase.  The layout
 * is a vertical flex column so the main area expands to the remaining
 * space with its own scroll container.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const token = useAuth((s) => s.token);
  const router = useRouter();
  useEffect(() => {
    if (token === null) {
      // hydrate runs once on app boot; null AFTER hydration means logged out.
      const stored =
        typeof window !== "undefined"
          ? localStorage.getItem("fincept.token")
          : null;
      if (!stored) router.replace("/login");
    }
  }, [token, router]);

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex h-screen flex-col overflow-hidden bg-background print:h-auto print:overflow-visible print:bg-white">
        <StreamInvalidator />
        <div className="print:hidden">
          <TitleBar />
          <SafetyStateBar />
          <NavTabs />
        </div>
        <main className="scrollbar-thin flex-1 overflow-y-auto p-2 print:block print:overflow-visible print:bg-white print:p-0">
          {children}
        </main>
        <div className="print:hidden">
          <StatusBar />
        </div>
      </div>
      <div className="print:hidden">
        <CommandPalette />
      </div>
    </TooltipProvider>
  );
}
