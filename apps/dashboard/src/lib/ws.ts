/**
 * WebSocket hook for /ws/stream.
 *
 * The server expects:
 *   - Auth via ``?token=`` query string (browsers can't send custom
 *     headers on WS upgrade) or Authorization header.
 *   - First text frame after connect: ``{"topics": [...]}``.
 *
 * The hook auto-reconnects with exponential backoff and re-sends the
 * subscription on every connect.  It exposes:
 *   - `lastFrame`: the most recent message (typed)
 *   - `status`: 'connecting' | 'open' | 'closed'
 *   - `frames`: a bounded ring buffer of recent frames
 */

"use client";

import { useEffect, useRef, useState } from "react";

import { API_URL } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { WsFrame } from "@/lib/types";

const WS_URL =
  process.env.NEXT_PUBLIC_WS_URL ??
  API_URL.replace(/^http/, "ws").replace(/^https/, "wss");

const MAX_FRAMES = 200;

export type WsStatus = "connecting" | "open" | "closed";

export interface UseWsOptions {
  topics: Array<"positions" | "fills" | "predictions" | "alerts">;
  /** Optional callback invoked synchronously for every frame received. */
  onFrame?: (frame: WsFrame) => void;
  /** Disable connection (useful before auth hydrates). */
  enabled?: boolean;
}

export function useFinceptStream({
  topics,
  onFrame,
  enabled = true,
}: UseWsOptions) {
  const token = useAuth((s) => s.token);
  const [status, setStatus] = useState<WsStatus>("closed");
  const [lastFrame, setLastFrame] = useState<WsFrame | null>(null);
  const [frames, setFrames] = useState<WsFrame[]>([]);
  const wsRef = useRef<WebSocket | null>(null);
  const onFrameRef = useRef(onFrame);
  onFrameRef.current = onFrame;

  // Stable string for effect dep; ordering doesn't matter to the server.
  const topicsKey = [...topics].sort().join(",");

  useEffect(() => {
    if (!enabled || !token) return;
    let cancelled = false;
    let attempt = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;

    function connect() {
      if (cancelled) return;
      setStatus("connecting");
      const url = `${WS_URL}/ws/stream?token=${encodeURIComponent(token!)}`;
      const ws = new WebSocket(url);
      wsRef.current = ws;
      ws.onopen = () => {
        attempt = 0;
        setStatus("open");
        ws.send(JSON.stringify({ topics: topicsKey.split(",") }));
      };
      ws.onmessage = (ev) => {
        try {
          const frame = JSON.parse(ev.data) as WsFrame;
          setLastFrame(frame);
          setFrames((prev) => {
            const next = [frame, ...prev];
            if (next.length > MAX_FRAMES) next.length = MAX_FRAMES;
            return next;
          });
          onFrameRef.current?.(frame);
        } catch {
          // Drop malformed frames silently - they'd just spam logs.
        }
      };
      ws.onerror = () => {
        // The 'close' handler runs after error; let it manage reconnection.
      };
      ws.onclose = () => {
        setStatus("closed");
        wsRef.current = null;
        if (cancelled) return;
        attempt += 1;
        const backoff = Math.min(1000 * 2 ** Math.min(attempt, 5), 15000);
        timer = setTimeout(connect, backoff);
      };
    }

    connect();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      wsRef.current?.close();
    };
  }, [enabled, token, topicsKey]);

  return { status, lastFrame, frames };
}
