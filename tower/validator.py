"""Validator: schema + physical feasibility checks on a MissionSpec.

Anything the LLM gets wrong dies here, before a robot moves. The error strings
are written so they can be fed straight back to the LLM for a retry.
"""
import re

from pydantic import ValidationError

from .model import MissionSpec
from .scenario import dist


def _agent_id(name, scenario):
    n = re.sub(r"[\s_]+", "-", str(name).strip().upper())
    if n in scenario.fleet:
        return n
    m = re.match(r"([A-Z]+)-?(\d+)$", n)
    if m and f"{m.group(1)}-{m.group(2)}" in scenario.fleet:
        return f"{m.group(1)}-{m.group(2)}"
    return n


NEEDS_PLACE = ("INSPECT", "DELIVER", "TRANSPORT")
LIFE_KINDS = ("DELIVER", "DELIVER_ON_DETECT")


def normalize(raw, scenario):
    """Parse a dict into a MissionSpec and fix harmless slips (case, aliases, priorities).

    Returns (spec or None, errors, notes). Notes record every automatic fix.
    """
    notes = []
    if not isinstance(raw, dict):
        return None, ["Reply must be a JSON object with an 'objectives' list."], notes
    raw = dict(raw)
    objs = raw.get("objectives")
    if not isinstance(objs, list):
        return None, ["'objectives' must be a list."], notes
    fixed = []
    for o in objs:
        if not isinstance(o, dict):
            return None, ["Every objective must be a JSON object."], notes
        o = dict(o)
        if isinstance(o.get("kind"), str):
            o["kind"] = o["kind"].strip().upper()
        if isinstance(o.get("priority"), str):
            o["priority"] = o["priority"].strip().upper()
        if isinstance(o.get("areas"), list):
            o["areas"] = [str(a).strip().upper().replace("SECTOR ", "") for a in o["areas"]]
        if o.get("place") in ("", None, "NONE", "none", "null"):
            o["place"] = None
        elif isinstance(o.get("place"), str):
            o["place"] = scenario.resolve_place(o["place"]) or o["place"]
        if isinstance(o.get("targets"), list):
            o["targets"] = [_agent_id(t, scenario) for t in o["targets"]]
        for k in ("payload_kg", "kits"):
            if o.get(k) is None:
                o.pop(k, None)
        if o.get("kind") not in NEEDS_PLACE and o.get("place"):
            o["place"] = None
        # clear fields that mean nothing for this kind, so stray values cannot fake a change
        kind = o.get("kind")
        if kind != "SURVEY":
            o["areas"] = []
        if kind != "KEEP_LINK":
            o["targets"] = []
        if kind != "TRANSPORT":
            o.pop("payload_kg", None)
        if kind != "DELIVER":
            o.pop("kits", None)
        if o.get("priority") == "LIFE" and o.get("kind") not in LIFE_KINDS:
            o["priority"] = "HIGH"
            notes.append(f"{o.get('kind')}: priority LIFE is reserved for kit deliveries, set to HIGH")
        if o.get("priority") != "LIFE" and o.get("kind") in LIFE_KINDS:
            o["priority"] = "LIFE"
            notes.append(f"{o.get('kind')}: kit deliveries are always LIFE priority")
        fixed.append(o)
    raw["objectives"] = fixed
    for k in ("deadline_min", "reserve_battery_pct"):
        if raw.get(k) is None:
            raw.pop(k, None)
    try:
        return MissionSpec.model_validate(raw), [], notes
    except ValidationError as e:
        errs = []
        for err in e.errors()[:8]:
            loc = ".".join(str(x) for x in err["loc"])
            errs.append(f"{loc}: {err['msg']}")
        return None, errs, notes


def _agents_with(scenario, cap):
    return [a for a in scenario.fleet.values() if cap in a.caps]


def _drone_round_trip_ok(scenario, agent, p, reserve, on_station_s=60):
    t = 2 * dist(scenario.base, p) / agent.speed + on_station_s
    return 100 * t / agent.endurance_s <= 100 - reserve


