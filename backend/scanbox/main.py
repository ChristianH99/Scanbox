"""FastAPI application factory and lifespan (startup recovery, workers, monitors)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .config import AppConfig, get_config
from .context import AppContext, build_context, set_context
from .db import dispose_engine, init_engine, session_scope
from .documents import DomainError
from .logging_setup import setup_logging
from .migrate import upgrade
from .models import Profile
from .profiles import BUILTIN_PROFILES

log = structlog.get_logger(__name__)

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "connect-src 'self'; font-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self'"
)


def seed_builtin_profiles() -> None:
    with session_scope() as s:
        for bp in BUILTIN_PROFILES:
            existing = s.scalar(select(Profile).where(Profile.builtin_key == bp["builtin_key"]))
            if existing is None:
                s.add(Profile(owner_id=None, **bp))


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN201
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Content-Security-Policy", CSP)
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith("/api/") and "cache-control" not in h:
            h["Cache-Control"] = "no-store"
        return resp


def create_app(
    config: AppConfig | None = None,
    *,
    customize: Callable[[AppContext], None] | None = None,
) -> FastAPI:
    """``customize`` lets tests inject HTTP client factories (simulator, mocks) before startup."""
    config = config or get_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(config.log_level, config.log_json, config.log_dir if not config.disable_background else None)
        ctx = build_context(config)
        if customize:
            customize(ctx)
        engine = init_engine(config.db_path)
        await asyncio.to_thread(upgrade, engine)
        seed_builtin_profiles()
        from .security.auth import ensure_local_admin, init_setup, purge_expired_sessions

        ensure_local_admin(config.admin_username, config.resolved_admin_password())
        ctx.setup_code = init_setup(config)
        purge_expired_sessions()

        from .jobs.worker import Worker, recover_on_startup
        from .scanner.service import ScannerService
        from .walkup.service import WalkupService

        ctx.bus.bind_loop(asyncio.get_running_loop())
        ctx.scanner = ScannerService(ctx)
        ctx.worker = Worker(ctx)
        ctx.walkup = WalkupService(ctx)
        set_context(ctx)
        app.state.ctx = ctx
        _stats, recovering = recover_on_startup(ctx)
        ctx.worker.recovering_deliveries |= recovering
        background: list[asyncio.Task] = []
        if not config.disable_background:
            ctx.worker.start()
            ctx.scanner.start_monitor()
            from .retention import retention_loop

            background.append(asyncio.create_task(retention_loop(ctx), name="retention"))
            ctx.walkup.start()
        log.info("app.started", version=__version__, data_dir=str(config.data_dir))
        try:
            yield
        finally:
            log.info("app.stopping")
            for t in background:
                t.cancel()
            await ctx.walkup.stop()
            await ctx.worker.stop()
            await ctx.scanner.close()
            for t in background:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            set_context(None)
            dispose_engine()

    app = FastAPI(
        title="Scanbox API",
        version=__version__,
        description="Self-hosted scanning for eSCL scanners. Authenticate with a session cookie (+ X-CSRF-Token "
        "header for writes) or `Authorization: Bearer sbx_…` API tokens.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(SecurityHeaders)

    @app.exception_handler(DomainError)
    async def _domain(_req: Request, e: DomainError) -> JSONResponse:
        return JSONResponse({"detail": {"code": e.code, "message": e.message}}, status_code=e.status)

    from .jobs.states import InvalidTransition

    @app.exception_handler(InvalidTransition)
    async def _transition(_req: Request, e: InvalidTransition) -> JSONResponse:
        return JSONResponse({"detail": {"code": "invalid_state",
                                        "message": f"Aktion im Status '{e.src}' nicht möglich"}}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def _validation(_req: Request, e: RequestValidationError) -> JSONResponse:
        msg = "; ".join(f"{'.'.join(str(x) for x in err['loc'][1:])}: {err['msg']}" for err in e.errors()[:10])
        # Only plain fields: 'ctx' can hold the raising exception object (not JSON-serialisable → was a 500),
        # and 'input' would echo submitted values (secrets) back.
        errors = [{"type": err.get("type"), "loc": list(err.get("loc", ())), "msg": err.get("msg")}
                  for err in e.errors()[:20]]
        return JSONResponse({"detail": {"code": "validation_error", "message": msg, "errors": errors}},
                            status_code=422)

    @app.exception_handler(HTTPException)
    async def _http(_req: Request, e: HTTPException) -> JSONResponse:
        detail = e.detail if isinstance(e.detail, dict) else {"code": f"http_{e.status_code}", "message": str(e.detail)}
        return JSONResponse({"detail": detail}, status_code=e.status_code, headers=e.headers)

    from .api import routes_admin, routes_auth, routes_documents, routes_scanner, routes_system, routes_user

    for r in (routes_auth.router, routes_scanner.router, routes_scanner.profiles, routes_documents.router,
              routes_user.router, routes_admin.router, routes_system.router):
        app.include_router(r)

    _mount_spa(app, config)
    return app


def _mount_spa(app: FastAPI, config: AppConfig) -> None:
    static = config.static_dir or Path(__file__).parent / "static"
    index = static / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(404, detail={"code": "not_found", "message": "Not found"})
        if path:
            candidate = (static / path).resolve()
            if candidate.is_file() and candidate.is_relative_to(static.resolve()):
                cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
                return FileResponse(candidate, headers={"Cache-Control": cache})
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        raise HTTPException(404, detail={"code": "frontend_missing", "message": "Frontend not built"})
