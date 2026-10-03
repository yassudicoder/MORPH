"""Shared data types: the task library, MissionSpec (compiler output) and Task."""
from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import BaseModel, Field

# The only actions TOWER may ever schedule. The LLM cannot invent new ones.
TASK_LIBRARY = {
    "SURVEY":    {"needs": {"camera"},    "what": "cover a sector with a lawnmower pattern"},
    "INSPECT":   {"needs": {"camera"},    "what": "hover over a point for a second look"},
    "DELIVER":   {"needs": {"deliver"},   "what": "drop medical kits at a point"},
    "RELAY":     {"needs": {"fly"},       "what": "hover at a relay point and forward radio traffic"},
    "TRANSPORT": {"needs": {"transport"}, "what": "carry supplies (kg) to a place"},
    "RTB":       {"needs": set(),         "what": "return to base: land, swap battery, reload"},
    "HOLD":      {"needs": set(),         "what": "hover or park in place and wait"},
}

PRIORITY_WEIGHT = {"LIFE": 100_000, "HIGH": 1_000, "NORMAL": 10}
PRIORITY_RANK = {"LIFE": 0, "HIGH": 1, "NORMAL": 2}

OBJECTIVE_KINDS = ["SURVEY", "INSPECT", "DELIVER", "DELIVER_ON_DETECT", "TRANSPORT", "KEEP_LINK"]


class Objective(BaseModel):
    kind: Literal["SURVEY", "INSPECT", "DELIVER", "DELIVER_ON_DETECT", "TRANSPORT", "KEEP_LINK"]
    areas: list[str] = Field(default_factory=list, description="sector ids for SURVEY")
    place: Optional[str] = Field(default=None, description="place id for INSPECT / DELIVER / TRANSPORT")
    payload_kg: float = 0
    kits: int = 0
    targets: list[str] = Field(default_factory=list, description="agent ids for KEEP_LINK")
    priority: Literal["LIFE", "HIGH", "NORMAL"] = "HIGH"


class MissionSpec(BaseModel):
    summary: str = ""
    deadline_min: float = 45
    reserve_battery_pct: float = 15
    objectives: list[Objective]


def mission_json_schema(scenario):
    """Hand-written, $ref-free schema so even small local models can follow it.

    Ollama turns this into a grammar, so the model physically cannot emit a task
    type or sector that is not in the library or on the map.
    """
    places = list(scenario.places)
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "deadline_min": {"type": "number"},
            "reserve_battery_pct": {"type": "number"},
            "objectives": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": OBJECTIVE_KINDS},
                        "place": {"type": "string", "enum": places + ["NONE"]},
                        "areas": {"type": "array", "items": {"type": "string", "enum": list(scenario.sectors)}},
                        "payload_kg": {"type": "number"},
                        "kits": {"type": "integer"},
                        "targets": {"type": "array", "items": {"type": "string", "enum": list(scenario.fleet)}},
                        "priority": {"type": "string", "enum": ["LIFE", "HIGH", "NORMAL"]},
                    },
                    "required": ["kind", "place", "priority"],
                },
            },
        },
        "required": ["summary", "deadline_min", "reserve_battery_pct", "objectives"],
    }


@dataclass
class Task:
    id: str
    type: str                       # one of TASK_LIBRARY
    priority: str                   # LIFE | HIGH | NORMAL
    pos: tuple
    area: Optional[str] = None      # SURVEY sector
    place: Optional[str] = None
    kits: int = 0
    kg: float = 0.0
    due: Optional[float] = None     # absolute mission seconds
    release: float = 0.0            # earliest start
    duration: float = 0.0           # RELAY: expected hover time
    origin: str = ""                # objective or event that created it
    status: str = "PENDING"         # PENDING | ACTIVE | DONE | FAILED | DROPPED | CANCELLED
    assignee: Optional[str] = None
    progress: float = 0.0           # SURVEY fraction done
    eta: Optional[float] = None
    meta: dict = field(default_factory=dict)

    @property
    def open(self):
        return self.status in ("PENDING", "ACTIVE")

    def label(self):
        where = self.area or self.place or f"({self.pos[0]:.0f},{self.pos[1]:.0f})"
        extra = f" {self.kits} kit" + ("s" if self.kits != 1 else "") if self.kits else ""
        extra += f" {self.kg:g} kg" if self.kg else ""
        return f"{self.type} {where}{extra}"

    def to_client(self):
        return {"id": self.id, "type": self.type, "priority": self.priority, "pos": self.pos,
                "area": self.area, "place": self.place, "kits": self.kits, "kg": self.kg,
                "due": self.due, "status": self.status, "assignee": self.assignee,
                "progress": round(self.progress, 2), "eta": self.eta, "label": self.label(),
                "origin": self.origin, "meta": self.meta}
