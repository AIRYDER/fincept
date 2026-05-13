"use client";

/**
 * Bloomberg-terminal title bar.
 *
 * Top-of-page strip with branding, date/time, session user, live
 * status pills (paper mode, API, WS), and logout.  Dense, monospace,
 * uppercase.  One line, no overflow.
 */

import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertTriangle, LogOut, Power } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useFinceptStream } from "@/lib/ws";
import { cn } from "@/lib/utils";

function useNow() {
  const [now, setNow] = useState<string>("");
  useEffect(() => {
    const tick = () => {
      const d = new Date();
      // "29 APR 26  21:33:05 UTC"
      const months = [
        "JAN",
        "FEB",
        "MAR",
        "APR",
        "MAY",
        "JUN",
        "JUL",
        "AUG",
        "SEP",
        "OCT",
        "NOV",
        "DEC",
      ];
      const utc = d.toISOString();
      const day = utc.slice(8, 10);
      const mon = months[d.getUTCMonth()];
      const yr = utc.slice(2, 4);
      const time = utc.slice(11, 19);
      setNow(`${day} ${mon} ${yr}  ${time} UTC`);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

function HealthPill() {
  const token = useAuth((s) => s.token);
  const { data, isError } = useQuery({
    queryKey: ["health"],
    queryFn: () => api.health(token),
    refetchInterval: 15_000,
    retry: 0,
  });
  const ok = !!data?.ok && !isError;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 px-2 py-[1px] text-[10px]",
        ok ? "text-long" : "text-short",
      )}
    >
      <span
        className={cn(
          "h-[6px] w-[6px] rounded-full",
          ok ? "bg-long animate-pulse-slow" : "bg-short",
        )}
      />
      API {data?.version ?? "OFFLINE"}
    </span>
  );
}

function OpenBBPill() {
  const token = useAuth((s) => s.token);
  const { data, isError } = useQuery({
    queryKey: ["openbb", "health"],
    queryFn: () => api.openbbHealth(token),
    enabled: !!token,
    refetchInterval: 30000,
    retry: 0,
  });
  const ok = !!data?.ok && !isError;
  const warning = ok && !!data?.warning;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 px-2 py-[1px] text-[10px]",
        ok ? (warning ? "text-warn" : "text-long") : "text-short",
      )}
      title={data?.url ?? "OpenBB health"}
    >
      <span
        className={cn(
          "h-[6px] w-[6px] rounded-full",
          ok ? (warning ? "bg-warn" : "bg-long animate-pulse-slow") : "bg-short",
        )}
      />
      OBB {ok ? `${data?.latency_ms ?? 0}MS` : "OFFLINE"}
    </span>
  );
}

function WsPill() {
  const { status } = useFinceptStream({ topics: ["alerts"] });
  const ok = status === "open";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 px-2 py-[1px] text-[10px]",
        ok ? "text-long" : "text-warn",
      )}
    >
      <span
        className={cn(
          "h-[6px] w-[6px] rounded-full",
          ok ? "bg-long animate-pulse-slow" : "bg-warn",
        )}
      />
      WS {status.toUpperCase()}
    </span>
  );
}

