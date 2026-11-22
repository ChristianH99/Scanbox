"""FastAPI app: eSCL endpoints + the ``/_sim`` control API."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .caps import capabilities_bytes
from .presets import PRESETS, preset
from .settings import SettingsError
from .state import EsclError, SimState
from .xmlgen import scan_image_info

try:  # httpx is what the backend uses; make in-process failures look like real transport errors
    import httpx

    _ConnectBase: type[Exception] = httpx.ConnectError
    _DropBase: type[Exception] = httpx.RemoteProtocolError
except ImportError:  # pragma: no cover
    _ConnectBase = ConnectionRefusedError
    _DropBase = ConnectionResetError


class SimulatedOffline(_ConnectBase):  # type: ignore[misc,valid-type]
    """Raised into the ASGI server/transport when the simulated scanner is offline."""

    def __init__(self, message: str = "escl-sim: scanner offline (simulated)"):
        super().__init__(message)


class SimulatedConnectionDrop(_DropBase):  # type: ignore[misc,valid-type]
    """Raised after a partial response body to simulate a dropped connection."""

    def __init__(self, message: str = "escl-sim: connection dropped mid-body (simulated)"):
        super().__init__(message)


XML = "text/xml"


class TruncatedResponse(Response):
    """Announces the full Content-Length, sends half the body, then kills the connection."""

    async def __call__(self, scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        await send({"type": "http.response.body", "body": self.body[: len(self.body) // 2], "more_body": True})
        raise SimulatedConnectionDrop()


class SimMiddleware:
    """Latency, offline simulation and request logging for /eSCL requests (pure ASGI)."""

    def __init__(self, app, state: SimState):
        self.app = app
        self.state = state

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/eSCL"):
            await self.app(scope, receive, send)
            return
        st = self.state
        entry = [scope["method"], scope["path"], 0]
        st.request_log.append(entry)  # type: ignore[arg-type]
        if st.faults.latency_ms > 0:
            await asyncio.sleep(st.faults.latency_ms / 1000)
        if st.check_offline():
            entry[2] = -1
            mode = st.config.offline_mode
            if mode == "raise":
                raise SimulatedOffline()
            if mode == "hang":
                await asyncio.sleep(st.config.hang_s)
            await Response("scanner offline (simulated)", status_code=503, media_type="text/plain")(
                scope, receive, send
            )
            return

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                entry[2] = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _error_response(exc: EsclError | SettingsError) -> Response:
    headers = getattr(exc, "headers", {}) or {}
    return Response(exc.message, status_code=exc.status, media_type="text/plain", headers=headers)


def create_app(state: SimState | None = None, **config: Any) -> FastAPI:
    """Build the simulator app. ``config`` keys update ``state.config`` (see SimConfig)."""
    state = state if state is not None else SimState()
    if config:
        state.config.update(**config)
    app = FastAPI(title="escl-sim", docs_url="/_sim/docs", openapi_url="/_sim/openapi.json", redoc_url=None)
    app.state.sim = state
    app.add_middleware(SimMiddleware, state=state)

    @app.exception_handler(EsclError)
    async def _escl_error(_: Request, exc: EsclError) -> Response:
        return _error_response(exc)

    @app.exception_handler(SettingsError)
    async def _settings_error(_: Request, exc: SettingsError) -> Response:
        return _error_response(exc)

    @app.exception_handler(ValueError)
    async def _value_error(_: Request, exc: ValueError) -> Response:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    # ------------------------------------------------------------------ eSCL

    @app.get("/eSCL/ScannerCapabilities")
    async def scanner_capabilities() -> Response:
        return Response(capabilities_bytes(), media_type=XML)

    @app.get("/eSCL/ScannerStatus")
    async def scanner_status() -> Response:
        return Response(state.status_xml(), media_type=XML)

    @app.post("/eSCL/ScanJobs")
    async def create_scan_job(request: Request) -> Response:
        body = await request.body()
        job = state.create_job(body)
        if state.config.location_style == "absolute":
            host = request.headers.get("host") or "localhost"
            location = f"{request.url.scheme}://{host}{job.path}"
        else:
            location = job.path
        return Response(status_code=201, headers={"Location": location})

    @app.get("/eSCL/ScanJobs/{job_id}/NextDocument")
    async def next_document(job_id: str) -> Response:
        result = await state.next_document(job_id)
        enc = result.encoded
        cls = TruncatedResponse if result.drop else Response
        return cls(enc.data, media_type=enc.content_type)

    @app.get("/eSCL/ScanJobs/{job_id}/ScanImageInfo")
    async def image_info(job_id: str) -> Response:
        job = state.get_job(job_id)
        return Response(scan_image_info(job), media_type=XML)

    @app.delete("/eSCL/ScanJobs/{job_id}")
    async def cancel_job(job_id: str) -> Response:
        state.cancel_job(job_id)
        return Response(status_code=200)

    # ------------------------------------------------------------------ control API

    @app.get("/_sim/state")
    async def sim_state() -> dict:
        return state.snapshot()

    @app.post("/_sim/reset")
    async def sim_reset() -> dict:
        state.reset()
        return state.snapshot()

    @app.post("/_sim/load")
    async def sim_load(body: dict) -> dict:
        append = bool(body.get("append", False))
        if body.get("empty"):
            state.load_empty()
        elif "stack" in body:
            state.load(str(body["stack"]), append=append)
        elif "pages" in body:
            if not isinstance(body["pages"], list):
                raise ValueError("pages must be a list")
            state.load(body["pages"], append=append)
        else:
            raise ValueError('expected {"stack": name}, {"pages": [...]} or {"empty": true}')
        return state.snapshot()

    @app.get("/_sim/faults")
    async def sim_get_faults() -> dict:
        return state.faults.to_dict()

    @app.post("/_sim/faults")
    async def sim_faults(body: dict) -> dict:
        state.set_faults(**body)
        return state.faults.to_dict()

    @app.delete("/_sim/faults")
    async def sim_clear_faults() -> dict:
        state.clear_faults()
        return state.faults.to_dict()

    @app.post("/_sim/clear_jam")
    async def sim_clear_jam(body: dict | None = None) -> dict:
        state.clear_jam(reinsert=bool((body or {}).get("reinsert", True)))
        return state.snapshot()

    @app.get("/_sim/config")
    async def sim_get_config() -> dict:
        return state.config.to_dict()

    @app.post("/_sim/config")
    async def sim_config(body: dict) -> dict:
        state.config.update(**body)
        return state.config.to_dict()

    @app.get("/_sim/presets")
    async def sim_presets() -> dict:
        return {name: [s.to_dict() for s in preset(name)] for name in PRESETS}

    return app
