"""End-to-end and unit tests. Run from the tower/ folder:  python -m pytest -q

No LLM or network is needed: the compiler runs in rules mode, and the LLM
retry loop is tested with a fake client.
"""
import pytest

from tower.compiler import compile_mission
from tower.config import load_settings
from tower.planner import AgentState, PlanContext, Planner
from tower.runtime import Runtime
from tower.scenario import Scenario, dist


@pytest.fixture
def settings():
    s = load_settings()
    s.kind = s.provider = "rules"
    return s


@pytest.fixture
def sc(settings):
    return Scenario.load(settings.scenario)


def start(settings, scripted=False):
    rt = Runtime(settings, scripted=scripted, save_ledger=False)
    rt.submit_mission(rt.sc.raw["default_objective"])
    assert rt.brain.phase == "AWAIT_APPROVAL"
    (eid,) = list(rt.brain.escalations)
    ok, _ = rt.brain.answer(eid, "A", by="test")
    assert ok and rt.brain.phase == "RUNNING"
    return rt


def run_until(rt, t_end, answer=True):
    wall = 0.0
    while rt.world.t < t_end and rt.brain.phase == "RUNNING":
        wall += 0.2
        rt.step(wall)
        while rt.goal_queue:
            rt.goal_change(rt.goal_queue.pop(0))
        if answer:
            for eid, e in list(rt.brain.escalations.items()):
                if e["status"] == "open":
                    rt.brain.answer(eid, e["recommended"], by="test")


# ------------------------------------------------------------------ compiler
def test_rule_compiler_default(sc):
    r = compile_mission(sc.raw["default_objective"], sc, None)
    assert r.source == "rules"
    kinds = [o.kind for o in r.spec.objectives]
    assert kinds == ["SURVEY", "DELIVER_ON_DETECT", "TRANSPORT", "KEEP_LINK"]
    assert r.spec.objectives[0].areas == list("ABCDEF")
    assert r.spec.deadline_min == 45 and r.spec.reserve_battery_pct == 15


@pytest.mark.parametrize("text,needle", [
    ("Carry 50 kg to the shelter in 45 minutes", "more than the whole fleet can carry"),
    ("Inspect the dam", "no-fly zone"),
    ("Survey sectors A to F in 5 minutes", "physically impossible"),
])
def test_validator_rejects_impossible(sc, text, needle):
    r = compile_mission(text, sc, None)
    assert r.spec is None and any(needle in e for e in r.errors)


class FakeLLM:
    """First reply forgets the TRANSPORT destination; second reply is correct."""
    enabled = True
    model = "fake"

    def __init__(self):
        self.calls = []

    def json_chat(self, system, user, schema):
        self.calls.append(user)
        objs = [{"kind": "SURVEY", "areas": ["A", "B"], "place": "NONE", "priority": "LIFE"},
                {"kind": "TRANSPORT", "payload_kg": 10, "place": "NONE", "priority": "HIGH"}]
        if len(self.calls) > 1:
            objs[1]["place"] = "shelter"
        return {"summary": "x", "deadline_min": 45, "reserve_battery_pct": 15, "objectives": objs}


def test_llm_retry_loop_feeds_validator_errors_back(sc):
    llm = FakeLLM()
    r = compile_mission("survey A and B, take 10 kg to the shelter", sc, llm)
    assert r.source == "llm" and len(r.attempts) == 2
    assert "'place' is missing" in r.attempts[0]["errors"][0]
    assert "validator rejected" in llm.calls[1]          # errors went back to the model
    assert r.spec.objectives[1].place == "SHELTER"       # alias resolved
    assert r.spec.objectives[0].priority == "HIGH"       # LIFE auto-demoted for a survey
    assert r.notes


# ------------------------------------------------------------------- planner
def _tasks_for(sc):
    from tower.compiler import expand
    from tower.model import Task
    spec = compile_mission(sc.raw["default_objective"], sc, None).spec
    tmpl, _ = expand(spec, sc)
    tasks = {}
    for i, t in enumerate(tmpl):
        t.pop("key")
        tasks[f"T{i}"] = Task(id=f"T{i}", **t)
    return tasks


