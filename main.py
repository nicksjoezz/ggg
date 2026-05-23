"""
main.py — FastAPI application.
Routes:
  GET  /                    → dashboard HTML
  GET  /settings            → settings HTML
  GET  /api/stats           → live bot stats (JSON)
  GET  /api/positions       → open positions (JSON)
  GET  /api/trades          → trade log (JSON)
  GET  /api/settings        → current settings (JSON)
  POST /api/settings        → save settings (JSON)
  POST /api/bot/start       → start engine
  POST /api/bot/stop        → stop engine
  GET  /api/sol-price       → sol price service status
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bot.config import load_settings, save_settings
from bot.engine import engine
from bot.state import bot_state

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if engine._running:
        await engine.stop()


app = FastAPI(title="PumpSniper", lifespan=lifespan)

# ─── Static files ──────────────────────────────────────────────────────────────
static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# ─── HTML pages ────────────────────────────────────────────────────────────────

def _read_template(name: str) -> str:
    path = BASE_DIR / "templates" / name
    return path.read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return _read_template("dashboard.html")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    return _read_template("settings.html")


# ─── Bot control ───────────────────────────────────────────────────────────────

@app.post("/api/bot/start")
async def start_bot():
    if bot_state.running:
        return {"ok": False, "message": "Bot already running"}
    asyncio.create_task(engine.start())
    return {"ok": True, "message": "Bot starting..."}


@app.post("/api/bot/stop")
async def stop_bot():
    if not bot_state.running:
        return {"ok": False, "message": "Bot not running"}
    await engine.stop()
    return {"ok": True, "message": "Bot stopped"}


# ─── Live data endpoints ────────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats():
    summary = bot_state.summary()
    summary["last_error"] = engine.last_error
    return JSONResponse(summary)


@app.get("/api/positions")
async def get_positions():
    return JSONResponse(bot_state.positions_list())


@app.get("/api/trades")
async def get_trades(limit: int = 50):
    return JSONResponse(bot_state.trade_log_list(limit=limit))


@app.get("/api/sol-price")
async def get_sol_price():
    if engine.sol_price_svc:
        return JSONResponse(engine.sol_price_svc.status())
    return JSONResponse({"cached_price": None, "age_minutes": 0, "is_stale": True})


# ─── Settings CRUD ─────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    s = load_settings()
    # Mask private key in response
    if s.get("wallet", {}).get("private_key"):
        s["wallet"]["private_key"] = "••••••••"
    if s.get("api_keys", {}).get("helius_api_key"):
        s["api_keys"]["helius_api_key"] = "••••••••"
    if s.get("api_keys", {}).get("helius_api_key_2"):
        s["api_keys"]["helius_api_key_2"] = "••••••••"
    if s.get("api_keys", {}).get("helius_api_key_3"):
        s["api_keys"]["helius_api_key_3"] = "••••••••"
    return JSONResponse(s)


@app.post("/api/settings")
async def update_settings(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    current = load_settings()

    # Don't overwrite private key or api key if masked value sent back
    if body.get("wallet", {}).get("private_key") == "••••••••":
        body["wallet"]["private_key"] = current.get("wallet", {}).get("private_key", "")
    
    if body.get("api_keys", {}).get("helius_api_key") == "••••••••":
        body["api_keys"]["helius_api_key"] = current.get("api_keys", {}).get("helius_api_key", "")
    if body.get("api_keys", {}).get("helius_api_key_2") == "••••••••":
        body["api_keys"]["helius_api_key_2"] = current.get("api_keys", {}).get("helius_api_key_2", "")
    if body.get("api_keys", {}).get("helius_api_key_3") == "••••••••":
        body["api_keys"]["helius_api_key_3"] = current.get("api_keys", {}).get("helius_api_key_3", "")

    # Ensure coingecko_api_key is not written
    body.setdefault("api_keys", {}).pop("coingecko_api_key", None)

    save_settings(body)

    # Hot-reload engine settings if running
    if engine._running:
        engine.reload_settings()

    return {"ok": True, "message": "Settings saved"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
