"""The software that runs on each robot.

Each robot gets only its own task list plus a rally point from the base. It
runs that list through a behavior tree, and handles L0 repairs on its own
without waiting for the base:

  - battery near the floor  -> return to base now
  - cut off from the base   -> finish the task underway, then go to the rally point
  - rover stuck             -> mark the road blocked, re-route, report it
  - partitioned with peers  -> auction a silent peer's tasks (lowest cost wins)

Radio traffic follows the value-of-information rules: position updates are
sent only when the base's dead-reckoning guess drifts more than 5 m.
"""
import math

from ..planner import HARD_FLOOR, LOAD_S, SERVICE_S, SWAP_S, wind_factor
from ..scenario import dist, edge_key
from .bt import FAILURE, RUNNING, SUCCESS, Action, Condition, Selector, Sequence
from .comms import Message

DR_TOL_M = 5.0
STATE_MAX_GAP_S = 15.0
CUTOFF_AFTER_S = 8.0
STUCK_AFTER_S = 20.0


class Robot:
    def __init__(self, sc, world, comms, aid):
        self.sc, self.world, self.comms, self.id = sc, world, comms, aid
        self.body = world.bodies[aid]
        self.spec = self.body.spec
        self.version = 0
        self.stops = []
        self.idx = 0
        self.phase = "start"
        self.svc_left = 0.0
        self.route = None                    # rover waypoints
        self.route_nodes = []
        self.route_i = 0
        self.rally = sc.rally_for(self.body.pos)
        self.blocked = set()
        self.done = set()
        self.released = set()
        self.branch = "idle"
        self.status_text = "waiting for plan"
        self.cutoff = False
        self.cutoff_idx = None
        self.not_c_since = None
        self.l0_battery = False
        self.stuck_since = None
        self.stuck_pos = None
        self.last_hb = -99.0
        self.dr = None
        self.progress_sent = {}
        self.auctioned = set()
        self.safety_sent = set()
        self.bt = Selector("root", [
            Sequence("L0 battery floor", [Condition("battery low", self._battery_low),
                                          Action("return now", self._act_emergency_rtb, "L0 battery floor")]),
            Sequence("L0 cut off", [Condition("rally?", self._should_rally),
                                    Action("go to rally", self._act_rally, "L0 cut off: rally")]),
            Sequence("run plan", [Condition("has stop", lambda: self.idx < len(self.stops)),
                                  Action("run stop", self._act_stop, "run plan")]),
            Action("idle", self._act_idle, "idle"),
        ])

    # ------------------------------------------------------------ helpers
    @property
    def t(self):
        return self.world.t

    def send(self, cls, kind, size, payload, key=None, voi=1.0):
        self.comms.up(self.id, Message(self.id, "BASE", cls, kind, size, dict(payload, agent=self.id, t=self.t),
                                       self.t, voi, key))

    def _stop(self):
        return self.stops[self.idx] if self.idx < len(self.stops) else None

    def _drain_rate(self):
        return 100.0 / self.spec.endurance_s * (wind_factor(self.world.wind_speed()) if self.spec.flies else 1.0)

    # ---------------------------------------------------------- messages in
    def _read_inbox(self):
        inbox = self.comms.robot_inbox[self.id]
        while inbox:
            m = inbox.pop(0)
            p = m.payload
            if m.kind == "assign" and p["version"] > self.version:
                cur = self._stop()
                self.version = p["version"]
                self.stops = [s for s in p["stops"] if s["id"] not in self.done]
                self.blocked |= {tuple(e) for e in p.get("blocked", [])}
                self.rally = (p["rally"][0], tuple(p["rally"][1]))
                keep = cur is not None and self.stops and self.stops[0]["id"] == cur["id"]
                self.idx = 0
                if not keep:
                    self.phase = "start"
                    self.route = None
                self.send(2, "ack", 64, {"version": self.version})
                self.comms.peer_share(self.id, {"version": self.version, "stops": self.stops,
                                                "pos": tuple(self.body.pos)})
            elif m.kind == "release":
                self.released.add(p["task"])

    # ----------------------------------------------------------- BT parts
    def _battery_low(self):
        b = self.body
        if not self.spec.flies:
            return False
        if self.l0_battery:
            return True
        cur = self._stop()
        if cur and cur["type"] == "RTB":
            return False
        need = dist(b.pos, self.sc.base) / self.spec.speed * self._drain_rate()
        if b.airborne and b.battery - need <= HARD_FLOOR + 3:
            self.l0_battery = True
            if ("battery", self.version) not in self.safety_sent:
                self.safety_sent.add(("battery", self.version))
                self.send(0, "safety", 64, {"event": "BATTERY_CRITICAL", "battery": round(b.battery, 1),
                                            "action": "returning to base (L0)"})
            return True
        return False

    def _act_emergency_rtb(self):
        b = self.body
        self.status_text = "L0: battery floor, returning to base"
        if self.world.fly_to(b, self.sc.base, self.spec.speed, self._dt):
            self.world.land(b)
            self.svc_left = SWAP_S
            self.l0_battery = False
            self.phase = "swap_after_l0"
        return RUNNING

    def _should_rally(self):
        cur = self._stop()
        if not self.cutoff or self.cutoff_idx is None:
            return False
        if cur is not None and (self.idx <= self.cutoff_idx or cur["type"] in ("RTB", "RELAY")):
            return False
        return True

    def _act_rally(self):
        rid, rp = self.rally
        self.status_text = f"L0: cut off, holding at rally {rid}"
        if ("rally", self.version) not in self.safety_sent:
            self.safety_sent.add(("rally", self.version))
            self.send(0, "safety", 64, {"event": "L0_RALLY", "rally": rid,
                                        "action": f"finished task underway, going to rally {rid}"})
        if self.spec.flies:
            if self.world.fly_to(self.body, rp, self.spec.speed, self._dt):
                self.world.hover(self.body, 0)
        else:
            self._drive(rp)
        return RUNNING

    def _act_idle(self):
        b = self.body
        if self.spec.flies and b.airborne:
            self.status_text = "plan finished, returning to base"
            if self.world.fly_to(b, self.sc.base, self.spec.speed, self._dt):
                self.world.land(b)
        else:
            self.status_text = "parked at base" if dist(b.pos, self.sc.base) < 40 else "parked, waiting"
            if self.spec.flies:
                self.world.land(b)
            else:
                self.world.park(b, self._dt)
        return SUCCESS

    # ------------------------------------------------------------- stops
    def _finish_stop(self, extra=None):
        s = self._stop()
        self.done.add(s["id"])
        self.send(2, "task", 200, dict({"event": "DONE", "task": s["id"], "type": s["type"]}, **(extra or {})))
        self.idx += 1
        self.phase = "start"
        self.route = None

    def _act_stop(self):
        s = self._stop()
        b = self.body
        dt = self._dt
        if s["id"] in self.done:
            self.idx += 1
            self.phase = "start"
            return RUNNING
        if s["id"] in self.released:
            self._finish_stop()
            return RUNNING
        if self.phase == "swap_after_l0":
            self.status_text = "L0: swapping battery"
            self.svc_left -= dt
            if self.svc_left <= 0:
                b.battery = 100.0
                b.kits = self.spec.kits
                self.phase = "start"
            return RUNNING
        if self.phase == "start":
            self.phase = "goto"
            self.svc_left = 0.0
            if s["type"] not in ("RTB", "HOLD"):
                self.send(2, "task", 200, {"event": "STARTED", "task": s["id"], "type": s["type"]})
            if s["type"] == "SURVEY":
                st = self.world.survey_state(s["area"], b.pos)
                self.target, _ = self.world.point_along(st["path"], st["done"])
            elif s["type"] == "RTB":
                self.target = self.sc.base
            else:
                self.target = tuple(s["pos"])
        t = s["type"]
        if self.phase == "goto":
            self.status_text = f"to {s.get('label', t)}"
            arrived = self.world.fly_to(b, self.target, self.spec.speed, dt) if self.spec.flies \
                else self._drive(self.target)
            if arrived:
                self.phase = "service"
                if t == "RTB":
                    if s.get("final"):
                        self.svc_left = 0.0
                    else:
                        self.svc_left = SWAP_S if self.spec.flies else LOAD_S
                    if self.spec.flies:
                        self.world.land(b)
                else:
                    self.svc_left = {"INSPECT": self.sc.sensing.get("inspect_s", 20)}.get(t, SERVICE_S.get(t, 0.0))
            return RUNNING
        # service
        if t == "SURVEY":
            st = self.world.survey_state(s["area"], b.pos)
            step = self.spec.survey_speed * dt
            st["done"] = min(st["len"], st["done"] + step)
            tgt, _ = self.world.point_along(st["path"], st["done"])
            self.world.fly_to(b, tgt, self.spec.survey_speed * 1.5, dt)
            frac = st["done"] / st["len"]
            self.status_text = f"surveying {s['area']} {frac * 100:.0f}%"
            for q in (0.25, 0.5, 0.75):
                if frac >= q and self.progress_sent.get(s["id"], 0) < q:
                    self.progress_sent[s["id"]] = q
                    self.send(2, "task", 200, {"event": "PROGRESS", "task": s["id"], "progress": q})
            if frac >= 0.999:
                self._finish_stop({"progress": 1.0})
            return RUNNING
        if t == "RELAY":
            self.world.hover(b, dt)
            self.status_text = "relaying radio traffic"
            if s["id"] in self.released:
                self._finish_stop()
            return RUNNING
        if t == "HOLD":
            self.status_text = "holding (waiting for operator)"
            if self.spec.flies:
                self.world.hover(b, dt)
            else:
                self.world.park(b, dt)
            return RUNNING
        if t == "RTB" and s.get("final"):
            self.world.land(b)
            self.status_text = "landed at base" if self.spec.flies else "parked at base"
            self._finish_stop()
            return RUNNING
        self.svc_left -= dt
        if self.spec.flies and b.airborne:
            self.world.hover(b, dt)
        elif not self.spec.flies:
            self.world.park(b, dt)
        self.status_text = {"DELIVER": "dropping kits", "INSPECT": "second look", "TRANSPORT": "unloading",
                            "RTB": "swap battery / reload"}.get(t, t.lower())
        if self.svc_left > 0:
            return RUNNING
        if t == "DELIVER":
            served = self.world.drop_kits(b, s["pos"], s.get("kits", 1))
            self._finish_stop({"served": served, "kits_left": b.kits})
        elif t == "INSPECT":
            res = self.world.inspect(s["pos"])
            self._finish_stop({"inspect": res})
        elif t == "TRANSPORT":
            b.kg = max(0.0, b.kg - s.get("kg", 0))
            self._finish_stop()
        elif t == "RTB":
            if self.spec.flies:
                b.battery = 100.0
            b.kits = self.spec.kits
            if "load" in s.get("purpose", "") or not self.spec.flies:
                b.kg = self.spec.kg
            self._finish_stop()
        else:
            self._finish_stop()
        return RUNNING

    # ------------------------------------------------------------- rovers
    def _drive(self, goal):
        b = self.body
        if self.route is None or dist(self.route[-1], goal) > 1:
            r = self.sc.rover_route(tuple(b.pos), b.last_node, goal, self.blocked)
            if r is None:
                self.status_text = "no road route; waiting"
                self.world.park(b, self._dt)
                return False
            self.route, self.route_nodes = r[1], r[2]
            self.route_i = 1
            self.stuck_since, self.stuck_pos = None, None
        while self.route_i < len(self.route) and dist(b.pos, self.route[self.route_i]) < 1.0:
            self._passed(self.route[self.route_i])
            self.route_i += 1
        if self.route_i >= len(self.route):
            return True
        res = self.world.drive_to(b, self.route[self.route_i], self.spec.speed, self._dt)
        if res == "arrived":
            self._passed(self.route[self.route_i])
            self.route_i += 1
            return self.route_i >= len(self.route)
        if res == "blocked":
            if self.stuck_since is None:
                self.stuck_since = self.t
            self.status_text = f"blocked for {self.t - self.stuck_since:.0f} s"
            if self.t - self.stuck_since >= STUCK_AFTER_S:
                self._report_blocked()
        else:
            self.stuck_since = None
        return False

    def _passed(self, wp):
        for n, p in self.sc.node_pos.items():
            if dist(p, wp) < 1.0:
                self.body.last_node = n

    def _report_blocked(self):
        b = self.body
        nxt = self.route[self.route_i]
        nxt_node = next((n for n, p in self.sc.node_pos.items() if dist(p, nxt) < 1.0), None)
        edge = edge_key(b.last_node, nxt_node) if b.last_node and nxt_node else self.sc.edge_near(b.pos, 40)
        if edge:
            self.blocked.add(edge)
            self.send(2, "task", 200, {"event": "BLOCKED", "task": (self._stop() or {}).get("id"),
                                       "edge": list(edge), "pos": [round(b.pos[0]), round(b.pos[1])],
                                       "action": "marked road blocked, re-routing (L0)"})
        self.route = None
        self.stuck_since = None

    # ------------------------------------------------------- mesh auction
    def _auction(self):
        # only while cut off from the base; a lone survivor of a partition still inherits its peer's work
        if self.comms.tier[self.id] not in ("P", "I"):
            return
        for peer, info in list(self.comms.peer_box[self.id].items()):
            if peer in self.auctioned:
                continue
            seen = self.comms.peer_last[self.id].get(peer)
            if seen is None:
                continue
            last_t, last_pos, last_h = seen
            if self.t - last_t < 5 or self.t - last_t > 120:
                continue
            # silence only means failure if I should still be able to hear the peer from here
            # (behind a ridge or out of range, silence means nothing)
            me_h = self.world.abs_alt(self.body)
            margin = self.sc.rssi(tuple(self.body.pos), me_h, last_pos, last_h) - self.sc.radio["usable_dbm"]
            if margin < 6:
                continue
            members = [m for m in self.comms.component[self.id] if m != "BASE"]
            self.auctioned.add(peer)
            for s in info["stops"]:
                if s["type"] in ("RTB", "HOLD") or s["id"] in self.done:
                    continue
                need = {"SURVEY": "camera", "INSPECT": "camera", "DELIVER": "deliver",
                        "TRANSPORT": "transport", "RELAY": "fly"}.get(s["type"])
                bids = []
                for m in members:
                    sp = self.world.bodies[m].spec
                    if need in sp.caps and self.world.bodies[m].alive:
                        bids.append((dist(self.world.bodies[m].pos, s["pos"]) / sp.speed, m))
                if not bids:
                    continue
                cost, winner = min(bids)
                if winner == self.id:
                    insert_at = len(self.stops)
                    if self.stops and self.stops[-1]["type"] == "RTB":
                        insert_at -= 1
                    self.stops.insert(insert_at, dict(s))
                    self.send(2, "auction", 120, {"event": "AUCTION_WON", "task": s["id"], "from": peer,
                                                  "bid_s": round(cost)})

    # ------------------------------------------------------------ tick
    def tick(self, dt):
        self._dt = dt
        b = self.body
        if not b.alive:
            return
        self._read_inbox()
        tier = self.comms.tier[self.id]
        if tier != "C":
            self.not_c_since = self.not_c_since if self.not_c_since is not None else self.t
            if not self.cutoff and self.t - self.not_c_since >= CUTOFF_AFTER_S:
                self.cutoff = True
                self.cutoff_idx = self.idx
        else:
            self.not_c_since = None
            self.cutoff = False
            self.cutoff_idx = None
        self._auction()
        self.bt.tick(self)
        # detections
        for d in self.world.sense(b, dt):
            self.send(1, "detection", 8000, {"pos": [round(d["pos"][0], 1), round(d["pos"][1], 1)],
                                             "count": d["count"], "conf": d["conf"]}, voi=50.0 * d["count"])
        # heartbeat + state with dead reckoning
        if self.t - self.last_hb >= 1.0:
            self.last_hb = self.t
            self.send(2, "hb", 16, {"version": self.version}, key=("hb", self.id))
        self._maybe_state()

    def _maybe_state(self):
        b = self.body
        cur = self._stop()
        mode = self.branch
        if self.dr is not None:
            dtp = self.t - self.dr["t"]
            guess = (self.dr["pos"][0] + self.dr["vel"][0] * dtp, self.dr["pos"][1] + self.dr["vel"][1] * dtp)
            err = dist(guess, b.pos)
            if (err <= DR_TOL_M and abs(b.battery - self.dr["batt"]) <= 2.0 and mode == self.dr["mode"]
                    and self.t - self.dr["t"] < STATE_MAX_GAP_S and b.kits == self.dr["kits"]):
                if float(self.t).is_integer():          # a 1 Hz position report we did not need to send
                    self.comms.stats["suppressed_state"] += 1
                return
        self.dr = {"pos": tuple(b.pos), "vel": b.vel, "t": self.t, "batt": b.battery, "mode": mode, "kits": b.kits}
        payload = {"pos": [round(b.pos[0], 1), round(b.pos[1], 1)], "vel": [round(b.vel[0], 2), round(b.vel[1], 2)],
                   "battery": round(b.battery, 1), "kits": b.kits, "kg": b.kg, "airborne": b.airborne,
                   "mode": mode, "status": self.status_text, "last_node": b.last_node,
                   "stop": cur["id"] if cur else None, "version": self.version, "tier": self.comms.tier[self.id]}
        if self.spec.flies and b.airborne:
            payload["wind"] = round(self.world.wind_speed() + self.world.rng.gauss(0, 0.3), 1)
        self.send(3, "state", 48, payload, key=("state", self.id))

    def to_client(self):
        cur = self._stop()
        return {"branch": self.branch, "status": self.status_text, "version": self.version,
                "stop": cur["id"] if cur else None, "cutoff": self.cutoff,
                "stops": [s["id"] for s in self.stops[self.idx:]]}
