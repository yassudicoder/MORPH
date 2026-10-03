"""Mission compiler: operator text -> MissionSpec -> concrete tasks.

The LLM (free, local Ollama by default) only translates language into a typed
spec built from the fixed task library. If the LLM is unavailable, or its reply
still fails validation after 2 retries, the rule-based compiler takes over, so
the system always works with zero AI cost.
"""
import json
import re
from dataclasses import dataclass, field

from .llm import LLMError
from .model import MissionSpec, mission_json_schema
from .validator import normalize, validate

MAX_RETRIES = 2


@dataclass
class CompileResult:
    spec: MissionSpec | None
    source: str                      # "llm" | "rules" | "failed"
    model: str = ""
    attempts: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    notes: list = field(default_factory=list)


# --------------------------------------------------------------------- LLM
def _system_prompt(scenario):
    places = "; ".join(f"{pid} = {', '.join(p['aliases'])}" for pid, p in scenario.places.items())
    fleet = "; ".join(f"{a.id} ({', '.join(sorted(a.caps))})" for a in scenario.fleet.values())
    return (
        "You are TOWER's mission compiler for a disaster-response robot swarm. Convert the operator's "
        "instruction into a MissionSpec JSON object. Do NOT assign robots or plan routes; a solver does that.\n\n"
        "Objective kinds (use only these):\n"
        "- SURVEY: search sectors for survivors. Fill 'areas' with sector ids.\n"
        "- DELIVER_ON_DETECT: drop medical kits to any survivor found. No place needed. Priority LIFE.\n"
        "- DELIVER: drop 'kits' medical kits at a known 'place'. Priority LIFE.\n"
        "- INSPECT: send a camera drone to look at a 'place'. Use it whenever someone is reported at a place "
        "or the operator says check / look at / verify a place.\n"
        "- TRANSPORT: carry 'payload_kg' kilograms of supplies to a 'place'.\n"
        "- KEEP_LINK: keep radio contact with the robots listed in 'targets'.\n\n"
        f"Sectors: {', '.join(scenario.sectors)}. 'Sectors A to F' means all six.\n"
        f"Places (id = names people use): {places}.\n"
        f"Fleet: {fleet}. 'Both rovers' means ROVER-1 and ROVER-2.\n"
        "Every objective must set 'place': the destination for TRANSPORT, DELIVER and INSPECT, or NONE.\n"
        "Priorities: LIFE only for DELIVER and DELIVER_ON_DETECT, HIGH for main goals, NORMAL for nice-to-have.\n"
        "deadline_min: minutes the operator allows (default 45). reserve_battery_pct: battery reserve (default 15).\n"
        "Reply with the JSON object only."
    )


def _llm_once(llm, scenario, user):
    raw = llm.json_chat(_system_prompt(scenario), user, mission_json_schema(scenario))
    return raw


def compile_mission(text, scenario, llm, current: MissionSpec | None = None):
    """Compile (or, with `current`, amend) a mission. Never raises."""
    attempts = []
    if current is None:
        base_user = f"Operator instruction:\n{text}"
    else:
        base_user = ("Current mission spec:\n" + current.model_dump_json() +
                     f"\n\nOperator's new instruction:\n{text}\n\n"
                     "Return the complete updated MissionSpec: keep existing objectives unless the "
                     "instruction changes or removes them, and add any new ones.")
    if llm is not None and llm.enabled:
        user, previous, errors = base_user, None, None
        for attempt in range(1 + MAX_RETRIES):
            if errors:
                user = (base_user + "\n\nYour previous reply was:\n" + json.dumps(previous) +
                        "\nThe validator rejected it:\n- " + "\n- ".join(errors) +
                        "\nReturn a corrected, complete MissionSpec.")
            try:
                previous = _llm_once(llm, scenario, user)
            except LLMError as e:
                attempts.append({"attempt": attempt + 1, "source": "llm", "errors": [str(e)]})
                break
            spec, errors, notes = normalize(previous, scenario)
            if spec is not None:
                errors = validate(spec, scenario)
                if not errors and current is not None:
                    errors = _amend_errors(spec, current, text, scenario)
            attempts.append({"attempt": attempt + 1, "source": "llm", "errors": errors})
            if not errors:
                return CompileResult(spec, "llm", llm.model, attempts, notes=notes)
    # rule-based fallback (also the only path in rules mode)
    raw = rule_compile(text, scenario) if current is None else rule_amend(text, current, scenario)
    spec, errors, notes = normalize(raw, scenario)
    if spec is not None:
        errors = validate(spec, scenario)
    attempts.append({"attempt": len(attempts) + 1, "source": "rules", "errors": errors})
    if not errors:
        return CompileResult(spec, "rules", "rule-based", attempts, notes=notes)
    return CompileResult(None, "failed", "", attempts, errors)


