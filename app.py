"""FastAPI web server + dashboard for LLM health monitor.

The checker runs as a co-located asyncio background task so everything
shares one event loop.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

import db
import checker
from config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("llm_monitor.web")

# Jinja2 templates
templates = Jinja2Templates(directory="templates")

# Background task handle
_checker_task: asyncio.Task | None = None

# Load and validate config once at import time
_APP_CFG = load_config()

_SERVER_CFG = _APP_CFG.server
_HOST = _SERVER_CFG.host
_PORT = _SERVER_CFG.port
_TIMEZONE = _SERVER_CFG.timezone

# Compute timezone offset in hours (e.g. +8 for Asia/Shanghai)
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(_TIMEZONE)
    _TZ_OFFSET = datetime.now(_TZ).utcoffset().total_seconds() / 3600
except Exception:
    _TZ_OFFSET = 0.0

# Build a lookup of config endpoints keyed by monitor-id.
# Only endpoints with show: True (or omitted) are visible in the UI.
_CONFIG_ENDPOINTS = {ep.monitor_id: ep for ep in _APP_CFG.endpoints}
_VISIBLE_MONITOR_IDS = {ep.monitor_id for ep in _APP_CFG.endpoints if ep.show}

_CHECK_DEFAULTS = _APP_CFG.check


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the checker background task on startup; cancel on shutdown."""
    global _checker_task
    logger.info("Initializing database...")
    await db.init_db()
    logger.info("Starting checker background task...")
    _checker_task = asyncio.create_task(checker.checker_loop())
    yield
    logger.info("Shutting down checker...")
    if _checker_task:
        _checker_task.cancel()
        try:
            await _checker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="LLM Health Monitor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "tz_offset": _TZ_OFFSET},
    )


@app.get("/alerts", response_class=HTMLResponse)
async def alerts_page(request: Request):
    return templates.TemplateResponse(
        "alerts.html",
        {"request": request, "tz_offset": _TZ_OFFSET},
    )


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    return templates.TemplateResponse(
        "logs.html",
        {"request": request, "tz_offset": _TZ_OFFSET},
    )


# ---------------------------------------------------------------------------
# JSON API routes
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status():
    """Current status + uptime for visible endpoints only."""
    endpoints = await db.get_enabled_endpoints()
    result = []
    for ep in endpoints:
        mid = ep.get("monitor_id")
        if mid not in _VISIBLE_MONITOR_IDS:
            continue
        cfg_ep = _CONFIG_ENDPOINTS.get(mid)
        eff = cfg_ep.effective(_CHECK_DEFAULTS) if cfg_ep else {}
        latest = await db.get_latest_check(ep["id"])
        uptime_1h = await db.get_uptime(ep["id"], hours=1)
        uptime_24h = await db.get_uptime(ep["id"], hours=24)
        uptime_7d = await db.get_uptime(ep["id"], hours=168)
        result.append(
            {
                "monitor_id": mid,
                "name": ep["name"],
                "endpoint_type": ep.get("endpoint_type", "llm"),
                "model": ep["model"],
                "base_url": ep["base_url"],
                "api_key": ep["api_key"],
                "check_override": ep.get("check_override", {}),
                "effective": eff,
                "latest": latest,
                "uptime": {
                    "1h": round(uptime_1h, 2) if uptime_1h is not None else None,
                    "24h": round(uptime_24h, 2) if uptime_24h is not None else None,
                    "7d": round(uptime_7d, 2) if uptime_7d is not None else None,
                },
            }
        )
    return {"endpoints": result}


@app.get("/api/history")
async def api_history(monitor_id: str, hours: int = 24):
    """Time-series check data for one endpoint."""
    # Look up the endpoint to verify it's visible
    ep = await db.get_endpoint_by_monitor_id(monitor_id)
    if not ep or ep.get("monitor_id") not in _VISIBLE_MONITOR_IDS:
        return {"error": "Endpoint not found or not visible"}, 404
    history = await db.get_history(ep["id"], hours=hours)
    return {"monitor_id": ep["monitor_id"], "hours": hours, "checks": history}


@app.get("/api/alerts")
async def api_alerts(active_only: bool = False):
    """Alert list — only for visible endpoints."""
    if active_only:
        data = await db.get_active_alerts()
    else:
        data = await db.get_recent_alerts(limit=50)
    # Filter to visible endpoints
    filtered = [a for a in data if a.get("monitor_id") in _VISIBLE_MONITOR_IDS]
    return {"alerts": filtered}


@app.get("/api/logs")
async def api_logs(limit: int = 200):
    """Recent check records with full endpoint + override details for debug.

    Only includes checks for endpoints that are visible in config.
    """
    data = await db.get_recent_checks(limit=limit)
    filtered = [c for c in data if c.get("monitor_id") in _VISIBLE_MONITOR_IDS]
    return {"checks": filtered}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run("app:app", host=_HOST, port=_PORT, reload=False)
