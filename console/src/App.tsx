import { useEffect, useState } from "react";
import MapView from "./MapView";
import {
  DecisionFeed, DisruptionPanel, EscalationPanel, EventsPanel, MissionPanel, RadioPanel, RobotsPanel, TasksPanel,
} from "./panels";
import { fmtT } from "./types";
import type { Snapshot } from "./types";
import { BACKEND, useTower } from "./useTower";

const SPEEDS = [1, 3, 10];

function Chain({ snap }: { snap: Snapshot | null }) {
  // The decision chain from the architecture diagram, lit with the latest decision's numbers.
  const d = snap?.brain.decisions.find((x) => x.options.length > 0);
  const comp = snap?.brain.compile;
  const best = d?.options[0];
  const steps: [string, string][] = [
    ["Compiler", comp ? (comp.source === "llm" ? comp.model : comp.source) : "—"],
    ["Validator", comp ? `${comp.attempts.length} attempt${comp.attempts.length === 1 ? "" : "s"}` : "—"],
    ["Planner", d ? `${d.options.length} option${d.options.length === 1 ? "" : "s"}` : "—"],
    ["Risk", best ? `P ${Math.round(best.risk.p_success * 100)}% · ${best.risk.runs} runs` : "—"],
    ["Gate", d ? d.gate.result : "—"],
    ["Dispatcher", snap ? `plan v${snap.brain.version}` : "—"],
  ];
  return (
    <div className="chain">
      {steps.map(([k, v], i) => (
        <div key={k} className="chain-step">
          <span className="chain-n">{i + 1}</span>
          <span className="chain-k">{k}</span>
          <span className={`chain-v ${k === "Gate" ? `gate-txt-${v.toLowerCase()}` : ""}`}>{v}</span>
        </div>
      ))}
    </div>
  );
}

