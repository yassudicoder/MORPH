"""Decision ledger: every decision with trigger, options, scores, gate result,
who chose, and a plain-English explanation. Stored in SQLite (free, built in).

Explanations come from templates by default (instant, no AI cost). Set
TOWER_LLM_EXPLAIN=1 to have the local LLM rewrite them in the background.
"""
import json
import sqlite3
import threading
from pathlib import Path


class Ledger:
    def __init__(self, path=None):
        self.records = []
        self._lock = threading.Lock()
        self.db = None
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.db.execute("create table if not exists decisions (id text primary key, t real, json text)")
            self.db.commit()

    def new_id(self):
        return f"D-{len(self.records) + 1:03d}"

    def add(self, rec):
        with self._lock:
            self.records.append(rec)
            self._save(rec)
        return rec

    def update(self, rec):
        with self._lock:
            self._save(rec)

    def _save(self, rec):
        if self.db:
            self.db.execute("insert or replace into decisions values (?,?,?)",
                            (rec["id"], rec["t"], json.dumps(rec, default=str)))
            self.db.commit()

    def get(self, did):
        return next((r for r in self.records if r["id"] == did), None)

    def recent(self, n=40):
        return list(reversed(self.records[-n:]))


def fmt_t(s):
    s = max(0, int(s))
    return f"{s // 60:02d}:{s % 60:02d}"


def explain(rec, tasks):
    """Template explanation: what happened, what TOWER chose, what it costs."""
    trig = rec["trigger"]
    opts = rec.get("options", [])
    chosen = next((o for o in opts if o["id"] == rec.get("chosen")), opts[0] if opts else None)
    head = trig.get("summary", trig.get("type", "event"))
    if not chosen:
        return f"{head}. No feasible plan; robots hold safe until the operator decides."
    parts = [f"{head}."]
    if chosen.get("changes"):
        parts.append(" ".join(chosen["changes"][:3]) + ("." if not chosen["changes"][0].endswith(".") else ""))
    r = chosen.get("risk", {})
    if r:
        parts.append(f"Estimated success {r.get('p_success', 0) * 100:.0f}% over {r.get('runs', 200)} "
                     f"simulated runs; worst-case return battery {r.get('p5_return_batt', 0):.0f}%.")
    g = rec.get("gate", {})
    if g.get("result") == "ASK" and len(opts) > 1:
        alt = next(o for o in opts if o["id"] != chosen["id"])
        parts.append(f"Alternative was '{alt['label']}' (score {alt['score']:.2f} vs {chosen['score']:.2f}).")
    return " ".join(parts)