def _amend_errors(spec, current, text, scenario):
    """Grounding checks for a goal change: it must change something, and use the places the operator named."""
    if _same_mission(spec, current):
        return ["Your reply is identical to the current mission, but the operator asked for a change: "
                f"\"{text}\". Add, remove or change objectives to do what they asked."]
    t = " " + text.lower() + " "
    used = {o.place for o in spec.objectives if o.place}
    named = [pid for pid, p in scenario.places.items() if pid != "BASE"
             and any(re.search(rf"{re.escape(a)}", t) for a in p["aliases"])]
    missing = [pid for pid in named if pid not in used]
    if missing:
        return [f"The operator mentioned {', '.join(missing)} but no objective uses it. Add an objective "
                f"with 'place' set to {missing[0]} (for example INSPECT to check it)."]
    return []


def _same_mission(a: MissionSpec, b: MissionSpec):
    key = lambda s: (s.deadline_min, s.reserve_battery_pct,
                     sorted(o.model_dump_json(exclude={"priority"}) for o in s.objectives))
    return key(a) == key(b)


# ------------------------------------------------------------------- rules
def _num(pattern, text, default=None):
    m = re.search(pattern, text)
    return float(m.group(1)) if m else default


def _areas(t, scenario):
    secs = sorted(scenario.sectors)
    found = []
    for m in re.finditer(r"\b(?:sectors?|zones?|areas?)\s+([a-z])\s*(?:-|–|to|through|thru)\s*([a-z])\b", t):
        a, b = m.group(1).upper(), m.group(2).upper()
        found += [s for s in secs if a <= s <= b]
    for m in re.finditer(r"\b(?:sectors?|zones?|areas?)\s+((?:[a-z]\b(?:\s*,\s*|\s+and\s+|\s*&\s*|\s+)?)+)", t):
        found += [c.upper() for c in re.findall(r"\b([a-z])\b", m.group(1)) if c.upper() in scenario.sectors]
    if re.search(r"\ball (?:the )?(?:sectors|zones|areas)\b|\beverywhere\b|\bwhole (?:village|area|map)\b", t):
        found += secs
    return [s for s in secs if s in found]


def _places_after(t, words, scenario):
    out = []
    for w in words:
        for m in re.finditer(rf"\b{w}\b(.{{0,60}})", t):
            pid = scenario.resolve_place(m.group(1))
            if pid and pid not in out:
                out.append(pid)
    return out


def rule_compile(text, scenario):
    t = " " + text.lower().replace("\n", " ") + " "
    objectives = []
    areas = _areas(t, scenario)
    wants_search = re.search(r"\b(search|find|survey|locate|look for|scan|sweep)\b", t)
    if wants_search and not areas:
        areas = sorted(scenario.sectors)
    if areas:
        objectives.append({"kind": "SURVEY", "areas": areas, "priority": "HIGH"})
    if re.search(r"\b(kit|kits|medical|first aid|medicine)\b", t) and \
            re.search(r"\b(anyone|any one|survivors?|found|people|victims?)\b", t):
        objectives.append({"kind": "DELIVER_ON_DETECT", "priority": "LIFE"})
    kg = _num(r"(\d+(?:\.\d+)?)\s*(?:kg|kilo)", t)
    if kg:
        dest = _places_after(t, ["to", "at", "for"], scenario)
        dest = [d for d in dest if d != "BASE"]
        objectives.append({"kind": "TRANSPORT", "payload_kg": kg,
                           "place": dest[0] if dest else "SHELTER", "priority": "HIGH"})
    for m in re.finditer(r"\bdeliver\s+(\d+)\s+(?:medical\s+)?kits?\s+(?:to|at)\s+(.{0,40})", t):
        pid = scenario.resolve_place(m.group(2))
        if pid:
            objectives.append({"kind": "DELIVER", "kits": int(m.group(1)), "place": pid, "priority": "LIFE"})
    for pid in _places_after(t, ["check", "inspect", "look at", "verify", "near", "trapped"], scenario):
        if pid not in ("BASE", "SHELTER") or "check" in t:
            objectives.append({"kind": "INSPECT", "place": pid, "priority": "HIGH"})
    if re.search(r"\b(radio|contact|communication|comms|link|connected|in touch)\b", t):
        rovers = [a.id for a in scenario.fleet.values() if not a.flies]
        named = [aid for aid in scenario.fleet if aid.lower() in t or aid.lower().replace("-", " ") in t]
        targets = named or (rovers if re.search(r"\brovers?\b|ground", t) else rovers)
        objectives.append({"kind": "KEEP_LINK", "targets": targets, "priority": "HIGH"})
    deadline = _num(r"(\d+(?:\.\d+)?)\s*(?:min|mins|minutes)\b", t, 45)
    if re.search(r"(\d+(?:\.\d+)?)\s*(?:hour|hours|hr|hrs)\b", t):
        deadline = _num(r"(\d+(?:\.\d+)?)\s*(?:hour|hours|hr|hrs)\b", t) * 60
    reserve = _num(r"(\d+(?:\.\d+)?)\s*%", t, 15)
    # de-duplicate INSPECTs
    seen, uniq = set(), []
    for o in objectives:
        k = (o["kind"], o.get("place"), tuple(o.get("areas", [])))
        if k not in seen:
            seen.add(k)
            uniq.append(o)
    return {"summary": text.strip()[:200], "deadline_min": deadline,
            "reserve_battery_pct": reserve, "objectives": uniq}


