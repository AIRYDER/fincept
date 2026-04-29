"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { CommandPalette } from "@/components/shell/command-palette";
import { Sidebar } from "@/components/shell/sidebar";
import { Topbar } from "@/components/shell/topbar";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useAuth } from "@/lib/auth";

/**
 * The default authenticated shell.  Pages render inside <main> with the
 * sidebar + topbar fixed.  When unauthenticated, the user is bounced to
 * /login so no API call is attempted.
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
      <div className="flex h-screen overflow-hidden">
        <Sidebar />
        <div className="flex min-w-0 flex-1 flex-col">
          <Topbar />
          <main className="flex-1 overflow-y-auto p-6 scrollbar-thin">
            {children}
          </main>
        </div>
      </div>
      <CommandPalette />
    </TooltipProvider>
  );
}
