"""Web server: FastAPI + WebSocket for the operator console.

  python -m tower.server            then open http://127.0.0.1:8000

The simulation runs inside the server's event loop. LLM calls (the only slow
part) run in a worker thread so robots keep moving while the model thinks.
"""
import asyncio
import contextlib
import hmac
import json
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import ROOT, load_settings
from .runtime import DT, Runtime

settings = load_settings()
rt = Runtime(settings, scripted=False)
clients: set[WebSocket] = set()


@contextlib.asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(sim_loop())
    asyncio.create_task(asyncio.to_thread(rt.llm.status, True))
    yield
    task.cancel()


app = FastAPI(title="TOWER ground station", lifespan=lifespan)
# lets a console hosted elsewhere (e.g. Vercel) read the report API; the API is read-only
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])


async def broadcast(msg):
    dead = []
    data = json.dumps(msg, default=str)
    for ws in list(clients):
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


async def sim_loop():
    last = time.monotonic()
    budget = 0.0
    last_push = 0.0
    sent_cov = -1
    while True:
        await asyncio.sleep(0.05)
        now = time.monotonic()
        elapsed, last = now - last, now
        if not rt.paused:
            budget = min(budget + elapsed * rt.speed, DT * 60)
            steps = 0
            while budget >= DT and steps < 60:
                rt.step(now)
                budget -= DT
                steps += 1
        else:
            rt.brain.tick(rt.world.t, now, [])
        while rt.goal_queue and rt.busy is None and rt.brain.phase == "RUNNING":
            asyncio.create_task(goal_change(rt.goal_queue.pop(0)))
        if now - last_push >= 0.5 and clients:
            last_push = now
            ver, _ = rt.coverage()
            snap = rt.snapshot(include_coverage=(ver != sent_cov))
            sent_cov = ver
            await broadcast(snap)


async def submit_mission(text):
    rt.busy = "compiling"
    await broadcast({"type": "busy", "busy": rt.busy})
    try:
        res = await asyncio.to_thread(rt.compile, text)
        rt.brain.start_mission(res)
    finally:
        rt.busy = None


async def goal_change(text):
    rt.busy = "goal change"
    rt.brain.log("operator", f"goal change submitted: \"{text[:100]}\" (compiling...)")
    try:
        res = await asyncio.to_thread(rt.compile, text, rt.brain.spec)
        rt.brain.apply_goal_change(res, text)
    finally:
        rt.busy = None


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    global rt
    await ws.accept()
    clients.add(ws)
    # with TOWER_ACCESS_KEY set, only links carrying ?key=... can control the mission; everyone else watches
    key = ws.query_params.get("key", "")
    can_control = not settings.access_key or hmac.compare_digest(key, settings.access_key)
    await ws.send_text(json.dumps({"type": "hello", "control": can_control, "protected": bool(settings.access_key)}))
    await ws.send_text(json.dumps({"type": "scenario", "scenario": rt.sc.to_client()}, default=str))
    await ws.send_text(json.dumps(rt.snapshot(include_coverage=True), default=str))
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg.get("type")
            if kind == "ping":
                continue                      # keep-alive from the console, so free hosts do not sleep
            if not can_control:
                await ws.send_text(json.dumps({"type": "error", "text": "View-only link: open the console with "
                                               "?key=<access key> to control the mission."}))
                continue
            if kind == "mission":
                text = (msg.get("text") or "").strip()
                if not text or rt.busy:
                    continue
                if rt.brain.phase == "RUNNING":
                    asyncio.create_task(goal_change(text))
                elif rt.brain.phase in ("IDLE", "DONE"):
                    if rt.brain.phase == "DONE":
                        rt.reset()
                    asyncio.create_task(submit_mission(text))
            elif kind == "answer":
                ok, why = rt.brain.answer(msg["id"], msg["option"], by="operator")
                if not ok:
                    await ws.send_text(json.dumps({"type": "error", "text": why}))
            elif kind == "inject":
                try:
                    rt.inject(msg["kind"], msg.get("args", {}), label=msg.get("label"))
                except Exception as e:
                    await ws.send_text(json.dumps({"type": "error", "text": str(e)}))
            elif kind == "control":
                a = msg.get("action")
                if a == "pause":
                    rt.paused = True
                elif a == "resume":
                    rt.paused = False
                elif a == "speed":
                    rt.speed = max(0.5, min(20.0, float(msg.get("value", 3))))
                elif a == "reset":
                    scripted = bool(msg.get("scripted", False))
                    rt.scripted = scripted
                    rt.reset()
                    await broadcast({"type": "scenario", "scenario": rt.sc.to_client()})
                    await broadcast(rt.snapshot(include_coverage=True))
                elif a == "llm_refresh":
                    await asyncio.to_thread(rt.llm.status, True)
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


@app.get("/api/scenario")
def api_scenario():
    return rt.sc.to_client()


@app.get("/api/report")
def api_report():
    return JSONResponse(json.loads(json.dumps(rt.report(), default=str)))


@app.get("/api/llm")
def api_llm():
    return rt.llm.status(refresh=True)


DIST = Path(ROOT) / "console" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="console")
else:
    @app.get("/")
    def no_console():
        return HTMLResponse(
            "<h2>TOWER backend is running.</h2><p>The console has not been built yet. In another terminal:</p>"
            "<pre>cd console\nnpm install\nnpm run build</pre><p>then restart the server, or run "
            "<code>npm run dev</code> and open http://localhost:5173.</p>")


def main():
    import uvicorn
    print(f"TOWER ground station on http://{settings.host}:{settings.port}")
    if settings.access_key:
        print("Access key set: share http://<address>/?key=<your key> with people who may control the mission")
    st = rt.llm.status(refresh=True)
    print(f"LLM: {st['provider']} / {st['model'] or '-'}  available={st['available']}  {st['detail']}")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
