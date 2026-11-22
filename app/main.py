from __future__ import annotations

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.routes.actions import router as actions_router
from app.routes.chat import router as chat_router
from app.routes.pages import router as pages_router
from app.routes.upload import router as upload_router
from app.routes.reconciliation import router as reconciliation_router
from app.services.file_service import FileService
from app.services.reconciliation_repository import ReconciliationRepository


settings = get_settings()
FileService(settings).ensure_storage()


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    FileService(settings).ensure_storage()
    ReconciliationRepository(settings).migrate()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true",
)
app.include_router(pages_router)
app.include_router(chat_router)
app.include_router(upload_router)
app.include_router(actions_router)
app.include_router(reconciliation_router)
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
app.mount("/storage", StaticFiles(directory=str(settings.storage_dir)), name="storage")


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "ai_configured": bool(settings.openai_api_key)}
