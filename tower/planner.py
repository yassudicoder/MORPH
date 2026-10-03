"""Planner: assign and order tasks across heterogeneous robots.

A vehicle-routing heuristic in plain Python (no solver licence, nothing to
install): regret-2 insertion by priority tier, then relocate local search.
Hard limits: capability, terrain (rovers stay on the road graph), battery with
reserve, kits and payload. Soft limits: deadlines, weighted by priority. When a
drone would run short it gets an automatic base visit (swap battery, reload
kits); when nothing can fit a task, the lowest-priority task is dropped first.

Repairs warm-start from the current routes and add a stability penalty for
moving a task to a different robot, so robots are not reshuffled for no reason.
"""
import time
from dataclasses import dataclass, field

from .model import PRIORITY_RANK, TASK_LIBRARY
from .scenario import dist

SERVICE_S = {"INSPECT": 20.0, "DELIVER": 15.0, "TRANSPORT": 90.0, "HOLD": 0.0}
SWAP_S = 90.0          # drone: land, swap battery, reload kits
LOAD_S = 60.0          # rover: load supplies / kits
HARD_FLOOR = 5.0       # % battery a drone must never go below

DROP_PENALTY = {"LIFE": 1e7, "HIGH": 1e5, "NORMAL": 1e3}

STRATEGIES = {
    #             time weight per minute     late weight per second    energy  end   stability
    "balanced": dict(wt={"LIFE": 20, "HIGH": 2, "NORMAL": 0.5}, wl={"LIFE": 50, "HIGH": 5, "NORMAL": 0.5}, we=0.05, wend=0.2, ws=30),
    "fast":     dict(wt={"LIFE": 30, "HIGH": 3, "NORMAL": 1.0}, wl={"LIFE": 50, "HIGH": 5, "NORMAL": 0.5}, we=0.01, wend=1.0, ws=30),
    "frugal":   dict(wt={"LIFE": 20, "HIGH": 1, "NORMAL": 0.2}, wl={"LIFE": 50, "HIGH": 5, "NORMAL": 0.5}, we=0.6, wend=0.1, ws=30),
}


def wind_factor(w):
    """Extra battery burn for a multirotor in wind of w m/s (fitted shape, +60% at 12 m/s)."""
    return 1.0 + 0.6 * (w / 12.0) ** 2


@dataclass
class AgentState:
    id: str
    spec: object
    pos: tuple
    battery: float
    kits: int
    kg: float
    last_node: str | None = None
    locked: list = field(default_factory=list)   # task ids that must stay first (in progress)
    alive: bool = True


@dataclass
class Leg:
    dur: float
    energy: float
    kind: str                 # travel | service | wait | swap
    flies: bool
    end_pos: tuple
    task: str | None = None   # task finished at the end of this leg
    reset: bool = False       # battery reset to 100 after this leg (swap)


@dataclass
class Stop:
    kind: str                 # task | base | rtb
    task: str | None
    pos: tuple
    arrive: float
    finish: float
    battery: float
    purpose: str = ""


@dataclass
class RouteEval:
    feasible: bool
    reason: str = ""
    stops: list = field(default_factory=list)
    legs: list = field(default_factory=list)
    end_time: float = 0.0
    end_batt: float = 100.0
    min_batt: float = 100.0
    start_batt: float = 100.0
    energy: float = 0.0
    late: dict = field(default_factory=dict)
    finish: dict = field(default_factory=dict)
    cost: float = 0.0

    def __post_init__(self):
        if not self.feasible:
            self.cost = 1e9


@dataclass
class Plan:
    routes: dict
    evals: dict
    dropped: list
    excluded: list
    strategy: str
    label: str
    cost: float = 0.0
    risk: dict = field(default_factory=dict)
    score: float = 0.0

    def signature(self):
        return tuple(sorted((a, tuple(r)) for a, r in self.routes.items()))

    def task_set(self):
        return frozenset(t for r in self.routes.values() for t in r)

    def assignee(self, tid):
        for a, r in self.routes.items():
            if tid in r:
                return a
        return None


