"""
main.py  —  FastAPI entry point
"""
import asyncio
asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import os

from app.db       import init_db
from app.routes   import quotes_router

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Quote Engine API", version="2.0")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()
    print(">>> DB initialized")

# ── Routes ────────────────────────────────────────────────────────────────────

app.include_router(quotes_router)

@app.get("/ping")
def ping():
    return {"status": "ok", "version": "2.0"}
