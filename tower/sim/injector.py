"""Disruption injector: changes reality or the radio, never TOWER's belief.

TOWER has to notice every problem the way it would in the field: a rover that
stops moving, a heartbeat that goes missing, batteries draining faster.
"""


class Injector:
    def __init__(self, world, comms, timeline=None):
        self.world, self.comms = world, comms
        self.timeline = sorted(timeline or [], key=lambda e: e["at"])
        self.fired = []

    def apply(self, kind, args, t):
        """Returns (description, operator_text_or_None)."""
        w = self.world
        if kind == "debris":
            edge = w.add_debris(args["pos"])
            msg = f"debris dropped on road {edge[0]}-{edge[1]}" if edge else "debris dropped"
        elif kind == "agent_fail":
            w.kill(args["agent"], args.get("why", "motor failure"))
            msg = f"{args['agent']} {args.get('why', 'motor failure')}"
        elif kind == "wind":
            w.set_wind(float(args["speed"]), float(args.get("direction", 0)))
            msg = f"wind changing to {args['speed']} m/s"
        elif kind == "battery_fault":
            w.battery_fault(args["agent"], float(args.get("drop_pct", 35)))
            msg = f"{args['agent']} lost {args.get('drop_pct', 35)}% battery (cell fault)"
        elif kind == "blackout":
            self.comms.add_blackout(args["center"], float(args["radius"]), t + float(args.get("duration", 120)))
            msg = f"radio blackout r={args['radius']} m for {args.get('duration', 120)} s"
        elif kind == "survivor":
            sid = w.add_survivor(args["pos"], int(args.get("count", 1)))
            msg = f"hidden survivor {sid} appears at ({args['pos'][0]},{args['pos'][1]})"
        elif kind == "report":
            msg = "operator receives a report"
            self.fired.append({"t": t, "kind": kind, "msg": msg})
            return msg, args["text"]
        else:
            raise ValueError(f"unknown disruption '{kind}'")
        self.fired.append({"t": t, "kind": kind, "msg": msg})
        return msg, None

    def due(self, t):
        out = []
        while self.timeline and self.timeline[0]["at"] <= t:
            out.append(self.timeline.pop(0))
        return out
