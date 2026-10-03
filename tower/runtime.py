"""Glue: one object that steps the simulator, the radio and the brain together.

Used by both the web server (server.py) and the terminal runner (headless.py).
"""
import time
from pathlib import Path

from .brain import Brain
from .compiler import compile_mission
from .config import ROOT
from .ledger import Ledger
from .llm import LLMClient
from .scenario import Scenario
from .sim.comms import Comms
from .sim.injector import Injector
from .sim.robot import Robot
from .sim.world import SimWorld

DT = 0.5


class Runtime:
    def __init__(self, settings, scripted=False, save_ledger=True):
        self.settings = settings
        self.scripted = scripted
        self.save_ledger = save_ledger
        self.llm = LLMClient(settings)
        self.reset()

    def reset(self):
        s = self.settings
        self.sc = Scenario.load(s.scenario)
        self.world = SimWorld(self.sc, seed=s.seed)
        self.comms = Comms(self.sc, self.world)
        self.robots = {aid: Robot(self.sc, self.world, self.comms, aid) for aid in self.sc.fleet}
        path = None
        if self.save_ledger:
            path = Path(ROOT) / "runs" / f"ledger-{time.strftime('%Y%m%d-%H%M%S')}.sqlite"
        self.brain = Brain(self.sc, self.comms, self.llm, s, Ledger(path))
        self.injector = Injector(self.world, self.comms, self.sc.raw.get("timeline", []) if self.scripted else [])
        self.speed = 3.0
        self.paused = False
        self.busy = None              # "compiling" | "goal change" while the LLM works
        self.goal_queue = []
        self.llm_calls = 0
        self._cov_key = None
        self._cov = None
        self._cov_ver = 0

    @property
    def t(self):
        return self.world.t

    # ---------------------------------------------------------------- steps
    def step(self, wall=None):
        wall = time.monotonic() if wall is None else wall
        b = self.brain
        if b.phase != "RUNNING":
            b.tick(self.world.t, wall, [])
            return False
        self.world.step_env(DT)
        for ev in self.injector.due(self.world.t):
            self.inject(ev["kind"], ev.get("args", {}), label=ev.get("label"))
        for r in self.robots.values():
            r.tick(DT)
        delivered = self.comms.step(DT, self.world.t)
        b.tick(self.world.t, wall, delivered)
        return True

    # ------------------------------------------------------------- actions
    def compile(self, text, current=None):
        """Blocking LLM call; run it in a thread from async code."""
        res = compile_mission(text, self.sc, self.llm, current=current)
        self.llm_calls += sum(1 for a in res.attempts if a["source"] == "llm")
        return res

    def submit_mission(self, text):
        self.busy = "compiling"
        try:
            res = self.compile(text)
            return self.brain.start_mission(res)
        finally:
            self.busy = None

    def goal_change(self, text):
        self.busy = "goal change"
        try:
            res = self.compile(text, current=self.brain.spec)
            return self.brain.apply_goal_change(res, text)
        finally:
            self.busy = None

    def inject(self, kind, args, label=None):
        msg, operator_text = self.injector.apply(kind, args, self.world.t)
        self.brain.log("inject", f"DISRUPTION: {label or msg}", "warn")
        if operator_text:
            self.goal_queue.append(operator_text)
        return msg

    # ------------------------------------------------------------ snapshot
    def coverage(self):
        r = self.sc.radio
        srcs = [(tuple(self.sc.base), self.sc.raw["base"]["antenna_m"], 0.0)]
        for p in self.brain._relay_sources():
            srcs.append((tuple(p), self.sc.abs_alt(p, r["drone_alt_m"]), 6.0))
        key = tuple((round(p[0]), round(p[1])) for p, _, _ in srcs)
        if key != self._cov_key:
            self._cov_key = key
            self._cov = self.sc.coverage_grid(srcs)
            self._cov_ver += 1
        return self._cov_ver, self._cov

    def snapshot(self, include_coverage=False):
        b = self.brain.to_client()
        ver, cov = self.coverage()
        snap = {
            "type": "snapshot", "t": round(self.world.t, 1), "speed": self.speed, "paused": self.paused,
            "busy": self.busy, "brain": b, "truth": self.world.to_client(), "comms": self.comms.to_client(),
            "robots": {a: r.to_client() for a, r in self.robots.items()},
            "llm": self.llm.status(), "coverage_version": ver,
            "timeline": [{"at": e["at"], "label": e.get("label", e["kind"])} for e in self.injector.timeline],
            "fired": self.injector.fired[-20:],
        }
        if include_coverage:
            snap["coverage"] = cov
        return snap

    def report(self):
        return self.brain.report(self.world, self.comms)
