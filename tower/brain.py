"""TOWER brain: the decision chain from the architecture diagram.

  operator text -> 1 Mission Compiler -> 2 Validator -> 3 Planner -> 4 Risk Engine
  -> 5 Autonomy Gate -> 6 Dispatcher -> robots -> telemetry -> World Model
  -> Monitor (every second) -> event -> Re-plan Engine (L1/L2/L3) -> back to 4 (or 1)

Everything runs locally. The only LLM call is the compiler (and optional
explanation polish), and it can be a free local model or no model at all.
"""
import contextlib
import threading

from .compiler import expand
from .gate import decide
from .ledger import Ledger, explain, fmt_t
from .model import PRIORITY_RANK, Task
from .planner import AgentState, PlanContext, Planner
from .risk import assess
from .scenario import dist
from .sim.comms import Message
from .world_model import WorldModel

LIFE_RESPONSE_S = 15 * 60       # a found survivor should get a kit within 15 minutes
INSPECT_RESPONSE_S = 10 * 60
CONF_CONFIRM = 0.7              # below this, take a second look before committing a kit
HEARTBEAT_LOST_S = 3.0
DARK_LOST_S = 240.0
LINK_HORIZON_S = 120.0
L2_MIN_GAP_S = 3.0

EVENT_LEVEL = {"DETECTION": 1, "INSPECT_CONFIRMED": 1, "LINK_RISK": 1, "RELAY_CANCEL": 1,
               "BLOCKED_EDGE": 2, "AGENT_LOST": 2, "AGENT_RECOVERED": 2, "BATTERY_SHORT": 2,
               "WIND_CHANGE": 2, "GOAL_CHANGE": 3}