def test_planner_assigns_everything_and_adds_swaps_in_wind(sc):
    tasks = _tasks_for(sc)
    agents = [AgentState(a.id, a, a.spawn, 100.0, a.kits, 0.0, sc.nearest_node(a.spawn)) for a in sc.fleet.values()]
    pl = Planner(sc)
    ctx = PlanContext(now=0, wind=4, reserve=15, deadline=2700, blocked=set(), tasks=tasks,
                      alive_ids={a.id for a in agents})
    best = pl.candidates(agents, ctx, list(tasks))[0]
    assert not best.dropped
    for aid, ev in best.evals.items():
        assert ev.feasible
        if sc.fleet[aid].flies:
            assert ev.end_batt >= 15 - 0.5
    ctx.wind = 11
    windy = pl.candidates(agents, ctx, list(tasks))[0]
    swaps = sum(1 for ev in windy.evals.values() for s in ev.stops if s.kind == "base")
    assert swaps >= 2 and not windy.dropped


def test_rover_avoids_blocked_road(sc):
    blocked = {("BRL", "L3")}
    r = sc.rover_route(sc.base, "BASE_N", sc.place_pos("SHELTER"), blocked)
    assert r is not None and "BRL" not in r[2]      # goes over the upper bridge instead
    assert "BRU" in r[2]


# ------------------------------------------------------------- full mission
def test_scripted_mission_completes(settings):
    rt = start(settings, scripted=True)
    run_until(rt, 3600)
    rep = rt.report()
    assert rep["phase"] == "DONE"
    assert sorted(rep["surveyed"]) == list("ABCDEF")
    assert all(s["served"] == s["count"] for s in rep["truth"]["survivors"])
    assert rep["supplies_kg"] == 20
    assert rep["truth"]["robots_lost"] == ["RELAY-1"]
    levels = {d["level"] for d in rep["decisions"]}
    assert {"L1", "L2", "L3"} <= levels
    # robots that only lost each other behind the ridge must not steal each other's tasks
    assert not any("mesh auction" in d["explain"] for d in rep["decisions"])


def test_goal_change_waits_for_approval(settings):
    rt = start(settings)
    run_until(rt, 200)
    rt.goal_change("Villagers report someone trapped near the temple. Check it.")
    b = rt.brain
    proposed = [t for t in b.tasks.values() if t.status == "PROPOSED"]
    assert len(proposed) == 1 and proposed[0].place == "TEMPLE"
    esc = [e for e in b.escalations.values() if e["status"] == "open"]
    assert esc and esc[0]["kind"] == "goal"
    # an automatic repair while the question is open must not sneak the new task in
    rt.inject("wind", {"speed": 11})
    run_until(rt, 320, answer=False)
    assert all(proposed[0].id not in [s["id"] for s in stops] for stops in b.dispatched.values())
    ok, _ = b.answer(esc[0]["id"], "KEEP", by="test")
    assert ok and proposed[0].status == "DISCARDED"


def test_goal_change_approved_reaches_robots(settings):
    rt = start(settings)
    run_until(rt, 200)
    rt.goal_change("Check the school.")
    b = rt.brain
    (esc,) = [e for e in b.escalations.values() if e["status"] == "open"]
    b.answer(esc["id"], esc["recommended"], by="test")
    new = [t for t in b.tasks.values() if t.place == "SCHOOL"][0]
    assert new.status == "PENDING"
    assert any(new.id in [s["id"] for s in stops] for stops in b.dispatched.values())


def test_robot_in_radio_shadow_is_not_declared_lost(settings):
    rt = start(settings)
    run_until(rt, 900)
    lost = [d for d in rt.brain.ledger.records if d["trigger"].get("type") == "AGENT_LOST"]
    assert not lost          # scouts and rovers went behind the ridge, nobody was falsely lost


