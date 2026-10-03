"""World model: TOWER's belief, built only from messages the radio delivered.

Every field carries the time it was last confirmed, so stale data looks stale.
"""
from dataclasses import dataclass, field

from .scenario import dist


@dataclass
class AgentBelief:
    id: str
    pos: tuple
    vel: tuple = (0.0, 0.0)
    battery: float = 100.0
    kits: int = 0
    kg: float = 0.0
    airborne: bool = False
    mode: str = "idle"
    status: str = ""
    last_node: str | None = None
    stop: str | None = None
    version_acked: int = 0
    t_state: float = 0.0
    t_heard: float = 0.0
    alive: bool = True             # what TOWER believes
    lost_reason: str = ""
    expected_dark: bool = False    # silent, but inside predicted radio shadow
    battery_hist: list = field(default_factory=list)

    def predicted_pos(self, now):
        dt = min(max(0.0, now - self.t_state), 30.0)
        return (self.pos[0] + self.vel[0] * dt, self.pos[1] + self.vel[1] * dt)


class WorldModel:
    def __init__(self, sc):
        self.sc = sc
        self.agents = {a.id: AgentBelief(a.id, tuple(a.spawn), kits=a.kits, last_node=sc.nearest_node(a.spawn)
                                         if not a.flies else None) for a in sc.fleet.values()}
        self.blocked = set()               # road edges known blocked
        self.blocked_t = {}
        self.wind = float(sc.raw.get("truth", {}).get("wind", [4, 0])[0])
        self.wind_reports = []
        self.wind_t = 0.0
        self.detections = []               # merged detections
        self.messages_in = 0

    def ingest(self, m, now):
        """Apply one delivered message. Returns a list of raw events for the monitor."""
        self.messages_in += 1
        p = m.payload
        a = self.agents[m.src]
        a.t_heard = max(a.t_heard, now)
        a.expected_dark = False
        events = []
        if not a.alive:
            a.alive = True
            events.append({"type": "AGENT_RECOVERED", "agent": a.id})
        if m.kind == "state":
            a.pos = tuple(p["pos"])
            a.vel = tuple(p["vel"])
            a.battery = p["battery"]
            a.kits, a.kg, a.airborne = p["kits"], p["kg"], p["airborne"]
            a.mode, a.status, a.stop = p["mode"], p["status"], p["stop"]
            a.last_node = p.get("last_node") or a.last_node
            a.t_state = p["t"]
            a.battery_hist.append((p["t"], p["battery"]))
            a.battery_hist = a.battery_hist[-20:]
            if "wind" in p:
                self.wind_reports.append((now, p["wind"]))
                self.wind_reports = [(t, w) for t, w in self.wind_reports if now - t < 30]
                ws = sorted(w for _, w in self.wind_reports)
                self.wind = ws[len(ws) // 2]
                self.wind_t = now
        elif m.kind == "ack":
            a.version_acked = max(a.version_acked, p["version"])
        elif m.kind == "task":
            events.append(dict(p, type="TASK_" + p["event"]))
            if p["event"] == "BLOCKED" and p.get("edge"):
                e = tuple(sorted(p["edge"]))
                if e not in self.blocked:
                    self.blocked.add(e)
                    self.blocked_t[e] = now
                    events.append({"type": "BLOCKED_EDGE", "edge": e, "agent": a.id, "pos": p.get("pos")})
        elif m.kind == "detection":
            pos = tuple(p["pos"])
            for d in self.detections:
                if dist(d["pos"], pos) < 40:
                    d["conf"] = max(d["conf"], p["conf"])
                    d["seen_by"].add(a.id)
                    break
            else:
                d = {"id": f"DET-{len(self.detections) + 1}", "pos": pos, "count": p["count"], "conf": p["conf"],
                     "by": a.id, "seen_by": {a.id}, "t": p["t"], "t_rx": now, "status": "NEW"}
                self.detections.append(d)
                events.append({"type": "DETECTION", "det": d["id"], "agent": a.id, "conf": p["conf"],
                               "count": p["count"], "pos": pos, "delay": round(now - p["t"], 1)})
        elif m.kind == "safety":
            events.append(dict(p, type="SAFETY"))
        elif m.kind == "auction":
            events.append(dict(p, type="AUCTION_WON"))
        return events

    def detection(self, did):
        return next((d for d in self.detections if d["id"] == did), None)

    def to_client(self, now):
        return {
            "agents": {aid: {"pos": [round(a.predicted_pos(now)[0], 1), round(a.predicted_pos(now)[1], 1)],
                             "reported_pos": a.pos, "vel": a.vel, "battery": a.battery, "kits": a.kits, "kg": a.kg,
                             "airborne": a.airborne, "mode": a.mode, "status": a.status, "stop": a.stop,
                             "version_acked": a.version_acked, "age_state": round(now - a.t_state, 1),
                             "age_heard": round(now - a.t_heard, 1), "alive": a.alive,
                             "lost_reason": a.lost_reason, "expected_dark": a.expected_dark}
                       for aid, a in self.agents.items()},
            "blocked": [list(e) for e in sorted(self.blocked)],
            "wind": round(self.wind, 1),
            "detections": [{k: (sorted(v) if isinstance(v, set) else v) for k, v in d.items()}
                           for d in self.detections],
        }
