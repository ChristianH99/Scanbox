"""Safe file handling: atomic writes, data-dir relative paths, disk usage."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

from .config import AppConfig


class UnsafePath(ValueError):
    pass


def safe_join(root: Path, *parts: str) -> Path:
    """Join and verify the result stays inside ``root`` (after resolving symlinks)."""
    root_r = root.resolve()
    p = root_r.joinpath(*parts).resolve()
    if p != root_r and not p.is_relative_to(root_r):
        raise UnsafePath(f"path escapes {root}")
    return p


def fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(target: Path, data: bytes, tmp_dir: Path | None = None) -> None:
    """Write data to target atomically (temp file in same dir/filesystem + fsync + rename)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    d = tmp_dir if tmp_dir is not None and _same_fs(tmp_dir, target.parent) else target.parent
    fd, tmp = tempfile.mkstemp(prefix=".sbx-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        fsync_dir(target.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_copy(src: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".sbx-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as f, open(src, "rb") as r:
            shutil.copyfileobj(r, f, length=1024 * 1024)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        fsync_dir(target.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def exclusive_atomic_copy(src: Path, target: Path) -> None:
    """Like atomic_copy but fails with FileExistsError instead of replacing an existing file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".sbx-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as f, open(src, "rb") as r:
            shutil.copyfileobj(r, f, length=1024 * 1024)
            f.flush()
            os.fsync(f.fileno())
        # os.link fails if target exists → no overwrite, atomic on POSIX.
        try:
            os.link(tmp, target)
        except (OSError, NotImplementedError) as e:
            if isinstance(e, FileExistsError):
                raise
            # Filesystems without hard links (some SMB mounts): best effort.
            if target.exists():
                raise FileExistsError(str(target)) from e
            os.replace(tmp, target)
            tmp = ""
        fsync_dir(target.parent)
    finally:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _same_fs(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


class Storage:
    """Paths of the data directory. Stored paths in the DB are relative to ``data_dir``."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.root = config.data_dir

    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root.resolve()).as_posix()

    def abs(self, rel: str) -> Path:
        return safe_join(self.root, rel)

    def raw_page_path(self, document_id: int, page_id: str, ext: str = "jpg") -> Path:
        return self.config.raw_dir / str(document_id) / f"{page_id}.{ext}"

    def processed_run_dir(self, document_id: int, run: int) -> Path:
        return self.config.processed_dir / str(document_id) / str(run)

    def render_cache_dir(self, page_id: str) -> Path:
        return self.config.cache_dir / "render" / page_id[:2] / page_id

    def save_raw_page(self, document_id: int, data: bytes, ext: str = "jpg") -> tuple[str, Path, str]:
        """Persist a page the moment it arrives. Returns (page_id, path, sha256)."""
        page_id = str(uuid.uuid4())
        path = self.raw_page_path(document_id, page_id, ext)
        atomic_write_bytes(path, data)
        return page_id, path, hashlib.sha256(data).hexdigest()

    @contextlib.contextmanager
    def temp_dir(self, prefix: str = "job-") -> Iterator[Path]:
        self.config.tmp_dir.mkdir(parents=True, exist_ok=True)
        d = Path(tempfile.mkdtemp(prefix=prefix, dir=self.config.tmp_dir))
        try:
            yield d
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def clean_tmp(self) -> int:
        n = 0
        if self.config.tmp_dir.exists():
            for p in self.config.tmp_dir.iterdir():
                with contextlib.suppress(OSError):
                    if p.is_dir():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
                    n += 1
        return n

    def free_mb(self) -> int:
        return free_bytes(self.root) // (1024 * 1024)
