"""Simulator state: feeder, faults, jobs, and the eSCL job logic (HTTP-agnostic)."""

from __future__ import annotations

import asyncio
import dataclasses
import math
import time
import uuid as uuidlib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import caps as capsmod
from . import imaging, pages
from .pages import BLANK, PageSpec
from .presets import preset
from .settings import ScanSettings, SettingsError, effective_region, parse_scan_settings, validate

# --- job state reasons (eSCL CamelCase of PWG keywords; some are guesses, see SIMULATOR.md)
R_SCANNING = "JobScanning"
R_COMPLETED = "JobCompletedSuccessfully"
R_CANCELED = "JobCanceledByUser"
R_ABORTED = "AbortedBySystem"
R_JAM = "MediaJam"
R_MULTIPICK = "MultipickDetected"
R_HATCH = "DoorOpen"
R_TIMEOUT = "JobTimedOut"
R_CONNECTION = "ConnectionLost"

ACTIVE_STATES = {"Pending", "Processing"}

BwBehaviour = Literal["gray_thresholded", "reject"]
DropMode = Literal["lose_page", "redeliver", "abort_job"]
SlowStyle = Literal["retry_after", "block"]
OfflineMode = Literal["raise", "503", "hang"]


class EsclError(Exception):
    """An eSCL request failure the device would answer with ``status``."""

    def __init__(self, status: int, message: str = "", headers: dict[str, str] | None = None):
        super().__init__(message or str(status))
        self.status = status
        self.message = message
        self.headers = headers or {}


@dataclass
class Faults:
    """Injected faults. Event faults fire once and reset unless ``sticky``.

    * one-shot events: ``jam_after_pages``, ``multipick_after_pages``,
      ``drop_connection_after_pages``, ``fail_create_status``, ``busy`` (next create),
      ``offline`` (next eSCL request).
    * conditions/modes (persist until changed): ``hatch_open``, ``slow_ms``, ``latency_ms``,
      ``slow_style``.
    """

    jam_after_pages: int | None = None
    multipick_after_pages: int | None = None
    hatch_open: bool = False
    slow_ms: int = 0
    slow_style: SlowStyle = "retry_after"
    latency_ms: int = 0
    drop_connection_after_pages: int | None = None
    offline: bool = False
    busy: bool = False
    fail_create_status: int | None = None
    sticky: bool = False

    def update(self, **kw: Any) -> None:
        names = {f.name for f in dataclasses.fields(self)}
        for k, v in kw.items():
            if k not in names:
                raise ValueError(f"unknown fault {k!r}")
            setattr(self, k, v)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class SimConfig:
    """Behaviour switches that are not faults (kept across ``reset()``)."""

    background: tuple[int, int, int] = (200, 200, 198)
    bw_jpeg_behaviour: BwBehaviour = "gray_thresholded"
    location_style: Literal["absolute", "relative"] = "absolute"
    # HTTP status of NextDocument after the job was aborted by a jam/multipick/hatch/timeout.
    abort_status: int = 404
    drop_mode: DropMode = "lose_page"
    job_timeout_s: float = 120.0
    multipick_detection_default: bool = True
    multipick_action_default: str = "StopJob"
    blank_threshold: float = 0.002  # ink fraction below which a page counts as blank
    job_history: int = 10
    samples_dir: Path = pages.DEFAULT_SAMPLES_DIR
    offline_mode: OfflineMode = "raise"
    hang_s: float = 300.0

    def update(self, **kw: Any) -> None:
        names = {f.name for f in dataclasses.fields(self)}
        for k, v in kw.items():
            if k not in names:
                raise ValueError(f"unknown config key {k!r}")
            if k == "background":
                v = tuple(int(c) for c in v)
            if k == "samples_dir":
                v = Path(v)
            setattr(self, k, v)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["samples_dir"] = str(self.samples_dir)
        d["background"] = list(self.background)
        return d


