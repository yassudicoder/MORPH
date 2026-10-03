"""Ground truth: where every robot really is, real batteries, the real debris.

This is the part Gazebo replaces. TOWER's brain never reads it directly.
"""
import math
import random

from ..planner import wind_factor
from ..scenario import dist

ARRIVE_M = 1.5
DEBRIS_STOP_M = 12.0


class Body:
    def __init__(self, spec, sc):
        self.id = spec.id
        self.spec = spec
        self.pos = [float(spec.spawn[0]), float(spec.spawn[1])]
        self.vel = (0.0, 0.0)
        self.airborne = False
        self.agl = 0.0
        self.battery = 100.0
        self.kits = spec.kits
        self.kg = 0.0
        self.alive = True
        self.last_node = sc.nearest_node(spec.spawn) if not spec.flies else None
        self.fault = None
        self.moved = 0.0


class SimWorld:
    def __init__(self, sc, seed=7):
        self.sc = sc
        self.rng = random.Random(seed)
        self.t = 0.0
        self.bodies = {a.id: Body(a, sc) for a in sc.fleet.values()}
        truth = sc.raw.get("truth", {})
        self.survivors = [dict(id=s["id"], pos=tuple(s["pos"]), count=s["count"], served=0, seen_by=set())
                          for s in truth.get("survivors", [])]
        self.decoys = [dict(id=d["id"], pos=tuple(d["pos"]), what=d.get("what", ""), seen_by=set())
                       for d in truth.get("decoys", [])]
        w = truth.get("wind", [4, 0])
        self.wind = [float(w[0]), float(w[1])]
        self.wind_target = list(self.wind)
        self.debris = []
        self.survey = {}            # sector -> {"path": [...], "done": metres covered, "len": total}
        self.log = []

    # --------------------------------------------------------------- env
    def wind_speed(self):
        return math.hypot(*self.wind)

    def step_env(self, dt):
        for i in range(2):
            d = self.wind_target[i] - self.wind[i]
            self.wind[i] += max(-1.0 * dt, min(1.0 * dt, d))
        self.t += dt

    def abs_alt(self, b):
        if not b.spec.flies:
            return self.sc.abs_alt(b.pos, self.sc.radio["rover_ant_m"])
        return self.sc.abs_alt(b.pos, b.agl if b.airborne else 0.5)

    # ----------------------------------------------------------- physics
    def _drain(self, b, dt, moving):
        if not b.alive:
            return
        if b.spec.flies:
            if b.airborne:
                b.battery -= dt * 100.0 / b.spec.endurance_s * wind_factor(self.wind_speed())
        elif moving:
            b.battery -= dt * 100.0 / b.spec.endurance_s
        if b.battery <= 0:
            b.battery = 0.0
            if b.spec.flies and b.airborne:
                self.kill(b.id, "battery exhausted")

    def fly_to(self, b, target, speed, dt):
        """Move a drone straight toward target. Returns True on arrival."""
        if not b.alive:
            return False
        b.airborne = True
        b.agl = self.sc.radio["drone_alt_m"]
        dx, dy = target[0] - b.pos[0], target[1] - b.pos[1]
        d = math.hypot(dx, dy)
        step = speed * dt
        if d <= max(ARRIVE_M, step):
            b.pos = [float(target[0]), float(target[1])]
            b.vel = (0.0, 0.0)
            b.moved += d
            self._drain(b, dt, True)
            return True
        b.pos[0] += dx / d * step
        b.pos[1] += dy / d * step
        b.vel = (dx / d * speed, dy / d * speed)
        b.moved += step
        self._drain(b, dt, True)
        return False

    def hover(self, b, dt):
        b.vel = (0.0, 0.0)
        self._drain(b, dt, False)

    def land(self, b):
        b.airborne = False
        b.agl = 0.0
        b.vel = (0.0, 0.0)

    def drive_to(self, b, target, speed, dt):
        """Move a rover toward the next waypoint. Returns 'arrived' | 'moving' | 'blocked'."""
        if not b.alive:
            return "blocked"
        dx, dy = target[0] - b.pos[0], target[1] - b.pos[1]
        d = math.hypot(dx, dy)
        step = min(d, speed * dt)
        if d > 1e-6:
            nx_, ny_ = b.pos[0] + dx / d * step, b.pos[1] + dy / d * step
            ahead = (b.pos[0] + dx / d * min(d, DEBRIS_STOP_M), b.pos[1] + dy / d * min(d, DEBRIS_STOP_M))
            for deb in self.debris:
                if dist(ahead, deb["pos"]) < DEBRIS_STOP_M or dist((nx_, ny_), deb["pos"]) < DEBRIS_STOP_M:
                    b.vel = (0.0, 0.0)
                    self._drain(b, dt, False)
                    return "blocked"
            b.pos = [nx_, ny_]
            b.vel = (dx / d * speed, dy / d * speed) if step < d else (0.0, 0.0)
            b.moved += step
        self._drain(b, dt, True)
        if d - step <= ARRIVE_M:
            b.vel = (0.0, 0.0)
            return "arrived"
        return "moving"

    def park(self, b, dt):
        b.vel = (0.0, 0.0)
        self._drain(b, dt, False)

    # ------------------------------------------------------------ sensing
    def sense(self, b, dt):
        """Thermal camera: what a drone sees this step (like Gazebo's logical camera + a noisy detector)."""
        if not (b.alive and b.airborne and "camera" in b.spec.caps):
            return []
        fp = self.sc.sensing["footprint_m"]
        p_det = 1 - (1 - self.sc.sensing.get("detect_p_per_s", 0.7)) ** dt
        out = []
        for s in self.survivors:
            left = s["count"] - s["served"]
            if left > 0 and b.id not in s["seen_by"] and dist(b.pos, s["pos"]) <= fp and self.rng.random() < p_det:
                s["seen_by"].add(b.id)
                jitter = (self.rng.uniform(-4, 4), self.rng.uniform(-4, 4))
                out.append({"pos": (s["pos"][0] + jitter[0], s["pos"][1] + jitter[1]), "count": left,
                            "conf": round(self.rng.uniform(0.74, 0.95), 2), "truth": s["id"]})
        for x in self.decoys:
            if b.id not in x["seen_by"] and dist(b.pos, x["pos"]) <= fp and self.rng.random() < p_det * 0.7:
                x["seen_by"].add(b.id)
                out.append({"pos": x["pos"], "count": 1, "conf": round(self.rng.uniform(0.45, 0.65), 2),
                            "truth": x["id"]})
        return out

    def inspect(self, pos):
        for s in self.survivors:
            left = s["count"] - s["served"]
            if left > 0 and dist(pos, s["pos"]) <= 35:
                return {"confirmed": True, "count": left, "pos": s["pos"], "truth": s["id"]}
        return {"confirmed": False, "count": 0, "pos": pos}

    def drop_kits(self, b, pos, n):
        n = min(n, b.kits)
        b.kits -= n
        served = 0
        for s in self.survivors:
            if n <= 0:
                break
            left = s["count"] - s["served"]
            if left > 0 and dist(pos, s["pos"]) <= 35:
                k = min(left, n)
                s["served"] += k
                served += k
                n -= k
        return served

    # --------------------------------------------------------- survey state
    def survey_state(self, sector, from_pos):
        """Shared coverage progress, so a second robot resumes where the first one stopped."""
        st = self.survey.get(sector)
        if st is None:
            path = self.sc.survey_path(sector, from_pos)
            L = sum(dist(path[i], path[i + 1]) for i in range(len(path) - 1))
            st = {"path": path, "done": 0.0, "len": L}
            self.survey[sector] = st
        return st

    @staticmethod
    def point_along(path, s):
        for i in range(len(path) - 1):
            seg = dist(path[i], path[i + 1])
            if s <= seg:
                f = s / seg if seg else 0
                return (path[i][0] + (path[i + 1][0] - path[i][0]) * f,
                        path[i][1] + (path[i + 1][1] - path[i][1]) * f), i
            s -= seg
        return path[-1], len(path) - 2

    # ---------------------------------------------------------- injections
    def kill(self, aid, why="failure"):
        b = self.bodies[aid]
        if b.alive:
            b.alive = False
            b.fault = why
            b.vel = (0.0, 0.0)
            self.log.append((self.t, f"{aid} lost: {why}"))

    def add_debris(self, pos):
        edge = self.sc.edge_near(pos)
        self.debris.append({"pos": tuple(pos), "edge": edge})
        self.log.append((self.t, f"debris at ({pos[0]:.0f},{pos[1]:.0f}) on edge {edge}"))
        return edge

    def set_wind(self, speed, direction_deg=0.0):
        r = math.radians(direction_deg)
        self.wind_target = [speed * math.cos(r), speed * math.sin(r)]

    def battery_fault(self, aid, drop):
        b = self.bodies[aid]
        b.battery = max(1.0, b.battery - drop)

    def add_survivor(self, pos, count=1):
        sid = f"S{len(self.survivors) + 1}"
        self.survivors.append(dict(id=sid, pos=tuple(pos), count=count, served=0, seen_by=set()))
        return sid

    # --------------------------------------------------------------- truth
    def to_client(self):
        return {
            "agents": {a: {"pos": [round(b.pos[0], 1), round(b.pos[1], 1)], "battery": round(b.battery, 1),
                           "alive": b.alive, "airborne": b.airborne, "kits": b.kits, "kg": b.kg}
                       for a, b in self.bodies.items()},
            "survivors": [{"id": s["id"], "pos": s["pos"], "count": s["count"], "served": s["served"]}
                          for s in self.survivors],
            "decoys": [{"id": d["id"], "pos": d["pos"], "what": d["what"]} for d in self.decoys],
            "debris": [{"pos": d["pos"], "edge": d["edge"]} for d in self.debris],
            "wind": round(self.wind_speed(), 1),
            "survey": {k: round(v["done"] / v["len"], 2) for k, v in self.survey.items()},
        }
