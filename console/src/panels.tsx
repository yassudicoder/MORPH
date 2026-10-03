import { useEffect, useState } from "react";
import type { Decision, Escalation, Scenario, Snapshot } from "./types";
import { AGENT_COLORS, fmtT } from "./types";

type Send = (m: object) => void;

const pct = (v: number) => `${Math.round(v * 100)}%`;

export function GatePill({ g }: { g: string }) {
  return <span className={`pill gate-${g.toLowerCase()}`}>{g}</span>;
}

/* ---------------------------------------------------------------- mission */
export function MissionPanel({ sc, snap, send }: { sc: Scenario; snap: Snapshot | null; send: Send }) {
  const [text, setText] = useState(sc.default_objective);
  const phase = snap?.brain.phase ?? "IDLE";
  const busy = snap?.busy;
  const running = phase === "RUNNING";
  const waiting = phase === "AWAIT_APPROVAL";
  const comp = snap?.brain.compile;
  const spec = snap?.brain.spec;
  useEffect(() => {
    if (running) setText("");
  }, [running]);
  return (
    <section className="panel">
      <header className="panel-head">
        <h2>Mission</h2>
        <span className={`phase phase-${phase.toLowerCase()}`}>{phase.replace("_", " ")}</span>
      </header>
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={running ? 2 : 5}
        placeholder={running ? "Change the goal mid-mission, e.g. 'Villagers report someone trapped near the school. Check it.'" : "Type the objective in plain English"} />
      <div className="row">
        <button className="btn primary" disabled={!!busy || waiting || !text.trim()}
          onClick={() => send({ type: "mission", text })}>
          {busy ? (busy === "compiling" ? "Compiling…" : "Re-compiling…") : running ? "Change goal (L3)" : phase === "DONE" ? "New mission" : "Compile & plan"}
        </button>
        {busy && <span className="muted small">{snap?.llm.kind === "rules" ? "rule-based compiler" : `${snap?.llm.model} is thinking (local, free)`}</span>}
      </div>
      {comp && (
        <div className="compile">
          <div className="small">
            Compiled by <b>{comp.source === "llm" ? `LLM · ${comp.model}` : comp.source === "rules" ? "rule-based compiler" : "nobody (rejected)"}</b>
            {" "}in {comp.attempts.length} attempt{comp.attempts.length === 1 ? "" : "s"}
          </div>
          <ol className="attempts">
            {comp.attempts.map((a: any) => (
              <li key={a.attempt} className={a.errors.length ? "bad" : "good"}>
                <span className="mono">{a.source}</span> {a.errors.length ? `rejected: ${a.errors[0]}` : "valid"}
              </li>
            ))}
          </ol>
          {comp.notes?.length > 0 && <div className="small muted">auto-fixed: {comp.notes.join("; ")}</div>}
        </div>
      )}
      {spec && (
        <div className="spec">
          <div className="small muted">deadline {spec.deadline_min} min · reserve {spec.reserve_battery_pct}%</div>
          <div className="chips">
            {spec.objectives.map((o: any, i: number) => (
              <span key={i} className={`chip prio-${o.priority.toLowerCase()}`}>
                {o.kind.replace(/_/g, " ").toLowerCase()}
                {o.areas?.length ? ` ${o.areas.join("")}` : ""}
                {o.place ? ` → ${o.place.toLowerCase()}` : ""}
                {o.payload_kg ? ` ${o.payload_kg} kg` : ""}
                {o.targets?.length ? ` ${o.targets.join(", ")}` : ""}
              </span>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

/* ----------------------------------------------------------------- robots */
const TIER: Record<string, [string, string]> = {
  C: ["C", "connected to base"], P: ["P", "partitioned: peers only"], I: ["I", "isolated"], X: ["X", "down"],
};

export function RobotsPanel({ sc, snap, selected, onSelect }: {
  sc: Scenario; snap: Snapshot | null; selected: string | null; onSelect: (id: string | null) => void;
}) {
  const reserve = snap?.brain.policy.reserve ?? 15;
  return (
    <section className="panel grow">
      <header className="panel-head"><h2>Robots</h2><span className="muted small">belief · radio tier</span></header>
      <div className="robots">
        {sc.fleet.map((f) => {
          const a = snap?.brain.belief.agents[f.id];
          const r = snap?.robots[f.id];
          const tier = snap?.comms.tier[f.id] ?? "C";
          const assigned = snap?.brain.assigned_version[f.id] ?? 0;
          const batt = a?.battery ?? 100;
          const l0 = r?.branch?.startsWith("L0");
          return (
            <div key={f.id} className={`robot ${selected === f.id ? "sel" : ""} ${a && !a.alive ? "dead" : ""}`}
              onClick={() => onSelect(selected === f.id ? null : f.id)}>
              <div className="robot-top">
                <span className="swatch" style={{ background: AGENT_COLORS[f.id] }} />
                <b>{f.id}</b>
                <span className={`tier tier-${tier}`} title={TIER[tier]?.[1]}>{TIER[tier]?.[0]}</span>
                <span className="muted small grow-r">{f.kind === "ugv" ? "rover" : f.kind}</span>
              </div>
              <div className="batt" title={`battery ${batt.toFixed(0)}% (reserve ${reserve}%)`}>
                <div className={`batt-fill ${batt < reserve ? "low" : ""}`} style={{ width: `${Math.max(0, batt)}%` }} />
                <div className="batt-reserve" style={{ left: `${reserve}%` }} />
                <span className="batt-text">{batt.toFixed(0)}%</span>
              </div>
              <div className="small robot-status">
                {a && !a.alive ? <span className="red">LOST · {a.lost_reason}</span> :
                  a?.expected_dark ? <span className="warn">out of contact · {a.age_heard.toFixed(0)} s (radio shadow)</span> :
                    <>{l0 && <span className="pill l0">{r?.branch}</span>} {a?.status || "—"}</>}
              </div>
              <div className="small muted robot-meta">
                {f.kits > 0 && <span>kits {a?.kits ?? 0}/{f.kits}</span>}
                {f.kg > 0 && <span>{a?.kg ?? 0}/{f.kg} kg</span>}
                <span>plan v{a?.version_acked ?? 0}{assigned > (a?.version_acked ?? 0) ? ` (v${assigned} pending)` : ""}</span>
                <span>heard {a ? a.age_heard.toFixed(0) : "–"} s ago</span>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

/* ------------------------------------------------------------- escalation */
function OptionRow({ o, recommended }: { o: Decision["options"][number]; recommended: boolean }) {
  return (
    <div className={`option ${recommended ? "rec" : ""}`}>
      <div className="option-head">
        <b>{o.id}</b> <span>{o.label}</span>
        {recommended && <span className="pill rec-pill">recommended</span>}
      </div>
      <div className="metrics small">
        <span title="probability every committed task finishes in time, over 200 simulated runs">P(success) <b>{pct(o.risk.p_success)}</b></span>
        <span>P(LIFE on time) <b>{pct(o.risk.p_life)}</b></span>
        <span title="5th-percentile battery when drones get home">worst return batt <b>{o.risk.p5_return_batt.toFixed(0)}%</b></span>
        <span>score <b>{o.score.toFixed(2)}</b></span>
      </div>
      {o.changes.length > 0 && <ul className="changes small">{o.changes.slice(0, 5).map((c, i) => <li key={i}>{c}</li>)}</ul>}
    </div>
  );
}

export function EscalationPanel({ snap, send }: { snap: Snapshot | null; send: Send }) {
  const list = snap?.brain.escalations ?? [];
  return (
    <section className={`panel ${list.length ? "alert" : ""}`}>
      <header className="panel-head">
        <h2>Escalation inbox</h2>
        <span className="muted small">{list.length ? `${list.length} waiting` : "nothing needs you"}</span>
      </header>
      {list.map((e: Escalation) => {
        const d = e.decision;
        return (
          <div key={e.id} className="esc">
            <div className="esc-title">
              <GatePill g={d.gate.result} /> <span className="mono small">{d.id} · {d.level} · T+{fmtT(d.t)}</span>
              {e.seconds_left !== null && <span className="timer">{e.seconds_left}s</span>}
            </div>
            <p className="esc-summary">{d.trigger.summary}</p>
            <ul className="why small">{d.gate.why.map((w, i) => <li key={i}>{w}</li>)}</ul>
            {d.options.map((o) => <OptionRow key={o.id} o={o} recommended={o.id === e.recommended} />)}
            <div className="row wrap">
              {d.options.map((o) => (
                <button key={o.id} className={`btn ${o.id === e.recommended ? "primary" : ""}`}
                  onClick={() => send({ type: "answer", id: e.id, option: o.id })}>
                  {e.kind === "first" ? `Approve plan ${o.id}` : `Choose ${o.id}`}
                </button>
              ))}
              {e.extra.map((x) => (
                <button key={x.id} className="btn ghost" onClick={() => send({ type: "answer", id: e.id, option: x.id })}>{x.label}</button>
              ))}
            </div>
            {e.seconds_left !== null && (
              <div className="small muted">
                {e.default ? `If nobody answers, option ${e.default} runs (it can be undone).` : "If nobody answers, robots hold safe (this change cannot be undone)."}
              </div>
            )}
          </div>
        );
      })}
    </section>
  );
}

/* ---------------------------------------------------------- decision feed */
export function DecisionFeed({ snap }: { snap: Snapshot | null }) {
  const [open, setOpen] = useState<string | null>(null);
  const list = snap?.brain.decisions ?? [];
  return (
    <section className="panel grow scroll">
      <header className="panel-head"><h2>Decision feed</h2><span className="muted small">why each change happened</span></header>
      {list.length === 0 && <p className="muted small">Decisions appear here with a plain-English reason.</p>}
      {list.map((d) => (
        <div key={d.id} className={`dec ${d.status === "superseded" ? "superseded" : ""}`} onClick={() => setOpen(open === d.id ? null : d.id)}>
          <div className="dec-head">
            <span className="mono small">T+{fmtT(d.t)}</span>
            <span className={`pill lvl lvl-${d.level.replace("--", "x").toLowerCase()}`}>{d.level === "--" ? "note" : d.level}</span>
            <GatePill g={d.gate.result} />
            {d.by && d.by !== "auto" && <span className="muted small">by {d.by}</span>}
            {d.status === "pending" && <span className="pill gate-ask">waiting</span>}
          </div>
          <div className="dec-text">{d.explain_llm || d.explain}</div>
          {open === d.id && d.options.length > 0 && (
            <div className="dec-detail">
              {d.gate.why.length > 0 && <div className="small muted">gate: {d.gate.why.join("; ")}</div>}
              {d.options.map((o) => <OptionRow key={o.id} o={o} recommended={o.id === (d.chosen ?? d.default)} />)}
            </div>
          )}
        </div>
      ))}
    </section>
  );
}

/* ------------------------------------------------------------ disruptions */
export function DisruptionPanel({ sc, snap, send }: { sc: Scenario; snap: Snapshot | null; send: Send }) {
  const running = snap?.brain.phase === "RUNNING";
  return (
    <section className="panel">
      <header className="panel-head"><h2>Disruption panel</h2><span className="muted small">changes reality, not TOWER's belief</span></header>
      <div className="presets">
        {sc.presets.map((p, i) => (
          <button key={i} className="btn small-btn" disabled={!running}
            onClick={() => send({ type: "inject", kind: p.kind, args: p.args, label: p.label })}>{p.label}</button>
        ))}
      </div>
      {snap && snap.timeline.length > 0 && (
        <div className="small muted">scripted next: {snap.timeline.slice(0, 3).map((e) => `T+${fmtT(e.at)} ${e.label}`).join(" · ")}</div>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ tasks */
export function TasksPanel({ snap }: { snap: Snapshot | null }) {
  const tasks = snap?.brain.tasks ?? [];
  return (
    <div className="table-wrap">
      <table className="tbl">
        <thead><tr><th>id</th><th>task</th><th>prio</th><th>status</th><th>robot</th><th className="num">ETA</th><th className="num">due</th></tr></thead>
        <tbody>
          {tasks.map((t) => (
            <tr key={t.id} className={["DONE", "CANCELLED", "DISCARDED"].includes(t.status) ? "dim" : ""} title={t.status === "PROPOSED" ? "waiting for the operator to approve the goal change" : undefined}>
              <td className="mono">{t.id}</td>
              <td>{t.label}{t.type === "SURVEY" && t.status === "ACTIVE" ? ` · ${Math.round(t.progress * 100)}%` : ""}</td>
              <td><span className={`chip prio-${t.priority.toLowerCase()}`}>{t.priority}</span></td>
              <td className={`st st-${t.status.toLowerCase()}`}>{t.status}</td>
              <td style={{ color: t.assignee ? AGENT_COLORS[t.assignee] : undefined }}>{t.assignee ?? "—"}</td>
              <td className="num mono">{t.status === "DONE" ? "done" : `T+${fmtT(t.eta)}`}</td>
              <td className="num mono">T+{fmtT(t.due)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function EventsPanel({ snap }: { snap: Snapshot | null }) {
  const ev = [...(snap?.brain.events ?? [])].reverse();
  return (
    <div className="events">
      {ev.map((e, i) => (
        <div key={i} className={`ev ev-${e.level}`}>
          <span className="mono small">T+{fmtT(e.t)}</span> <span className="ev-kind">{e.kind}</span> {e.text}
        </div>
      ))}
    </div>
  );
}

export function RadioPanel({ snap }: { snap: Snapshot | null }) {
  const st = snap?.comms.stats;
  if (!st) return null;
  const total = Object.values(st.bytes).reduce((a, b) => a + b, 0) || 1;
  const naive = (st.suppressed_state + (st.msgs["P3 STATE"] ?? 0)) * 48;
  return (
    <div className="radio">
      <div className="bars">
        {Object.entries(st.bytes).map(([k, v]) => (
          <div key={k} className="bar-row">
            <span className="mono small bar-k">{k}</span>
            <div className="bar-track"><div className="bar-fill" style={{ width: `${(v / total) * 100}%` }} /></div>
            <span className="mono small bar-v">{(v / 1000).toFixed(1)} kB · {st.msgs[k]} msg</span>
          </div>
        ))}
      </div>
      <div className="small">
        Position reports skipped by dead reckoning: <b>{st.suppressed_state}</b> (≈{(st.suppressed_state * 48 / 1000).toFixed(0)} kB of {(naive / 1000).toFixed(0)} kB saved).
        Stale messages replaced in queue: <b>{st.superseded}</b>. Lost with downed robots: <b>{st.lost_with_robot}</b>.
      </div>
      <div className="small muted">
        Queued (store-and-forward): {Object.entries(snap!.comms.queued).filter(([, n]) => n > 0).map(([a, n]) => `${a} ${n}`).join(" · ") || "nothing"}
      </div>
    </div>
  );
}
