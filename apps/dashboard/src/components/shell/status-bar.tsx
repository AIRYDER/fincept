"use client";

/**
 * Bloomberg-terminal bottom status bar.
 *
 * Single-line sub-pixel strip at the bottom of every authenticated
 * page.  Mirrors the look-and-feel of the reference screenshots:
 * version  ·  asset classes  ·  session timer  ·  layout  ·
 * feeds  ·  memory  ·  latency  ·  READY/ALERTS.
 */

import { useEffect, useState } from "react";

import { useFinceptStream } from "@/lib/ws";
import { cn } from "@/lib/utils";

function formatDuration(sec: number) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
}

export function StatusBar() {
  // Session timer — since app mount.
  const [since] = useState(() => Date.now());
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const id = setInterval(() => {
      setElapsed(Math.floor((Date.now() - since) / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, [since]);

  // Feed / latency reads WS health.
  const { status } = useFinceptStream({ topics: ["alerts"] });
  const feedsOk = status === "open";

  // JS heap size (MB) if available.
  const [memMB, setMemMB] = useState<number | null>(null);
  useEffect(() => {
    const id = setInterval(() => {
      const perf = (
        window.performance as unknown as {
          memory?: { usedJSHeapSize: number };
        }
      ).memory;
      if (perf?.usedJSHeapSize !== undefined) {
        setMemMB(Math.round(perf.usedJSHeapSize / 1024 / 1024));
      }
    }, 2000);
    return () => clearInterval(id);
  }, []);

  return (
    <footer className="flex h-6 shrink-0 items-center gap-2 border-t border-border bg-card px-2 text-[10px] uppercase tracking-wider text-muted-foreground">
      <span className="text-foreground">v0.1.0</span>
      <span className="text-border">│</span>
      <span className="text-muted-foreground">EQ · FX · CM · FI · CR</span>
      <span className="text-border">│</span>
      <span>
        SESSION <span className="text-foreground">{formatDuration(elapsed)}</span>
      </span>
      <span className="text-border">│</span>
      <span>
        LAYOUT <span className="text-foreground">DEFAULT</span>
      </span>
      <span className="text-border">│</span>
      <span>
        FEEDS{" "}
        <span className={cn(feedsOk ? "text-long" : "text-short")}>
          {feedsOk ? "CONNECTED" : "DISCONNECTED"}
        </span>
      </span>

      <span className="flex-1" />

      {memMB !== null && (
        <>
          <span>
            MEM{" "}
            <span className={memMB < 200 ? "text-long" : "text-warn"}>
              {memMB}MB
            </span>
          </span>
          <span className="text-border">│</span>
        </>
      )}
      <span>
        LAT{" "}
        <span className={feedsOk ? "text-long" : "text-warn"}>
          {feedsOk ? "< 100ms" : "—"}
        </span>
      </span>
      <span className="text-border">│</span>
      <span className={feedsOk ? "text-long" : "text-warn"}>
        {feedsOk ? "● READY" : "● STANDBY"}
      </span>
      <span className="text-border">│</span>
      <span className="text-muted-foreground">ALERTS 0</span>
    </footer>
  );
}
