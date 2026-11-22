"""Admin: settings, users, usage/cache, diagnostics, logs, connection tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__
from ..context import AppContext
from ..db import get_session
from ..logging_setup import RECENT_ERRORS
from ..models import Task, User
from ..processing.ocr import tools_status
from ..retention import run_retention, usage
from ..security.auth import Principal, require_admin
from ..settings_store import ADMIN_SETTINGS
from .deps import CtxDep
from .schemas import (
    AdminSettingsIn,
    AdminSettingsOut,
    AdminSettingValue,
    ClearCacheOut,
    DiagnosticsOut,
    ScannerStatusOut,
    TestMailIn,
    TestResultOut,
    UsageOut,
    UserAdminIn,
    UserOut,
)

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/settings", response_model=AdminSettingsOut)
def get_admin_settings(ctx: AppContext = CtxDep) -> AdminSettingsOut:
    return AdminSettingsOut(settings={k: AdminSettingValue(**v) for k, v in ctx.settings.admin_view().items()})


@router.patch("/settings", response_model=AdminSettingsOut)
async def patch_admin_settings(body: AdminSettingsIn, ctx: AppContext = CtxDep) -> AdminSettingsOut:
    errors: dict[str, str] = {}
    changed: set[str] = set()
    for key, value in body.values.items():
        d = ADMIN_SETTINGS.get(key)
        if d is None or d.internal:
            errors[key] = "unknown setting"
            continue
        if d.secret and value in (None, "") and key not in body.clear_secrets:
            continue  # keep existing secret
        if key == "output.roots":
            err = _validate_roots(value, ctx.config.data_dir)
            if err:
                errors[key] = err
                continue
        if key == "scanner.url" and value:
            from ..escl.client import normalise_base_url

            try:
                value = normalise_base_url(str(value))
            except ValueError as e:
                errors[key] = str(e)
                continue
        try:
            await asyncio.to_thread(ctx.settings.set, key, value)
            changed.add(key)
        except (PermissionError, ValueError, TypeError) as e:
            errors[key] = str(e)
    for key in body.clear_secrets:
        if key in ADMIN_SETTINGS and ADMIN_SETTINGS[key].secret and not ctx.settings.is_locked(key):
            await asyncio.to_thread(ctx.settings.set, key, None)
            changed.add(key)
    if errors:
        raise HTTPException(422, detail={"code": "invalid_settings", "message": "; ".join(f"{k}: {v}" for k, v in errors.items())})
    if any(k.startswith("oidc.") for k in changed):
        from .routes_auth import reset_oidc_client

        reset_oidc_client()
    if "scanner.url" in changed and ctx.scanner:
        ctx.scanner._caps = None  # noqa: SLF001
        asyncio.create_task(_refresh_scanner(ctx))
    if any(k.startswith("walkup.") for k in changed) and ctx.walkup:
        await ctx.walkup.restart()
    return get_admin_settings(ctx)


async def _refresh_scanner(ctx: AppContext) -> None:
    try:
        await ctx.scanner.refresh_capabilities()  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
    await ctx.scanner.poll_status()  # type: ignore[union-attr]


def _validate_roots(value: Any, data_dir: Path) -> str | None:
    if not isinstance(value, dict):
        return "expected an object name → absolute path"
    for name, path in value.items():
        if not str(name).strip() or len(str(name)) > 50:
            return f"invalid name {name!r}"
        p = Path(str(path))
        if not p.is_absolute():
            return f"{name}: path must be absolute"
        rp = p.resolve()
        data = data_dir.resolve()
        forbidden = [Path(x) for x in ("/proc", "/sys", "/etc", "/dev", "/usr", "/bin", "/app")]
        if rp == Path(rp.anchor) or any(rp == f or rp.is_relative_to(f) for f in forbidden) or (
                rp == data or rp.is_relative_to(data) or data.is_relative_to(rp)):
            return f"{name}: path not allowed"
        if not p.is_dir():
            return f"{name}: {p} does not exist in the container (mount it as a volume)"
    return None


# ------------------------------------------------------------------ users
@router.get("/users", response_model=list[UserOut])
def list_users(s: Session = Depends(get_session)) -> list[User]:
    return list(s.scalars(select(User).order_by(User.username)))


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: UserAdminIn, s: Session = Depends(get_session),
                a: Principal = Depends(require_admin)) -> User:
    u = s.get(User, user_id)
    if u is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "Benutzer nicht gefunden"})
    if u.id == a.id and (body.disabled or body.is_admin is False):
        raise HTTPException(409, detail={"code": "self", "message": "Eigenes Konto kann nicht herabgestuft werden"})
    if body.is_admin is not None:
        u.is_admin = body.is_admin  # note: OIDC users get their role re-evaluated at next login
    if body.disabled is not None:
        u.disabled = body.disabled
        if body.disabled:
            from sqlalchemy import delete

            from ..models import SessionRow

            s.execute(delete(SessionRow).where(SessionRow.user_id == u.id))
    return u


# ------------------------------------------------------------------ data
@router.get("/usage", response_model=UsageOut)
async def get_usage(ctx: AppContext = CtxDep) -> UsageOut:
    return UsageOut(**await asyncio.to_thread(usage, ctx))


@router.post("/clear-cache", response_model=ClearCacheOut)
async def clear_cache(ctx: AppContext = CtxDep) -> ClearCacheOut:
    """Delete cached raw + processed files of finished documents now.

    Unfinished work and unclaimed inbox items are kept."""
    return ClearCacheOut(**await asyncio.to_thread(run_retention, ctx, force_all=True))


@router.get("/diagnostics", response_model=DiagnosticsOut)
def diagnostics(s: Session = Depends(get_session), ctx: AppContext = CtxDep) -> DiagnosticsOut:
    rows = s.execute(select(Task.lane, Task.status, func.count(Task.id)).group_by(Task.lane, Task.status)).all()
    s.commit()
    queue: dict[str, dict[str, int]] = {}
    for lane, status, n in rows:
        queue.setdefault(lane, {})[status] = n
    return DiagnosticsOut(
        version=__version__,
        scanner=ScannerStatusOut(**ctx.scanner.status.to_dict()) if ctx.scanner else ScannerStatusOut(
            configured=False, online=False, state="unconfigured"),
        tools=tools_status(),
        queue=queue,
        workers_alive=bool(ctx.worker and ctx.worker.alive),
        usage=UsageOut(**usage(ctx)),
        recent_errors=list(RECENT_ERRORS)[-50:][::-1],
        walkup=ctx.walkup.status.to_dict() if ctx.walkup else None,
    )


@router.get("/logs", responses={200: {"content": {"text/plain": {}}}})
def download_logs(ctx: AppContext = CtxDep) -> FileResponse:
    path = ctx.config.log_dir / "scanbox.log"
    if not path.exists():
        raise HTTPException(404, detail={"code": "no_logs", "message": "Keine Logdatei"})
    return FileResponse(path, media_type="text/plain", filename="scanbox.log")


# ------------------------------------------------------------------ tests
async def _env(ctx: AppContext, a: Principal):  # noqa: ANN202
    from ..db import run_db
    from ..destinations.service import make_env

    user_values = await run_db(ctx.settings.user_all, a.id)
    return make_env(ctx, a.id, user_values=user_values)


@router.post("/test/smtp", response_model=TestResultOut)
async def test_smtp_ep(ctx: AppContext = CtxDep, a: Principal = Depends(require_admin)) -> TestResultOut:
    from ..destinations.email import test_smtp

    ok, msg = await test_smtp(await _env(ctx, a))
    return TestResultOut(ok=ok, message=msg)


@router.post("/test/mail", response_model=TestResultOut)
async def test_mail(body: TestMailIn, ctx: AppContext = CtxDep, a: Principal = Depends(require_admin)) -> TestResultOut:
    from ..destinations.base import DeliveryError
    from ..destinations.email import send_test_mail

    try:
        await send_test_mail(await _env(ctx, a), body.to)
    except DeliveryError as e:
        return TestResultOut(ok=False, message=e.message)
    return TestResultOut(ok=True, message=f"Testmail an {body.to} gesendet")


@router.post("/test/paperless", response_model=TestResultOut)
async def test_paperless_ep(ctx: AppContext = CtxDep, a: Principal = Depends(require_admin)) -> TestResultOut:
    """Uses the calling admin's own Paperless token."""
    from ..destinations.paperless import test_paperless

    ok, msg = await test_paperless(await _env(ctx, a))
    return TestResultOut(ok=ok, message=msg)


@router.post("/walkup/restart", response_model=dict)
async def walkup_restart(ctx: AppContext = CtxDep) -> dict:
    if ctx.walkup:
        await ctx.walkup.restart()
        return ctx.walkup.status.to_dict()
    return {}