class Brain:
    def __init__(self, sc, comms, llm, settings, ledger=None):
        self.sc, self.comms, self.llm, self.settings = sc, comms, llm, settings
        self.wm = WorldModel(sc)
        self.planner = Planner(sc)
        self.ledger = ledger or Ledger()
        self.tasks = {}
        self._tid = 0
        self.spec = None
        self.policy = {"deliver_on_detect": None, "keep_link": [], "reserve": 15.0, "deadline_s": 2700.0}
        self.compile_info = None
        self.plan = None
        self.plan_wind = self.wm.wind
        self.version = 0
        self.dispatched = {a: [] for a in sc.fleet}
        self.assigned_version = {a: 0 for a in sc.fleet}
        self.route_lines = {}
        self.phase = "IDLE"
        self.escalations = {}
        self._eid = 0
        self.events = []
        self.flags = {}
        self.relay_for = {}             # target agent -> relay task id
        self.last_monitor = -1.0
        self.last_l2 = -99.0
        self.pending = []               # events waiting for a re-plan
        self.now = 0.0
        self.wall = 0.0
        self.lock = threading.RLock()

    # ============================================================== helpers
    def log(self, kind, text, level="info"):
        self.events.append({"t": round(self.now, 1), "kind": kind, "text": text, "level": level})
        self.events = self.events[-200:]

    def new_task(self, **kw):
        self._tid += 1
        tid = f"T{self._tid:02d}"
        self.tasks[tid] = Task(id=tid, **kw)
        return tid

    def open_tasks(self):
        return [t for t, task in self.tasks.items() if task.open and task.type not in ("RTB", "HOLD")]

    def agent_states(self):
        out = []
        for aid, spec in self.sc.fleet.items():
            b = self.wm.agents[aid]
            locked = [t for t in self.open_tasks()
                      if self.tasks[t].status == "ACTIVE" and self.tasks[t].assignee == aid]
            out.append(AgentState(aid, spec, b.predicted_pos(self.now), max(0.0, b.battery), b.kits, b.kg,
                                  b.last_node, locked[:1], alive=b.alive))
        return out

    def ctx(self, extra_deadline=0.0):
        alive = {a for a, b in self.wm.agents.items() if b.alive}
        prev = {}
        if self.plan:
            for a, r in self.plan.routes.items():
                for t in r:
                    prev[t] = a
        return PlanContext(now=self.now, wind=self.wm.wind, reserve=self.policy["reserve"],
                           deadline=self.policy["deadline_s"] + extra_deadline, blocked=set(self.wm.blocked),
                           tasks=self.tasks, prev_assign=prev, alive_ids=alive)

    # -- radio coverage predictions (TOWER's own model, from belief only) --
    def _relay_sources(self):
        """Hover points of relay tasks that are active or planned (assume the relay will be there)."""
        out = []
        for t in self.tasks.values():
            if t.type == "RELAY" and t.open:
                out.append(t.pos)
        return out

    def link_rssi(self, p, flies=False):
        r = self.sc.radio
        h = self.sc.abs_alt(p, r["drone_alt_m"] if flies else r["rover_ant_m"])
        best = self.sc.rssi(self.sc.base, self.sc.raw["base"]["antenna_m"], p, h)
        for q in self._relay_sources():
            qh = self.sc.abs_alt(q, r["drone_alt_m"])
            via = min(self.sc.rssi(self.sc.base, self.sc.raw["base"]["antenna_m"], q, qh, 6),
                      self.sc.rssi(q, qh, p, h, 6))
            best = max(best, via)
        return best

    def direct_rssi(self, p, flies=False):
        r = self.sc.radio
        h = self.sc.abs_alt(p, r["drone_alt_m"] if flies else r["rover_ant_m"])
        return self.sc.rssi(self.sc.base, self.sc.raw["base"]["antenna_m"], p, h)

    def link_bad(self, p):
        return self.link_rssi(p, False) < self.sc.radio["warn_dbm"]

    def _covered_now(self, aid, p):
        """Would agent `aid` at p be heard by the base, given where TOWER believes everyone is?"""
        r = self.sc.radio
        spec = self.sc.fleet[aid]
        nodes = {"BASE": (self.sc.base, self.sc.raw["base"]["antenna_m"], 0.0)}
        for oid, b in self.wm.agents.items():
            if oid == aid or not b.alive or b.expected_dark:
                continue
            os_ = self.sc.fleet[oid]
            pp = b.predicted_pos(self.now)
            agl = r["drone_alt_m"] if (os_.flies and b.airborne) else r["rover_ant_m"]
            nodes[oid] = (pp, self.sc.abs_alt(pp, agl), os_.radio_gain_db)
        agl = r["drone_alt_m"] if spec.flies and self.wm.agents[aid].airborne else r["rover_ant_m"]
        me = (p, self.sc.abs_alt(p, agl), spec.radio_gain_db)
        reach, frontier = {"BASE"}, ["BASE"]
        while frontier:
            x = frontier.pop()
            for y, n in nodes.items():
                if y not in reach and self.sc.rssi(nodes[x][0], nodes[x][1], n[0], n[1], nodes[x][2] + n[2]) >= r["usable_dbm"]:
                    reach.add(y)
                    frontier.append(y)
        return any(self.sc.rssi(nodes[x][0], nodes[x][1], me[0], me[1], nodes[x][2] + me[2]) >= r["usable_dbm"]
                   for x in reach)

    # ========================================================== mission start
    def start_mission(self, result):
        """Called with a CompileResult. Builds the first plan and asks the operator to approve it."""
        with self.lock:
            rec_base = {"id": self.ledger.new_id(), "t": self.now, "level": "L3", "status": "done",
                        "compile": {"source": result.source, "model": result.model, "attempts": result.attempts,
                                    "notes": result.notes}}
            self.compile_info = {"source": result.source, "model": result.model, "attempts": result.attempts,
                                 "notes": result.notes, "errors": result.errors}
            if result.spec is None:
                self.phase = "IDLE"
                rec = dict(rec_base, trigger={"type": "COMPILE_FAILED", "summary": "Mission could not be compiled"},
                           options=[], gate={"result": "HOLD", "why": result.errors}, chosen=None, by=None,
                           explain="The validator rejected the mission: " + " ".join(result.errors))
                self.ledger.add(rec)
                self.log("compile", "Mission rejected: " + "; ".join(result.errors), "error")
                return rec
            self.spec = result.spec
            templates, self.policy = expand(result.spec, self.sc)
            self.tasks, self._tid = {}, 0
            for t in templates:
                key = t.pop("key")
                self.new_task(origin=f"objective:{key[0]}", **t)
            src = "LLM " + result.model if result.source == "llm" else "rule-based compiler"
            self.log("compile", f"Compiled by {src} in {len(result.attempts)} attempt(s): "
                                f"{len(self.tasks)} tasks, deadline {self.spec.deadline_min:g} min")
            cands = self._candidates(level=3, cold_only=True)
            gate = decide(cands, first_plan=True, tasks=self.tasks)
            trig = {"type": "MISSION_START", "summary": f"New mission compiled by {src}"}
            rec = self._record(rec_base["id"], 3, trig, cands, gate, extra={"compile": rec_base["compile"]})
            self.phase = "AWAIT_APPROVAL"
            self._escalate(rec, cands, gate, kind="first", timeout=False)
            return rec

    # ================================================================ tick
    def tick(self, now, wall, delivered):
        with self.lock:
            self.now, self.wall = now, wall
            events = []
            for m in delivered:
                events += self.wm.ingest(m, now)
            events = self._apply_task_events(events)
            if self.phase == "RUNNING":
                if now - self.last_monitor >= 1.0:
                    self.last_monitor = now
                    events += self.monitor()
                self.pending += events
                self._process_pending()
                self._timeouts()
                self._check_done()

    def _apply_task_events(self, events):
        out = []
        for e in events:
            t = e["type"]
            task = self.tasks.get(e.get("task")) if e.get("task") else None
            if t == "TASK_STARTED" and task and task.open:
                task.status, task.assignee = "ACTIVE", e["agent"]
                task.meta.setdefault("started", e["t"])
            elif t == "TASK_PROGRESS" and task:
                task.progress = max(task.progress, e.get("progress", 0))
            elif t == "TASK_DONE" and task:
                if task.status != "DONE":
                    task.status, task.progress, task.assignee = "DONE", 1.0, e["agent"]
                    task.meta["done_t"] = e["t"]
                    self.log("task", f"{e['agent']} finished {task.label()} at T+{fmt_t(e['t'])}")
                    if task.type == "DELIVER":
                        task.meta["served"] = e.get("served", 0)
                        for d in self.wm.detections:
                            if d["id"] == task.meta.get("det"):
                                d["status"] = "SERVED"
                    if task.type == "INSPECT":
                        res = e.get("inspect", {})
                        det = self.wm.detection(task.meta.get("det")) if task.meta.get("det") else None
                        if res.get("confirmed"):
                            if det:
                                det["status"], det["conf"], det["count"] = "CONFIRMED", 0.95, res["count"]
                            out.append({"type": "INSPECT_CONFIRMED", "pos": tuple(res["pos"]), "count": res["count"],
                                        "agent": e["agent"], "det": task.meta.get("det"), "task": task.id})
                        else:
                            if det:
                                det["status"] = "FALSE_ALARM"
                            self._note(f"Second look at {task.label()} by {e['agent']}: false alarm, no kit committed",
                                       {"type": "FALSE_ALARM", "agent": e["agent"]})
                    if task.type == "RELAY":
                        for k, v in list(self.relay_for.items()):
                            if v == task.id:
                                del self.relay_for[k]
            elif t == "AUCTION_WON" and task:
                task.assignee = e["agent"]
                self._note(f"L0 mesh auction: {e['agent']} won {task.label()} from silent {e['from']} "
                           f"while cut off from base", {"type": "AUCTION_WON", "agent": e["agent"]}, level="L0")
            elif t == "SAFETY":
                self._note(f"{e['agent']}: {e['event'].replace('_', ' ').lower()} - {e.get('action', '')}",
                           {"type": e["event"], "agent": e["agent"]}, level="L0")
            elif t == "TASK_BLOCKED":
                self.log("robot", f"{e['agent']} blocked at {e.get('pos')}: {e.get('action', '')}", "warn")
            else:
                out.append(e)
        return out

    # ============================================================== monitor
    def monitor(self):
        """Runs every second on TOWER's belief. Emits typed events."""
        ev = []
        now = self.now
        reserve = self.policy["reserve"]
        ctx = self.ctx()
        for aid, b in self.wm.agents.items():
            if not b.alive:
                continue
            silent = now - b.t_heard
            if silent > HEARTBEAT_LOST_S:
                # once a robot is known to be in a shadow it gets the long grace period: a relay
                # arriving or the robot driving out will bring it back
                if not b.expected_dark and self._everywhere_covered(aid, b):
                    b.alive, b.lost_reason = False, f"heartbeat missing {silent:.0f} s in radio coverage"
                    ev.append({"type": "AGENT_LOST", "agent": aid, "why": b.lost_reason})
                elif silent > DARK_LOST_S:
                    b.alive, b.lost_reason = False, f"silent {silent:.0f} s"
                    ev.append({"type": "AGENT_LOST", "agent": aid, "why": b.lost_reason})
                else:
                    if not b.expected_dark:
                        self.log("comms", f"{aid} out of contact (expected: predicted radio shadow)")
                    b.expected_dark = True
                continue
            # battery projection along the dispatched plan (skipped while holding for the operator)
            spec = self.sc.fleet[aid]
            if spec.flies and self.dispatched[aid] and not self._holding(aid):
                stops = self._remaining_stops(aid)
                st = AgentState(aid, spec, b.predicted_pos(now), b.battery, b.kits, b.kg, b.last_node)
                worst = self.planner.follow(st, stops, ctx)
                last = self.flags.get(("BATTERY_SHORT", aid))
                fresh = last is None or (now - last[0] > 30 and last[1] != self.assigned_version[aid])
                if worst < reserve - 3 and fresh:
                    self.flags[("BATTERY_SHORT", aid)] = (now, self.assigned_version[aid])
                    ev.append({"type": "BATTERY_SHORT", "agent": aid, "projected": round(worst, 1)})
        # wind
        if abs(self.wm.wind - self.plan_wind) >= 3.0 and ("WIND", round(self.wm.wind)) not in self.flags:
            self.flags[("WIND", round(self.wm.wind))] = now
            ev.append({"type": "WIND_CHANGE", "from": round(self.plan_wind, 1), "to": round(self.wm.wind, 1)})
        ev += self._link_watch()
        return ev

    def _holding(self, aid):
        d = self.dispatched[aid]
        return bool(d) and d[0]["type"] == "HOLD"

    def _everywhere_covered(self, aid, b):
        """True only if the agent's last position, its predicted position and its whole current
        task area all have radio coverage. Then silence means failure, not a radio shadow."""
        pts = [b.pos, b.predicted_pos(self.now)]
        cur = next((s for s in self.dispatched[aid] if s["id"] == b.stop), None)
        if cur:
            if cur["type"] == "SURVEY" and cur.get("area"):
                x0, y0, x1, y1 = self.sc.sectors[cur["area"]]
                pts += [(x0 + (x1 - x0) * fx, y0 + (y1 - y0) * fy) for fx in (0.1, 0.5, 0.9) for fy in (0.1, 0.5, 0.9)]
            else:
                pts.append(tuple(self.sc.base if cur["type"] == "RTB" else cur["pos"]))
        return all(self._covered_now(aid, p) for p in pts)

    def _remaining_stops(self, aid):
        stops = self.dispatched[aid]
        cur = self.wm.agents[aid].stop
        ids = [s["id"] for s in stops]
        start = ids.index(cur) if cur in ids else 0
        return [s for s in stops[start:] if s["type"] == "RTB" or (s["id"] in self.tasks and self.tasks[s["id"]].open)]

    def _future_path(self, aid, horizon):
        """Sample where a rover will be over the next `horizon` seconds, following its dispatched stops."""
        b = self.wm.agents[aid]
        spec = self.sc.fleet[aid]
        pos, node, t = b.predicted_pos(self.now), b.last_node, 0.0
        samples, dests = [], []
        for s in self._remaining_stops(aid):
            goal = self.sc.base if s["type"] == "RTB" else tuple(s["pos"])
            r = self.sc.rover_route(pos, node, goal, self.wm.blocked)
            if r is None:
                break
            pts = r[1]
            for i in range(len(pts) - 1):
                seg = dist(pts[i], pts[i + 1])
                n = max(1, int(seg // 25))
                for k in range(1, n + 1):
                    q = (pts[i][0] + (pts[i + 1][0] - pts[i][0]) * k / n, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * k / n)
                    samples.append((t + (seg * k / n) / spec.speed, q))
                t += seg / spec.speed
            svc = 90.0 if s["type"] == "TRANSPORT" else 15.0
            dests.append((t, goal, svc, s))
            t += svc
            pos, node = goal, r[2][-1]
            if t > horizon + 400:
                break
        return samples, dests

    def _link_watch(self):
        """Predict link loss along each KEEP_LINK robot's path; create or release relay tasks."""
        ev = []
        warn = self.sc.radio["warn_dbm"]
        for aid in self.policy.get("keep_link", []):
            b = self.wm.agents.get(aid)
            if not b or not b.alive or self.sc.fleet[aid].flies or self._holding(aid):
                continue
            if self.flags.get(("NO_RELAY", aid), -1) > self.now:
                continue                      # operator or gate chose to drop this relay recently
            relay_tid = self.relay_for.get(aid)
            samples, dests = self._future_path(aid, LINK_HORIZON_S)
            direct_bad = [(t, q) for t, q in samples if self.direct_rssi(q) < warn]
            here_bad = self.direct_rssi(b.predicted_pos(self.now)) < warn
            def dark_window(t_enter):
                """How long the robot stays out of direct coverage once it enters the shadow."""
                t_exit = t_enter
                for t, q in samples:
                    if t >= t_enter:
                        if self.direct_rssi(q) < warn:
                            t_exit = t
                        elif t_exit > t_enter:
                            break
                for t, g, svc, s in dests:
                    if self.direct_rssi(g) < warn and t >= t_enter:
                        t_exit = max(t_exit, t + svc)
                return t_exit - t_enter

            if relay_tid is None:
                soon = [(t, q) for t, q in direct_bad if t <= LINK_HORIZON_S]
                if soon or (here_bad and not b.expected_dark):
                    t_enter = soon[0][0] if soon else 0.0
                    shadow = [q for t, q in direct_bad] or [b.predicted_pos(self.now)]
                    ev.append({"type": "LINK_RISK", "agent": aid, "t_enter": round(t_enter),
                               "dur": round(dark_window(t_enter) + 60), "shadow": shadow[:12],
                               "rssi": round(min(self.direct_rssi(q) for q in shadow), 1)})
            else:
                rt = self.tasks.get(relay_tid)
                still = any(t <= LINK_HORIZON_S * 1.5 for t, _ in direct_bad) or here_bad
                if rt and rt.status == "ACTIVE" and still:
                    # keep the relay's expected hover time current, so battery projections stay honest
                    t_enter = 0.0 if here_bad else min(t for t, _ in direct_bad)
                    rt.duration = (self.now - rt.meta.get("started", self.now)) + t_enter + dark_window(t_enter) + 60
                if rt and rt.open and not still:
                    if rt.status == "ACTIVE" and rt.assignee and self.now - rt.meta.get("released", -999) > 30:
                        self.comms.down(rt.assignee, Message("BASE", rt.assignee, 2, "release", 64,
                                                             {"task": rt.id}, self.now, key=("release", rt.id)))
                        if "released" not in rt.meta:
                            self._note(f"{aid} is back in direct radio coverage; {rt.assignee} released from relay duty",
                                       {"type": "LINK_CLEAR", "agent": aid})
                        rt.meta["released"] = self.now
                    elif rt.status == "PENDING":
                        rt.status = "CANCELLED"
                        del self.relay_for[aid]
                        ev.append({"type": "RELAY_CANCEL", "agent": aid, "task": rt.id})
        return ev

    # ======================================================== re-plan engine
    def _process_pending(self):
        if not self.pending:
            return
        level, triggers = 0, []
        keep = []
        for e in self.pending:
            t = e["type"]
            lv = EVENT_LEVEL.get(t)
            if lv is None:
                if t.startswith("TASK_") or t in ("FALSE_ALARM",):
                    continue
                self.log("event", f"unhandled event {t}", "warn")
                continue
            if t == "DETECTION":
                det = self.wm.detection(e["det"])
                if e["conf"] >= CONF_CONFIRM:
                    if self.policy.get("deliver_on_detect") and det and det["status"] == "NEW":
                        det["status"] = "KIT_PLANNED"
                        tid = self.new_task(type="DELIVER", priority="LIFE", pos=tuple(e["pos"]), kits=e["count"],
                                            due=min(self.now + LIFE_RESPONSE_S, self.now + 3600),
                                            origin=f"detection:{e['det']}", meta={"det": e["det"]})
                        e["new_task"] = tid
                        e["summary"] = (f"{e['agent']} found {e['count']} survivor(s) at ({e['pos'][0]:.0f},"
                                        f"{e['pos'][1]:.0f}), confidence {e['conf']:.2f}"
                                        + (f", reported {e['delay']:.0f} s late (store-and-forward)" if e["delay"] > 3 else ""))
                    else:
                        self.log("detect", f"{e['agent']} found {e['count']} survivor(s); mission has no kit delivery objective")
                        continue
                else:
                    if det:
                        det["status"] = "INSPECTING"
                    tid = self.new_task(type="INSPECT", priority="HIGH", pos=tuple(e["pos"]),
                                        due=self.now + INSPECT_RESPONSE_S, origin=f"detection:{e['det']}",
                                        meta={"det": e["det"]})
                    e["new_task"] = tid
                    e["summary"] = (f"{e['agent']} saw a possible survivor at ({e['pos'][0]:.0f},{e['pos'][1]:.0f}) "
                                    f"with low confidence {e['conf']:.2f}; second look before committing a kit")
            elif t == "INSPECT_CONFIRMED":
                if not self.policy.get("deliver_on_detect"):
                    continue
                tid = self.new_task(type="DELIVER", priority="LIFE", pos=tuple(e["pos"]), kits=e["count"],
                                    due=self.now + LIFE_RESPONSE_S, origin=f"inspect:{e.get('det')}",
                                    meta={"det": e.get("det")})
                e["summary"] = f"{e['agent']} confirmed {e['count']} survivor(s) on second look"
            elif t == "LINK_RISK":
                aid = e["agent"]
                if aid in self.relay_for:
                    continue
                p = self.sc.best_relay_point(e["shadow"])
                tid = self.new_task(type="RELAY", priority="HIGH", pos=p, due=self.now + max(30, e["t_enter"]),
                                    duration=max(120, e["dur"]), origin=f"keep_link:{aid}", meta={"for": aid})
                self.relay_for[aid] = tid
                when = "now" if e["t_enter"] <= 1 else f"in {e['t_enter']} s"
                e["summary"] = (f"{aid} will lose radio contact {when} (predicted {e['rssi']:.0f} dBm behind the ridge); "
                                f"relay needed at ({p[0]:.0f},{p[1]:.0f}) for about {max(120, e['dur']) // 60} min")
            elif t == "RELAY_CANCEL":
                e["summary"] = f"{e['agent']} no longer needs a relay; relay task cancelled"
            elif t == "BLOCKED_EDGE":
                u, v = e["edge"]
                name = self.sc.graph.edges[u, v].get("name", "road") if self.sc.graph.has_edge(u, v) else "road"
                e["summary"] = f"{e['agent']} stopped making progress: {name} {u}-{v} is blocked"
            elif t == "AGENT_LOST":
                aid = e["agent"]
                orphan = [x for x in self.open_tasks() if self.tasks[x].assignee == aid
                          or (self.plan and x in self.plan.routes.get(aid, []))]
                for x in orphan:
                    if self.tasks[x].status == "ACTIVE":
                        self.tasks[x].status = "PENDING"
                    self.tasks[x].assignee = None
                e["orphans"] = orphan
                e["summary"] = f"{aid} lost ({e['why']}); {len(orphan)} task(s) need a new robot"
            elif t == "AGENT_RECOVERED":
                e["summary"] = f"{e['agent']} is back in contact"
            elif t == "BATTERY_SHORT":
                e["summary"] = (f"{e['agent']} projected to finish its plan with {e['projected']:.0f}% battery "
                                f"(reserve is {self.policy['reserve']:g}%)")
            elif t == "WIND_CHANGE":
                e["summary"] = f"Wind changed from {e['from']} to {e['to']} m/s; battery use re-estimated"
            if lv >= 2 and self.now - self.last_l2 < L2_MIN_GAP_S:
                keep.append(e)
                continue
            level = max(level, lv)
            triggers.append(e)
        self.pending = keep
        if triggers:
            self.replan(level, triggers)

    def replan(self, level, triggers, goal_change=False, proposal=None):
        if level >= 2:
            self.last_l2 = self.now
        variants = None
        orphans = [x for e in triggers if e["type"] == "AGENT_LOST" for x in e.get("orphans", [])]
        optional = [x for x in orphans if x in self.tasks and self.tasks[x].priority != "LIFE"]
        if optional:
            variants = [("keep every task", [])] + [(f"drop {self.tasks[x].label()}", [x]) for x in optional[:2]]
        cands = self._candidates(level=level, variants=variants)
        # L3: no plan can serve a LIFE task in time -> offer a goal change (extend deadline / flag ground team)
        best = cands[0] if cands else None
        life_trouble = best and any(self.tasks[t].priority == "LIFE" for t in best.dropped) or \
            (best and any(self.tasks[t].priority == "LIFE" for ev in best.evals.values() for t in ev.late))
        if life_trouble and not goal_change:
            return self._offer_l3(triggers, cands)
        prev_finish = {}
        if self.plan:
            for ev in self.plan.evals.values():
                prev_finish.update(ev.finish)
        prev_tasks = self.plan.task_set() if self.plan else set()
        gate = decide(cands, goal_change=goal_change, prev_finish=prev_finish, prev_tasks=prev_tasks,
                      tasks=self.tasks)
        summary = "; ".join(e.get("summary", e["type"]) for e in triggers[:3])
        trig = {"type": triggers[0]["type"] if len(triggers) == 1 else "MULTIPLE", "summary": summary,
                "events": [{k: v for k, v in e.items() if k not in ("shadow",)} for e in triggers]}
        rec = self._record(self.ledger.new_id(), level, trig, cands, gate)
        kind = "goal" if goal_change else ("hold" if gate["result"] == "HOLD" else "plan")
        if gate["result"] == "AUTO" and not goal_change:
            self._commit(cands[0], rec, by="auto")
            return rec
        if gate["result"] == "HOLD":
            self._hold(rec)
        self._escalate(rec, cands, gate, kind=kind, proposal=proposal)
        return rec

    def _offer_l3(self, triggers, cands):
        """A LIFE task cannot be served in time. Offer: extend the deadline, or keep it and flag a ground team."""
        life = [t for t in self.open_tasks() if self.tasks[t].priority == "LIFE"]
        options = []
        saved = {t: self.tasks[t].due for t in life}
        ext_plan = None
        for ext in (5, 10, 15, 20, 30):
            for t in life:
                self.tasks[t].due = saved[t] + ext * 60
            c = self._candidates(level=3, extra_deadline=ext * 60)
            ok = c and not any(self.tasks[x].priority == "LIFE" for x in c[0].dropped) and \
                not any(self.tasks[x].priority == "LIFE" for ev in c[0].evals.values() for x in ev.late)
            if ok:
                ext_plan = c[0]
                ext_plan.label = f"extend deadline {ext} min"
                ext_plan.meta_ext = ext
                break
        for t in life:
            self.tasks[t].due = saved[t]
        if ext_plan:
            options.append(ext_plan)
        keep = cands[0] if cands else None
        if keep:
            keep.label = "keep deadline, flag late LIFE task for a ground team"
            options.append(keep)
        summary = "; ".join(e.get("summary", e["type"]) for e in triggers[:2]) + \
            ". No plan serves every LIFE task inside its deadline"
        trig = {"type": "INFEASIBLE", "summary": summary, "events": triggers}
        gate = {"result": "ASK", "why": ["a LIFE task cannot be served in time", "the mission goal changes"],
                "reversible": False, "gap": None}
        rec = self._record(self.ledger.new_id(), 3, trig, options, gate)
        self._escalate(rec, options, gate, kind="infeasible")
        return rec

    def _candidates(self, level, variants=None, cold_only=False, extra_deadline=0.0):
        # a relay that missed its on-station time is still needed: give it a fresh response window
        for t in self.tasks.values():
            if t.type == "RELAY" and t.open and t.due is not None and t.due < self.now + 60:
                t.due = self.now + 300
        agents = self.agent_states()
        ctx = self.ctx(extra_deadline)
        ids = self.open_tasks()
        init = None
        if self.plan and not cold_only:
            init = {}
            for aid, r in self.plan.routes.items():
                if self.wm.agents[aid].alive:
                    keep = [t for t in r if t in self.tasks and self.tasks[t].open]
                    locked = [t for t in ids if self.tasks[t].status == "ACTIVE" and self.tasks[t].assignee == aid][:1]
                    init[aid] = locked + [t for t in keep if t not in locked]
        if level == 1:
            cands = self.planner.candidates(agents, ctx, ids, init, variants, strategies=("balanced",), cold=False)
        else:
            cands = self.planner.candidates(agents, ctx, ids, init, variants)
        seed = int(self.now * 10) % 100000
        for i, c in enumerate(cands):
            c.risk = assess(c, ctx, self.link_bad, seed=seed, all_task_ids=ids)
            c.score = c.risk["score"]
        cands.sort(key=lambda c: (-c.score, c.cost))
        # keep the best plan of each distinct task set, at most 3 options
        out, seen = [], set()
        for c in cands:
            ts = c.task_set()
            if ts in seen and len(out) >= 1:
                continue
            seen.add(ts)
            out.append(c)
            if len(out) == 3:
                break
        return out

    # ============================================================ decisions
    def _changes(self, plan):
        """Plain-English diff between the committed plan and a candidate."""
        out = []
        old = self.plan
        for aid, r in plan.routes.items():
            ev = plan.evals.get(aid)
            for tid in r:
                prev = old.assignee(tid) if old else None
                fin = ev.finish.get(tid) if ev else None
                eta = f" (done by T+{fmt_t(fin)})" if fin else ""
                if old is None:
                    continue
                if prev is None:
                    out.append(f"{aid} takes {self.tasks[tid].label()}{eta}.")
                elif prev != aid:
                    out.append(f"{self.tasks[tid].label()} moves from {prev} to {aid}{eta}.")
                else:
                    of = old.evals[prev].finish.get(tid) if prev in old.evals else None
                    if of and fin and fin - of > 120:
                        out.append(f"{self.tasks[tid].label()} now done by T+{fmt_t(fin)} (+{(fin - of) / 60:.0f} min).")
            if ev:
                visits = [s for s in ev.stops if s.kind == "base"]
                old_visits = [s for s in old.evals[aid].stops if s.kind == "base"] if old and aid in old.evals else []
                if len(visits) > len(old_visits):
                    out.append(f"{aid} returns to base first to {visits[0].purpose}.")
        if old is None:
            for aid, r in plan.routes.items():
                if r:
                    out.append(f"{aid}: " + " -> ".join(self.tasks[t].label() for t in r) + ".")
        for tid in plan.dropped + plan.excluded:
            out.append(f"{self.tasks[tid].label()} is dropped.")
        return out[:8]

    def _record(self, rid, level, trig, cands, gate, extra=None):
        opts = []
        for i, c in enumerate(cands):
            opts.append({"id": "ABC"[i], "label": c.label if c.label != "all tasks" or len(cands) == 1 else
                         f"{c.label} ({c.strategy.split('/')[0]})", "strategy": c.strategy,
                         "risk": {k: v for k, v in c.risk.items() if k != "p_task"}, "score": c.score,
                         "dropped": [self.tasks[t].label() for t in c.dropped + c.excluded],
                         "changes": self._changes(c)})
        rec = {"id": rid, "t": round(self.now, 1), "level": f"L{level}", "trigger": trig, "options": opts,
               "gate": gate, "default": "A" if opts else None, "chosen": None, "by": None,
               "status": "pending" if gate["result"] != "AUTO" else "done"}
        if extra:
            rec.update(extra)
        rec["explain"] = explain(dict(rec, chosen="A"), self.tasks)
        self.ledger.add(rec)
        self.log("decision", f"{rid} {rec['level']} {gate['result']}: {trig.get('summary', '')[:140]}",
                 "warn" if gate["result"] != "AUTO" else "info")
        if self.settings.llm_explain and self.llm is not None and self.llm.enabled:
            threading.Thread(target=self._polish, args=(rec,), daemon=True).start()
        return rec

    def _polish(self, rec):
        try:
            text = self.llm.text_chat(
                "You explain robot-swarm decisions to a disaster-response operator in 2 short plain sentences. "
                "No jargon, no markdown.", rec["explain"], max_tokens=120)
            if text:
                rec["explain_llm"] = text
                self.ledger.update(rec)
        except Exception:
            pass

    def _note(self, text, trig, level="--"):
        rec = {"id": self.ledger.new_id(), "t": round(self.now, 1), "level": level,
               "trigger": dict(trig, summary=text), "options": [], "gate": {"result": "AUTO", "why": []},
               "default": None, "chosen": None, "by": "robot" if level == "L0" else "auto", "status": "done",
               "explain": text}
        self.ledger.add(rec)
        self.log("note", text)

    def _escalate(self, rec, cands, gate, kind, timeout=True, proposal=None):
        for e in self.escalations.values():
            # a newer question replaces an older one of the same family; a pending goal change
            # is never thrown away by an unrelated repair question
            same = (e["kind"] == "goal") == (kind == "goal")
            if e["status"] == "open" and e["kind"] != "first" and same:
                e["status"] = "superseded"
                if e.get("proposal") and e["proposal"] is not proposal:
                    self._discard_proposal(e["proposal"])
                old = self.ledger.get(e["decision"])
                if old:
                    old["status"] = "superseded"
                    self.ledger.update(old)
        self._eid += 1
        eid = f"E-{self._eid}"
        options = {o["id"]: c for o, c in zip(rec["options"], cands)}
        extra = []
        if kind == "goal":
            extra.append({"id": "KEEP", "label": "keep the current mission"})
        if kind in ("hold", "infeasible") or gate["result"] == "HOLD" or not cands:
            extra.append({"id": "RTB", "label": "recall every robot to base"})
        self.escalations[eid] = {
            "id": eid, "decision": rec["id"], "kind": kind, "status": "open", "options": options,
            "extra": extra, "recommended": rec["default"] if cands else None,
            # on timeout the recommendation runs only if it can be undone; otherwise robots hold safe
            "default": rec["default"] if gate.get("reversible", True) and cands else None,
            "deadline_wall": self.wall + self.settings.ask_timeout_s if timeout and kind != "first" else None,
            "opened_t": self.now, "version_at": self.version, "proposal": proposal}
        return eid

    def answer(self, eid, option, by="operator"):
        with self.lock:
            esc = self.escalations.get(eid)
            if not esc or esc["status"] != "open":
                return False, "escalation is not open"
            rec = self.ledger.get(esc["decision"])
            if option in esc["options"]:
                esc["status"] = "answered"
                plan = esc["options"][option]
                if esc.get("proposal"):
                    self._accept_proposal(esc["proposal"])
                ext = getattr(plan, "meta_ext", 0)
                if ext:
                    self.policy["deadline_s"] += ext * 60
                    for t in self.open_tasks():
                        if self.tasks[t].priority == "LIFE":
                            self.tasks[t].due = (self.tasks[t].due or self.policy["deadline_s"]) + ext * 60
                    if self.spec:
                        self.spec.deadline_min += ext
                rec["chosen"], rec["by"], rec["status"] = option, by, "done"
                rec["explain"] = explain(rec, self.tasks)
                self.ledger.update(rec)
                if esc["kind"] == "first":
                    self.phase = "RUNNING"
                elif esc.get("proposal") or ext or self.version != esc["version_at"] or self.now - esc["opened_t"] > 2:
                    # the robots kept moving while the operator decided: re-plan the chosen option
                    # (same tasks kept or dropped) from the current state before sending it
                    fresh = self._candidates(level=2, variants=[(plan.label, list(plan.excluded))])
                    if fresh:
                        fresh[0].label = plan.label
                        plan = fresh[0]
                        rec["replanned_at_answer"] = round(self.now, 1)
                        rec["explain"] += (f" Re-planned from the robots' positions at T+{fmt_t(self.now)} "
                                           "when the answer came in.")
                        self.ledger.update(rec)
                self._commit(plan, rec, by=by)
                self.log("operator", f"{eid}: {by} chose option {option} ({plan.label})")
                return True, "ok"
            if option == "KEEP" and esc.get("proposal"):
                self._discard_proposal(esc["proposal"])
                esc["status"] = "answered"
                rec["chosen"], rec["by"], rec["status"] = "KEEP", by, "done"
                rec["explain"] = "Operator kept the current mission; the goal change was discarded."
                self.ledger.update(rec)
                return True, "ok"
            if option == "RTB":
                esc["status"] = "answered"
                rec["chosen"], rec["by"], rec["status"] = "RTB", by, "done"
                rec["explain"] = "Operator recalled every robot to base."
                self.ledger.update(rec)
                for t in self.open_tasks():
                    self.tasks[t].status = "CANCELLED"
                self._dispatch_all(lambda aid: [], "recall")
                return True, "ok"
            return False, "unknown option"

    def _timeouts(self):
        for eid, e in list(self.escalations.items()):
            if e["status"] != "open" or e["deadline_wall"] is None or self.wall < e["deadline_wall"]:
                continue
            if e["default"]:
                self.log("operator", f"{eid}: no answer in {self.settings.ask_timeout_s:.0f} s, running default {e['default']}")
                self.answer(eid, e["default"], by="timeout")
            else:
                e["status"] = "timeout-hold"
                self.log("operator", f"{eid}: no answer; change is not reversible, robots hold safe", "warn")
                rec = self.ledger.get(e["decision"])
                self._hold(rec)

    # ============================================================ dispatch
    def _commit(self, plan, rec, by):
        # re-plan from current state if the plan was built more than a few seconds ago
        self.plan = plan
        self.plan_wind = self.wm.wind
        for t in self.tasks.values():
            if t.open:
                t.eta = None
        for aid, ev in plan.evals.items():
            for tid, fin in ev.finish.items():
                self.tasks[tid].eta = round(fin, 1)
                if self.tasks[tid].status == "PENDING":
                    self.tasks[tid].assignee = aid
        for tid in plan.dropped + plan.excluded:
            if self.tasks[tid].open:
                self.tasks[tid].status = "DROPPED"
                self.tasks[tid].meta["dropped_by"] = rec["id"]
                if self.tasks[tid].type == "RELAY":
                    for k, v in list(self.relay_for.items()):
                        if v == tid:
                            del self.relay_for[k]
                            self.flags[("NO_RELAY", k)] = self.now + 300
        if rec.get("status") == "pending":
            rec["status"] = "done"
        if rec.get("chosen") is None:
            rec["chosen"], rec["by"] = "A", by
            rec["explain"] = explain(rec, self.tasks)
        self.ledger.update(rec)

        def stops_for(aid):
            ev = plan.evals.get(aid)
            if not ev:
                return []
            out = []
            next_task = [s.task for s in ev.stops if s.kind == "task"]
            for i, s in enumerate(ev.stops):
                if s.kind == "task":
                    t = self.tasks[s.task]
                    out.append({"id": t.id, "type": t.type, "pos": list(t.pos), "area": t.area, "kits": t.kits,
                                "kg": t.kg, "label": t.label()})
                elif s.kind == "base":
                    before = next((x.task for x in ev.stops[i + 1:] if x.kind == "task"), "end")
                    out.append({"id": f"B{self.version + 1}-{aid}-{before}", "type": "RTB", "pos": list(self.sc.base),
                                "purpose": s.purpose, "final": False, "label": f"base: {s.purpose}"})
                else:
                    out.append({"id": f"F{self.version + 1}-{aid}", "type": "RTB", "pos": list(self.sc.base),
                                "final": True, "label": "return to base"})
            return out

        self._dispatch_all(stops_for, rec["id"])

    def _hold(self, rec):
        def hold(aid):
            b = self.wm.agents[aid]
            if not b.alive or not self.dispatched[aid]:
                return self.dispatched[aid]
            return [{"id": f"H{self.version + 1}-{aid}", "type": "HOLD", "pos": list(b.predicted_pos(self.now)),
                     "label": "hold"}]
        self._dispatch_all(hold, rec["id"] if rec else "hold")
        self.log("dispatch", "HOLD: robots hover or park until the operator decides", "warn")

    def _dispatch_all(self, stops_for, reason):
        self.version += 1
        sent = []
        for aid in self.sc.fleet:
            if not self.wm.agents[aid].alive:
                continue
            stops = stops_for(aid)
            if [s["id"] for s in stops] == [s["id"] for s in self.dispatched[aid]] and self.dispatched[aid]:
                continue
            if not stops and not self.dispatched[aid] and self.version > 1:
                continue
            self.dispatched[aid] = stops
            self.assigned_version[aid] = self.version
            last = stops[-2]["pos"] if len(stops) > 1 else (stops[0]["pos"] if stops else self.wm.agents[aid].pos)
            rally = self.sc.rally_for(last)
            payload = {"version": self.version, "stops": stops, "rally": [rally[0], list(rally[1])],
                       "blocked": [list(e) for e in self.wm.blocked]}
            self.comms.down(aid, Message("BASE", aid, 2, "assign", 120 + 60 * len(stops), payload, self.now,
                                         key=("assign", aid)))
            sent.append(aid)
        self._route_lines()
        self.log("dispatch", f"plan v{self.version} ({reason}) sent to {', '.join(sent) if sent else 'nobody (no change)'}")

    def _route_lines(self):
        lines = {}
        for aid, stops in self.dispatched.items():
            b = self.wm.agents[aid]
            if not b.alive or not stops:
                continue
            spec = self.sc.fleet[aid]
            pts = [b.predicted_pos(self.now)]
            node = b.last_node
            for s in stops:
                goal = tuple(self.sc.base if s["type"] == "RTB" else s["pos"])
                if s["type"] == "SURVEY" and s.get("area"):
                    goal = self.sc.sector_center(s["area"])
                if spec.flies:
                    pts.append(goal)
                else:
                    r = self.sc.rover_route(pts[-1], node, goal, self.wm.blocked)
                    if r:
                        pts += r[1][1:]
                        node = r[2][-1]
                    else:
                        pts.append(goal)
            lines[aid] = [[round(p[0], 1), round(p[1], 1)] for p in pts]
        self.route_lines = lines

    # ======================================================== goal change
    def apply_goal_change(self, result, text):
        """Operator changed the goal mid-mission (L3). `result` is a CompileResult for the amended spec."""
        with self.lock:
            if result.spec is None:
                self._note("Goal change rejected by the validator: " + "; ".join(result.errors),
                           {"type": "GOAL_CHANGE_REJECTED"}, level="L3")
                return None
            templates, policy = expand(result.spec, self.sc)
            by_key = {}
            for tid, t in self.tasks.items():
                if t.origin.startswith("objective:") and t.status != "PROPOSED":
                    k = (t.type, t.area or t.place) + ((int(t.meta.get("n", 1)),) if t.type == "TRANSPORT" else ())
                    by_key.setdefault(k, tid)
            wanted, add, dues = set(), [], {}
            n_tr = 0
            for t in templates:
                key = t.pop("key")
                k = (t["type"], t.get("area") or t.get("place"))
                if t["type"] == "TRANSPORT":
                    n_tr += 1
                    k = k + (n_tr,)
                if k in by_key:
                    wanted.add(by_key[k])
                    dues[by_key[k]] = t["due"]
                else:
                    meta = {"n": n_tr} if t["type"] == "TRANSPORT" else {}
                    tid = self.new_task(origin=f"objective:{key[0]}", meta=meta, status="PROPOSED", **t)
                    add.append(tid)
                    wanted.add(tid)
            cancel = [tid for tid, t in self.tasks.items()
                      if t.origin.startswith("objective:") and t.open and tid not in wanted]
            proposal = {"spec": result.spec, "policy": policy, "add": add, "cancel": cancel, "dues": dues,
                        "text": text}
            src = "LLM " + result.model if result.source == "llm" else "rule-based compiler"
            trig = {"type": "GOAL_CHANGE",
                    "summary": f'Operator changed the goal: "{text[:120]}" (compiled by {src})'}
            # nothing changes for the robots until the operator approves the new goal
            with self._proposed(proposal):
                return self.replan(3, [trig], goal_change=True, proposal=proposal)

    @contextlib.contextmanager
    def _proposed(self, p):
        """Temporarily apply a proposed goal change so the planner and gate can evaluate it."""
        saved = {t: (self.tasks[t].status, self.tasks[t].due) for t in p["add"] + p["cancel"] + list(p["dues"])}
        saved_policy = self.policy
        try:
            for t in p["add"]:
                self.tasks[t].status = "PENDING"
            for t in p["cancel"]:
                self.tasks[t].status = "CANCELLED"
            for t, d in p["dues"].items():
                self.tasks[t].due = d
            self.policy = p["policy"]
            yield
        finally:
            for t, (st, due) in saved.items():
                self.tasks[t].status, self.tasks[t].due = st, due
            self.policy = saved_policy

    def _accept_proposal(self, p):
        for t in p["add"]:
            if self.tasks[t].status == "PROPOSED":
                self.tasks[t].status = "PENDING"
        for t in p["cancel"]:
            if self.tasks[t].open:
                self.tasks[t].status = "CANCELLED"
        for t, d in p["dues"].items():
            self.tasks[t].due = d
        self.spec, self.policy = p["spec"], p["policy"]
        self.log("operator", "goal change approved: " + p["text"][:100])

    def _discard_proposal(self, p):
        for t in p["add"]:
            if self.tasks[t].status == "PROPOSED":
                self.tasks[t].status = "DISCARDED"
        self.log("operator", "goal change discarded: " + p["text"][:100])

    # ============================================================== done?
    def _check_done(self):
        if self.phase != "RUNNING" or self.open_tasks():
            return
        drones_home = all(not b.airborne or not b.alive for aid, b in self.wm.agents.items()
                          if self.sc.fleet[aid].flies)
        if drones_home:
            self.phase = "DONE"
            self.log("mission", f"Mission complete at T+{fmt_t(self.now)}")
            self._note(f"Mission complete at T+{fmt_t(self.now)}. After-action report is ready.",
                       {"type": "MISSION_DONE"})

    # ============================================================== client
    def to_client(self):
        with self.lock:
            esc = []
            for e in self.escalations.values():
                if e["status"] != "open":
                    continue
                rec = self.ledger.get(e["decision"])
                left = None if e["deadline_wall"] is None else max(0, round(e["deadline_wall"] - self.wall))
                esc.append({"id": e["id"], "kind": e["kind"], "decision": rec, "default": e["default"],
                            "recommended": e["recommended"], "extra": e["extra"], "seconds_left": left})
            return {
                "phase": self.phase, "version": self.version, "t": round(self.now, 1),
                "spec": self.spec.model_dump() if self.spec else None, "compile": self.compile_info,
                "policy": self.policy,
                "tasks": [t.to_client() for t in sorted(self.tasks.values(),
                                                        key=lambda t: (not t.open, PRIORITY_RANK[t.priority], t.id))],
                "routes": self.route_lines,
                "dispatched": {a: [s["id"] for s in v] for a, v in self.dispatched.items()},
                "assigned_version": self.assigned_version,
                "escalations": esc,
                "decisions": self.ledger.recent(40),
                "events": self.events[-60:],
                "belief": self.wm.to_client(self.now),
                "relay_points": [t.pos for t in self.tasks.values() if t.type == "RELAY" and t.open],
            }

    def report(self, world=None, comms=None):
        """After-action report: coverage, survivors served, decisions with reasons, lowest battery."""
        with self.lock:
            done = [t for t in self.tasks.values() if t.status == "DONE"]
            rep = {
                "mission": self.spec.model_dump() if self.spec else None,
                "phase": self.phase, "duration_s": round(self.now, 1),
                "tasks": {s: sum(1 for t in self.tasks.values() if t.status == s)
                          for s in ("DONE", "DROPPED", "CANCELLED", "PENDING", "ACTIVE")},
                "surveyed": sorted(t.area for t in done if t.type == "SURVEY"),
                "kits_delivered": sum(t.kits for t in done if t.type == "DELIVER"),
                "survivors_served_reported": sum(t.meta.get("served", 0) for t in done if t.type == "DELIVER"),
                "supplies_kg": sum(t.kg for t in done if t.type == "TRANSPORT"),
                "decisions": [{"id": r["id"], "t": fmt_t(r["t"]), "level": r["level"],
                               "gate": r["gate"]["result"], "by": r.get("by"), "chosen": r.get("chosen"),
                               "trigger": r["trigger"].get("summary", ""), "explain": r.get("explain_llm") or r["explain"]}
                              for r in self.ledger.records],
                "compile": self.compile_info,
            }
            if world is not None:
                rep["truth"] = {"survivors": [{"id": s["id"], "count": s["count"], "served": s["served"]}
                                              for s in world.survivors],
                                "lowest_battery": {a: round(b.battery, 1) for a, b in world.bodies.items()
                                                   if b.spec.flies},
                                "robots_lost": [a for a, b in world.bodies.items() if not b.alive]}
            if comms is not None:
                rep["comms"] = comms.to_client()["stats"]
            return rep