function Report({ onClose }: { onClose: () => void }) {
  const [rep, setRep] = useState<any>(null);
  useEffect(() => {
    fetch(`${BACKEND}/api/report`).then((r) => r.json()).then(setRep);
  }, []);
  const download = () => {
    const blob = new Blob([JSON.stringify(rep, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "tower-after-action-report.json";
    a.click();
  };
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <header className="panel-head"><h2>After-action report</h2><button className="btn ghost keep" onClick={onClose}>Close</button></header>
        {!rep ? <p>Loading…</p> : (
          <>
            <div className="kpis">
              <div><span>mission time</span><b>{fmtT(rep.duration_s)}</b></div>
              <div><span>sectors surveyed</span><b>{rep.surveyed.length}/6</b></div>
              <div><span>kits delivered</span><b>{rep.kits_delivered}</b></div>
              <div><span>supplies</span><b>{rep.supplies_kg} kg</b></div>
              <div><span>survivors served (truth)</span><b>{rep.truth?.survivors.reduce((a: number, s: any) => a + s.served, 0)}/{rep.truth?.survivors.reduce((a: number, s: any) => a + s.count, 0)}</b></div>
              <div><span>robots lost</span><b>{rep.truth?.robots_lost.length ?? 0}</b></div>
              <div><span>decisions</span><b>{rep.decisions.length}</b></div>
              <div><span>LLM cost</span><b>₹0</b></div>
            </div>
            <div className="table-wrap tall">
              <table className="tbl">
                <thead><tr><th>id</th><th>T+</th><th>level</th><th>gate</th><th>by</th><th>what and why</th></tr></thead>
                <tbody>
                  {rep.decisions.map((d: any) => (
                    <tr key={d.id}><td className="mono">{d.id}</td><td className="mono">{d.t}</td><td>{d.level}</td><td>{d.gate}</td><td>{d.by ?? ""}</td><td>{d.explain}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
            <button className="btn primary keep" onClick={download}>Download JSON</button>
          </>
        )}
      </div>
    </div>
  );
}

export default function App() {
  const { scenario, snap, coverage, connected, error, send, canControl } = useTower();
  const [layers, setLayers] = useState({ truth: false, coverage: true, links: false, routes: true });
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<"tasks" | "events" | "radio">("tasks");
  const [report, setReport] = useState(false);

  if (!scenario) {
    return <div className="loading">{connected ? "Loading scenario…" : "Connecting to the TOWER ground station… (a sleeping free server takes about a minute to wake up)"}</div>;
  }
  const b = snap?.brain;
  const llm = snap?.llm;
  return (
    <div className={`app ${canControl ? "" : "readonly"}`}>
      <header className="top">
        <div className="brand">
          <span className="wordmark">TOWER</span>
          <span className="muted small">{scenario.title}</span>
        </div>
        <div className="clock mono">T+{fmtT(snap?.t ?? 0)}</div>
        <div className="row">
          <button className="btn" onClick={() => send({ type: "control", action: snap?.paused ? "resume" : "pause" })}>
            {snap?.paused ? "Resume" : "Pause"}
          </button>
          {SPEEDS.map((s) => (
            <button key={s} className={`btn ${snap?.speed === s ? "on" : ""}`} onClick={() => send({ type: "control", action: "speed", value: s })}>{s}×</button>
          ))}
        </div>
        <div className="row">
          <button className="btn ghost" onClick={() => send({ type: "control", action: "reset", scripted: false })}>Reset</button>
          <button className="btn ghost" title="Reset and play the scripted disruptions (debris, wind, villager report, relay failure)"
            onClick={() => send({ type: "control", action: "reset", scripted: true })}>Reset + scripted demo</button>
          <button className="btn ghost keep" onClick={() => setReport(true)}>Report</button>
        </div>
        <div className={`llm ${llm?.available ? "" : "off"}`} title={llm?.detail}>
          <span className="dot" /> LLM: <b>{llm?.provider ?? "…"}</b> · {llm?.model || "—"} · <span className="free">free</span>
        </div>
        {!connected && <span className="pill gate-hold">offline</span>}
        {!canControl && <span className="pill gate-ask" title="Open the link with ?key=<access key> to control the mission">view only</span>}
      </header>
      <Chain snap={snap} />
      {error && <div className="toast">{error}</div>}
      <main className="grid">
        <div className="col left">
          <MissionPanel sc={scenario} snap={snap} send={send} />
          <RobotsPanel sc={scenario} snap={snap} selected={selected} onSelect={setSelected} />
        </div>
        <div className="col mid">
          <section className="panel map-panel">
            <header className="panel-head">
              <h2>Live map</h2>
              <span className="muted small">TOWER's belief · wind {b?.belief.wind ?? "–"} m/s</span>
              <div className="toggles">
                {(["routes", "coverage", "links", "truth"] as const).map((k) => (
                  <label key={k} className="toggle">
                    <input type="checkbox" checked={layers[k]} onChange={() => setLayers({ ...layers, [k]: !layers[k] })} />
                    {k === "truth" ? "ground truth" : k === "coverage" ? "radio coverage" : k === "links" ? "radio links" : "planned routes"}
                  </label>
                ))}
              </div>
            </header>
            <MapView sc={scenario} snap={snap} coverage={coverage} layers={layers} selected={selected} onSelect={setSelected} />
            <div className="legend small muted">
              <span><i className="lg lg-drone" />drone</span><span><i className="lg lg-rover" />rover</span>
              <span><i className="lg lg-det" />survivors found</span><span><i className="lg lg-relay" />relay point</span>
              <span><i className="lg lg-blocked" />blocked road</span><span><i className="lg lg-shadow" />no radio coverage</span>
              <span>"?" = out of contact</span>
            </div>
          </section>
          <section className="panel bottom">
            <header className="panel-head">
              <div className="tabs">
                {(["tasks", "events", "radio"] as const).map((t) => (
                  <button key={t} className={`tab ${tab === t ? "on" : ""}`} onClick={() => setTab(t)}>
                    {t === "tasks" ? `Tasks (${b?.tasks.filter((x) => x.status === "PENDING" || x.status === "ACTIVE").length ?? 0} open)` : t === "events" ? "Event log" : "Radio"}
                  </button>
                ))}
              </div>
            </header>
            {tab === "tasks" && <TasksPanel snap={snap} />}
            {tab === "events" && <EventsPanel snap={snap} />}
            {tab === "radio" && <RadioPanel snap={snap} />}
          </section>
        </div>
        <div className="col right">
          <EscalationPanel snap={snap} send={send} />
          <DecisionFeed snap={snap} />
          <DisruptionPanel sc={scenario} snap={snap} send={send} />
        </div>
      </main>
      {report && <Report onClose={() => setReport(false)} />}
    </div>
  );
}
