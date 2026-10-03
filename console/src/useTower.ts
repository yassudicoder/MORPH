import { useCallback, useEffect, useRef, useState } from "react";
import type { Scenario, Snapshot } from "./types";

// Empty when the backend also serves the console (laptop, Render, VM). Set VITE_TOWER_BACKEND at build
// time when the console lives somewhere else, such as Vercel.
export const BACKEND = (import.meta.env.VITE_TOWER_BACKEND ?? "").replace(/\/$/, "");

function wsUrl() {
  const key = new URLSearchParams(location.search).get("key");
  const base = BACKEND
    ? BACKEND.replace(/^http/, "ws")
    : `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
  return `${base}/ws${key ? `?key=${encodeURIComponent(key)}` : ""}`;
}

// One WebSocket to the backend. Reconnects on its own if the server restarts.
export function useTower() {
  const [scenario, setScenario] = useState<Scenario | null>(null);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [coverage, setCoverage] = useState<Snapshot["coverage"] | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [canControl, setCanControl] = useState(true);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const connect = () => {
      const ws = new WebSocket(wsUrl());
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (alive) timer = window.setTimeout(connect, 1500);
      };
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === "hello") setCanControl(!!msg.control);
        else if (msg.type === "scenario") setScenario(msg.scenario);
        else if (msg.type === "snapshot") {
          if (msg.coverage) setCoverage(msg.coverage);
          setSnap(msg);
        } else if (msg.type === "error") {
          setError(msg.text);
          window.setTimeout(() => setError(null), 5000);
        }
      };
    };
    connect();
    // free hosts put idle services to sleep; a ping every 4 minutes keeps it awake while a tab is open
    const ping = window.setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify({ type: "ping" }));
    }, 240_000);
    return () => {
      alive = false;
      clearInterval(ping);
      if (timer) clearTimeout(timer);
      wsRef.current?.close();
    };
  }, []);

  const send = useCallback((msg: object) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  }, []);

  return { scenario, snap, coverage, connected, error, send, canControl };
}
