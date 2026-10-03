import { memo, useMemo } from "react";
import type { Scenario, Snapshot, XY } from "./types";
import { AGENT_COLORS } from "./types";

interface Props {
  sc: Scenario;
  snap: Snapshot | null;
  coverage: Snapshot["coverage"] | null;
  layers: { truth: boolean; coverage: boolean; links: boolean; routes: boolean };
  selected: string | null;
  onSelect: (id: string | null) => void;
}

function rssiColor(r: number) {
  if (r >= -78) return "var(--cov-strong)";
  if (r >= -85) return "var(--cov-ok)";
  if (r >= -90) return "var(--cov-weak)";
  return "var(--cov-none)";
}

const CoverageLayer = memo(function CoverageLayer({ cov, H }: { cov: NonNullable<Snapshot["coverage"]>; H: number }) {
  const cells = [];
  for (let j = 0; j < cov.ny; j++) {
    for (let i = 0; i < cov.nx; i++) {
      const r = cov.rssi[j * cov.nx + i];
      cells.push(
        <rect key={`${i}-${j}`} x={i * cov.cell} y={H - (j + 1) * cov.cell} width={cov.cell + 0.5} height={cov.cell + 0.5}
          fill={rssiColor(r)} />,
      );
    }
  }
  return <g className="coverage">{cells}</g>;
});

const StaticLayer = memo(function StaticLayer({ sc }: { sc: Scenario }) {
  const H = sc.height;
  const P = (p: XY) => `${p[0]},${H - p[1]}`;
  const r = sc.ridge;
  return (
    <g>
      <defs>
        <pattern id="nf" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="8" stroke="var(--red)" strokeWidth="1.2" opacity="0.6" />
        </pattern>
      </defs>
      {Object.entries(sc.sectors).map(([id, [x0, y0, x1, y1]]) => (
        <g key={id}>
          <rect x={x0} y={H - y1} width={x1 - x0} height={y1 - y0} className="sector" />
          <text x={x0 + 10} y={H - y1 + 30} className="sector-label">{id}</text>
        </g>
      ))}
      {sc.no_fly.map((z) => (
        <g key={z.id}>
          <polygon points={z.poly.map(P).join(" ")} fill="url(#nf)" stroke="var(--red)" strokeDasharray="4 3" />
          <text x={z.poly[0][0] + 4} y={H - z.poly[0][1] - 6} className="map-label red">no-fly {z.id}</text>
        </g>
      ))}
      {[1, 0.72, 0.45, 0.2].map((f) => (
        <ellipse key={f} cx={r.center[0]} cy={H - r.center[1]} rx={r.rx * Math.sqrt(f)} ry={r.ry * Math.sqrt(f)} className="contour" />
      ))}
      <text x={r.center[0]} y={H - r.center[1] - r.ry - 6} className="map-label" textAnchor="middle">ridge {r.height_m} m</text>
      <polyline points={sc.river.map(P).join(" ")} className="river" />
      {sc.roads.edges.map(([u, v, name]) => {
        const a = sc.roads.nodes[u], b = sc.roads.nodes[v];
        return <line key={`${u}-${v}`} x1={a[0]} y1={H - a[1]} x2={b[0]} y2={H - b[1]} className={name.includes("bridge") ? "road bridge" : "road"} />;
      })}
      {Object.entries(sc.places).filter(([id]) => id !== "BASE").map(([id, p]) => (
        <g key={id}>
          <circle cx={p[0]} cy={H - p[1]} r={3} className="place" />
          <text x={p[0] + 6} y={H - p[1] + 4} className="map-label">{id.toLowerCase().replace("_", " ")}</text>
        </g>
      ))}
      {Object.entries(sc.rally_points).map(([id, p]) => (
        <g key={id}>
          <path d={`M${p[0] - 7},${H - p[1] + 6} L${p[0]},${H - p[1] - 7} L${p[0] + 7},${H - p[1] + 6} Z`} className="rally" />
          <text x={p[0] + 9} y={H - p[1] + 4} className="map-label">{id}</text>
        </g>
      ))}
      <rect x={sc.base[0] - 12} y={H - sc.base[1] - 12} width={24} height={24} className="base" />
      <text x={sc.base[0] - 12} y={H - sc.base[1] + 26} className="map-label strong">BASE</text>
    </g>
  );
});

