"use client";

import { useQuery } from "@tanstack/react-query";
import {
  CircleDot,
  Command,
  LogOut,
  Power,
  Search,
  Wifi,
  WifiOff,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useCommandPalette } from "@/components/shell/command-palette";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useFinceptStream } from "@/lib/ws";
import { cn } from "@/lib/utils";

function HealthDot() {
  const token = useAuth((s) => s.token);
  const { data, isError } = useQuery({
    queryKey: ["health"],
    queryFn: () => api.health(token),
    refetchInterval: 5000,
    retry: 0,
  });
  const ok = !!data?.ok && !isError;
  return (
    <div className="flex items-center gap-2 rounded-md border border-border/60 bg-background/40 px-3 py-1.5">
      <CircleDot
        className={cn(
          "h-3.5 w-3.5",
          ok ? "text-long animate-pulse-slow" : "text-short",
        )}
      />
      <span className="text-xs font-medium">
        API <span className="text-muted-foreground">{data?.version ?? "—"}</span>
      </span>
    </div>
  );
}

function WsStatus() {
  const { status } = useFinceptStream({
    topics: ["alerts"],
  });
  return (
    <div className="flex items-center gap-2 rounded-md border border-border/60 bg-background/40 px-3 py-1.5">
      {status === "open" ? (
        <Wifi className="h-3.5 w-3.5 text-long animate-pulse-slow" />
      ) : (
        <WifiOff className="h-3.5 w-3.5 text-warn" />
      )}
      <span className="text-xs font-medium uppercase tracking-wider">
        WS <span className="text-muted-foreground">{status}</span>
      </span>
    </div>
  );
}

function NowClock() {
  const [now, setNow] = useState<string>("");
  useEffect(() => {
    const tick = () => {
      const d = new Date();
      const utc = d.toISOString().slice(11, 19);
      setNow(utc);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <div className="hidden items-center gap-2 rounded-md border border-border/60 bg-background/40 px-3 py-1.5 md:flex">
      <span className="font-mono text-xs tabular-nums">{now} UTC</span>
    </div>
  );
}

export function Topbar() {
  const router = useRouter();
  const setToken = useAuth((s) => s.setToken);
  const open = useCommandPalette((s) => s.setOpen);

  const handleLogout = () => {
    setToken(null);
    router.push("/login");
  };

  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b border-border/60 bg-card/40 px-4 backdrop-blur">
      <Button
        variant="outline"
        size="sm"
        className="flex w-72 justify-between font-normal text-muted-foreground"
        onClick={() => open(true)}
      >
        <span className="flex items-center gap-2">
          <Search className="h-3.5 w-3.5" />
          Search, jump, command…
        </span>
        <kbd className="pointer-events-none ml-auto inline-flex h-5 select-none items-center gap-1 rounded border border-border/60 bg-background/60 px-1.5 font-mono text-[10px]">
          <Command className="h-3 w-3" />K
        </kbd>
      </Button>

      <div className="flex-1" />

      <Badge variant="warn" className="hidden md:inline-flex">
        PAPER
      </Badge>

      <NowClock />
      <HealthDot />
      <WsStatus />

      <Button
        variant="destructive"
        size="sm"
        onClick={() => router.push("/risk")}
        className="ml-2"
      >
        <Power className="mr-1 h-3.5 w-3.5" />
        Kill switch
      </Button>

      <Button variant="ghost" size="icon" onClick={handleLogout} aria-label="Sign out">
        <LogOut className="h-4 w-4" />
      </Button>
    </header>
  );
}