function KillSwitchButton() {
  const token = useAuth((s) => s.token);
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [result, setResult] = useState<
    | { kind: "idle" }
    | { kind: "success"; alertId: string }
    | { kind: "error"; message: string }
  >({ kind: "idle" });

  const mutate = useMutation({
    mutationFn: (payloadReason: string) =>
      api.tripKillSwitch(token, payloadReason || "manual"),
    onSuccess: (data) => {
      setResult({ kind: "success", alertId: data.alert_id });
    },
    onError: (err: unknown) => {
      const msg =
        err instanceof ApiError
          ? `API ${err.status}: ${String(err.body ?? err.message)}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      setResult({ kind: "error", message: msg });
    },
  });

  const handleOpenChange = (next: boolean) => {
    setOpen(next);
    if (!next) {
      setReason("");
      setResult({ kind: "idle" });
    }
  };

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1 bg-destructive/10 px-2 py-[2px] text-[10px] font-semibold uppercase tracking-wider text-destructive hover:bg-destructive/20"
      >
        <Power className="h-3 w-3" />
        Kill
      </button>
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-destructive">
              <AlertTriangle className="h-5 w-5" />
              Trip Kill Switch
            </DialogTitle>
            <DialogDescription>
              Publishes a <code>kill_switch_engaged</code> alert on the
              events bus.  OMS and strategy-host consumers halt new
              orders and cancel open ones.  Paper-mode only until
              Gate 5 lifts.
            </DialogDescription>
          </DialogHeader>
          {result.kind === "idle" ? (
            <div className="flex flex-col gap-3">
              <label className="text-xs uppercase tracking-wider text-muted-foreground">
                Reason
                <input
                  type="text"
                  value={reason}
                  autoFocus
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="manual drill"
                  className="mt-1 w-full border border-border bg-background px-2 py-1 text-sm"
                />
              </label>
              <div className="flex justify-end gap-2">
                <button
                  onClick={() => handleOpenChange(false)}
                  className="border border-border px-3 py-1 text-xs uppercase tracking-wider hover:bg-accent"
                >
                  Cancel
                </button>
                <button
                  onClick={() => mutate.mutate(reason)}
                  disabled={mutate.isPending}
                  className="bg-destructive px-3 py-1 text-xs font-semibold uppercase tracking-wider text-destructive-foreground hover:bg-destructive/90 disabled:opacity-60"
                >
                  {mutate.isPending ? "Tripping…" : "Trip Kill Switch"}
                </button>
              </div>
            </div>
          ) : result.kind === "success" ? (
            <div className="space-y-2 text-sm">
              <div className="text-long">● Kill switch engaged.</div>
              <div className="text-xs text-muted-foreground">
                Alert ID:{" "}
                <code className="text-foreground">{result.alertId}</code>
              </div>
              <div className="flex justify-end">
                <button
                  onClick={() => handleOpenChange(false)}
                  className="border border-border px-3 py-1 text-xs uppercase tracking-wider hover:bg-accent"
                >
                  Close
                </button>
              </div>
            </div>
          ) : (
            <div className="space-y-2 text-sm">
              <div className="text-short">● Failed to trip kill switch.</div>
              <div className="text-xs text-muted-foreground">
                {result.message}
              </div>
              <div className="flex justify-end gap-2">
                <button
                  onClick={() => setResult({ kind: "idle" })}
                  className="border border-border px-3 py-1 text-xs uppercase tracking-wider hover:bg-accent"
                >
                  Retry
                </button>
                <button
                  onClick={() => handleOpenChange(false)}
                  className="border border-border px-3 py-1 text-xs uppercase tracking-wider hover:bg-accent"
                >
                  Close
                </button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

export function TitleBar() {
  const router = useRouter();
  const setToken = useAuth((s) => s.setToken);
  const now = useNow();

  const handleLogout = () => {
    setToken(null);
    router.push("/login");
  };

  return (
    <header className="flex h-7 shrink-0 items-center justify-between gap-3 border-b border-border bg-card px-2 text-[11px]">
      {/* Left — branding */}
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2">
          <span className="live-dot" />
          <span className="font-semibold tracking-wider text-foreground">
            FINCEPT TERMINAL
          </span>
        </div>
        <span className="text-border">│</span>
        <span className="text-muted-foreground">
          PROFESSIONAL RESEARCH DESK
        </span>
        <span className="text-border">│</span>
        <span className="text-warn tracking-wider">● PAPER</span>
      </div>

      {/* Center — clock */}
      <div className="hidden text-muted-foreground md:block">{now}</div>

      {/* Right — status + controls */}
      <div className="flex items-center gap-2">
        <span className="bg-warn/15 px-2 py-[1px] text-[10px] font-semibold uppercase tracking-wider text-warn">
          Paper
        </span>
        <span className="text-border">│</span>
        <HealthPill />
        <OpenBBPill />
        <WsPill />
        <span className="text-border">│</span>
        <KillSwitchButton />
        <button
          onClick={handleLogout}
          className="inline-flex items-center gap-1 px-2 py-[2px] text-[10px] font-semibold uppercase tracking-wider text-muted-foreground hover:text-foreground"
          aria-label="Sign out"
        >
          <LogOut className="h-3 w-3" />
          Logout
        </button>
      </div>
    </header>
  );
}