function AgentGlyph({ id, x, y, flies, heading, color, ghost, dead, dark, selected, hideLabel }: {
  id: string; x: number; y: number; flies: boolean; heading: number; color: string;
  ghost?: boolean; dead?: boolean; dark?: boolean; selected?: boolean; hideLabel?: boolean;
}) {
  const cls = ghost ? "ghost" : "";
  return (
    <g transform={`translate(${x},${y})`} className={`agent ${cls}`} opacity={dead ? 0.5 : 1}>
      {selected && !ghost && <circle r={16} className="sel-ring" />}
      {flies ? (
        <path d="M0,-10 L7,7 L0,3 L-7,7 Z" transform={`rotate(${heading})`} fill={ghost ? "none" : color}
          stroke={ghost ? color : "var(--bg)"} strokeWidth={ghost ? 1.2 : 1.2} strokeDasharray={dark ? "2 2" : undefined} />
      ) : (
        <rect x={-7} y={-6} width={14} height={12} rx={3} fill={ghost ? "none" : color}
          stroke={ghost ? color : "var(--bg)"} strokeWidth={1.2} strokeDasharray={dark ? "2 2" : undefined} />
      )}
      {dead && !ghost && <path d="M-9,-9 L9,9 M9,-9 L-9,9" stroke="var(--red)" strokeWidth={2.5} />}
      {!ghost && !hideLabel && (
        <text y={-14} textAnchor="middle" className="agent-label" fill={color}>
          {id}{dark ? " ?" : ""}
        </text>
      )}
    </g>
  );
}

