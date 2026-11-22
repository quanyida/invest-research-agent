from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.auth import me as auth_me, router as auth_router
from app.api.catalog import router as catalog_router
from app.api.chat import router as chat_router
from app.api.conversations import router as conversations_router
from app.api.health import router as health_router
from app.api.usage import router as usage_router
from app.agent.run_worker import RunWorker
from app.config import ROOT, get_settings

logging.basicConfig(
    level=getattr(logging, get_settings().log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    worker: RunWorker | None = None
    if settings.run_worker_enabled:
        worker = RunWorker(settings)
        await worker.start()
    try:
        yield
    finally:
        if worker is not None:
            await worker.stop()

app = FastAPI(
    title="投研 Agent",
    version="1.0.0",
    docs_url="/docs" if get_settings().is_dev else None,
    lifespan=lifespan,
)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(conversations_router)
app.include_router(catalog_router)
app.include_router(chat_router)
app.include_router(usage_router)

STATIC_DIR = ROOT / "static"
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        payload = detail
    else:
        payload = {"code": "ERROR", "message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content=payload, headers=exc.headers)

@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/ui/")

@app.get("/api/v1/me")
def api_me(request: Request) -> dict:
    return auth_me(request)

@app.get("/ui/login")
def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html")

@app.get("/ui/")
def chat_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/ui/usage")
def usage_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "usage.html")