@dataclass
class Job:
    uuid: str
    settings: ScanSettings
    params: imaging.OutputParams
    created: float
    state: str = "Processing"
    reasons: list[str] = field(default_factory=lambda: [R_SCANNING])
    images_completed: int = 0
    sheets_fed: int = 0
    blank_removed: int = 0
    pending: deque = field(default_factory=deque)  # deque[(PageSpec, sheet_no, side)]
    last_activity: float = 0.0
    ready_at: float | None = None
    last_image: imaging.Encoded | None = None
    delivered: list[dict] = field(default_factory=list)
    lost: list[dict] = field(default_factory=list)

    @property
    def path(self) -> str:
        return f"/eSCL/ScanJobs/{self.uuid}"

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def images_to_transfer(self) -> int:
        return len(self.pending)

    def finish(self, state: str, *reasons: str) -> None:
        self.state = state
        keep = [r for r in self.reasons if r == R_MULTIPICK]
        self.reasons = list(dict.fromkeys([*reasons, *keep]))
        self.pending.clear()
        self.ready_at = None

    def to_dict(self, now: float) -> dict:
        return {
            "uuid": self.uuid,
            "uri": self.path,
            "state": self.state,
            "reasons": list(self.reasons),
            "age_s": round(now - self.created, 3),
            "images_completed": self.images_completed,
            "images_to_transfer": self.images_to_transfer,
            "sheets_fed": self.sheets_fed,
            "blank_removed": self.blank_removed,
            "delivered": list(self.delivered),
            "lost": list(self.lost),
            "settings": self.settings.summary(),
        }


@dataclass
class PageResult:
    """Outcome of NextDocument: a page (possibly to be cut off mid-body)."""

    encoded: imaging.Encoded
    drop: bool = False