export default function MapView({ sc, snap, coverage, layers, selected, onSelect }: Props) {
  const H = sc.height;
  const fleet = useMemo(() => Object.fromEntries(sc.fleet.map((f) => [f.id, f])), [sc]);
  const b = snap?.brain;
  const surveyProgress = useMemo(() => {
    const m: Record<string, number> = {};
    b?.tasks.forEach((t) => {
      if (t.type === "SURVEY" && t.area) m[t.area] = t.status === "DONE" ? 1 : t.progress;
    });
    return m;
  }, [b?.tasks]);

  const nodePos = (id: string): XY | null => {
    if (id === "BASE") return sc.base;
    const a = snap?.truth.agents[id];
    return a ? a.pos : null;
  };

  return (
    <svg className="map" viewBox={`0 0 ${sc.width} ${H}`} preserveAspectRatio="xMidYMid meet" onClick={() => onSelect(null)}>
      <rect x={0} y={0} width={sc.width} height={H} className="map-bg" />
      {layers.coverage && coverage && <CoverageLayer cov={coverage} H={H} />}
      {Object.entries(sc.sectors).map(([id, [x0, y0, x1, y1]]) => {
        const p = surveyProgress[id] ?? 0;
        return p > 0 ? <rect key={id} x={x0} y={H - y1} width={(x1 - x0) * p} height={y1 - y0} className="survey-fill" /> : null;
      })}
      <StaticLayer sc={sc} />

      {/* blocked roads, as TOWER believes them */}
      {b?.belief.blocked.map(([u, v]) => {
        const a = sc.roads.nodes[u], c = sc.roads.nodes[v];
        return a && c ? <line key={`blk-${u}-${v}`} x1={a[0]} y1={H - a[1]} x2={c[0]} y2={H - c[1]} className="road-blocked" /> : null;
      })}

      {snap?.comms.blackouts.map((z, i) => (
        <circle key={`bo-${i}`} cx={z.center[0]} cy={H - z.center[1]} r={z.radius} className="blackout" />
      ))}

      {layers.links && snap?.comms.links.map(([a, c, r]) => {
        const p = nodePos(a), q = nodePos(c);
        if (!p || !q) return null;
        return <line key={`ln-${a}-${c}`} x1={p[0]} y1={H - p[1]} x2={q[0]} y2={H - q[1]} className="link"
          stroke={r >= -80 ? "var(--ok)" : r >= -86 ? "var(--warn)" : "var(--red)"} />;
      })}

      {layers.routes && b && Object.entries(b.routes).map(([id, pts]) => (
        <polyline key={`rt-${id}`} points={pts.map((p) => `${p[0]},${H - p[1]}`).join(" ")} className="route"
          stroke={AGENT_COLORS[id] ?? "#aaa"} opacity={selected && selected !== id ? 0.15 : 0.85} />
      ))}

      {b?.relay_points.map((p, i) => (
        <g key={`rp-${i}`}>
          <rect x={p[0] - 6} y={H - p[1] - 6} width={12} height={12} transform={`rotate(45 ${p[0]} ${H - p[1]})`} className="relay-point" />
          <text x={p[0] + 10} y={H - p[1] - 8} className="map-label blue">relay point</text>
        </g>
      ))}

      {/* truth overlay: what really is out there (TOWER does not see this) */}
      {layers.truth && snap && (
        <g className="truth">
          {snap.truth.survivors.map((s) => (
            <g key={s.id}>
              <circle cx={s.pos[0]} cy={H - s.pos[1]} r={9} className={s.served >= s.count ? "truth-surv served" : "truth-surv"} />
              <text x={s.pos[0] + 12} y={H - s.pos[1] + 4} className="map-label truth-text">{s.id} {s.served}/{s.count}</text>
            </g>
          ))}
          {snap.truth.decoys.map((d) => (
            <text key={d.id} x={d.pos[0]} y={H - d.pos[1] + 4} className="map-label truth-text" textAnchor="middle">({d.what})</text>
          ))}
          {snap.truth.debris.map((d, i) => (
            <path key={`db-${i}`} d={`M${d.pos[0] - 8},${H - d.pos[1] - 8} L${d.pos[0] + 8},${H - d.pos[1] + 8} M${d.pos[0] + 8},${H - d.pos[1] - 8} L${d.pos[0] - 8},${H - d.pos[1] + 8}`} className="debris" />
          ))}
          {Object.entries(snap.truth.agents).map(([id, a]) => (
            <AgentGlyph key={`g-${id}`} id={id} x={a.pos[0]} y={H - a.pos[1]} flies={fleet[id]?.kind !== "ugv"} heading={0}
              color={AGENT_COLORS[id]} ghost dead={!a.alive} />
          ))}
        </g>
      )}

      {/* detections as TOWER knows them */}
      {b?.belief.detections.map((d) => (
        <g key={d.id} className={`det det-${d.status.toLowerCase()}`}>
          <circle cx={d.pos[0]} cy={H - d.pos[1]} r={11} />
          <text x={d.pos[0]} y={H - d.pos[1] + 4} textAnchor="middle" className="det-text">
            {d.status === "FALSE_ALARM" ? "×" : d.status === "SERVED" ? "✓" : d.status === "INSPECTING" ? "?" : d.count}
          </text>
        </g>
      ))}

      {/* robots parked at base share one stacked label instead of overlapping */}
      {b && (() => {
        const home = Object.entries(b.belief.agents).filter(([, a]) => Math.hypot(a.pos[0] - sc.base[0], a.pos[1] - sc.base[1]) < 45);
        return home.length > 0 ? (
          <g>
            {home.map(([id], i) => (
              <text key={`hl-${id}`} x={sc.base[0] + 24} y={H - sc.base[1] - 34 - i * 13} className="agent-label" fill={AGENT_COLORS[id]}>{id}</text>
            ))}
          </g>
        ) : null;
      })()}

      {/* robots, where TOWER believes they are */}
      {b && Object.entries(b.belief.agents).map(([id, a]) => {
        const moving = Math.hypot(a.vel[0], a.vel[1]) > 0.2;
        const heading = moving ? (Math.atan2(a.vel[0], a.vel[1]) * 180) / Math.PI : 0;
        return (
          <g key={id} onClick={(e) => { e.stopPropagation(); onSelect(id); }} style={{ cursor: "pointer" }}>
            <AgentGlyph id={id} x={a.pos[0]} y={H - a.pos[1]} flies={fleet[id]?.kind !== "ugv"} heading={heading}
              color={AGENT_COLORS[id]} dead={!a.alive} dark={a.expected_dark} selected={selected === id}
              hideLabel={Math.hypot(a.pos[0] - sc.base[0], a.pos[1] - sc.base[1]) < 45} />
          </g>
        );
      })}
      <g className="scale">
        <line x1={sc.width - 230} y1={H - 20} x2={sc.width - 30} y2={H - 20} />
        <text x={sc.width - 130} y={H - 28} textAnchor="middle" className="map-label">200 m</text>
      </g>
    </svg>
  );
}