def rule_amend(text, current: MissionSpec, scenario):
    t = " " + text.lower() + " "
    spec = current.model_dump()
    add = rule_compile(text, scenario)
    m = re.search(r"(?:extend|push|move)\b.{0,30}?\bby\s+(\d+)\s*(?:min|minutes)", t)
    if m:
        spec["deadline_min"] = current.deadline_min + float(m.group(1))
    elif re.search(r"\b(deadline|finish|within|in)\b.{0,20}\d+\s*(?:min|minutes)", t):
        spec["deadline_min"] = add["deadline_min"]
    if re.search(r"\d+\s*%", t):
        spec["reserve_battery_pct"] = add["reserve_battery_pct"]
    if re.search(r"\b(cancel|stop|abort|drop|forget)\b.{0,30}\b(transport|supplies|kg)", t):
        spec["objectives"] = [o for o in spec["objectives"] if o["kind"] != "TRANSPORT"]
    keys = {(o["kind"], o.get("place"), tuple(o.get("areas") or [])) for o in spec["objectives"]}
    for o in add["objectives"]:
        k = (o["kind"], o.get("place"), tuple(o.get("areas") or []))
        if o["kind"] in ("DELIVER_ON_DETECT", "KEEP_LINK") and any(x[0] == o["kind"] for x in keys):
            continue
        if k not in keys:
            spec["objectives"].append(o)
    spec["summary"] = (current.summary + " | " + text.strip())[:300]
    return spec


# ------------------------------------------------------------------ expand
def expand(spec: MissionSpec, scenario):
    """Turn objectives into task templates. Policies (deliver-on-detect, keep-link) are returned separately."""
    deadline_s = spec.deadline_min * 60
    tasks, policy = [], {"deliver_on_detect": None, "keep_link": [], "reserve": spec.reserve_battery_pct,
                         "deadline_s": deadline_s}
    max_kg = max((a.kg for a in scenario.fleet.values() if "transport" in a.caps), default=0)
    for o in spec.objectives:
        if o.kind == "SURVEY":
            for a in o.areas:
                tasks.append({"key": ("SURVEY", a), "type": "SURVEY", "priority": o.priority, "area": a,
                              "pos": scenario.sector_center(a), "due": deadline_s})
        elif o.kind == "INSPECT":
            tasks.append({"key": ("INSPECT", o.place), "type": "INSPECT", "priority": o.priority,
                          "place": o.place, "pos": scenario.place_pos(o.place), "due": deadline_s})
        elif o.kind == "DELIVER":
            tasks.append({"key": ("DELIVER", o.place), "type": "DELIVER", "priority": o.priority,
                          "place": o.place, "kits": max(1, o.kits), "pos": scenario.place_pos(o.place),
                          "due": deadline_s})
        elif o.kind == "TRANSPORT":
            left, n = o.payload_kg, 0
            while left > 1e-6 and max_kg > 0:
                chunk = min(max_kg, left)
                n += 1
                tasks.append({"key": ("TRANSPORT", o.place, n), "type": "TRANSPORT", "priority": o.priority,
                              "place": o.place, "kg": chunk, "pos": scenario.place_pos(o.place), "due": deadline_s})
                left -= chunk
        elif o.kind == "DELIVER_ON_DETECT":
            policy["deliver_on_detect"] = o.priority
        elif o.kind == "KEEP_LINK":
            policy["keep_link"] = sorted(set(policy["keep_link"]) | set(o.targets))
    return tasks, policy