class SimState:
    """Everything the simulated scanner knows. Safe to mutate directly from tests."""

    def __init__(self, stack: str | list | None = "letter_de", **config: Any):
        self.config = SimConfig()
        self.config.update(**config)
        self._initial_stack = stack
        self.caps = capsmod.capabilities()
        self.faults = Faults()
        self.feeder: list[PageSpec] = []
        self.jobs: dict[str, Job] = {}
        self.condition: str | None = None  # "jam" | "multipick" (paper stuck in the path)
        self.paper_path: list[PageSpec] = []
        self.lost_sheets: list[PageSpec] = []  # double-fed sheets that went through unscanned
        self.request_log: deque[tuple[str, str, int]] = deque(maxlen=500)
        self.clock = time.monotonic
        self.reset()

    # ------------------------------------------------------------------ control API

    def reset(self) -> None:
        self.faults = Faults()
        self.jobs.clear()
        self.condition = None
        self.paper_path = []
        self.lost_sheets = []
        self.request_log.clear()
        self.feeder = []
        if self._initial_stack is not None:
            self.load(self._initial_stack)

    def load(self, stack: str | list | None = None, *, append: bool = False) -> None:
        """Put sheets into the feeder: a preset name or a list of PageSpec / dicts."""
        if stack is None:
            sheets: list[PageSpec] = []
        elif isinstance(stack, str):
            sheets = preset(stack)
        else:
            sheets = [PageSpec.from_any(s) for s in stack]
        if len(sheets) + (len(self.feeder) if append else 0) > self.caps.feeder_capacity * 4:
            raise ValueError("stack too large")
        self.feeder = (self.feeder if append else []) + sheets

    def load_empty(self) -> None:
        self.feeder = []

    def set_faults(self, **kw: Any) -> Faults:
        self.faults.update(**kw)
        return self.faults

    def clear_faults(self) -> None:
        self.faults = Faults()

    def clear_jam(self, reinsert: bool = True) -> None:
        """Operator opens the hatch and removes stuck paper (optionally putting it back on top)."""
        if reinsert:
            self.feeder = self.paper_path + self.feeder
        self.paper_path = []
        self.condition = None

    @property
    def active_job(self) -> Job | None:
        return next((j for j in self.jobs.values() if j.active), None)

    # ------------------------------------------------------------------ status

    @property
    def adf_state(self) -> str:
        if self.faults.hatch_open:
            return "ScannerAdfHatchOpen"
        if self.condition == "jam":
            return "ScannerAdfJam"
        if self.condition == "multipick":
            return "ScannerAdfMultipickDetected"
        if self.active_job is not None and self.feeder:
            return "ScannerAdfProcessing"
        return "ScannerAdfLoaded" if self.feeder else "ScannerAdfEmpty"

    @property
    def scanner_state(self) -> str:
        if self.active_job is not None or self.faults.busy:
            return "Processing"
        if self.faults.hatch_open or self.condition is not None:
            return "Stopped"
        return "Idle"

    def expire_jobs(self) -> None:
        now = self.clock()
        for job in self.jobs.values():
            if job.active and now - job.last_activity > self.config.job_timeout_s:
                job.finish("Aborted", R_ABORTED, R_TIMEOUT)

    def jobs_for_status(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)

    def status_xml(self) -> bytes:
        from .xmlgen import scanner_status

        self.expire_jobs()
        return scanner_status(self.caps.version, self.scanner_state, self.adf_state,
                              self.jobs_for_status(), self.clock())

    def snapshot(self) -> dict:
        now = self.clock()
        return {
            "scanner_state": self.scanner_state,
            "adf_state": self.adf_state,
            "condition": self.condition,
            "feeder": [s.to_dict() for s in self.feeder],
            "feeder_count": len(self.feeder),
            "paper_path": [s.to_dict() for s in self.paper_path],
            "lost_sheets": [s.to_dict() for s in self.lost_sheets],
            "faults": self.faults.to_dict(),
            "config": self.config.to_dict(),
            "jobs": [j.to_dict(now) for j in self.jobs_for_status()],
            "requests": [list(r) for r in list(self.request_log)[-100:]],
        }

    # ------------------------------------------------------------------ eSCL operations

    def check_offline(self) -> bool:
        """True if this eSCL request must fail because the scanner is offline (consumes one-shot)."""
        if not self.faults.offline:
            return False
        if not self.faults.sticky:
            self.faults.offline = False
        return True

    def create_job(self, body: bytes) -> Job:
        f = self.faults
        self.expire_jobs()
        if f.fail_create_status is not None:
            status = f.fail_create_status
            if not f.sticky:
                f.fail_create_status = None
            raise EsclError(status, "injected failure")
        settings = parse_scan_settings(body)  # 400 on malformed XML
        if self.active_job is not None:
            raise EsclError(503, "scanner busy: another job is active")
        if f.busy:
            if not f.sticky:
                f.busy = False
            raise EsclError(503, "scanner busy (injected)")
        icaps = validate(settings, self.caps, bw_jpeg_behaviour=self.config.bw_jpeg_behaviour)
        if f.hatch_open:
            raise EsclError(409, "ADF hatch open")
        if self.condition is not None:
            raise EsclError(409, f"ADF {self.condition}: clear the paper path first")
        if not self.feeder:
            raise EsclError(409, "ADF empty")

        region = effective_region(settings, icaps)
        params = imaging.OutputParams(
            dpi=settings.x_resolution,
            region=(region.width, region.height, region.x_offset, region.y_offset),
            bed_width=icaps.max_width,
            background=tuple(self.config.background),
            color_mode=settings.color_mode,
            document_format="application/pdf" if settings.is_pdf else "image/jpeg",
            quality=imaging.jpeg_quality(settings.compression_factor),
            threshold=128 if settings.threshold is None else settings.threshold,
            brightness=settings.brightness,
            contrast=settings.contrast,
            auto_crop=settings.auto_crop,
            samples_dir=self.config.samples_dir,
        )
        now = self.clock()
        job = Job(uuid=str(uuidlib.uuid4()), settings=settings, params=params, created=now, last_activity=now)
        self.jobs[job.uuid] = job
        self._trim_history()
        return job

    def _trim_history(self) -> None:
        done = [j for j in self.jobs_for_status() if not j.active]
        for j in done[self.config.job_history:]:
            del self.jobs[j.uuid]

    def get_job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise EsclError(404, "no such job")
        return job

    def cancel_job(self, job_id: str) -> Job:
        job = self.get_job(job_id)
        if job.active:
            job.finish("Canceled", R_CANCELED)
        return job

    def _is_blank(self, spec: PageSpec, settings: ScanSettings) -> bool:
        thr = self.config.blank_threshold
        if settings.blank_page_sensitivity is not None:
            thr *= max(settings.blank_page_sensitivity, 1) / 128
        return pages.ink_fraction(spec.side(), self.config.samples_dir) < thr

    def _abort_with_condition(self, job: Job, condition: str | None, reason: str, sheets: list) -> None:
        if condition:
            self.condition = condition
            self.paper_path = [s for s in sheets if s is not None]
        job.finish("Aborted", R_ABORTED, reason)

    def _feed(self, job: Job) -> None:
        """Feed sheets until an image is pending or the job ends."""
        f = self.faults
        s = job.settings
        while not job.pending and job.active:
            if f.hatch_open:
                self._abort_with_condition(job, None, R_HATCH, [])
                return
            if not self.feeder:
                job.finish("Completed", R_COMPLETED)
                return
            if f.jam_after_pages is not None and job.sheets_fed >= f.jam_after_pages:
                if not f.sticky:
                    f.jam_after_pages = None
                self._abort_with_condition(job, "jam", R_JAM, [self.feeder.pop(0)])
                return
            multipick = f.multipick_after_pages is not None and job.sheets_fed >= f.multipick_after_pages
            sheet = self.feeder.pop(0)
            job.sheets_fed += 1
            if multipick:
                if not f.sticky:
                    f.multipick_after_pages = None
                second = self.feeder.pop(0) if self.feeder else None
                detect = s.multipick_detection
                if detect is None:
                    detect = self.config.multipick_detection_default
                action = s.multipick_action or self.config.multipick_action_default
                if detect and action == "StopJob":
                    job.sheets_fed -= 1
                    self._abort_with_condition(job, "multipick", R_MULTIPICK, [sheet, second])
                    return
                if detect and R_MULTIPICK not in job.reasons:
                    job.reasons.append(R_MULTIPICK)
                if second is not None:  # went through stuck to the first sheet: never scanned
                    self.lost_sheets.append(second)
                    job.sheets_fed += 1
            sides = [("front", sheet)]
            if s.duplex:
                sides.append(("back", sheet.back or BLANK))
            for side_name, spec in sides:
                spec = spec.side()
                if s.drop_blank_pages and self._is_blank(spec, s):
                    job.blank_removed += 1
                    continue
                job.pending.append((spec, job.sheets_fed, side_name))

    def _maybe_complete(self, job: Job) -> None:
        if job.active and not job.pending and not self.feeder and not self.faults.hatch_open:
            job.finish("Completed", R_COMPLETED)

    def _ended_error(self, job: Job) -> EsclError:
        if job.state == "Aborted":
            return EsclError(self.config.abort_status, "job aborted: " + ",".join(job.reasons))
        return EsclError(404, f"no more documents (job {job.state})")

    async def next_document(self, job_id: str) -> PageResult:
        self.expire_jobs()
        job = self.get_job(job_id)
        if not job.active:
            raise self._ended_error(job)
        now = self.clock()
        job.last_activity = now
        f = self.faults
        needs_scan = not job.pending and bool(self.feeder)
        if f.slow_ms > 0 and needs_scan:
            if f.slow_style == "block":
                await asyncio.sleep(f.slow_ms / 1000)
            else:
                if job.ready_at is None:
                    job.ready_at = now + f.slow_ms / 1000
                remaining = job.ready_at - self.clock()
                if remaining > 0:
                    raise EsclError(503, "scanning", {"Retry-After": str(max(1, math.ceil(remaining)))})
        if not job.active:  # cancelled while we were sleeping
            raise self._ended_error(job)
        self._feed(job)
        if not job.pending:
            raise self._ended_error(job)
        spec, sheet_no, side = job.pending.popleft()
        encoded = imaging.encode_page(spec, job.params)
        job.ready_at = None
        job.last_activity = self.clock()

        drop = f.drop_connection_after_pages is not None and job.images_completed >= f.drop_connection_after_pages
        record = {"sheet": sheet_no, "side": side, "kind": spec.kind, "qr": spec.qr,
                  "width": encoded.width, "height": encoded.height, "color_mode": encoded.color_mode}
        if drop:
            if not f.sticky:
                f.drop_connection_after_pages = None
            mode = self.config.drop_mode
            if mode == "redeliver":
                job.pending.appendleft((spec, sheet_no, side))
            else:
                job.images_completed += 1  # the device considers it sent
                job.lost.append(record)
                if mode == "abort_job":
                    job.finish("Aborted", R_ABORTED, R_CONNECTION)
        else:
            job.images_completed += 1
            job.last_image = encoded
            job.delivered.append(record)
        self._maybe_complete(job)
        return PageResult(encoded=encoded, drop=drop)
