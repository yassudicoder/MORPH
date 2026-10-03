"""Radio emulator between the robots and the base station.

Signal fades with distance (log-distance path loss), the ridge blocks line of
sight, every link has a bandwidth cap, and robots relay for each other (multi-
hop mesh). Messages wait in a store-and-forward queue while a robot is cut off
and go out by priority class, then by value of information per byte, when the
link returns. Newer state messages replace older queued ones.
"""
import heapq
import itertools
from dataclasses import dataclass, field

from ..scenario import dist

CLASS_NAMES = {0: "P0 SAFETY", 1: "P1 MISSION", 2: "P2 PLAN", 3: "P3 STATE", 4: "P4 BULK"}


@dataclass
class Message:
    src: str
    dst: str
    cls: int
    kind: str
    size: int
    payload: dict
    t: float
    voi: float = 1.0
    key: tuple | None = None          # newer message with the same key replaces a queued one
    seq: int = 0
    dropped: bool = field(default=False, compare=False)


class Comms:
    def __init__(self, sc, world):
        self.sc = sc
        self.world = world
        self._seq = itertools.count()
        self.up_q = {a: [] for a in sc.fleet}
        self.down_q = {a: [] for a in sc.fleet}
        self.keyed = {}                       # key -> queued Message
        self.robot_inbox = {a: [] for a in sc.fleet}
        self.credit = {a: 0.0 for a in sc.fleet}
        self.tier = {a: "C" for a in sc.fleet}
        self.links = []
        self.component = {a: {"BASE"} for a in sc.fleet}
        self.peer_last = {a: {} for a in sc.fleet}
        self.peer_box = {a: {} for a in sc.fleet}
        self.blackouts = []
        self.stats = {"bytes": {c: 0 for c in CLASS_NAMES}, "msgs": {c: 0 for c in CLASS_NAMES},
                      "superseded": 0, "suppressed_state": 0, "lost_with_robot": 0}

    # ------------------------------------------------------------- queueing
    def _push(self, q, msg):
        msg.seq = next(self._seq)
        if msg.key is not None:
            old = self.keyed.get(msg.key)
            if old is not None and not old.dropped:
                old.dropped = True
                self.stats["superseded"] += 1
            self.keyed[msg.key] = msg
        heapq.heappush(q, (msg.cls, -msg.voi / max(1, msg.size), msg.seq, msg))

    def up(self, aid, msg):
        self._push(self.up_q[aid], msg)

    def down(self, aid, msg):
        self._push(self.down_q[aid], msg)

    def queued(self, aid):
        return sum(1 for *_, m in self.up_q[aid] if not m.dropped)

    def add_blackout(self, center, radius, until):
        self.blackouts.append({"center": tuple(center), "radius": radius, "until": until})

    def peer_share(self, src, payload):
        """Robot broadcasts its task list to every peer it can currently hear."""
        for other in self.component.get(src, ()):
            if other != "BASE" and other != src:
                self.peer_box[other][src] = payload

    # ----------------------------------------------------------------- step
    def _node(self, nid):
        if nid == "BASE":
            return self.sc.base, self.sc.raw["base"]["antenna_m"], 0.0
        b = self.world.bodies[nid]
        return tuple(b.pos), self.world.abs_alt(b), b.spec.radio_gain_db

    def _blacked(self, p, t):
        return any(dist(p, z["center"]) <= z["radius"] for z in self.blackouts if z["until"] > t)

    def step(self, dt, t):
        self.blackouts = [z for z in self.blackouts if z["until"] > t]
        alive = [a for a, b in self.world.bodies.items() if b.alive]
        for a, b in self.world.bodies.items():
            if not b.alive and (self.up_q[a] or self.down_q[a]):
                self.stats["lost_with_robot"] += sum(1 for *_, m in self.up_q[a] if not m.dropped)
                self.up_q[a].clear()
                self.down_q[a].clear()
        nodes = ["BASE"] + alive
        info = {n: self._node(n) for n in nodes}
        usable = self.sc.radio["usable_dbm"]
        adj = {n: set() for n in nodes}
        self.links = []
        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                a, b = nodes[i], nodes[j]
                (pa, ha, ga), (pb, hb, gb) = info[a], info[b]
                if self._blacked(pa, t) != self._blacked(pb, t):
                    continue
                r = self.sc.rssi(pa, ha, pb, hb, ga + gb)
                if r >= usable:
                    adj[a].add(b)
                    adj[b].add(a)
                    self.links.append((a, b, round(r, 1)))
        # connected components
        seen, comps = set(), []
        for n in nodes:
            if n in seen:
                continue
            comp, stack = set(), [n]
            while stack:
                x = stack.pop()
                if x in comp:
                    continue
                comp.add(x)
                stack.extend(adj[x] - comp)
            seen |= comp
            comps.append(comp)
        for comp in comps:
            for a in comp:
                if a == "BASE":
                    continue
                self.component[a] = comp
                self.tier[a] = "C" if "BASE" in comp else ("P" if len(comp) > 1 else "I")
                for o in comp:
                    if o != "BASE" and o != a:
                        self.peer_last[a][o] = (t, info[o][0], info[o][1])   # when, where, altitude
        for a, b in self.world.bodies.items():
            if not b.alive:
                self.tier[a] = "X"
                self.component[a] = {a}

        delivered = []
        bw = self.sc.radio["bandwidth_Bps"]
        for a in alive:
            if self.tier[a] != "C":
                continue
            # one big message may span several steps; credit is capped so idle time is not banked
            self.credit[a] = min(self.credit[a] + bw * dt, max(bw * dt, 9000))
            q = self.up_q[a]
            while q:
                cls, _, _, m = q[0]
                if m.dropped:
                    heapq.heappop(q)
                    continue
                if m.size > self.credit[a]:
                    break
                heapq.heappop(q)
                self.credit[a] -= m.size
                if m.key is not None and self.keyed.get(m.key) is m:
                    del self.keyed[m.key]
                self.stats["bytes"][cls] += m.size
                self.stats["msgs"][cls] += 1
                delivered.append(m)
            dq = self.down_q[a]
            while dq:
                *_, m = heapq.heappop(dq)
                if m.dropped:
                    continue
                if m.key is not None and self.keyed.get(m.key) is m:
                    del self.keyed[m.key]
                self.robot_inbox[a].append(m)
        return delivered

    def to_client(self):
        return {
            "links": self.links,
            "tier": dict(self.tier),
            "queued": {a: self.queued(a) for a in self.up_q},
            "blackouts": [{"center": z["center"], "radius": z["radius"]} for z in self.blackouts],
            "stats": {"bytes": {CLASS_NAMES[c]: v for c, v in self.stats["bytes"].items()},
                      "msgs": {CLASS_NAMES[c]: v for c, v in self.stats["msgs"].items()},
                      "superseded": self.stats["superseded"],
                      "suppressed_state": self.stats["suppressed_state"],
                      "lost_with_robot": self.stats["lost_with_robot"]},
        }
