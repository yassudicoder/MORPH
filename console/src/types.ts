// Shapes of the messages the Python backend sends. Kept loose where the backend is the source of truth.
export type XY = [number, number];

export interface Scenario {
  name: string;
  title: string;
  width: number;
  height: number;
  base: XY;
  sectors: Record<string, [number, number, number, number]>;
  places: Record<string, XY>;
  no_fly: { id: string; why: string; poly: XY[] }[];
  river: XY[];
  ridge: { center: XY; rx: number; ry: number; height_m: number };
  rally_points: Record<string, XY>;
  roads: { nodes: Record<string, XY>; edges: [string, string, string][] };
  fleet: { id: string; kind: string; caps: string[]; speed: number; endurance_s: number; kits: number; kg: number; label: string }[];
  presets: { kind: string; args: Record<string, unknown>; label: string }[];
  default_objective: string;
}

export interface Risk {
  p_success: number;
  p_life: number;
  p5_return_batt: number;
  min_batt_p5?: number;
  score: number;
  runs: number;
}

export interface Option {
  id: string;
  label: string;
  strategy: string;
  risk: Risk;
  score: number;
  dropped: string[];
  changes: string[];
}

export interface Decision {
  id: string;
  t: number;
  level: string;
  trigger: { type: string; summary?: string; events?: any[] };
  options: Option[];
  gate: { result: "AUTO" | "ASK" | "HOLD"; why: string[]; reversible?: boolean; gap?: number | null };
  default: string | null;
  chosen: string | null;
  by: string | null;
  status: string;
  explain: string;
  explain_llm?: string;
  compile?: { source: string; model: string; attempts: { attempt: number; source: string; errors: string[] }[]; notes: string[] };
}

export interface Escalation {
  id: string;
  kind: string;
  decision: Decision;
  default: string | null;
  recommended: string | null;
  extra: { id: string; label: string }[];
  seconds_left: number | null;
}

export interface TaskT {
  id: string;
  type: string;
  priority: "LIFE" | "HIGH" | "NORMAL";
  pos: XY;
  area: string | null;
  place: string | null;
  kits: number;
  kg: number;
  due: number | null;
  status: string;
  assignee: string | null;
  progress: number;
  eta: number | null;
  label: string;
  origin: string;
}

export interface AgentBelief {
  pos: XY;
  vel: XY;
  battery: number;
  kits: number;
  kg: number;
  airborne: boolean;
  mode: string;
  status: string;
  stop: string | null;
  version_acked: number;
  age_state: number;
  age_heard: number;
  alive: boolean;
  lost_reason: string;
  expected_dark: boolean;
}

export interface Detection {
  id: string;
  pos: XY;
  count: number;
  conf: number;
  by: string;
  status: string;
  t: number;
}

export interface Snapshot {
  type: "snapshot";
  t: number;
  speed: number;
  paused: boolean;
  busy: string | null;
  brain: {
    phase: string;
    version: number;
    t: number;
    spec: any;
    compile: any;
    policy: { deliver_on_detect: string | null; keep_link: string[]; reserve: number; deadline_s: number };
    tasks: TaskT[];
    routes: Record<string, XY[]>;
    dispatched: Record<string, string[]>;
    assigned_version: Record<string, number>;
    escalations: Escalation[];
    decisions: Decision[];
    events: { t: number; kind: string; text: string; level: string }[];
    belief: { agents: Record<string, AgentBelief>; blocked: [string, string][]; wind: number; detections: Detection[] };
    relay_points: XY[];
  };
  truth: {
    agents: Record<string, { pos: XY; battery: number; alive: boolean; airborne: boolean; kits: number; kg: number }>;
    survivors: { id: string; pos: XY; count: number; served: number }[];
    decoys: { id: string; pos: XY; what: string }[];
    debris: { pos: XY; edge: [string, string] | null }[];
    wind: number;
    survey: Record<string, number>;
  };
  comms: {
    links: [string, string, number][];
    tier: Record<string, string>;
    queued: Record<string, number>;
    blackouts: { center: XY; radius: number }[];
    stats: { bytes: Record<string, number>; msgs: Record<string, number>; superseded: number; suppressed_state: number; lost_with_robot: number };
  };
  robots: Record<string, { branch: string; status: string; version: number; stop: string | null; cutoff: boolean; stops: string[] }>;
  llm: { provider: string; kind: string; model: string; available: boolean; detail: string; cost: string };
  coverage_version: number;
  coverage?: { cell: number; nx: number; ny: number; rssi: number[] };
  timeline: { at: number; label: string }[];
  fired: { t: number; kind: string; msg: string }[];
}

export const AGENT_COLORS: Record<string, string> = {
  "SCOUT-1": "#7aa2ff",
  "SCOUT-2": "#55d0e6",
  "LIFT-1": "#c79bf2",
  "RELAY-1": "#f2b84b",
  "ROVER-1": "#8bd17c",
  "ROVER-2": "#e8a07a",
};

export const fmtT = (s: number | null | undefined) => {
  if (s === null || s === undefined) return "—";
  const v = Math.max(0, Math.floor(s));
  return `${String(Math.floor(v / 60)).padStart(2, "0")}:${String(v % 60).padStart(2, "0")}`;
};
