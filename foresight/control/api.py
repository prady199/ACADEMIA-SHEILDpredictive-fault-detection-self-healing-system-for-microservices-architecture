"""REST and WebSocket API plus the dashboard. Interactive API docs are at /docs."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from foresight import config as cfg
from foresight.control.assistant import ask
from foresight.control.incidents import incident_report
from foresight.control.platform import MODES, Platform, num
from foresight.ml import faults as fl


class ChaosBody(BaseModel):
    target: str
    kind: str
    params: dict | None = None


class ModeBody(BaseModel):
    mode: str


class LoadBody(BaseModel):
    rps: float = Field(ge=0, le=200)


class AskBody(BaseModel):
    question: str = Field(max_length=500)


class SettingsBody(BaseModel):
    webhook_url: str = Field(default="", max_length=500)


def create_app(mode: str = "autonomous", rps: float = cfg.DEFAULT_RPS, db_path=None) -> FastAPI:
    platform = Platform(mode=mode, rps=rps, db_path=db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await platform.start()
        yield
        await platform.stop()

    app = FastAPI(title="Academia Shield", description="Microservice failure prediction and recovery", version="1.0", lifespan=lifespan)
    app.state.platform = platform

    # ------------------------------------------------------------ live state
    @app.get("/api/state")
    async def state() -> dict:
        return platform.snapshot or platform.build_snapshot()

    @app.get("/api/topology")
    async def topology() -> dict:
        return {"services": sorted(platform.deps.services), "edges": platform.deps.edge_rows(),
                "blast_radius": {s: platform.deps.blast_radius(s) for s in platform.deps.services}}

    @app.get("/api/history/{instance}")
    async def history(instance: str, seconds: int = 300) -> list[dict]:
        rows = platform.history.window(instance, min(seconds, 900))
        keys = ("ts", "mem_pct", "cpu", "err_self", "err_dep", "p95_ms", "self_p95_ms", "lag_ms", "pool_pct", "rps", "risk_ema")
        return [{k: num(r.get(k)) for k in keys} for r in rows]

    @app.get("/api/traffic")
    async def traffic(seconds: int = 300) -> list[dict]:
        since = int(time.time()) - min(seconds, 3600)
        return [v for t, v in sorted(platform.traffic.items()) if t >= since]

    # ------------------------------------------------------------ history
    @app.get("/api/incidents")
    async def incidents(limit: int = 100) -> list[dict]:
        return platform.store.incidents(min(limit, 1000))

    @app.get("/api/incidents/{iid}")
    async def incident(iid: int) -> dict:
        inc = platform.store.incident(iid)
        if not inc:
            raise HTTPException(404, f"No incident {iid}")
        return inc

    @app.get("/api/incidents/{iid}/report", response_class=PlainTextResponse)
    async def report(iid: int) -> str:
        inc = platform.store.incident(iid)
        if not inc:
            raise HTTPException(404, f"No incident {iid}")
        return incident_report(inc)

    @app.get("/api/analytics")
    async def analytics() -> dict:
        return platform.analytics()

    @app.get("/api/notifications")
    async def notifications(limit: int = 100) -> list[dict]:
        return platform.store.notifications(min(limit, 500))

    @app.get("/api/model")
    async def model() -> dict:
        return platform.predictor.card or {}

    @app.get("/api/benchmark")
    async def benchmark() -> dict:
        path = cfg.MODEL_DIR / "benchmark.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    # ------------------------------------------------------------ control
    @app.post("/api/mode")
    async def set_mode(body: ModeBody) -> dict:
        try:
            await platform.set_mode(body.mode)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"mode": platform.mode, "label": MODES[platform.mode]}

    @app.get("/api/chaos/catalog")
    async def catalog() -> dict:
        return {"progressive": fl.PROGRESSIVE, "sudden": fl.SUDDEN + ["bad_release"], "benign": fl.BENIGN, "labels": fl.LABELS}

    @app.post("/api/chaos")
    async def chaos(body: ChaosBody) -> dict:
        try:
            return await platform.inject(body.target, body.kind, body.params)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/services/{target}/restart")
    async def restart(target: str) -> dict:
        try:
            return await platform.restart(target)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/services/{service}/rollback")
    async def rollback(service: str) -> dict:
        if service not in cfg.TOPOLOGY:
            raise HTTPException(404, f"No service {service}")
        await platform.recovery.rollback(service)
        return {"ok": True}

    @app.post("/api/load")
    async def load(body: LoadBody) -> dict:
        return {"rps": await platform.set_load(body.rps)}

    @app.post("/api/assistant")
    async def assistant(body: AskBody) -> dict:
        return ask(platform, body.question)

    @app.post("/api/scenario/start")
    async def scenario_start() -> dict:
        try:
            platform.scenario.start()
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return platform.scenario.view()

    @app.post("/api/scenario/stop")
    async def scenario_stop() -> dict:
        platform.scenario.cancel()
        return platform.scenario.view()

    @app.get("/api/settings")
    async def get_settings() -> dict:
        smtp = platform.notifier.smtp_config()
        return {"webhook_url": platform.notifier.webhook_url,
                "email": {"configured": bool(smtp), "to": smtp["to"] if smtp else None}}

    @app.put("/api/settings")
    async def put_settings(body: SettingsBody) -> dict:
        url = body.webhook_url.strip()
        if url and not url.startswith(("http://", "https://")):
            raise HTTPException(400, "The webhook address must start with http:// or https://")
        platform.store.set_setting("webhook_url", url)
        return {"webhook_url": url}

    @app.post("/api/settings/test")
    async def test_notification() -> dict:
        note = platform.notifier.notify("info", "Test notification", "If you can read this, alerts are reaching this channel.",
                                        dedup_key=f"test:{time.time()}")
        await asyncio.gather(*platform.notifier.tasks, return_exceptions=True)
        return platform.store.notifications(1)[0] if note else {}

    @app.post("/api/history/clear")
    async def clear_history() -> dict:
        platform.store.clear_history()
        platform.incidents.next_id = platform.store.next_incident_id() + len(platform.incidents.open)
        return {"ok": True}

    # ------------------------------------------------------------ live stream
    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        platform.subscribers.add(q)
        try:
            if platform.snapshot:
                await socket.send_json({"type": "state", "data": platform.snapshot})
            while True:
                await socket.send_json(await q.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            platform.subscribers.discard(q)

    # ------------------------------------------------------------ dashboard
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(cfg.WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(cfg.WEB_DIR)), name="static")
    return app
