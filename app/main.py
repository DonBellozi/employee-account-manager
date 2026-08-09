from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import Base, SessionLocal, engine, ensure_compatibility_schema
from app.routers import (
    admin,
    auth,
    employees,
    hr_reconcile_multisource,
    hr_registry_mapping,
    hr_registry_multisource,
    onec_sources,
    settings_ui,
    telegram_settings,
    zimbra_lifecycle,
    zimbra_observer,
    zimbra_protection,
)
from app.security import CSRFMismatchError, ensure_bootstrap_admin
from app.services.blocking_worker import BlockingQueueWorker
from app.services.onec_scheduler import OneCAutoImportScheduler
from app.services.onec_sources import OneCSourceRegistryService
from app.services.telegram_worker import TelegramNotificationWorker
from app.services.zimbra_observer_scheduler import ZimbraObserverScheduler

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.dry_run and settings.app_secret_key.startswith("change-me"):
        raise RuntimeError("Замените APP_SECRET_KEY перед рабочим запуском")
    Base.metadata.create_all(bind=engine)
    ensure_compatibility_schema()
    with SessionLocal() as db:
        source_registry = OneCSourceRegistryService(settings, db)
        source_registry.ensure_primary()
        source_registry.apply_primary_to_settings()
        ensure_bootstrap_admin(db, settings)

    onec_scheduler = OneCAutoImportScheduler(settings, SessionLocal)
    blocking_worker = BlockingQueueWorker(settings, SessionLocal)
    telegram_worker = TelegramNotificationWorker(
        settings.app_secret_key,
        SessionLocal,
    )
    zimbra_observer_scheduler = ZimbraObserverScheduler(settings, SessionLocal)
    onec_scheduler.start()
    blocking_worker.start()
    telegram_worker.start()
    zimbra_observer_scheduler.start()
    try:
        yield
    finally:
        zimbra_observer_scheduler.stop()
        telegram_worker.stop()
        blocking_worker.stop()
        onec_scheduler.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key,
    session_cookie=settings.session_cookie_name,
    same_site=settings.session_cookie_samesite,
    https_only=settings.session_cookie_secure,
    max_age=8 * 60 * 60,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth.router)
app.include_router(hr_registry_multisource.router)
app.include_router(hr_registry_mapping.router)
app.include_router(employees.router)
app.include_router(hr_reconcile_multisource.router)
app.include_router(settings_ui.router)
app.include_router(onec_sources.router)
app.include_router(telegram_settings.router)
app.include_router(zimbra_observer.router)
app.include_router(zimbra_protection.router)
app.include_router(zimbra_lifecycle.router)
app.include_router(admin.router)


@app.exception_handler(CSRFMismatchError)
async def csrf_mismatch_handler(request: Request, _: CSRFMismatchError):
    request.session.clear()
    return RedirectResponse("/login?csrf_error=1", status_code=303)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-src 'self'; frame-ancestors 'self'; "
        "form-action 'self'"
    )
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/health")
def health():
    return {"status": "ok", "dry_run": settings.dry_run}
