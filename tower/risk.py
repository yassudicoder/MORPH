"""Risk engine: replay each candidate plan 200 times with random noise.

Randomised per run: wind (and gusts), battery burn, rover terrain speed,
service-time overruns and radio dropouts in poor-coverage areas. Vectorised in
NumPy, so 200 runs of a full plan take a few milliseconds.

Outputs: P(success), P(all LIFE tasks on time), 5th-percentile return battery,
per-task on-time probability, and the score the autonomy gate uses.
"""
import numpy as np

from .model import PRIORITY_WEIGHT
from .planner import HARD_FLOOR, wind_factor

N_RUNS = 200


def assess(plan, ctx, link_bad=None, seed=0, n=N_RUNS, all_task_ids=None):
    """Monte Carlo assessment of one plan.

    link_bad(pos) -> bool tells which leg end-points sit in poor radio coverage.
    all_task_ids: open tasks the score is measured against (dropped ones count as 0).
    """
    rng = np.random.default_rng(seed)
    w0 = ctx.wind
    wind = np.maximum(0.0, w0 + rng.normal(0, 1.5, n) + (rng.random(n) < 0.10) * rng.uniform(2, 5, n))
    wind_ratio = wind_factor(wind) / wind_factor(w0)

    p_task = {}
    success = np.ones(n, dtype=bool)
    life_ok = np.ones(n, dtype=bool)
    return_batt = []
    min_batt_all = np.full(n, 100.0)

    for aid, ev in plan.evals.items():
        legs = ev.legs
        if not legs:
            continue
        L = len(legs)
        dur = np.array([l.dur for l in legs])
        eng = np.array([l.energy for l in legs])
        flies = legs[0].flies
        is_travel = np.array([l.kind == "travel" for l in legs])
        is_service = np.array([l.kind in ("service", "swap") for l in legs])
        bad = np.array([bool(link_bad and l.kind == "travel" and link_bad(l.end_pos)) for l in legs])

        if flies:
            tf = rng.lognormal(0, 0.05, (n, L)) * (1 + 0.02 * (wind - w0))[:, None]
            ef = rng.lognormal(0, 0.06, (n, 1)) * wind_ratio[:, None]
        else:
            tf = rng.lognormal(0, 0.12, (n, L))
            ef = rng.lognormal(0, 0.05, (n, 1))
        sf = rng.lognormal(0, 0.08, (n, L))
        factor = np.where(is_travel, tf, np.where(is_service, sf, 1.0))
        p_drop = np.where(bad, 0.5, 0.03)
        delay = (rng.random((n, L)) < p_drop) * rng.uniform(np.where(bad, 20, 5), np.where(bad, 90, 30), (n, L))
        delay = np.where(is_travel, delay, 0.0)
        t_end = ctx.now + np.cumsum(dur * factor + delay, axis=1)

        # battery with resets at swaps
        e = eng * ef
        batt = np.empty((n, L))
        level = np.full(n, ev.start_batt)
        for j, leg in enumerate(legs):
            level = level - e[:, j]
            batt[:, j] = level
            if leg.reset:
                level = np.full(n, 100.0)
        dead = np.minimum.accumulate(batt, axis=1) < 0 if flies else np.zeros((n, L), dtype=bool)
        min_b = batt.min(axis=1) if flies else np.full(n, 100.0)
        min_batt_all = np.minimum(min_batt_all, min_b)
        if flies:
            return_batt.append(batt[:, -1])
            success &= min_b >= HARD_FLOOR

        for j, leg in enumerate(legs):
            if not leg.task:
                continue
            task = ctx.tasks[leg.task]
            due = task.due if task.due is not None else ctx.deadline
            # relays are judged on arrival ("on station by"), other tasks on completion
            t_task = (t_end[:, j - 1] if j > 0 else np.full(n, ctx.now)) if task.type == "RELAY" else t_end[:, j]
            ok = (t_task <= due) & ~dead[:, j]
            p_task[leg.task] = float(ok.mean())
            # a late relay or second look costs score; the mission fails only on the mission
            # deadline, a late LIFE task, or a drone below the battery floor
            if task.priority == "LIFE":
                life_ok &= ok
                success &= ok
            else:
                success &= (t_task <= max(due, ctx.deadline)) & ~dead[:, j]

    # dropped tasks score zero; P(success) covers the tasks the plan actually commits to
    for tid in plan.dropped + plan.excluded:
        p_task[tid] = 0.0
        if ctx.tasks[tid].priority == "LIFE":
            life_ok &= False

    ids = all_task_ids if all_task_ids is not None else list(p_task)
    total_w = sum(PRIORITY_WEIGHT[ctx.tasks[t].priority] for t in ids) or 1.0
    value = sum(PRIORITY_WEIGHT[ctx.tasks[t].priority] * p_task.get(t, 0.0) for t in ids) / total_w
    rb = np.min(np.vstack(return_batt), axis=0) if return_batt else np.full(n, 100.0)
    p5 = float(np.percentile(rb, 5))
    shortfall = max(0.0, ctx.reserve - p5) / 100.0
    score = value - 0.5 * shortfall
    return {
        "p_success": round(float(success.mean()), 3),
        "p_life": round(float(life_ok.mean()), 3),
        "p5_return_batt": round(p5, 1),
        "min_batt_p5": round(float(np.percentile(min_batt_all, 5)), 1),
        "p_task": {k: round(v, 3) for k, v in p_task.items()},
        "score": round(float(score), 4),
        "runs": n,
    }