def validate(spec: MissionSpec, scenario):
    errs = []
    if not spec.objectives:
        return ["The mission has no objectives."]
    deadline_s = spec.deadline_min * 60
    if not 0 < spec.deadline_min <= 600:
        errs.append(f"deadline_min must be between 1 and 600 (got {spec.deadline_min}).")
    if not 0 <= spec.reserve_battery_pct <= 50:
        errs.append(f"reserve_battery_pct must be between 0 and 50 (got {spec.reserve_battery_pct}).")
    reserve = spec.reserve_battery_pct

    cams = _agents_with(scenario, "camera")
    delivers = _agents_with(scenario, "deliver")
    movers = _agents_with(scenario, "transport")
    total_kits = sum(a.kits for a in delivers)
    total_kg = sum(a.kg for a in movers)
    survey_work = 0.0

    for i, o in enumerate(spec.objectives):
        tag = f"objective {i + 1} ({o.kind})"
        if o.kind == "SURVEY":
            if not o.areas:
                errs.append(f"{tag}: list at least one sector in 'areas'.")
            bad = [a for a in o.areas if a not in scenario.sectors]
            if bad:
                errs.append(f"{tag}: sectors {bad} are not on the map. Valid: {sorted(scenario.sectors)}.")
            if not cams:
                errs.append(f"{tag}: no robot in the fleet has a camera.")
            for a in o.areas:
                if a in scenario.sectors and cams:
                    c = scenario.sector_center(a)
                    fastest = min(dist(scenario.base, c) / s.speed + scenario.survey_length(a) / s.survey_speed for s in cams)
                    survey_work += scenario.survey_length(a) / max(s.survey_speed for s in cams)
                    if fastest > deadline_s:
                        errs.append(f"{tag}: deadline is physically impossible; surveying {a} alone takes "
                                    f"at least {fastest / 60:.1f} min.")
                    if not any(_drone_round_trip_ok(scenario, s, c, reserve) for s in cams):
                        errs.append(f"{tag}: no camera drone can reach sector {a} and return with "
                                    f"{reserve:g}% battery left.")
        elif o.kind in ("INSPECT", "DELIVER", "TRANSPORT"):
            if not o.place:
                what = {"TRANSPORT": "the destination for the supplies", "DELIVER": "where to drop the kits",
                        "INSPECT": "what to look at"}[o.kind]
                errs.append(f"{tag}: 'place' is missing. Set it to {what}, one of {sorted(scenario.places)}.")
                continue
            if o.place not in scenario.places:
                errs.append(f"{tag}: place '{o.place}' is not on the map. Valid: {sorted(scenario.places)}.")
                continue
            p = scenario.place_pos(o.place)
            nf = scenario.in_no_fly(p)
            if o.kind == "INSPECT":
                if not cams:
                    errs.append(f"{tag}: no robot in the fleet has a camera.")
                elif nf:
                    errs.append(f"{tag}: {o.place} is inside no-fly zone {nf['id']} ({nf['why']}) "
                                f"and only drones carry cameras.")
                elif not any(_drone_round_trip_ok(scenario, s, p, reserve) for s in cams):
                    errs.append(f"{tag}: no camera drone can reach {o.place} and return with {reserve:g}% battery.")
            if o.kind == "DELIVER":
                if o.kits < 1:
                    errs.append(f"{tag}: kits must be at least 1.")
                if o.kits > total_kits:
                    errs.append(f"{tag}: fleet carries only {total_kits} kits in total, asked for {o.kits}.")
                air_ok = not nf and any(a.flies for a in delivers)
                ground_ok = any(not a.flies for a in delivers) and scenario.rover_reachable(p)
                if not (air_ok or ground_ok):
                    errs.append(f"{tag}: no delivery robot can reach {o.place} (no-fly zone and no road access).")
            if o.kind == "TRANSPORT":
                if o.payload_kg <= 0:
                    errs.append(f"{tag}: payload_kg must be more than 0.")
                if o.payload_kg > total_kg:
                    errs.append(f"{tag}: {o.payload_kg:g} kg is more than the whole fleet can carry "
                                f"({total_kg:g} kg across {len(movers)} rovers).")
                if not scenario.rover_reachable(p):
                    errs.append(f"{tag}: {o.place} has no road access for ground robots.")
                elif movers:
                    best = min(a.speed for a in movers)
                    r = scenario.rover_route(scenario.base, None, p, set())
                    if r and r[0] / best > deadline_s:
                        errs.append(f"{tag}: deadline is physically impossible; driving to {o.place} "
                                    f"takes at least {r[0] / best / 60:.1f} min.")
        elif o.kind == "DELIVER_ON_DETECT":
            if not delivers:
                errs.append(f"{tag}: no robot in the fleet can deliver kits.")
        elif o.kind == "KEEP_LINK":
            if not o.targets:
                errs.append(f"{tag}: list the robots to keep in contact with in 'targets'.")
            bad = [t for t in o.targets if t not in scenario.fleet]
            if bad:
                errs.append(f"{tag}: unknown robots {bad}. Valid: {sorted(scenario.fleet)}.")

    if cams and survey_work / len(cams) > deadline_s:
        errs.append(f"Deadline is physically impossible: the requested surveys need at least "
                    f"{survey_work / len(cams) / 60:.1f} min even with all {len(cams)} camera drones.")
    return errs
