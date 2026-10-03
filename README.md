# TOWER: Tactical Orchestration With Explainable Re-planning

Mission control for a mixed swarm of drones and ground robots (Elevate 1.0, EL-05).
TOWER plans the mission, watches it, repairs the plan when things break, and asks a
human only when the call is close or can't be undone.

**Total cost: ₹0.** No paid API, no paid cloud, no licences. The only AI is a free
local model (Ollama), and if that's missing TOWER falls back to a rule-based
compiler, so the whole system still works with no AI at all.

---

## Quick start (Windows)

You need Python 3.10+ and Node.js 18+ (both free).

```bat
cd tower
start.bat
```

That installs the Python packages, builds the console once, and opens
http://127.0.0.1:8000. On Linux, macOS or WSL, run `./start.sh` instead.

Manual steps, if you prefer:

```bash
pip install -r requirements.txt
cd console && npm install && npm run build && cd ..
python -m tower.server          # open http://127.0.0.1:8000
```

### The free LLM

TOWER uses Ollama by default and auto-picks a model you already have installed.

```bash
ollama pull qwen2.5:3b          # 2 GB, good on a laptop CPU (qwen2.5:7b is more reliable)
```

To choose a provider, copy `.env.example` to `.env`:

| `TOWER_LLM_PROVIDER` | What it is | Cost | Needs |
|---|---|---|---|
| `ollama` (default) | local model, works offline | free | Ollama installed |
| `lmstudio` | local model via LM Studio | free | LM Studio server running |
| `groq` | cloud, OpenAI-compatible | free tier, rate-limited | `GROQ_API_KEY` |
| `gemini` | cloud, OpenAI-compatible | free tier, rate-limited | `GEMINI_API_KEY` |
| `openrouter` | cloud, free models | free (`:free` models) | `OPENROUTER_API_KEY` + model name |
| `rules` | no LLM, keyword compiler | free | nothing |

Free cloud tiers change their model names now and then. If one stops working,
pick a current model from the provider's list and set `TOWER_LLM_MODEL`.

**Why a small local model is enough:** the LLM never controls a robot. It only
translates the operator's sentence into a typed `MissionSpec`, built from a fixed
task library. Ollama enforces the JSON schema as a grammar, so even a 4B model
can't invent a task type or a sector that isn't on the map. If the spec is still
wrong (for example, a missing destination), the validator's error goes back to
the model for up to 2 retries, as in the architecture diagram. Then the rule
compiler takes over. On a CPU laptop one compile takes about 15–20 s.

---

## Running the demo

1. Click **Reset + scripted demo**, then **Compile & plan**. The mission text is pre-filled.
2. The first plan always needs a person: click **Approve plan A** in the escalation inbox.
3. Set the speed to **10×** and watch. The scripted timeline does this:

| Mission time | What happens | What TOWER does |
|---|---|---|
| ~03:40 | SCOUT-2 finds 2 survivors behind the ridge (no radio there) | detection arrives late via store-and-forward; L1: LIFT-1 delivers kits |
| ~07:00 | low-confidence heat signature (a cattle shed) | L1: second look before committing a kit; false alarm, no kit wasted |
| 05:00 / ~07:50 | debris on the lower road | rover stops moving, reports a blocked edge; L2: re-route over the upper bridge |
| ~08:10 | 3 survivors in sector C | L1: LIFT-1 goes back to base to reload, then delivers |
| 10:20 | wind rises to 11 m/s | L2: battery use re-estimated, scouts get battery-swap stops |
| 14:00 | villagers report someone at the temple | L3: LLM re-compiles the goal; **ASK** (goal changes need approval) |
| ~16:40 | rover will drive into the radio shadow | L1: predicted link loss, so a relay drone is sent *before* contact drops |
| 19:10 | RELAY-1 motor failure | L2: **ASK**: keep the relay with SCOUT-1 (battery risk) or drop it |

Use the **Disruption panel** to add your own failures at any time: kill a robot,
a battery cell fault, a radio blackout, a hidden survivor, or a villager report.
Tick **ground truth** on the map to see what's really out there, compared with
what TOWER believes.

### No browser? Run it in the terminal

```bash
python -m tower.headless              # rule compiler, scripted disruptions, ~5 s
python -m tower.headless --llm        # compile with your free LLM
python -m tower.headless --text "Survey sectors A and B and keep 20% reserve"
```

### Put it online

See **[HOSTING.md](HOSTING.md)**: a laptop + Cloudflare Tunnel for demo day,
Render's free plan for a link that stays up, or a free VM. All ₹0. A
`Dockerfile` and `render.yaml` are included.

### Tests

```bash
python -m pytest -q                   # 15 tests, no LLM or network needed
```

---

## Where each box of the architecture lives

| Diagram box | File | What it does |
|---|---|---|
| Operator console | `console/src/` | React + TypeScript + WebSocket: map, robots, inbox, decision feed, disruption panel |
| 1. Mission compiler | `tower/compiler.py`, `tower/llm.py` | text → `MissionSpec` (free LLM or rules); task library in `tower/model.py` |
| 2. Validator | `tower/validator.py` | schema, capability, payload, deadline, no-fly and battery-reserve checks |
| 3. Planner | `tower/planner.py` | regret insertion + local search; battery, payload, capability, terrain, priority |
| 4. Risk engine | `tower/risk.py` | 200 NumPy Monte Carlo runs: P(success), P(LIFE on time), worst-case battery |
| 5. Autonomy gate | `tower/gate.py` | AUTO / ASK (60 s timer, safe default) / HOLD |
| 6. Dispatcher | `tower/brain.py` (`_commit`, `_dispatch_all`) | versioned plans; each robot gets only its own list + rally point |
| Robot swarm | `tower/sim/robot.py`, `tower/sim/bt.py` | behavior tree, L0 playbook, rally point, mesh auction |
| World model | `tower/world_model.py` | belief built only from delivered messages, every field timestamped |
| Monitor | `tower/brain.py` (`monitor`) | every second: battery projection, heartbeats, predicted link loss, wind |
| Re-plan engine | `tower/brain.py` (`replan`) | L0 on robot, L1 insert/remove, L2 warm re-solve, L3 re-compile / extend deadline |
| Decision ledger | `tower/ledger.py` | SQLite log, plain-English explanation per decision (`runs/` folder) |
| Radio | `tower/sim/comms.py` | path loss, ridge line-of-sight, mesh, bandwidth cap, P0–P4 queue, store-and-forward |
| Gazebo world | `tower/sim/world.py` | built-in 2D stand-in; see `GAZEBO.md` to swap in Gazebo |

The scenario (map, fleet, radio, hidden survivors, scripted timeline) is a single
file, `scenarios/landslide_v1.json`. Change it to run mine inspection, crop
surveys or pipeline patrols with the same brain.

---

## What changed from the field-notes design (to keep it free)

| Field notes said | This build uses | Why |
|---|---|---|
| Claude Sonnet / Haiku API (≈₹16 per mission) | Ollama local model, or rules (₹0) | no budget needed, works offline |
| Google OR-Tools | built-in Python routing heuristic | nothing to install; handles automatic battery swaps and reloads; OR-Tools is still free if you want to swap it in |
| Gazebo + ROS 2 from day one | built-in 2D simulator, Gazebo as the next step | runs on Windows today; your doc's build plan already starts the brain on a 2D world |
| LLM explanations | templates (instant); optional local-LLM polish with `TOWER_LLM_EXPLAIN=1` | zero cost, zero wait |
| Cloud VM fallback | not needed | a full 25-minute mission simulates in ~5 s on a laptop |

Every number in the demo comes from the simulation, not from field measurements.