def test_dead_relay_is_detected(settings):
    rt = start(settings)
    run_until(rt, 60)
    rt.inject("agent_fail", {"agent": "SCOUT-1"})
    run_until(rt, 80)
    assert not rt.brain.wm.agents["SCOUT-1"].alive
    lost = [d for d in rt.brain.ledger.records if d["trigger"].get("type") == "AGENT_LOST"]
    assert lost and lost[0]["t"] - 60 < 6


def test_l0_battery_floor_on_robot(settings):
    """Cut off from the base with a failing battery, the robot must bring itself home (L0)."""
    rt = start(settings)
    run_until(rt, 120)
    b = rt.world.bodies["SCOUT-1"]
    rt.comms.add_blackout(tuple(b.pos), 120, until=10_000)   # base cannot help
    b.battery = 12.0                                         # sudden cell failure
    run_until(rt, 500)
    notes = [d for d in rt.brain.ledger.records if d["level"] == "L0"]
    assert any("battery critical" in d["explain"] for d in notes)
    assert rt.world.bodies["SCOUT-1"].alive


def test_dead_reckoning_saves_radio(settings):
    rt = start(settings)
    run_until(rt, 300)
    st = rt.comms.stats
    assert st["suppressed_state"] > st["msgs"][3]   # most 1 Hz position reports were not needed


def test_partition_auction(settings):
    """Two scouts cut off from base together: when one goes silent, the other takes its survey."""
    rt = start(settings)
    run_until(rt, 30)
    w, c = rt.world, rt.comms
    for aid, pos in (("SCOUT-1", (1000, 520)), ("SCOUT-2", (1010, 530))):
        w.bodies[aid].pos = list(pos)
    c.add_blackout((1000, 500), 150, until=10_000)
    r1, r2 = rt.robots["SCOUT-1"], rt.robots["SCOUT-2"]
    survey = {"id": "T99", "type": "SURVEY", "pos": [720, 470], "area": "B", "kits": 0, "kg": 0, "label": "SURVEY B"}
    r1.stops = [survey]
    r1.idx = 0
    c.step(0.5, w.t)
    c.peer_share("SCOUT-1", {"version": 9, "stops": [survey], "pos": (1000, 520)})
    assert c.tier["SCOUT-2"] == "P"
    w.kill("SCOUT-1")
    for _ in range(20):
        w.step_env(0.5)
        c.step(0.5, w.t)
        r2.tick(0.5)
    assert any(s["id"] == "T99" for s in r2.stops)


class LazyLLM:
    """Returns the current mission unchanged the first time it is asked to amend it."""
    enabled = True
    model = "lazy"

    def __init__(self, current):
        self.current, self.calls = current, 0

    def json_chat(self, system, user, schema):
        self.calls += 1
        d = self.current.model_dump()
        if self.calls > 1:
            d["objectives"].append({"kind": "INSPECT", "place": "TEMPLE", "priority": "HIGH"})
        return d


def test_amend_that_changes_nothing_is_sent_back(sc):
    cur = compile_mission(sc.raw["default_objective"], sc, None).spec
    llm = LazyLLM(cur)
    r = compile_mission("Check the temple.", sc, llm, current=cur)
    assert r.source == "llm" and len(r.attempts) == 2
    assert "identical to the current mission" in r.attempts[0]["errors"][0]
    assert any(o.kind == "INSPECT" and o.place == "TEMPLE" for o in r.spec.objectives)


def test_repair_question_does_not_discard_pending_goal_change(settings):
    rt = start(settings)
    run_until(rt, 200)
    rt.goal_change("Check the school.")
    b = rt.brain
    (goal,) = [e for e in b.escalations.values() if e["status"] == "open"]
    rt.inject("agent_fail", {"agent": "RELAY-1"})       # may or may not raise its own question
    run_until(rt, 240, answer=False)
    assert b.escalations[goal["id"]]["status"] == "open"
    assert any(t.status == "PROPOSED" and t.place == "SCHOOL" for t in b.tasks.values())
