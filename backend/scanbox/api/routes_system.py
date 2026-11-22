"""SSE events, health and readiness."""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import __version__
from ..context import AppContext
from ..db import check_writable
from ..security.auth import Principal
from .deps import CtxDep, PrincipalDep

router = APIRouter(tags=["system"])

HEARTBEAT_S = 20.0


@router.get("/api/events", responses={200: {"content": {"text/event-stream": {}}}})
async def events(request: Request, ctx: AppContext = CtxDep, p: Principal = PrincipalDep) -> StreamingResponse:
    """Server-Sent Events: scanner.status, document.*, page.*, delivery.updated, inbox.changed, heartbeat."""
    sub = ctx.bus.subscribe(p.id, p.is_admin)
    if ctx.scanner:
        ctx.scanner.wake()  # switch the monitor to fast polling
        initial = ctx.scanner.status.to_dict()
    else:
        initial = None

    async def stream():  # noqa: ANN202
        try:
            yield "retry: 3000\n\n"
            if initial:
                import json

                yield f"event: scanner.status\ndata: {json.dumps(initial, default=str)}\n\n"
            while True:
                if sub.dropped or await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(sub.queue.get(), timeout=HEARTBEAT_S)
                    yield ev.encode()
                except TimeoutError:
                    yield "event: heartbeat\ndata: {}\n\n"
        finally:
            ctx.bus.unsubscribe(sub)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/readyz", include_in_schema=False)
async def readyz(ctx: AppContext = CtxDep) -> JSONResponse:
    checks: dict[str, object] = {}
    ok = True
    try:
        await asyncio.to_thread(check_writable)
        checks["db"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["db"] = f"error: {e}"
        ok = False
    free = ctx.storage.free_mb()
    min_free = int(ctx.settings.get("disk.min_free_mb") or 0)
    checks["disk_free_mb"] = free
    checks["disk"] = "ok" if free >= min_free else "low"
    workers = bool(ctx.worker and ctx.worker.alive) or ctx.config.disable_background
    checks["workers"] = "ok" if workers else "down"
    ok = ok and workers
    # Scanner state is informative only: an offline scanner must not restart the container.
    if ctx.scanner:
        with contextlib.suppress(Exception):
            checks["scanner"] = ctx.scanner.status.state
    return JSONResponse({"status": "ok" if ok else "fail", "checks": checks}, status_code=200 if ok else 503)