class Planner:
    def __init__(self, scenario):
        self.sc = scenario
        self._rover_cache = {}
        self._survey_cache = {}
        self._reach_cache = {}

    # ------------------------------------------------------------ geometry
    def _rover_leg(self, pos, last_node, goal, blocked):
        key = (round(pos[0]), round(pos[1]), last_node, round(goal[0]), round(goal[1]), frozenset(blocked))
        if key not in self._rover_cache:
            r = self.sc.rover_route(pos, last_node, goal, blocked)
            self._rover_cache[key] = None if r is None else (r[0], r[2][-1])
            if len(self._rover_cache) > 50000:
                self._rover_cache.clear()
        return self._rover_cache[key]

    def _survey(self, sector, from_pos, progress, active_inside):
        if active_inside:
            L = self.sc.survey_length(sector)
            return from_pos, self.sc.sector_center(sector), (1 - progress) * L
        if progress > 0.01:
            L = self.sc.survey_length(sector)
            c = self.sc.sector_center(sector)
            return c, c, (1 - progress) * L
        x0, y0, x1, y1 = self.sc.sectors[sector]
        corner = (from_pos[0] > (x0 + x1) / 2, from_pos[1] > (y0 + y1) / 2)
        key = (sector, corner)
        if key not in self._survey_cache:
            path = self.sc.survey_path(sector, from_pos)
            L = sum(dist(path[i], path[i + 1]) for i in range(len(path) - 1))
            self._survey_cache[key] = (path[0], path[-1], L)
        return self._survey_cache[key]

    def capable(self, agent, task, blocked=None):
        if not TASK_LIBRARY[task.type]["needs"] <= agent.spec.caps:
            return False
        key = (agent.spec.flies, round(task.pos[0]), round(task.pos[1]))
        if key not in self._reach_cache:
            self._reach_cache[key] = (not self.sc.in_no_fly(task.pos)) if agent.spec.flies                 else self.sc.rover_reachable(task.pos)
        return self._reach_cache[key]

    # ----------------------------------------------------------- evaluation
    def evaluate(self, agent, seq, ctx, strategy="balanced"):
        """Simulate one robot's route on paper. Returns RouteEval (feasible or not)."""
        W = STRATEGIES[strategy]
        sp = agent.spec
        flies = sp.flies
        drain = (100.0 / sp.endurance_s) * (wind_factor(ctx.wind) if flies else 1.0)
        base = self.sc.base
        reserve = ctx.reserve
        t, pos, b = ctx.now, tuple(agent.pos), agent.battery
        kits, kg, node = agent.kits, agent.kg, agent.last_node
        ev = RouteEval(True, start_batt=b)
        min_b = b

        def travel(frm, to, nd):
            if flies:
                d = dist(frm, to)
                return d / sp.speed, nd
            r = self._rover_leg(frm, nd, to, ctx.blocked)
            if r is None:
                return None, nd
            return r[0] / sp.speed, r[1]

        def base_visit(purpose):
            nonlocal t, pos, b, kits, kg, node, min_b
            dt, node2 = travel(pos, base, node)
            if dt is None:
                return False
            e = dt * drain
            if flies and b - e < HARD_FLOOR:
                return False
            ev.legs.append(Leg(dt, e, "travel", flies, base))
            t += dt
            b -= e
            min_b = min(min_b, b)
            arrive = t
            svc = SWAP_S if flies else LOAD_S
            ev.legs.append(Leg(svc, 0.0, "swap", flies, base, reset=flies))
            t += svc
            if flies:
                b = 100.0
            kits, kg, pos, node = sp.kits, sp.kg, base, node2
            ev.stops.append(Stop("base", None, base, arrive, t, b, purpose))
            return True

        for i, tid in enumerate(seq):
            task = ctx.tasks[tid]
            if not self.capable(agent, task):
                return RouteEval(False, f"{sp.id} cannot do {task.label()}")
            active_inside = (task.status == "ACTIVE" and task.assignee == sp.id and task.type == "SURVEY"
                             and self.sc.sector_of(pos) == task.area)
            if task.type == "SURVEY":
                entry, exit_, length = self._survey(task.area, pos, task.progress, active_inside)
                svc = length / sp.survey_speed
            else:
                entry = exit_ = tuple(task.pos)
                if task.type == "RELAY":
                    svc = max(30.0, task.duration - (ctx.now - task.meta.get("started", ctx.now))
                              if task.status == "ACTIVE" else task.duration)
                else:
                    svc = SERVICE_S.get(task.type, 0.0)

            needs = []
            if task.kits and kits < task.kits:
                needs.append("reload kits")
            if task.kg and kg < task.kg - 1e-6:
                needs.append("load supplies")
            if needs and (task.kits > sp.kits or task.kg > sp.kg + 1e-6):
                return RouteEval(False, f"{sp.id} can never carry {task.label()}")

            dt_go, nd_go = travel(pos, entry, node)
            if dt_go is None:
                return RouteEval(False, f"{sp.id} has no route to {task.label()}")
            wait = max(0.0, task.release - (t + dt_go))
            if flies and not needs:
                e_need = (dt_go + wait + svc) * drain + dist(exit_, base) / sp.speed * drain
                if b - e_need < reserve:
                    needs.append("swap battery")
            if needs:
                if not base_visit(", ".join(needs)):
                    return RouteEval(False, f"{sp.id} cannot get back to base before {task.label()}")
                dt_go, nd_go = travel(pos, entry, node)
                if dt_go is None:
                    return RouteEval(False, f"{sp.id} has no route to {task.label()}")
                wait = max(0.0, task.release - (t + dt_go))
                if flies:
                    e_need = (dt_go + wait + svc) * drain + dist(exit_, base) / sp.speed * drain
                    if b - e_need < reserve:
                        return RouteEval(False, f"{task.label()} is beyond {sp.id}'s range with reserve")

            e_go = dt_go * drain
            ev.legs.append(Leg(dt_go, e_go, "travel", flies, entry))
            t += dt_go
            b -= e_go
            arrive = t
            if wait > 0:
                ev.legs.append(Leg(wait, wait * drain, "wait", flies, entry))
                t += wait
                b -= wait * drain
            e_svc = svc * drain
            ev.legs.append(Leg(svc, e_svc, "service", flies, exit_, task=tid))
            t += svc
            b -= e_svc
            min_b = min(min_b, b)
            pos, node = exit_, nd_go
            kits -= task.kits
            kg -= task.kg
            ev.finish[tid] = t
            due = task.due if task.due is not None else ctx.deadline
            # a relay's deadline means "on station by"; everything else means "finished by"
            late = max(0.0, (arrive + wait if task.type == "RELAY" else t) - due)
            if late > 0:
                ev.late[tid] = late
            ev.stops.append(Stop("task", tid, exit_, arrive, t, b))
            ev.cost += W["wt"][task.priority] * (t - ctx.now) / 60.0 + W["wl"][task.priority] * late

        # always finish at base
        if seq or dist(pos, base) > 40:
            dt, _ = travel(pos, base, node)
            if dt is None:
                if flies:
                    return RouteEval(False, f"{sp.id} cannot return to base")
                dt = 0.0
            e = dt * drain
            ev.legs.append(Leg(dt, e, "travel", flies, base))
            t += dt
            b -= e
            min_b = min(min_b, b)
            ev.stops.append(Stop("rtb", None, base, t, t, b, "return to base"))
        if flies and b < reserve - 0.5 and seq:
            return RouteEval(False, f"{sp.id} would return with {b:.0f}% (< {reserve:g}% reserve)")
        ev.end_time, ev.end_batt, ev.min_batt = t, b, min_b
        ev.energy = sum(l.energy for l in ev.legs)
        ev.cost += W["we"] * ev.energy + W["wend"] * (t - ctx.now) / 60.0
        return ev

    def follow(self, agent, stops, ctx):
        """Lowest battery along a fixed, already-dispatched stop list (no automatic base visits).

        The monitor uses this every second to project each drone's battery to the end of its plan.
        """
        sp = agent.spec
        if not sp.flies:
            return 100.0
        drain = 100.0 / sp.endurance_s * wind_factor(ctx.wind)
        base = self.sc.base
        pos, b = tuple(agent.pos), agent.battery
        worst = b
        for s in stops:
            if s["type"] == "RTB":
                b -= dist(pos, base) / sp.speed * drain
                worst = min(worst, b)
                pos = base
                if not s.get("final"):
                    b = 100.0
                continue
            task = ctx.tasks.get(s["id"])
            if task is None or not task.open:
                continue
            if task.type == "SURVEY":
                inside = task.status == "ACTIVE" and self.sc.sector_of(pos) == task.area
                entry, exit_, length = self._survey(task.area, pos, task.progress, inside)
                svc = length / sp.survey_speed
            else:
                entry = exit_ = tuple(task.pos)
                if task.type == "RELAY":
                    svc = task.duration - (ctx.now - task.meta.get("started", ctx.now)) if task.status == "ACTIVE"                         else task.duration
                    svc = max(30.0, svc)
                else:
                    svc = SERVICE_S.get(task.type, 0.0)
            b -= (dist(pos, entry) / sp.speed + svc) * drain
            pos = exit_
        if dist(pos, base) > 1:
            b -= dist(pos, base) / sp.speed * drain
        return min(worst, b)

    # --------------------------------------------------------------- search
    def _stab(self, ctx, tid, aid, W):
        prev = ctx.prev_assign.get(tid)
        return W["ws"] if prev and prev != aid and prev in ctx.alive_ids else 0.0

    def build(self, agents, ctx, task_ids, init_routes, strategy, time_limit=0.6):
        W = STRATEGIES[strategy]
        live = [a for a in agents if a.alive]
        routes = {a.id: list(init_routes.get(a.id, [])) for a in live}
        evals = {a.id: self.evaluate(a, routes[a.id], ctx, strategy) for a in live}
        # a warm-start route can become infeasible (e.g. wind): peel tasks off its end until it fits
        orphans = []
        for a in live:
            while not evals[a.id].feasible and len(routes[a.id]) > len(a.locked):
                orphans.append(routes[a.id].pop())
                evals[a.id] = self.evaluate(a, routes[a.id], ctx, strategy)
            if not evals[a.id].feasible and a.locked:
                # cannot even finish the task in progress (e.g. a long relay): hand it to a relief robot
                orphans += routes[a.id]
                routes[a.id], a.locked = [], []
                evals[a.id] = self.evaluate(a, [], ctx, strategy)
        placed = {t for r in routes.values() for t in r}
        pending = [t for t in task_ids if t not in placed] + [t for t in orphans if t not in placed]
        pending = list(dict.fromkeys(pending))
        dropped = []
        by_id = {a.id: a for a in live}
        for tier in ("LIFE", "HIGH", "NORMAL"):
            tier_tasks = [t for t in pending if ctx.tasks[t].priority == tier]
            while tier_tasks:
                choice = None
                for tid in tier_tasks:
                    per_agent = []
                    for a in live:
                        if not self.capable(a, ctx.tasks[tid]):
                            continue
                        r = routes[a.id]
                        best_a = None
                        for i in range(len(a.locked), len(r) + 1):
                            ev = self.evaluate(a, r[:i] + [tid] + r[i:], ctx, strategy)
                            if not ev.feasible:
                                continue
                            d = ev.cost - evals[a.id].cost + self._stab(ctx, tid, a.id, W)
                            if best_a is None or d < best_a[0]:
                                best_a = (d, a.id, i, ev)
                        if best_a:
                            per_agent.append(best_a)
                    if not per_agent:
                        choice = ("drop", tid)
                        break
                    per_agent.sort(key=lambda x: x[0])
                    regret = (per_agent[1][0] - per_agent[0][0]) if len(per_agent) > 1 else 1e9
                    key = (regret, -per_agent[0][0])
                    if choice is None or key > choice[0]:
                        choice = (key, tid, per_agent[0])
                if choice[0] == "drop":
                    dropped.append(choice[1])
                    tier_tasks.remove(choice[1])
                    continue
                _, tid, (d, aid, i, ev) = choice
                routes[aid].insert(i, tid)
                evals[aid] = ev
                tier_tasks.remove(tid)
        self._local_search(by_id, routes, evals, ctx, strategy, time_limit)
        # a dropped task may fit after local search tightened the routes
        for tid in list(dropped):
            best = None
            for a in live:
                if not self.capable(a, ctx.tasks[tid]):
                    continue
                r = routes[a.id]
                for i in range(len(a.locked), len(r) + 1):
                    ev = self.evaluate(a, r[:i] + [tid] + r[i:], ctx, strategy)
                    if ev.feasible and (best is None or ev.cost - evals[a.id].cost < best[0]):
                        best = (ev.cost - evals[a.id].cost, a.id, i, ev)
            if best:
                routes[best[1]].insert(best[2], tid)
                evals[best[1]] = best[3]
                dropped.remove(tid)
        return routes, evals, dropped

    def _local_search(self, by_id, routes, evals, ctx, strategy, time_limit):
        W = STRATEGIES[strategy]
        t_end = time.perf_counter() + time_limit
        improved = True
        while improved and time.perf_counter() < t_end:
            improved = False
            for aid, r in routes.items():
                a = by_id[aid]
                for idx in range(len(a.locked), len(r)):
                    tid = r[idx]
                    r_minus = r[:idx] + r[idx + 1:]
                    ev_minus = self.evaluate(a, r_minus, ctx, strategy)
                    if not ev_minus.feasible:
                        continue
                    old_a = evals[aid].cost + self._stab(ctx, tid, aid, W)
                    best = None
                    for bid, rb in routes.items():
                        b = by_id[bid]
                        if not self.capable(b, ctx.tasks[tid]):
                            continue
                        base_r = r_minus if bid == aid else rb
                        for j in range(len(b.locked), len(base_r) + 1):
                            if bid == aid and j == idx:
                                continue
                            ev_new = self.evaluate(b, base_r[:j] + [tid] + base_r[j:], ctx, strategy)
                            if not ev_new.feasible:
                                continue
                            if bid == aid:
                                gain = old_a - (ev_new.cost + self._stab(ctx, tid, aid, W))
                            else:
                                gain = (old_a + evals[bid].cost) - (ev_minus.cost + ev_new.cost +
                                                                    self._stab(ctx, tid, bid, W))
                            if gain > 1e-6 and (best is None or gain > best[0]):
                                best = (gain, bid, j, ev_new)
                    if best:
                        _, bid, j, ev_new = best
                        if bid == aid:
                            routes[aid] = r_minus[:j] + [tid] + r_minus[j:]
                            evals[aid] = ev_new
                        else:
                            routes[aid] = r_minus
                            evals[aid] = ev_minus
                            routes[bid] = routes[bid][:j] + [tid] + routes[bid][j:]
                            evals[bid] = ev_new
                        improved = True
                        break
                if improved or time.perf_counter() > t_end:
                    break

    def plan_cost(self, routes, evals, dropped, ctx):
        c = sum(e.cost for e in evals.values())
        c += sum(DROP_PENALTY[ctx.tasks[t].priority] for t in dropped)
        return c

    def candidates(self, agents, ctx, task_ids, init_routes=None, variants=None,
                   strategies=("balanced", "fast", "frugal"), cold=True):
        """Build 2-3 meaningfully different plans. `variants` = [(label, excluded task ids)]."""
        variants = variants or [("all tasks", [])]
        out, seen = [], set()
        for label, excluded in variants:
            ids = [t for t in task_ids if t not in excluded]
            starts = []
            if init_routes:
                starts.append(("warm", {a: [t for t in r if t not in excluded] for a, r in init_routes.items()}))
            if cold or not init_routes:
                starts.append(("cold", {a.id: list(a.locked) for a in agents}))
            for start_name, init in starts:
                for strat in strategies:
                    routes, evals, dropped = self.build(agents, ctx, ids, init, strat)
                    p = Plan(routes, evals, dropped, list(excluded), f"{strat}/{start_name}", label)
                    # compare plans under one yardstick
                    p.evals = {a.id: self.evaluate(a, routes[a.id], ctx, "balanced") for a in agents if a.alive}
                    p.cost = self.plan_cost(routes, p.evals, dropped, ctx)
                    sig = p.signature()
                    if sig in seen:
                        continue
                    seen.add(sig)
                    out.append(p)
        out.sort(key=lambda p: p.cost)
        return out


@dataclass
class PlanContext:
    now: float
    wind: float
    reserve: float
    deadline: float
    blocked: set
    tasks: dict
    prev_assign: dict = field(default_factory=dict)
    alive_ids: set = field(default_factory=set)


def priority_sorted(task_ids, tasks):
    return sorted(task_ids, key=lambda t: (PRIORITY_RANK[tasks[t].priority], t))
