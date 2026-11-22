"""Paperless-ngx destination (REST API upload or consume folder).

API assumptions (Paperless-ngx 2.x / 3.x, REST API versions 9-10; see docs/DESTINATIONS.md):

* ``POST /api/documents/post_document/`` (multipart): ``document``, ``title``, ``created``,
  ``correspondent``/``document_type`` (ids), ``tags`` (id, repeated), ``archive_serial_number``,
  ``custom_fields`` (JSON object ``{"<field id>": value}``; very old servers only accept a list of
  ids, then we upload without and PATCH the value afterwards). Returns the task UUID as JSON string.
* ``GET /api/tasks/?task_id=<uuid>``: list (API <= 9) or paginated object (API 10) of tasks with
  ``status`` (PENDING/STARTED/SUCCESS/FAILURE/REVOKED), ``result`` and ``related_document``.
* ``GET /api/documents/?checksum__iexact=<hex>``: duplicate detection. Paperless 3 stores SHA-256,
  older versions MD5, so we query both. Paperless 3 *accepts* duplicates by default
  (``PAPERLESS_CONSUMER_DELETE_DUPLICATES=false``), so we check for an existing document before
  re-uploading instead of relying on a duplicate rejection.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, Field, model_validator

from scanbox.destinations.base import (
    Artifact,
    DeliveryEnv,
    DeliveryError,
    DeliveryJob,
    DeliveryResult,
    RecoverAction,
)
from scanbox.destinations.templating import render_template

API_VERSION = 9  # supported by Paperless-ngx >= 2.16 and 3.x; we fall back to the server default on 406
ASN_MAX = 0xFFFFFFFF
TASK_POLL_TIMEOUT_S = 60.0
TASK_POLL_INTERVAL_S = 1.0
_DUPLICATE_RE = re.compile(r"duplicate of .*\(#(\d+)\)|document_already_exists", re.IGNORECASE)
_NEW_DOC_RE = re.compile(r"New document id (\d+) created", re.IGNORECASE)


class PaperlessConfig(BaseModel):
    mode: Literal["api", "consume"] = "api"
    title_template: str = "{title}"
    tags: list[str] = Field(default_factory=list)
    create_missing_tags: bool = False
    correspondent: str | None = None
    document_type: str | None = None
    set_created_date: bool = True
    qr_mapping: Literal["none", "asn", "custom_field", "tag"] = "asn"
    qr_custom_field: str | None = None
    asn_fallback: Literal["custom_field", "tag", "none"] = "none"
    skip_ocr: bool = False
    wait_for_task: bool = True
    consume_subdir: str | None = None

    @model_validator(mode="after")
    def _check(self) -> PaperlessConfig:
        needs_field = self.qr_mapping == "custom_field" or (
            self.qr_mapping == "asn" and self.asn_fallback == "custom_field"
        )
        if needs_field and not (self.qr_custom_field or "").strip():
            raise ValueError("qr_custom_field is required for the selected QR mapping")
        if self.consume_subdir:
            sanitize_subdir(self.consume_subdir)  # raises ValueError
        return self


# --------------------------------------------------------------------------- helpers

_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _sanitize_segment(seg: str) -> str:
    seg = unicodedata.normalize("NFC", seg)
    seg = _BAD_CHARS.sub("_", seg)
    seg = " ".join(seg.split()).lstrip(".-").rstrip(". ")
    if seg.split(".")[0].upper() in _RESERVED:
        seg += "_"
    return seg[:100]


def sanitize_subdir(subdir: str) -> Path:
    """Sanitise a relative sub-directory per segment. Rejects ``..`` and absolute paths."""
    raw = subdir.replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ValueError("consume_subdir must be relative")
    parts = []
    for seg in raw.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError("consume_subdir must not contain '..'")
        clean = _sanitize_segment(seg)
        if clean:
            parts.append(clean)
    return Path(*parts) if parts else Path()


def _sanitize_filename(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, "pdf"
    stem = _sanitize_segment(stem) or "scan"
    ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:10] or "pdf"
    return f"{stem}.{ext}"


def _file_hashes(path: Path) -> tuple[str, str]:
    sha, md5 = hashlib.sha256(), hashlib.md5()  # noqa: S324 - Paperless 2.x checksum
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(body, dict):
        if "detail" in body and len(body) == 1:
            return str(body["detail"])
        return "; ".join(
            f"{k}: {' '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in body.items()
        )
    if isinstance(body, list):
        return "; ".join(map(str, body))
    return str(body)


def _raise_for_status(resp: httpx.Response, what: str) -> None:
    if resp.is_success:
        return
    code = resp.status_code
    msg = _error_text(resp)
    if code in (401, 403):
        raise DeliveryError(
            "paperless_auth_failed",
            f"Paperless rejected the API token ({code}: {msg}). Check your Paperless token.",
            retryable=False,
        )
    if code == 429 or code >= 500 or code in (408, 502, 503, 504):
        raise DeliveryError(
            "paperless_unavailable", f"Paperless {what} failed ({code}: {msg})", retryable=True
        )
    raise DeliveryError("paperless_rejected", f"Paperless {what} failed ({code}: {msg})", retryable=False)


class _Api:
    """Thin Paperless REST client with error mapping and version negotiation."""

    def __init__(self, env: DeliveryEnv) -> None:
        s = env.settings
        url = (s.admin("paperless.url") or "").strip().rstrip("/")
        token = (s.user("paperless.token") or "").strip()
        if not url:
            raise DeliveryError(
                "paperless_not_configured",
                "Paperless URL is not set (admin setting paperless.url).",
                retryable=False,
            )
        if not url.startswith(("http://", "https://")):
            raise DeliveryError(
                "paperless_not_configured", f"Invalid Paperless URL {url!r}.", retryable=False
            )
        if not token:
            raise DeliveryError(
                "paperless_not_configured",
                "No Paperless API token set. Add it in your user settings (paperless.token).",
                retryable=False,
            )
        verify = s.admin("paperless.verify_tls", True) not in (False, "false", "0", 0)
        self.client = env.http_client_factory(
            base_url=url,
            headers={"Authorization": f"Token {token}"},
            timeout=httpx.Timeout(60.0, connect=10.0),
            verify=verify,
            follow_redirects=True,
        )
        self.accept = f"application/json; version={API_VERSION}"
        self.last_response: httpx.Response | None = None

    async def __aenter__(self) -> _Api:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client.aclose()

    async def request(self, method: str, url: str, *, what: str, **kw: Any) -> httpx.Response:
        for _ in range(2):
            headers = {"Accept": self.accept} if self.accept else {}
            try:
                resp = await self.client.request(method, url, headers=headers, **kw)
            except httpx.TimeoutException as e:
                raise DeliveryError(
                    "paperless_timeout", f"Paperless {what} timed out: {e}", retryable=True
                ) from e
            except httpx.TransportError as e:
                raise DeliveryError(
                    "paperless_unavailable", f"Cannot reach Paperless ({what}): {e}", retryable=True
                ) from e
            if resp.status_code == 406 and self.accept:
                self.accept = None  # server predates API_VERSION: use its default version
                continue
            break
        self.last_response = resp
        _raise_for_status(resp, what)
        return resp

    async def find_by_name(self, endpoint: str, name: str) -> dict | None:
        url: str | None = f"/api/{endpoint}/"
        params: dict[str, Any] | None = {"name__iexact": name, "page_size": 100}
        for _ in range(20):  # page limit
            resp = await self.request("GET", url, params=params, what=f"{endpoint} lookup")  # type: ignore[arg-type]
            body = resp.json()
            items = body.get("results", []) if isinstance(body, dict) else body
            for it in items:
                if str(it.get("name", "")).casefold() == name.casefold():
                    return it
            url = body.get("next") if isinstance(body, dict) else None
            params = None
            if not url:
                return None
        return None

    async def find_document_by_checksum(self, *checksums: str) -> int | None:
        for cs in checksums:
            resp = await self.request(
                "GET",
                "/api/documents/",
                params={"checksum__iexact": cs, "page_size": 1},
                what="duplicate check",
            )
            body = resp.json()
            items = body.get("results", []) if isinstance(body, dict) else body
            if items:
                return int(items[0]["id"])
        return None

    async def get_task(self, task_id: str) -> dict | None:
        resp = await self.request("GET", "/api/tasks/", params={"task_id": task_id}, what="task status")
        body = resp.json()
        items = body.get("results", []) if isinstance(body, dict) else body
        return items[0] if items else None


def _task_status(task: dict) -> str:
    return str(task.get("status", "")).upper()


def _task_document_id(task: dict) -> int | None:
    rel = task.get("related_document")
    if rel not in (None, ""):
        try:
            return int(rel)
        except (TypeError, ValueError):
            pass
    m = _NEW_DOC_RE.search(str(task.get("result") or ""))
    return int(m.group(1)) if m else None


def _duplicate_id(result: str) -> int | None | bool:
    m = _DUPLICATE_RE.search(result)
    if not m:
        return False
    return int(m.group(1)) if m.group(1) else None


# --------------------------------------------------------------------------- destination


class PaperlessDestination:
    type: ClassVar[str] = "paperless"
    Config: ClassVar[type[BaseModel]] = PaperlessConfig

    task_poll_timeout_s: float = TASK_POLL_TIMEOUT_S
    task_poll_interval_s: float = TASK_POLL_INTERVAL_S

    async def validate(self, cfg: BaseModel, env: DeliveryEnv) -> list[str]:
        assert isinstance(cfg, PaperlessConfig)
        problems: list[str] = []
        s = env.settings
        if cfg.mode == "api":
            if not (s.admin("paperless.url") or "").strip():
                problems.append("Paperless URL is not set (admin setting paperless.url).")
            if not (s.user("paperless.token") or "").strip():
                problems.append("No Paperless API token set in your user settings.")
        else:
            root = (s.admin("paperless.consume_dir") or "").strip()
            if not root:
                problems.append("Paperless consume folder is not set (admin setting paperless.consume_dir).")
            elif not await asyncio.to_thread(Path(root).is_dir):
                problems.append(f"Paperless consume folder {root} does not exist.")
        return problems

    @staticmethod
    def _artifact(job: DeliveryJob, cfg: PaperlessConfig) -> Artifact:
        if cfg.skip_ocr and job.image_only_pdf is not None:
            return job.image_only_pdf
        return job.primary

    # ------------------------------------------------------------------ api mode

    async def _resolve_id(self, api: _Api, endpoint: str, name: str, *, create: bool) -> int:
        found = await api.find_by_name(endpoint, name)
        if found:
            return int(found["id"])
        if not create:
            label = endpoint.rstrip("s").replace("_", " ")
            raise DeliveryError(
                f"paperless_unknown_{endpoint.rstrip('s')}",
                f"Paperless has no {label} named {name!r}.",
                retryable=False,
            )
        resp = await api.request("POST", f"/api/{endpoint}/", json={"name": name}, what=f"create {endpoint}")
        return int(resp.json()["id"])

    async def _metadata(
        self, job: DeliveryJob, cfg: PaperlessConfig, api: _Api
    ) -> tuple[list[tuple[str, str]], dict[int, Any], dict[str, Any]]:
        """Returns (form fields, custom field values by id, info for the result detail)."""
        fields: list[tuple[str, str]] = []
        info: dict[str, Any] = {}
        title = " ".join(render_template(cfg.title_template, job.template_vars()).split())[:128]
        if title:
            fields.append(("title", title))
        if cfg.set_created_date:
            fields.append(("created", job.created_at.isoformat()))
        if cfg.correspondent:
            cid = await self._resolve_id(api, "correspondents", cfg.correspondent, create=False)
            fields.append(("correspondent", str(cid)))
        if cfg.document_type:
            did = await self._resolve_id(api, "document_types", cfg.document_type, create=False)
            fields.append(("document_type", str(did)))

        tag_names = list(cfg.tags)
        qr_tags: list[str] = []
        custom: dict[int, Any] = {}
        qr = (job.qr_value or "").strip()
        mapping = cfg.qr_mapping
        if qr and mapping == "asn":
            if qr.isdigit() and int(qr) <= ASN_MAX:
                fields.append(("archive_serial_number", str(int(qr))))
                info["asn"] = int(qr)
            else:
                info["asn_fallback"] = cfg.asn_fallback
                mapping = cfg.asn_fallback if cfg.asn_fallback != "none" else "none"
        if qr and mapping == "tag":
            qr_tags.append(qr[:100])
        if qr and mapping == "custom_field":
            assert cfg.qr_custom_field
            cf = await api.find_by_name("custom_fields", cfg.qr_custom_field)
            if not cf:
                raise DeliveryError(
                    "paperless_unknown_custom_field",
                    f"Paperless has no custom field named {cfg.qr_custom_field!r}.",
                    retryable=False,
                )
            value: Any = qr
            if cf.get("data_type") == "integer":
                if not re.fullmatch(r"-?\d+", qr):
                    raise DeliveryError(
                        "paperless_bad_custom_field",
                        f"Custom field {cf['name']!r} is an integer field but the QR value {qr!r} is not a number.",
                    )
                value = int(qr)
            custom[int(cf["id"])] = value
            info["custom_field"] = {"name": cf["name"], "value": value}

        tag_ids: list[int] = []
        for name in tag_names:
            tag_ids.append(await self._resolve_id(api, "tags", name, create=cfg.create_missing_tags))
        for name in qr_tags:  # QR tags are per document, so they are always created
            tag_ids.append(await self._resolve_id(api, "tags", name, create=True))
        for tid in dict.fromkeys(tag_ids):
            fields.append(("tags", str(tid)))
        if qr_tags:
            info["qr_tag"] = qr_tags[0]
        return fields, custom, info

    async def _upload(
        self, api: _Api, art: Artifact, filename: str, fields: list[tuple[str, str]], custom: dict[int, Any]
    ) -> tuple[str, bool]:
        """Upload; returns (task_id, custom_fields_pending)."""
        data: dict[str, Any] = {}
        for k, v in fields:
            data.setdefault(k, []).append(v)
        attempts = [True, False] if custom else [False]
        for with_custom in attempts:
            payload = dict(data)
            if with_custom:
                payload["custom_fields"] = json.dumps({str(k): v for k, v in custom.items()})
            with art.path.open("rb") as fh:
                try:
                    resp = await api.request(
                        "POST",
                        "/api/documents/post_document/",
                        data=payload,
                        files={"document": (filename, fh, art.content_type or "application/pdf")},
                        what="upload",
                    )
                except DeliveryError as e:
                    # Old servers only take a list of ids -> upload without, PATCH afterwards.
                    if with_custom and e.code == "paperless_rejected" and "custom_fields" in e.message:
                        continue
                    raise
            task_id = resp.json()
            if isinstance(task_id, dict):  # defensive: some proxies/versions wrap it
                task_id = task_id.get("task_id") or task_id.get("id")
            return str(task_id), bool(custom) and not with_custom
        raise AssertionError("unreachable")

    async def _wait_task(self, api: _Api, task_id: str) -> dict | None:
        deadline = time.monotonic() + self.task_poll_timeout_s
        while True:
            task = await api.get_task(task_id)
            if task and _task_status(task) in ("SUCCESS", "FAILURE", "REVOKED"):
                return task
            if time.monotonic() >= deadline:
                return task
            await asyncio.sleep(self.task_poll_interval_s)

    async def _deliver_api(self, job: DeliveryJob, cfg: PaperlessConfig, env: DeliveryEnv) -> DeliveryResult:
        art = self._artifact(job, cfg)
        filename = _sanitize_filename(job.filename)
        async with _Api(env) as api:
            sha, md5 = await asyncio.to_thread(_file_hashes, art.path)
            if job.attempt > 1:
                # A previous attempt may have reached Paperless (e.g. timeout after upload).
                existing = await api.find_document_by_checksum(sha, md5)
                if existing is not None:
                    return DeliveryResult(
                        f"Already in Paperless (document #{existing})",
                        {"document_id": existing, "duplicate": True, "sha256": sha},
                    )
            fields, custom, info = await self._metadata(job, cfg, api)
            task_id, cf_pending = await self._upload(api, art, filename, fields, custom)
            detail: dict[str, Any] = {"task_id": task_id, "sha256": sha, **info}
            server = api.last_response.headers.get("X-Version") if api.last_response else None
            if server:
                detail["paperless_version"] = server
            if not (cfg.wait_for_task or cf_pending):
                return DeliveryResult("Uploaded to Paperless (consumption queued)", detail)

            task = await self._wait_task(api, task_id)
            status = _task_status(task) if task else "UNKNOWN"
            detail["task_status"] = status
            if status == "SUCCESS":
                doc_id = _task_document_id(task or {})
                detail["document_id"] = doc_id
                message = f"Stored in Paperless as document #{doc_id}" if doc_id else "Stored in Paperless"
                if cf_pending and doc_id is not None:
                    try:
                        await api.request(
                            "PATCH",
                            f"/api/documents/{doc_id}/",
                            json={"custom_fields": [{"field": k, "value": v} for k, v in custom.items()]},
                            what="set custom field",
                        )
                    except DeliveryError as e:
                        # The document exists; re-uploading would duplicate it. Report, don't fail.
                        detail["warning"] = f"Custom field not set: {e.message}"
                        message += " (custom field could not be set)"
                elif cf_pending:
                    detail["warning"] = "Custom field not set: document id unknown"
                return DeliveryResult(message, detail)
            if status in ("FAILURE", "REVOKED"):
                result = str((task or {}).get("result") or "unknown error")
                dup = _duplicate_id(result)
                if dup is not False:
                    detail.update(duplicate=True, document_id=dup)
                    where = f" (document #{dup})" if dup else ""
                    return DeliveryResult(f"Already in Paperless{where}", detail)
                raise DeliveryError(
                    "paperless_consume_failed", f"Paperless could not consume the document: {result}"
                )
            # Still pending after the timeout: it is queued in Paperless, don't upload again.
            detail["warning"] = "Paperless has not finished processing yet"
            msg = "Uploaded to Paperless (still processing)"
            if cf_pending:
                detail["warning"] += "; custom field not set"
            return DeliveryResult(msg, detail)

    # ------------------------------------------------------------------ consume mode

    @staticmethod
    def _consume_target(cfg: PaperlessConfig, env: DeliveryEnv) -> tuple[Path, Path]:
        root_s = (env.settings.admin("paperless.consume_dir") or "").strip()
        if not root_s:
            raise DeliveryError(
                "paperless_not_configured",
                "Paperless consume folder is not set (admin setting paperless.consume_dir).",
                retryable=False,
            )
        root = Path(root_s).resolve()
        if not root.is_dir():
            # Could be an unmounted network share – retry later.
            raise DeliveryError(
                "paperless_consume_dir_missing", f"Consume folder {root} does not exist.", retryable=True
            )
        try:
            sub = sanitize_subdir(cfg.consume_subdir or "")
        except ValueError as e:
            raise DeliveryError("paperless_bad_subdir", str(e), retryable=False) from e
        target = (root / sub).resolve()
        if not target.is_relative_to(root):
            raise DeliveryError(
                "paperless_bad_subdir", "consume_subdir escapes the consume folder", retryable=False
            )
        return root, target

    async def _deliver_consume(
        self, job: DeliveryJob, cfg: PaperlessConfig, env: DeliveryEnv
    ) -> DeliveryResult:
        root, target = await asyncio.to_thread(self._consume_target, cfg, env)
        art = self._artifact(job, cfg)
        filename = _sanitize_filename(job.filename)
        try:
            final, sha = await asyncio.to_thread(_atomic_copy_no_overwrite, art.path, target, filename, root)
        except DeliveryError:
            raise
        except OSError as e:
            raise DeliveryError(
                "paperless_write_failed", f"Writing to the consume folder failed: {e}", retryable=True
            ) from e
        return DeliveryResult(
            f"Written to Paperless consume folder as {final.relative_to(root).as_posix()}",
            {"path": str(final), "sha256": sha, "mode": "consume"},
        )

    # ------------------------------------------------------------------ protocol

    async def deliver(self, job: DeliveryJob, cfg: BaseModel, env: DeliveryEnv) -> DeliveryResult:
        assert isinstance(cfg, PaperlessConfig)
        if cfg.mode == "consume":
            return await self._deliver_consume(job, cfg, env)
        return await self._deliver_api(job, cfg, env)

    async def recover(
        self, job: DeliveryJob, cfg: BaseModel, env: DeliveryEnv, previous: dict[str, Any] | None
    ) -> RecoverAction:
        assert isinstance(cfg, PaperlessConfig)
        art = self._artifact(job, cfg)
        try:
            sha, md5 = await asyncio.to_thread(_file_hashes, art.path)
        except OSError:
            return RecoverAction.REDELIVER
        if cfg.mode == "consume":
            try:
                _root, target = await asyncio.to_thread(self._consume_target, cfg, env)
            except DeliveryError:
                return RecoverAction.REDELIVER
            if await asyncio.to_thread(_dir_has_sha256, target, sha, art.size):
                return RecoverAction.DONE
            return RecoverAction.REDELIVER
        # API mode: a known task tells us the outcome; otherwise look for the checksum.
        try:
            async with _Api(env) as api:
                task_id = (previous or {}).get("task_id")
                if task_id:
                    task = await api.get_task(str(task_id))
                    if task and _task_status(task) in ("SUCCESS", "PENDING", "STARTED"):
                        return RecoverAction.DONE
                    if task and _duplicate_id(str(task.get("result") or "")) is not False:
                        return RecoverAction.DONE
                if await api.find_document_by_checksum(sha, md5) is not None:
                    return RecoverAction.DONE
        except DeliveryError:
            pass
        # Re-uploading is safe: Paperless < 3 rejects duplicates (treated as success) and deliver()
        # re-checks the checksum on attempt > 1.
        return RecoverAction.REDELIVER


# --------------------------------------------------------------------------- file helpers


def _atomic_copy_no_overwrite(src: Path, target_dir: Path, filename: str, root: Path) -> tuple[Path, str]:
    target_dir.mkdir(parents=True, exist_ok=True)
    if not target_dir.resolve().is_relative_to(root):
        raise DeliveryError(
            "paperless_bad_subdir", "consume_subdir escapes the consume folder", retryable=False
        )
    # Dot-prefixed temp name: Paperless ignores hidden files (CONSUMER_IGNORE_PATTERNS).
    tmp = target_dir / f".scanbox-{secrets.token_hex(8)}.tmp"
    sha = hashlib.sha256()
    try:
        with src.open("rb") as fin, tmp.open("xb") as fout:
            for chunk in iter(lambda: fin.read(1 << 20), b""):
                sha.update(chunk)
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        stem, _, ext = filename.rpartition(".")
        for n in range(1, 1000):
            name = filename if n == 1 else f"{stem} ({n}).{ext}"
            final = target_dir / name
            if not final.resolve().is_relative_to(root):
                raise DeliveryError("paperless_bad_subdir", "target path escapes the consume folder")
            try:
                os.link(tmp, final)  # atomic and fails if the name exists -> never overwrites
            except FileExistsError:
                continue
            except OSError:
                # Filesystem without hard links (some SMB/FUSE mounts): best-effort check + replace.
                if final.exists():
                    continue
                os.replace(tmp, final)
            break
        else:
            raise DeliveryError(
                "paperless_write_failed", "Too many files with the same name in the consume folder"
            )
        _fsync_dir(target_dir)
        return final, sha.hexdigest()
    finally:
        tmp.unlink(missing_ok=True)


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _dir_has_sha256(target: Path, sha256: str, size: int) -> bool:
    if not target.is_dir():
        return False
    for p in target.iterdir():
        if p.name.startswith(".") or not p.is_file():
            continue
        try:
            if p.stat().st_size != size:
                continue
            h = hashlib.sha256()
            with p.open("rb") as fh:
                for chunk in iter(lambda fh=fh: fh.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() == sha256:
                return True
        except OSError:
            continue
    return False


async def test_paperless(env: DeliveryEnv) -> tuple[bool, str]:
    """Check URL + token; for the settings "test connection" button."""
    try:
        async with _Api(env) as api:
            resp = await api.request("GET", "/api/tags/", params={"page_size": 1}, what="connection test")
    except DeliveryError as e:
        return False, e.message
    version = resp.headers.get("X-Version")
    api_version = resp.headers.get("X-Api-Version")
    msg = "Connected to Paperless-ngx"
    if version:
        msg += f" {version}"
    if api_version:
        msg += f" (API v{api_version})"
    return True, msg


test_paperless.__test__ = False  # type: ignore[attr-defined]  # not a pytest test
