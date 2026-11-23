"""Built-in destinations: download (no-op) and NAS folder (atomic writes into admin-defined roots)."""

from __future__ import annotations

import asyncio
import glob
import threading
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ..filenames import sanitize_subpath, unique_path
from ..storage import UnsafePath, exclusive_atomic_copy, safe_join, sha256_file
from .base import DeliveryEnv, DeliveryError, DeliveryJob, DeliveryResult, RecoverAction
from .templating import render_template

_WRITE_LOCK = threading.Lock()


class DownloadDestination:
    type: ClassVar[str] = "download"

    class Config(BaseModel):
        model_config = ConfigDict(extra="ignore")

    async def validate(self, cfg: BaseModel, env: DeliveryEnv) -> list[str]:
        return []

    async def deliver(self, job: DeliveryJob, cfg: BaseModel, env: DeliveryEnv) -> DeliveryResult:
        return DeliveryResult("Zum Download bereit", {"files": [a.filename for a in job.artifacts]})

    async def recover(self, job: DeliveryJob, cfg: BaseModel, env: DeliveryEnv, previous: dict | None) -> RecoverAction:
        return RecoverAction.REDELIVER


class FolderConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    root: str = Field(..., description="Name of an admin-defined output root")
    subfolder: str = Field("", max_length=300, description="Template, e.g. '{user}/{date:%Y}'")
    include_text: bool = False


class FolderDestination:
    type: ClassVar[str] = "folder"
    Config = FolderConfig

    def _roots(self, env: DeliveryEnv) -> dict[str, str]:
        roots = env.settings.admin("output.roots", {}) or {}
        return roots if isinstance(roots, dict) else {}

    def _target_dir(self, job: DeliveryJob, cfg: FolderConfig, env: DeliveryEnv) -> Path:
        roots = self._roots(env)
        if cfg.root not in roots:
            raise DeliveryError("folder_root_unknown", f"Ausgabeordner '{cfg.root}' ist nicht (mehr) freigegeben")
        root = Path(roots[cfg.root])
        if not root.is_dir():
            raise DeliveryError("folder_root_missing", f"Ausgabeordner {cfg.root} ist nicht eingehängt", retryable=True)
        vars_ = job.template_vars()
        vars_["year"] = job.created_at.strftime("%Y")
        vars_["month"] = job.created_at.strftime("%m")
        sub = render_template(cfg.subfolder or "", vars_)
        try:
            rel = sanitize_subpath(sub)
            return safe_join(root, *rel.parts) if rel.parts else root.resolve()
        except (ValueError, UnsafePath) as e:
            raise DeliveryError("folder_invalid_subfolder", f"Ungültiger Unterordner: {e}") from e

    async def validate(self, cfg: BaseModel, env: DeliveryEnv) -> list[str]:
        assert isinstance(cfg, FolderConfig)
        roots = self._roots(env)
        if cfg.root not in roots:
            return [f"Ausgabeordner '{cfg.root}' unbekannt (verfügbar: {', '.join(roots) or '–'})"]
        try:
            sanitize_subpath(cfg.subfolder.replace("{", "").replace("}", ""))
        except ValueError as e:
            return [str(e)]
        return []

    async def deliver(self, job: DeliveryJob, cfg: BaseModel, env: DeliveryEnv) -> DeliveryResult:
        assert isinstance(cfg, FolderConfig)
        target_dir = self._target_dir(job, cfg, env)
        written: list[dict[str, Any]] = []

        def _write() -> None:
            target_dir.mkdir(parents=True, exist_ok=True)
            for a in job.artifacts:
                if a.kind == "pdf_image" or (a.kind == "txt" and not cfg.include_text and len(job.artifacts) > 1):
                    continue
                for _ in range(20):
                    path = unique_path(target_dir, a.filename)
                    try:
                        exclusive_atomic_copy(a.path, path)
                        break
                    except FileExistsError:
                        continue  # raced with another writer → next suffix
                else:
                    raise DeliveryError("folder_conflict", "Kein freier Dateiname gefunden")
                written.append({"path": str(path), "sha256": a.sha256, "artifact": a.filename})

        def _locked_write() -> None:
            with _WRITE_LOCK:  # serialise "find free name + write" across delivery lanes (SMB fallback)
                _write()

        try:
            await asyncio.to_thread(_locked_write)
        except PermissionError as e:
            raise DeliveryError("folder_permission", f"Keine Schreibrechte: {e}") from e
        except OSError as e:
            raise DeliveryError("folder_io", f"Schreibfehler: {e}", retryable=True) from e
        names = ", ".join(Path(w["path"]).name for w in written)
        return DeliveryResult(f"Gespeichert: {names}", {"files": written, "dir": str(target_dir)})

    async def recover(self, job: DeliveryJob, cfg: BaseModel, env: DeliveryEnv, previous: dict | None) -> RecoverAction:
        """Done if every artifact already exists in the target dir with the same checksum."""
        assert isinstance(cfg, FolderConfig)
        try:
            target_dir = self._target_dir(job, cfg, env)
        except DeliveryError:
            return RecoverAction.REDELIVER
        if not target_dir.is_dir():
            return RecoverAction.REDELIVER

        def _check() -> bool:
            for a in job.artifacts:
                if a.kind == "pdf_image":
                    continue
                stem, _, ext = a.filename.rpartition(".")
                candidates = [target_dir / a.filename, *target_dir.glob(f"{glob.escape(stem)} (*).{glob.escape(ext)}")]
                if not any(c.is_file() and c.stat().st_size == a.size and sha256_file(c) == a.sha256 for c in candidates):
                    return False
            return True

        return RecoverAction.DONE if await asyncio.to_thread(_check) else RecoverAction.REDELIVER
