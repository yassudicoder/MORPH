"""Run a whole mission in the terminal, as fast as the CPU allows.

  python -m tower.headless                      # rule-based compiler, scripted disruptions
  python -m tower.headless --llm                # use the configured free LLM (Ollama by default)
  python -m tower.headless --text "Survey sectors A and B, keep 20% reserve"
  python -m tower.headless --no-script          # no disruptions

Operator questions (ASK) are answered with TOWER's recommended default, so
the run is fully automatic. Prints the decision ledger and the after-action report.
"""
import argparse
import json
import sys
import time

from .config import load_settings
from .ledger import fmt_t
from .runtime import Runtime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")


def main():
    ap = argparse.ArgumentParser(description="TOWER headless mission run")
    ap.add_argument("--text", help="operator objective (default: scenario's default)")
    ap.add_argument("--llm", action="store_true", help="compile with the configured LLM instead of rules")
    ap.add_argument("--no-script", action="store_true", help="do not inject the scripted disruptions")
    ap.add_argument("--max-min", type=float, default=60, help="stop after this many mission minutes")
    ap.add_argument("--json", action="store_true", help="print the after-action report as JSON")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    s = load_settings()
    if not args.llm:
        s.kind, s.provider = "rules", "rules"
    rt = Runtime(s, scripted=not args.no_script, save_ledger=False)
    text = args.text or rt.sc.raw["default_objective"]
    print(f"OBJECTIVE: {text}\n")
    t0 = time.perf_counter()
    rec = rt.submit_mission(text)
    c = rt.brain.compile_info
    print(f"COMPILER: {c['source']} {c['model']} in {len(c['attempts'])} attempt(s), {time.perf_counter() - t0:.1f} s")
    for a in c["attempts"]:
        if a["errors"]:
            print(f"   attempt {a['attempt']} ({a['source']}) rejected: {a['errors'][:2]}")
    if rt.brain.phase != "AWAIT_APPROVAL":
        print("Mission rejected:", c["errors"])
        return 1
    seen = 0

    def flush():
        nonlocal seen
        recs = rt.brain.ledger.records
        while seen < len(recs):
            r = recs[seen]
            seen += 1
            if args.quiet:
                continue
            g = r["gate"]["result"]
            print(f"[T+{fmt_t(r['t'])}] {r['id']} {r['level']:<2} {g:<5} {r['trigger'].get('summary', '')[:150]}")
            if r.get("options") and g != "AUTO":
                for o in r["options"]:
                    rk = o["risk"]
                    print(f"      option {o['id']}: {o['label'][:60]:<60} P={rk.get('p_success', 0):.2f} "
                          f"score={o['score']:.3f} p5batt={rk.get('p5_return_batt', 0):.0f}%")
                print(f"      gate: {'; '.join(r['gate']['why'])}")

    flush()
    # the first plan always needs a person; headless approves the recommended default
    for eid in list(rt.brain.escalations):
        rt.brain.answer(eid, "A", by="headless")
    flush()
    wall = 0.0
    t0 = time.perf_counter()
    while rt.brain.phase == "RUNNING" and rt.world.t < args.max_min * 60:
        wall += 0.5 / 3.0
        rt.step(wall)
        while rt.goal_queue:
            text2 = rt.goal_queue.pop(0)
            print(f"[T+{fmt_t(rt.world.t)}] OPERATOR: {text2}")
            rt.goal_change(text2)
        for eid, e in list(rt.brain.escalations.items()):
            if e["status"] == "open":
                rt.brain.answer(eid, e["recommended"] or "RTB", by="headless")
        flush()
    flush()
    rep = rt.report()
    print(f"\nSimulated {fmt_t(rt.world.t)} of mission in {time.perf_counter() - t0:.1f} s wall time. Phase: {rep['phase']}")
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
        return 0
    print(f"Tasks: {rep['tasks']}")
    print(f"Sectors surveyed: {rep['surveyed']}  kits delivered: {rep['kits_delivered']}  supplies: {rep['supplies_kg']} kg")
    tr = rep["truth"]
    print("Survivors (truth): " + ", ".join(f"{s['id']} {s['served']}/{s['count']} served" for s in tr["survivors"]))
    print(f"Robots lost: {tr['robots_lost'] or 'none'}   Final drone battery: {tr['lowest_battery']}")
    cm = rep["comms"]
    print(f"Radio: bytes by class {cm['bytes']}; state reports avoided by dead reckoning: {cm['suppressed_state']}; "
          f"superseded in queue: {cm['superseded']}")
    n = rt.llm_calls
    print(f"LLM calls: {n} (cost: Rs 0, {'free model: ' + rt.llm.model if n else 'no LLM used'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
