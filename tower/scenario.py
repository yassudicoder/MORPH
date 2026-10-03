"""Scenario loader and the geometry every other module shares.

One JSON file describes the map, fleet, radio and hidden truth. The planner,
the risk engine, the radio emulator and the simulator all read the same file,
so the planner's map and the simulator can never disagree.
"""
import json
import math
from dataclasses import dataclass, field

import networkx as nx
import numpy as np


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def point_in_poly(p, poly):
    x, y = p
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _ccw(a, b, c):
    return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(a, b, c, d):
    return _ccw(a, c, d) != _ccw(b, c, d) and _ccw(a, b, c) != _ccw(a, b, d)


def point_segment_dist(p, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return dist(p, (ax + t * dx, ay + t * dy))


def path_length(pts):
    return sum(dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def edge_key(u, v):
    return tuple(sorted((u, v)))


@dataclass
class AgentSpec:
    id: str
    kind: str                 # quad | hex | ugv
    caps: frozenset
    speed: float              # transit speed, m/s
    survey_speed: float
    endurance_s: float        # full battery lasts this long while moving
    kits: int
    kg: float
    radio_gain_db: float
    spawn: tuple
    label: str = ""

    @property
    def flies(self):
        return "fly" in self.caps


@dataclass
class Scenario:
    raw: dict
    name: str
    width: float
    height: float
    base: tuple
    sectors: dict
    places: dict
    no_fly: list
    river: list
    ridge: dict
    rally_points: dict
    radio: dict
    sensing: dict
    fleet: dict
    graph: nx.Graph = field(repr=False, default=None)
    node_pos: dict = field(default_factory=dict)
    _sp_cache: dict = field(default_factory=dict, repr=False)
    _relay_cache: dict = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        fleet = {}
        for a in raw["fleet"]:
            fleet[a["id"]] = AgentSpec(
                id=a["id"], kind=a["kind"], caps=frozenset(a["caps"]), speed=a["speed"],
                survey_speed=a.get("survey_speed", a["speed"]), endurance_s=a["endurance_s"],
                kits=a.get("kits", 0), kg=a.get("kg", 0), radio_gain_db=a.get("radio_gain_db", 0),
                spawn=tuple(a["spawn"]), label=a.get("label", ""))
        sc = cls(
            raw=raw, name=raw["name"], width=raw["world"]["width"], height=raw["world"]["height"],
            base=tuple(raw["base"]["pos"]), sectors={k: tuple(v["rect"]) for k, v in raw["sectors"].items()},
            places={k: {"pos": tuple(v["pos"]), "aliases": v.get("aliases", [])} for k, v in raw["places"].items()},
            no_fly=raw.get("no_fly", []), river=[tuple(p) for p in raw.get("river", [])],
            ridge=raw["ridge"], rally_points={k: tuple(v) for k, v in raw["rally_points"].items()},
            radio=raw["radio"], sensing=raw["sensing"], fleet=fleet)
        g = nx.Graph()
        for n, p in raw["roads"]["nodes"].items():
            g.add_node(n)
            sc.node_pos[n] = tuple(p)
        for u, v, name in raw["roads"]["edges"]:
            g.add_edge(u, v, length=dist(sc.node_pos[u], sc.node_pos[v]), name=name)
        sc.graph = g
        return sc

    # ------------------------------------------------------------- lookups
    def sector_center(self, s):
        x0, y0, x1, y1 = self.sectors[s]
        return ((x0 + x1) / 2, (y0 + y1) / 2)

    def sector_of(self, p):
        for s, (x0, y0, x1, y1) in self.sectors.items():
            if x0 <= p[0] <= x1 and y0 <= p[1] <= y1:
                return s
        return None

    def resolve_place(self, name):
        """Map whatever the LLM or operator wrote to a canonical place id."""
        if not name:
            return None
        n = str(name).strip().lower().replace("_", " ")
        for pid, p in self.places.items():
            if n == pid.lower().replace("_", " ") or n in p["aliases"]:
                return pid
        for pid, p in self.places.items():
            if any(a in n for a in p["aliases"]) or pid.lower().replace("_", " ") in n:
                return pid
        return None

    def place_pos(self, pid):
        return self.places[pid]["pos"]

    def in_no_fly(self, p):
        for z in self.no_fly:
            if point_in_poly(p, z["poly"]):
                return z
        return None

    # --------------------------------------------------------------- roads
    def _sp(self, blocked):
        key = frozenset(blocked)
        if key not in self._sp_cache:
            g = self.graph.copy()
            for u, v in blocked:
                if g.has_edge(u, v):
                    g.remove_edge(u, v)
            lengths, paths = {}, {}
            for src, (d, p) in nx.all_pairs_dijkstra(g, weight="length"):
                lengths[src], paths[src] = d, p
            self._sp_cache[key] = (lengths, paths)
        return self._sp_cache[key]

    def crosses_river(self, a, b):
        r = self.river
        return any(segments_intersect(a, b, r[i], r[i + 1]) for i in range(len(r) - 1))

    def road_access_nodes(self, p):
        """Road nodes a rover can leave the road from to reach point p (closest first)."""
        lim = self.raw["roads"].get("offroad_max_m", 100)
        out = []
        for n, q in self.node_pos.items():
            d = dist(p, q)
            if d <= lim and not self.crosses_river(p, q):
                out.append((d, n))
        out.sort()
        return [n for _, n in out]

    def nearest_node(self, p):
        return min(self.node_pos, key=lambda n: dist(p, self.node_pos[n]))

    def rover_route(self, start_pos, start_node, goal_pos, blocked):
        """Shortest rover path. Returns (length_m, waypoints) or None if unreachable."""
        lengths, paths = self._sp(blocked)
        start_node = start_node or self.nearest_node(start_pos)
        best = None
        for gn in self.road_access_nodes(goal_pos):
            d = lengths.get(start_node, {}).get(gn)
            if d is None:
                continue
            total = dist(start_pos, self.node_pos[start_node]) + d + dist(self.node_pos[gn], goal_pos)
            if best is None or total < best[0]:
                best = (total, gn)
        if best is None:
            return None
        nodes = paths[start_node][best[1]]
        pts = [tuple(start_pos)] + [self.node_pos[n] for n in nodes] + [tuple(goal_pos)]
        clean = [pts[0]]
        for q in pts[1:]:
            if dist(q, clean[-1]) > 0.5:
                clean.append(q)
        return best[0], clean, nodes

    def rover_reachable(self, p):
        return bool(self.road_access_nodes(p))

    def edge_near(self, p, tol=20):
        """Road edge that point p sits on (used to turn debris into a blocked edge)."""
        best = None
        for u, v in self.graph.edges:
            d = point_segment_dist(p, self.node_pos[u], self.node_pos[v])
            if d <= tol and (best is None or d < best[0]):
                best = (d, edge_key(u, v))
        return best[1] if best else None

    # ------------------------------------------------------------- terrain
    def terrain_h(self, x, y):
        r = self.ridge
        cx, cy = r["center"]
        r2 = ((np.asarray(x) - cx) / r["rx"]) ** 2 + ((np.asarray(y) - cy) / r["ry"]) ** 2
        return np.where(r2 < 1.0, r["height_m"] * (1.0 - r2), 0.0)

    def abs_alt(self, p, agl):
        return float(self.terrain_h(p[0], p[1])) + agl

    def los_clear(self, p1, h1, p2, h2, samples=28):
        t = np.linspace(0.0, 1.0, samples)[1:-1]
        xs = p1[0] + (p2[0] - p1[0]) * t
        ys = p1[1] + (p2[1] - p1[1]) * t
        line = h1 + (h2 - h1) * t
        return bool(np.all(line > self.terrain_h(xs, ys)))

    def rssi(self, p1, h1, p2, h2, gain_db=0.0):
        r = self.radio
        d = max(1.0, dist(p1, p2))
        val = r["tx_dbm"] + gain_db - r["pl_d0_db"] - 10 * r["exponent"] * math.log10(d)
        if not self.los_clear(p1, h1, p2, h2):
            val -= r["ridge_loss_db"]
        return val

    def coverage_grid(self, sources, cell=20.0, agl=1.5):
        """Best RSSI at ground level from any of `sources` [(pos, abs_h, gain)]. Vectorised."""
        r = self.radio
        nx_, ny_ = int(self.width // cell), int(self.height // cell)
        gx = (np.arange(nx_) + 0.5) * cell
        gy = (np.arange(ny_) + 0.5) * cell
        X, Y = np.meshgrid(gx, gy)
        H = self.terrain_h(X, Y) + agl
        best = np.full(X.shape, -200.0)
        t = np.linspace(0, 1, 24)[1:-1]
        for (p, h, gain) in sources:
            d = np.maximum(1.0, np.hypot(X - p[0], Y - p[1]))
            val = r["tx_dbm"] + gain - r["pl_d0_db"] - 10 * r["exponent"] * np.log10(d)
            xs = p[0] + (X[..., None] - p[0]) * t
            ys = p[1] + (Y[..., None] - p[1]) * t
            line = h + (H[..., None] - h) * t
            blocked = np.any(line <= self.terrain_h(xs, ys), axis=-1)
            val = np.where(blocked, val - r["ridge_loss_db"], val)
            best = np.maximum(best, val)
        return {"cell": cell, "nx": nx_, "ny": ny_, "rssi": np.round(best).astype(int).ravel().tolist()}

    def best_relay_point(self, targets):
        """Hover point that maximises the weakest link base->relay->targets (grid search)."""
        key = tuple((round(p[0] / 20), round(p[1] / 20)) for p in targets)
        if key in self._relay_cache:
            return self._relay_cache[key]
        r = self.radio
        base_h = self.raw["base"]["antenna_m"]
        best = None
        for x in range(20, int(self.width), 20):
            for y in range(20, int(self.height), 20):
                p = (float(x), float(y))
                if self.in_no_fly(p):
                    continue
                h = self.abs_alt(p, r["drone_alt_m"])
                weakest = self.rssi(self.base, base_h, p, h, 6.0)
                if best and weakest < best[0]:
                    continue
                for tp in targets:
                    weakest = min(weakest, self.rssi(p, h, tp, self.abs_alt(tp, r["rover_ant_m"]), 6.0))
                    if best and weakest < best[0]:
                        break
                # prefer points closer to base when links are equally good
                score = weakest - 0.002 * dist(self.base, p)
                if best is None or score > best[0]:
                    best = (score, p)
        self._relay_cache[key] = best[1]
        return best[1]

    def rally_for(self, p):
        """Nearest rally point (all rally points are placed inside base radio coverage)."""
        return min(self.rally_points.items(), key=lambda kv: dist(kv[1], p))

    # -------------------------------------------------------------- survey
    def survey_path(self, sector, entry_from):
        """Lawnmower waypoints over a sector, starting at the corner nearest `entry_from`."""
        x0, y0, x1, y1 = self.sectors[sector]
        swath = self.sensing["swath_m"]
        n = max(1, math.ceil((y1 - y0) / swath))
        spacing = (y1 - y0) / n
        ys = [y0 + spacing * (i + 0.5) for i in range(n)]
        margin = swath / 2
        xa, xb = x0 + margin, x1 - margin
        best = None
        for ylist in (ys, ys[::-1]):
            for xs, xe in ((xa, xb), (xb, xa)):
                d = dist(entry_from, (xs, ylist[0]))
                if best is None or d < best[0]:
                    best = (d, ylist, xs, xe)
        _, ylist, xs, xe = best
        pts = []
        for i, y in enumerate(ylist):
            a, b = (xs, xe) if i % 2 == 0 else (xe, xs)
            pts += [(a, y), (b, y)]
        return pts

    def survey_length(self, sector):
        return path_length(self.survey_path(sector, self.sector_center(sector)))

    # ------------------------------------------------------------ for UI
    def to_client(self):
        return {
            "name": self.name, "title": self.raw.get("title", self.name),
            "width": self.width, "height": self.height, "base": self.base,
            "sectors": self.sectors, "places": {k: v["pos"] for k, v in self.places.items()},
            "no_fly": self.no_fly, "river": self.river, "ridge": self.ridge,
            "rally_points": self.rally_points,
            "roads": {"nodes": self.node_pos,
                      "edges": [[u, v, d.get("name", "")] for u, v, d in self.graph.edges(data=True)]},
            "fleet": [{"id": a.id, "kind": a.kind, "caps": sorted(a.caps), "speed": a.speed,
                       "endurance_s": a.endurance_s, "kits": a.kits, "kg": a.kg, "label": a.label}
                      for a in self.fleet.values()],
            "presets": self.raw.get("presets", []),
            "default_objective": self.raw.get("default_objective", ""),
        }
