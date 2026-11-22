"""Mock Paperless-ngx REST API (enough for the Scanbox paperless destination).

Use in tests::

    app = create_paperless_mock(token="t")
    env = DeliveryEnv(..., http_client_factory=paperless_client_factory(app))
    app.state.mock.offline = True   # toggles: offline, fail_status, delay_s, ...

Standalone for development (from ``backend/``)::

    python -m tests.mocks.paperless --port 8000 --token dev-token

Implemented: token auth, ``Accept: application/json; version=N`` negotiation (406 above
``max_api_version``), ``X-Version``/``X-Api-Version`` headers, ``POST /api/documents/post_document/``
(multipart, repeated ``tags``, ``custom_fields`` as JSON object or id list), ``/api/tasks/?task_id=``
(list for API <= 9, paginated for >= 10), duplicate checksum and duplicate ASN failures,
``GET /api/documents/`` (``checksum__iexact``), ``GET/PATCH /api/documents/{id}/``,
lookups + create for tags/correspondents/document_types and custom_fields.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


@dataclass
class MockState:
    token: str = "test-token"  # noqa: S105
    version: str = "2.18.4"
    max_api_version: int = 9
    # Toggles
    offline: bool = False  # client transport raises ConnectError (standalone: 503)
    fail_status: int | None = None  # every request returns this status (e.g. 500, 429)
    delay_s: float = 0.0  # slow responses
    reject_duplicates: bool = True  # Paperless < 3 / PAPERLESS_CONSUMER_DELETE_DUPLICATES
    legacy_custom_fields: bool = False  # only accept a list of ids (old 2.x)
    task_pending_polls: int = 1  # how many polls a task stays PENDING
    consume_error: str | None = None  # force task FAILURE with this message
    # Data
    tags: dict[int, dict] = field(default_factory=dict)
    correspondents: dict[int, dict] = field(default_factory=dict)
    document_types: dict[int, dict] = field(default_factory=dict)
    custom_fields: dict[int, dict] = field(default_factory=dict)
    documents: dict[int, dict] = field(default_factory=dict)
    tasks: dict[str, dict] = field(default_factory=dict)
    uploads: list[dict] = field(default_factory=list)  # raw form data of each upload
    requests: list[str] = field(default_factory=list)
    _next_id: int = 100

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id


def _err(status: int, detail: Any) -> JSONResponse:
    return JSONResponse(detail if isinstance(detail, dict) else {"detail": detail}, status_code=status)


def create_paperless_mock(
    *,
    token: str = "test-token",  # noqa: S107
    tags: list[str] = ("Inbox", "Scan"),
    correspondents: list[str] = ("ACME Corp",),
    document_types: list[str] = ("Invoice",),
    custom_fields: list[tuple[str, str]] = (("Scan ID", "string"), ("Number", "integer")),
    **state_kw: Any,
) -> FastAPI:
    st = MockState(token=token, **state_kw)
    for coll, names in (
        (st.tags, tags),
        (st.correspondents, correspondents),
        (st.document_types, document_types),
    ):
        for n in names:
            i = st.next_id()
            coll[i] = {"id": i, "name": n}
    for n, dtype in custom_fields:
        i = st.next_id()
        st.custom_fields[i] = {"id": i, "name": n, "data_type": dtype}

    app = FastAPI(title="Mock Paperless-ngx")
    app.state.mock = st

    @app.middleware("http")
    async def gate(request: Request, call_next):
        st.requests.append(f"{request.method} {request.url.path}")
        if st.delay_s:
            await asyncio.sleep(st.delay_s)
        if st.offline:
            return _err(503, "offline")
        if st.fail_status:
            return _err(st.fail_status, "Injected failure")
        accept = request.headers.get("accept", "")
        m = re.search(r"version=(\d+)", accept)
        api_version = int(m.group(1)) if m else st.max_api_version
        if api_version > st.max_api_version or api_version < 1:
            return _err(406, 'Invalid version in "Accept" header.')
        request.state.api_version = api_version
        if request.headers.get("authorization") != f"Token {st.token}":
            return _err(
                401,
                "Invalid token."
                if request.headers.get("authorization")
                else "Authentication credentials were not provided.",
            )
        resp = await call_next(request)
        resp.headers["X-Api-Version"] = str(api_version)
        resp.headers["X-Version"] = st.version
        return resp

    # ---------------------------------------------------------------- lookups

    def _listing(items: list[dict], request: Request) -> dict:
        q = request.query_params
        name = q.get("name__iexact")
        if name is not None:
            items = [x for x in items if x["name"].lower() == name.lower()]
        return {"count": len(items), "next": None, "previous": None, "results": items}

    def _lookup_routes(path: str, coll: dict[int, dict]) -> None:
        @app.get(f"/api/{path}/", name=f"list_{path}")
        async def list_(request: Request):
            return _listing(list(coll.values()), request)

        @app.post(f"/api/{path}/", name=f"create_{path}")
        async def create(request: Request):
            body = await request.json()
            name = str(body.get("name", "")).strip()
            if not name:
                return _err(400, {"name": ["This field is required."]})
            if any(x["name"].lower() == name.lower() for x in coll.values()):
                return _err(400, {"name": ["Object with this name already exists."]})
            i = st.next_id()
            coll[i] = {"id": i, "name": name}
            return JSONResponse(coll[i], status_code=201)

    _lookup_routes("tags", st.tags)
    _lookup_routes("correspondents", st.correspondents)
    _lookup_routes("document_types", st.document_types)

    @app.get("/api/custom_fields/")
    async def list_custom_fields(request: Request):
        return _listing(list(st.custom_fields.values()), request)

    # ---------------------------------------------------------------- documents

    @app.get("/api/documents/")
    async def list_documents(request: Request):
        docs = list(st.documents.values())
        cs = request.query_params.get("checksum__iexact")
        if cs is not None:
            docs = [d for d in docs if d["checksum"].lower() == cs.lower()]
        return {"count": len(docs), "next": None, "previous": None, "results": docs}

    @app.get("/api/documents/{doc_id}/")
    async def get_document(doc_id: int):
        if doc_id not in st.documents:
            return _err(404, "No Document matches the given query.")
        return st.documents[doc_id]

    @app.patch("/api/documents/{doc_id}/")
    async def patch_document(doc_id: int, request: Request):
        if doc_id not in st.documents:
            return _err(404, "No Document matches the given query.")
        body = await request.json()
        doc = st.documents[doc_id]
        if "custom_fields" in body:
            for cf in body["custom_fields"]:
                if cf["field"] not in st.custom_fields:
                    return _err(400, {"custom_fields": [f"Invalid pk {cf['field']}"]})
            doc["custom_fields"] = body["custom_fields"]
        for k in ("title", "tags", "archive_serial_number"):
            if k in body:
                doc[k] = body[k]
        return doc

    @app.post("/api/documents/post_document/")
    async def post_document(request: Request):
        form = await request.form()
        upload = form.get("document")
        if upload is None or isinstance(upload, str):
            return _err(400, {"document": ["No file was submitted."]})
        data = await upload.read()
        record: dict[str, Any] = {"filename": upload.filename, "size": len(data)}
        errors: dict[str, list[str]] = {}

        def _ids(key: str, coll: dict) -> list[int]:
            out = []
            for v in form.getlist(key):
                if not str(v).isdigit() or int(v) not in coll:
                    errors[key] = [f'Invalid pk "{v}" - object does not exist.']
                else:
                    out.append(int(v))
            return out

        record["title"] = form.get("title")
        record["created"] = form.get("created")
        if record["created"]:
            try:
                datetime.fromisoformat(str(record["created"]))
            except ValueError:
                errors["created"] = ["Datetime has wrong format."]
        record["tags"] = _ids("tags", st.tags)
        corr = _ids("correspondent", st.correspondents)
        record["correspondent"] = corr[0] if corr else None
        dt_ = _ids("document_type", st.document_types)
        record["document_type"] = dt_[0] if dt_ else None
        asn = form.get("archive_serial_number")
        if asn is not None:
            if not str(asn).isdigit() or int(asn) > 0xFFFFFFFF:
                errors["archive_serial_number"] = ["A valid integer is required."]
            else:
                record["archive_serial_number"] = int(asn)
        cf_raw = form.getlist("custom_fields")
        cfs: dict[int, Any] = {}
        if cf_raw:
            try:
                parsed = json.loads(cf_raw[0]) if len(cf_raw) == 1 else [int(x) for x in cf_raw]
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, int):
                parsed = [parsed]
            if isinstance(parsed, dict) and not st.legacy_custom_fields:
                cfs = {int(k): v for k, v in parsed.items()}
            elif isinstance(parsed, list):
                cfs = {int(k): None for k in parsed}
            else:
                errors["custom_fields"] = ["A valid integer is required."]
            if any(k not in st.custom_fields for k in cfs):
                errors["custom_fields"] = ["Custom field does not exist."]
        record["custom_fields"] = cfs
        if errors:
            return _err(400, errors)
        st.uploads.append(record)

        task_id = str(uuid.uuid4())
        checksum = hashlib.sha256(data).hexdigest()
        st.tasks[task_id] = {
            "id": len(st.tasks) + 1,
            "task_id": task_id,
            "task_file_name": upload.filename,
            "date_created": datetime.now().isoformat(),
            "date_done": None,
            "type": "file",
            "status": "PENDING",
            "result": None,
            "acknowledged": False,
            "related_document": None,
            "_polls": 0,
            "_record": record,
            "_checksum": checksum,
        }
        return JSONResponse(task_id)

    def _consume(task: dict) -> None:
        rec, checksum = task["_record"], task["_checksum"]
        name = rec["filename"]
        task["date_done"] = datetime.now().isoformat()
        if st.consume_error:
            task["status"], task["result"] = "FAILURE", f"{name}: {st.consume_error}"
            return
        dup = next((d for d in st.documents.values() if d["checksum"] == checksum), None)
        if dup and st.reject_duplicates:
            task["status"] = "FAILURE"
            task["result"] = (
                f"{name}: Not consuming {name}: It is a duplicate of {dup['title']} (#{dup['id']})"
            )
            return
        asn = rec.get("archive_serial_number")
        if asn is not None and any(d.get("archive_serial_number") == asn for d in st.documents.values()):
            task["status"] = "FAILURE"
            task["result"] = f"{name}: Not consuming {name}: Given ASN {asn} already exists!"
            return
        doc_id = st.next_id()
        st.documents[doc_id] = {
            "id": doc_id,
            "title": rec["title"] or name.rsplit(".", 1)[0],
            "created": rec["created"],
            "tags": rec["tags"],
            "correspondent": rec["correspondent"],
            "document_type": rec["document_type"],
            "archive_serial_number": asn,
            "custom_fields": [{"field": k, "value": v} for k, v in rec["custom_fields"].items()],
            "checksum": checksum,
            "original_file_name": name,
        }
        task["status"] = "SUCCESS"
        task["result"] = f"Success. New document id {doc_id} created"
        task["related_document"] = str(doc_id)

    @app.get("/api/tasks/")
    async def tasks(request: Request):
        tid = request.query_params.get("task_id")
        found = [t for t in st.tasks.values() if tid is None or t["task_id"] == tid]
        for t in found:
            if t["status"] == "PENDING":
                t["_polls"] += 1
                if t["_polls"] > st.task_pending_polls:
                    _consume(t)
        public = [{k: v for k, v in t.items() if not k.startswith("_")} for t in found]
        if request.state.api_version >= 10:
            return {"count": len(public), "next": None, "previous": None, "results": public}
        return public

    def process_all() -> None:
        for t in st.tasks.values():
            if t["status"] == "PENDING":
                _consume(t)

    app.state.process_all = process_all
    return app


class _ToggleTransport(httpx.AsyncBaseTransport):
    """ASGITransport that simulates an unreachable server when ``mock.offline`` is set."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._inner = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._app.state.mock.offline:
            raise httpx.ConnectError("[mock] connection refused", request=request)
        return await self._inner.handle_async_request(request)


def paperless_client_factory(app: FastAPI):
    """An ``http_client_factory`` for DeliveryEnv that routes to the mock app in-process."""

    def factory(**kw: Any) -> httpx.AsyncClient:
        kw.pop("verify", None)
        return httpx.AsyncClient(transport=_ToggleTransport(app), **kw)

    return factory


def main() -> None:  # pragma: no cover - dev helper
    import argparse

    import uvicorn

    p = argparse.ArgumentParser(description="Mock Paperless-ngx API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--token", default="dev-token")
    p.add_argument("--api-version", type=int, default=9)
    p.add_argument("--allow-duplicates", action="store_true")
    a = p.parse_args()
    app = create_paperless_mock(
        token=a.token, max_api_version=a.api_version, reject_duplicates=not a.allow_duplicates
    )
    print(f"Mock Paperless on http://{a.host}:{a.port}  token={a.token}")  # noqa: T201
    uvicorn.run(app, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
