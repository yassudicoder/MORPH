"""Autonomy gate: decide whether a plan change runs on its own, asks, or holds.

Looks at how likely the best plan is to work, how far ahead it is of the
runner-up (a plan that keeps a different set of tasks), whether the change can
be undone, and whether a LIFE task is affected.
"""
AUTO_P = 0.80
HOLD_P = 0.55
GAP = 0.10
LIFE_DELAY_S = 300
BATTERY_FLOOR = 5.0


def runner_up(cands):
    """Best alternative that is a genuinely different trade-off (different task set)."""
    if len(cands) < 2:
        return None
    best = cands[0]
    for c in cands[1:]:
        if c.task_set() != best.task_set():
            return c
    return None


def decide(cands, *, first_plan=False, goal_change=False, unknown_event=False,
           prev_finish=None, prev_tasks=None, tasks=None):
    prev_finish = prev_finish or {}
    prev_tasks = prev_tasks or set()
    if unknown_event:
        return {"result": "HOLD", "why": ["event type TOWER has never seen"], "reversible": False, "gap": None}
    if not cands:
        return {"result": "HOLD", "why": ["no feasible plan"], "reversible": False, "gap": None}
    best = cands[0]
    p = best.risk.get("p_success", 0.0)
    why = []
    alt = runner_up(cands)
    gap = None
    if alt is not None:
        gap = (best.score - alt.score) / max(abs(best.score), 1e-6)

    lost = sorted(t for t in prev_tasks if t in tasks and tasks[t].open and t not in best.task_set())
    reversible = not lost
    life_hit = []
    for t in lost:
        if tasks[t].priority == "LIFE":
            life_hit.append(f"{tasks[t].label()} would be dropped")
    for aid, ev in best.evals.items():
        for tid, fin in ev.finish.items():
            if tasks[tid].priority == "LIFE" and tid in prev_finish and fin - prev_finish[tid] > LIFE_DELAY_S:
                life_hit.append(f"{tasks[tid].label()} delayed {(fin - prev_finish[tid]) / 60:.0f} min")
    for tid in best.dropped:
        if tasks[tid].priority == "LIFE" and tid not in lost:
            life_hit.append(f"{tasks[tid].label()} cannot be scheduled")

    if p < HOLD_P:
        return {"result": "HOLD", "why": [f"P(success) {p:.2f} below {HOLD_P}"] + life_hit,
                "reversible": reversible, "gap": gap}
    if first_plan:
        why.append("first plan is always approved by a person")
    if goal_change:
        why.append("the mission goal changes")
    if p < AUTO_P:
        why.append(f"P(success) {p:.2f} below {AUTO_P}")
    floor = best.risk.get("min_batt_p5", 100.0)
    if floor < BATTERY_FLOOR:
        why.append(f"in the worst 5% of simulated runs a drone drops to {floor:.0f}% battery "
                   f"(floor {BATTERY_FLOOR:g}%)")
    if gap is not None and gap < GAP:
        why.append(f"score gap {gap * 100:.0f}% < {GAP * 100:.0f}% vs runner-up")
    if life_hit:
        why.append("LIFE task affected: " + "; ".join(life_hit))
    if lost:
        why.append("part of the mission would be dropped: " + ", ".join(tasks[t].label() for t in lost))
    return {"result": "ASK" if why else "AUTO", "why": why, "reversible": reversible, "gap": gap}
