"""Document processing: raw pages + edits → artifacts → deliveries (docs/ARCHITECTURE.md §7).

Pure with respect to the inputs: re-running a processing run produces the same output and
never touches raw pages. Each run writes into ``processed/<doc>/<run>/``.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import img2pdf
import numpy as np
import structlog
from PIL import Image
from sqlalchemy import func, select

from ..db import run_db, session_scope, utcnow
from ..filenames import TemplateVars, build_filename, render_filename_template, sanitize_component
from ..jobs import states as st
from ..jobs.queue import enqueue
from ..models import ArtifactRow, Delivery, Document, Page, Task
from ..profiles import ProfileSettings
from ..storage import sha256_file
from . import imageops as ops
from .ocr import OcrError, ghostscript_compress, run_ocrmypdf, tool_available
from .qr import QrHit, select_hit
from .render import PageEdits, render_page
from .suggest import suggest_date, suggest_title

log = structlog.get_logger(__name__)

OPTIMIZE = {"none": 0, "low": 1, "medium": 2, "high": 3}
JPEG_Q = {"none": 95, "low": 90, "medium": 80, "high": 65}


class ProcessingBlocked(Exception):
    """Processing stopped because a human decision is needed (e.g. QR missing with on_missing=block)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class PageInput:
    id: str
    index: int
    raw_path: Path
    edits: dict[str, Any]
    analysis: dict[str, Any]
    keep_blank: bool
    dpi: int


@dataclass
class PageOutput:
    id: str
    image_path: Path
    is_blank: bool
    ink: float | None
    qr_hits: list[QrHit]
    analysis_updates: dict[str, Any]
    rotate: int
    angle: float
    dpi: int


@dataclass
class ProcessResult:
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    ocr_text: str = ""
    qr_value: str | None = None
    qr_page_id: str | None = None
    warnings: list[tuple[str, str]] = field(default_factory=list)
    kept_pages: int = 0


def process_page_sync(p: PageInput, profile: ProfileSettings, run_dir: Path, detect_qr: bool) -> PageOutput:
    """CPU-bound per-page work (runs in a thread)."""
    raw = ops.load(str(p.raw_path))
    edits = PageEdits.model_validate(p.edits or {})
    measure = profile.processing.blank_removal in ("software", "both")
    res = render_page(raw, edits, profile, p.analysis, detect_qr_codes=detect_qr, measure_blank=measure)
    for h in res.qr_hits:
        h.page_id = p.id
        h.page_index = p.index
    is_blank = bool(measure and res.ink is not None and res.ink < profile.processing.blank_threshold)
    out_img = res.image
    # B/W pages → 1-bit PNG (lossless, small); others → JPEG.
    if out_img.ndim == 2 and _is_binary(out_img):
        path = run_dir / "pages" / f"{p.index:04d}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(out_img).convert("1").save(path, dpi=(p.dpi, p.dpi), optimize=True)
    else:
        path = run_dir / "pages" / f"{p.index:04d}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        q = profile.output.jpeg_quality if profile.output.format in ("jpeg",) else JPEG_Q[profile.output.compression]
        if out_img.ndim == 3:
            Image.fromarray(out_img[:, :, ::-1]).save(path, quality=q, optimize=True, dpi=(p.dpi, p.dpi))
        else:
            Image.fromarray(out_img).save(path, quality=q, optimize=True, dpi=(p.dpi, p.dpi))
    return PageOutput(
        id=p.id,
        image_path=path,
        is_blank=is_blank,
        ink=res.ink,
        qr_hits=res.qr_hits,
        analysis_updates=res.analysis_updates,
        rotate=res.rotate,
        angle=res.angle,
        dpi=p.dpi,
    )


def _is_binary(img: np.ndarray) -> bool:
    vals = np.unique(img[:: max(1, img.shape[0] // 200), :: max(1, img.shape[1] // 200)])
    return len(vals) <= 2 and set(vals.tolist()) <= {0, 255}


def decide_qr(profile: ProfileSettings, outputs: list[PageOutput]) -> QrHit | None:
    if not profile.qr.enabled:
        return None
    if profile.qr.pages == "first":
        candidates = [outputs[0]] if outputs else []
    else:
        candidates = outputs
    for o in candidates:
        hit = select_hit(o.qr_hits, profile.qr)
        if hit is not None:
            return hit
    return None


class Processor:
    def __init__(self, ctx) -> None:  # noqa: ANN001
        self.ctx = ctx

    async def run(self, task: Task) -> None:
        doc_id = int(task.document_id or 0)
        run = int(task.payload.get("run", 0))
        structlog.contextvars.bind_contextvars(document_id=doc_id, run=run)
        try:
            await self._run(doc_id, run)
        finally:
            structlog.contextvars.unbind_contextvars("document_id", "run")

    def _prepare(self, doc_id: int, run: int):  # noqa: ANN202
        """Load inputs (sync, runs in a thread). None → nothing to do."""
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            if doc is None or doc.status != st.PROCESSING or doc.processing_run != run:
                log.info("process.skip", status=doc.status if doc else None)
                return None
            if doc.raw_expired:
                st.transition(s, doc, st.FAILED, code="process.raw_expired", level="error",
                              message="Originalseiten sind abgelaufen", error_code="raw_expired")
                return None
            profile = ProfileSettings.model_validate(doc.profile_snapshot)
            pages = s.scalars(
                select(Page).where(Page.document_id == doc_id, Page.deleted.is_(False)).order_by(Page.position)
            ).all()
            inputs = [
                PageInput(p.id, i, self.ctx.storage.abs(p.raw_path), dict(p.edits or {}), dict(p.analysis or {}),
                          p.keep_blank, p.dpi or profile.resolution)
                for i, p in enumerate(pages)
            ]
            if not inputs:
                st.transition(s, doc, st.NEEDS_ATTENTION, code="process.no_pages", level="error",
                              message="Dokument hat keine Seiten", error_code="no_pages")
                return None
            manual_qr = doc.qr_value if doc.qr_source in ("manual", "none") else None
            st.add_event(s, doc, "process.started", f"Lauf {run}, {len(inputs)} Seite(n)")
            return profile, inputs, manual_qr, doc.qr_source

    @staticmethod
    def _attention(doc_id: int, code: str, message: str, level: str, error_code: str) -> None:
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            assert doc
            st.transition(s, doc, st.NEEDS_ATTENTION, code=code, message=message, level=level, error_code=error_code)

    async def _run(self, doc_id: int, run: int) -> None:
        prepared = await run_db(self._prepare, doc_id, run)
        if prepared is None:
            return
        profile, inputs, manual_qr, qr_source = prepared
        run_dir = self.ctx.storage.processed_run_dir(doc_id, run)
        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = await self._process(doc_id, run, profile, inputs, run_dir, manual_qr, qr_source)
        except ProcessingBlocked as e:
            await run_db(self._attention, doc_id, f"process.{e.code}", e.message, "warning", e.code)
            return
        except (OcrError, OSError, ValueError) as e:
            log.exception("process.failed")
            await run_db(self._attention, doc_id, "process.error", f"Verarbeitung fehlgeschlagen: {e}"[:1000],
                         "error", "processing_failed")
            return
        await run_db(self._commit, doc_id, run, profile, result, run_dir)

    async def _process(
        self,
        doc_id: int,
        run: int,
        profile: ProfileSettings,
        inputs: list[PageInput],
        run_dir: Path,
        manual_qr: str | None,
        qr_source: str | None,
    ) -> ProcessResult:
        res = ProcessResult()
        need_qr = profile.qr.enabled and qr_source not in ("manual", "none")
        outputs: list[PageOutput] = []
        owner = await run_db(self._owner, doc_id)
        for p in inputs:
            # Splitting needs QR values of every page; otherwise respect qr.pages.
            detect = profile.processing.split.mode != "off" or (
                need_qr and (profile.qr.pages != "first" or p.index == 0)
            )
            o = await asyncio.to_thread(process_page_sync, p, profile, run_dir, detect)
            outputs.append(o)
            await run_db(self._save_page_analysis, o)
            self.ctx.bus.publish(
                "document.progress",
                {"id": doc_id, "step": "pages", "done": p.index + 1, "total": len(inputs)},
                audience=owner,
            )

        # QR value
        if qr_source == "manual":
            res.qr_value = manual_qr
        elif qr_source == "none":
            res.qr_value = None
        elif profile.qr.enabled:
            hit = decide_qr(profile, outputs)
            if hit is not None:
                res.qr_value, res.qr_page_id = hit.value, hit.page_id
            elif profile.qr.on_missing == "block":
                raise ProcessingBlocked("qr_missing", "Kein QR-Code gefunden – bitte Wert eingeben oder bestätigen")
            elif profile.qr.on_missing == "warn":
                res.warnings.append(("qr.missing", "Kein QR-Code im Suchbereich gefunden"))

        # Split at separator sheets (creates child documents for the following segments).
        if profile.processing.split.mode != "off":
            outputs = await self._split(doc_id, profile, outputs)

        # Blank pages (the QR page is never dropped)
        keep_blank = {p.id for p in inputs if p.keep_blank}
        kept = [o for o in outputs if not o.is_blank or o.id in keep_blank or o.id == res.qr_page_id]
        dropped = len(outputs) - len(kept)
        if not kept:
            raise ProcessingBlocked("all_blank", "Alle Seiten wurden als leer erkannt – bitte prüfen")
        if dropped:
            res.warnings.append(("process.blank_removed", f"{dropped} leere Seite(n) entfernt"))
        res.kept_pages = len(kept)

        await self._assemble(doc_id, run, profile, kept, run_dir, res)
        return res

    async def _split(self, doc_id: int, profile: ProfileSettings, outputs: list[PageOutput]) -> list[PageOutput]:
        opts = profile.processing.split
        rx = re.compile(opts.pattern) if opts.mode == "qr_pattern" and opts.pattern else None
        segments: list[list[PageOutput]] = [[]]
        current: str | None = None
        for o in outputs:
            values = [h.value for h in o.qr_hits]
            if opts.mode == "qr_pattern" and rx is not None:
                sep = next((v for v in values if rx.search(v)), None)
                if sep is not None:
                    if segments[-1]:
                        segments.append([])
                    if not opts.remove_separator:
                        segments[-1].append(o)
                    continue
            elif opts.mode == "qr_change":
                hit = select_hit(o.qr_hits, profile.qr) if profile.qr.enabled else (
                    QrHit(values[0], 0, 0, 0, 0) if values else None)
                if hit is not None:
                    if current is not None and hit.value != current and segments[-1]:
                        segments.append([])
                    current = hit.value
            segments[-1].append(o)
        segments = [seg for seg in segments if seg]
        if len(segments) <= 1:
            return segments[0] if segments else outputs
        from ..documents import split_into_children

        def _db() -> None:
            with session_scope() as s:
                children = split_into_children(s, doc_id, [[o.id for o in seg] for seg in segments[1:]])
                dropped = {o.id for o in outputs} - {o.id for seg in segments for o in seg}
                if dropped:
                    for pid in dropped:
                        p = s.get(Page, pid)
                        if p:
                            p.deleted = True
                doc = s.get(Document, doc_id)
                assert doc
                st.add_event(s, doc, "process.split", f"In {len(segments)} Dokumente aufgeteilt",
                             data={"children": children})

        await run_db(_db)
        return segments[0]

    async def _assemble(
        self, doc_id: int, run: int, profile: ProfileSettings, kept: list[PageOutput], run_dir: Path,
        res: ProcessResult,
    ) -> None:
        fmt = profile.output.format
        out_dir = run_dir / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        jobs = int(self.ctx.settings.get("jobs.ocr_jobs") or 2)
        langs = list(profile.ocr.languages)
        image_pdf = run_dir / "image.pdf"
        need_pdf = fmt in ("pdf", "pdfa", "pdf_image", "txt") or profile.ocr.enabled
        if need_pdf:
            await asyncio.to_thread(_images_to_pdf, [o.image_path for o in kept], [o.dpi for o in kept], image_pdf)

        ocr_wanted = profile.ocr.enabled
        have_ocr = tool_available("ocrmypdf") and tool_available("tesseract")
        if ocr_wanted and not have_ocr:
            res.warnings.append(("ocr.unavailable", "OCR nicht verfügbar (tesseract/ocrmypdf fehlt)"))
            ocr_wanted = False

        if fmt in ("pdf", "pdfa"):
            final = out_dir / "document.pdf"
            if have_ocr and (ocr_wanted or fmt == "pdfa"):
                r = await run_ocrmypdf(
                    image_pdf, final, languages=langs, pdfa=fmt == "pdfa", do_ocr=ocr_wanted,
                    optimize=OPTIMIZE[profile.output.compression], jobs=jobs,
                    jpeg_quality=JPEG_Q[profile.output.compression],
                    clean=profile.processing.unpaper_clean,
                )
                res.ocr_text = r.text
            else:
                shutil.copyfile(image_pdf, final)
            final = await self._fit_target(profile, final, out_dir, res)
            res.artifacts.append({"kind": "pdf", "path": final, "ext": "pdf", "ctype": "application/pdf"})
        elif fmt == "pdf_image":
            final = out_dir / "document.pdf"
            shutil.copyfile(image_pdf, final)
            final = await self._fit_target(profile, final, out_dir, res)
            res.artifacts.append({"kind": "pdf", "path": final, "ext": "pdf", "ctype": "application/pdf"})
            if ocr_wanted:
                r = await run_ocrmypdf(image_pdf, out_dir / "ocr-tmp.pdf", languages=langs, pdfa=False, do_ocr=True,
                                       optimize=0, jobs=jobs)
                res.ocr_text = r.text
        elif fmt == "txt":
            if not have_ocr:
                raise ProcessingBlocked("ocr_unavailable", "Textausgabe benötigt OCR (tesseract fehlt)")
            r = await run_ocrmypdf(image_pdf, out_dir / "ocr-tmp.pdf", languages=langs, pdfa=False, do_ocr=True,
                                   optimize=0, jobs=jobs)
            res.ocr_text = r.text
            txt = out_dir / "document.txt"
            txt.write_text(r.text, encoding="utf-8")
            res.artifacts.append({"kind": "txt", "path": txt, "ext": "txt", "ctype": "text/plain; charset=utf-8"})
        else:  # jpeg | png | tiff
            files = await asyncio.to_thread(_export_images, kept, fmt, out_dir, profile.output.jpeg_quality)
            ext = {"jpeg": "jpg", "png": "png", "tiff": "tif"}[fmt]
            ctype = {"jpeg": "image/jpeg", "png": "image/png", "tiff": "image/tiff"}[fmt]
            for i, f in enumerate(files):
                res.artifacts.append({"kind": "image", "path": f, "ext": ext, "ctype": ctype,
                                      "index": i + 1, "count": len(files)})
            if ocr_wanted and need_pdf:
                r = await run_ocrmypdf(image_pdf, out_dir / "ocr-tmp.pdf", languages=langs, pdfa=False, do_ocr=True,
                                       optimize=0, jobs=jobs)
                res.ocr_text = r.text

        # Keep the image-only PDF if a Paperless destination wants to skip our OCR.
        wants_image_pdf = any(
            d.type == "paperless" and d.enabled and d.config.get("skip_ocr") for d in profile.destinations
        )
        if wants_image_pdf and image_pdf.exists():
            ip = out_dir / "image-only.pdf"
            shutil.copyfile(image_pdf, ip)
            res.artifacts.append({"kind": "pdf_image", "path": ip, "ext": "pdf", "ctype": "application/pdf"})
        tmp = out_dir / "ocr-tmp.pdf"
        if tmp.exists():
            tmp.unlink()

    async def _fit_target(self, profile: ProfileSettings, pdf: Path, out_dir: Path, res: ProcessResult) -> Path:
        target = profile.output.target_size_mb
        if not target or pdf.stat().st_size <= target * 1024 * 1024:
            return pdf
        for level in ("ebook", "screen"):
            out = out_dir / f"document-{level}.pdf"
            try:
                await ghostscript_compress(pdf, out, level)
            except OcrError as e:
                res.warnings.append(("process.compress_failed", str(e)[:300]))
                return pdf
            if out.stat().st_size <= target * 1024 * 1024:
                res.warnings.append(("process.compressed", f"Auf {level} komprimiert für Zielgröße {target} MB"))
                return out
        res.warnings.append(("process.target_size_missed", f"Zielgröße {target} MB nicht erreicht"))
        return out

    # ------------------------------------------------------------------ DB helpers
    def _owner(self, doc_id: int) -> int | None:
        with session_scope() as s:
            return s.scalar(select(Document.owner_id).where(Document.id == doc_id))

    def _save_page_analysis(self, o: PageOutput) -> None:
        with session_scope() as s:
            p = s.get(Page, o.id)
            if p is None:
                return
            a = dict(p.analysis or {})
            a.update(o.analysis_updates)
            a["applied_rotate"] = o.rotate
            a["applied_angle"] = o.angle
            if o.ink is not None:
                a["ink"] = round(o.ink, 5)
            a["qr"] = [h.to_dict() for h in o.qr_hits]
            p.analysis = a
            p.is_blank = o.is_blank

    def _commit(self, doc_id: int, run: int, profile: ProfileSettings, res: ProcessResult, run_dir: Path) -> None:
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            if doc is None or doc.processing_run != run or doc.status != st.PROCESSING:
                log.info("process.stale_run")
                return
            doc.qr_value = res.qr_value
            if doc.qr_source not in ("manual", "none"):
                doc.qr_source = "auto" if res.qr_value else None
            doc.qr_page_id = res.qr_page_id
            if res.ocr_text:
                doc.ocr_text = res.ocr_text[:2_000_000]
                if profile.ocr.suggest_title:
                    doc.title_suggestion = suggest_title(res.ocr_text)
                if not doc.document_date:
                    doc.document_date = suggest_date(res.ocr_text)
            owner_name = doc.owner.display_name or doc.owner.username if doc.owner else "inbox"
            if not doc.title:
                tv = TemplateVars(created=doc.created_at, qr=doc.qr_value, profile=doc.profile_name,
                                  user=owner_name, pages=res.kept_pages)
                doc.title = (
                    doc.title_suggestion
                    if profile.ocr.suggest_title and doc.title_suggestion
                    else sanitize_component(render_filename_template(profile.title_template, tv), 200)
                ) or doc.profile_name or "Scan"
            if doc.counter is None and doc.owner_id is not None:
                doc.counter = self.ctx.settings.next_counter(doc.owner_id, "filename", s)
            tv = TemplateVars(
                created=doc.created_at, qr=doc.qr_value, title=doc.title, counter=doc.counter or 0,
                profile=doc.profile_name, user=owner_name, pages=res.kept_pages,
            )
            # Remove previous runs' artifacts (rows + files).
            for a in list(doc.artifacts):
                s.delete(a)
            primary_ext = res.artifacts[0]["ext"] if res.artifacts else "pdf"
            doc.filename = build_filename(profile.filename_template, tv, primary_ext, add_qr=doc.add_qr_to_filename)
            stem = doc.filename.rsplit(".", 1)[0]
            for a in res.artifacts:
                if a["kind"] == "image" and a.get("count", 1) > 1:
                    fname = f"{stem}_{a['index']:03d}.{a['ext']}"
                elif a["kind"] == "pdf_image":
                    fname = f"{stem}.image.pdf"
                else:
                    fname = f"{stem}.{a['ext']}"
                path: Path = a["path"]
                s.add(ArtifactRow(
                    document_id=doc.id, run=run, kind=a["kind"], path=self.ctx.storage.rel(path), filename=fname,
                    content_type=a["ctype"], size=path.stat().st_size, sha256=sha256_file(path),
                ))
            doc.page_count = res.kept_pages
            doc.processed_expired = False
            for code, msg in res.warnings:
                st.add_event(s, doc, code, msg, level="warning")
            st.add_event(s, doc, "process.done", f"{doc.filename}")
            s.flush()
            self._start_deliveries(s, doc, profile, run)
        # Drop older run directories.
        parent = run_dir.parent
        for d in parent.iterdir():
            if d.is_dir() and d.name != str(run):
                shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(run_dir / "pages", ignore_errors=True)
        image_pdf = run_dir / "image.pdf"
        if image_pdf.exists():
            image_pdf.unlink()

    def _start_deliveries(self, s, doc: Document, profile: ProfileSettings, run: int) -> None:  # noqa: ANN001
        dests = [d for d in profile.destinations if d.enabled]
        if not dests or doc.owner_id is None:
            st.transition(s, doc, st.DONE, code="done")
            schedule_webhook(s, doc)
            return
        st.transition(s, doc, st.DELIVERING, code="deliver.started")
        for i, d in enumerate(dests):
            dl = Delivery(document_id=doc.id, destination_type=d.type, label=d.label or d.type, config=d.config,
                          run=run)
            s.add(dl)
            s.flush()
            enqueue(s, "deliver", idempotency_key=f"deliver:{dl.id}:1", document_id=doc.id, delivery_id=dl.id,
                    max_attempts=6, payload={"index": i})


def schedule_webhook(s, doc: Document) -> None:  # noqa: ANN001
    if doc.webhook_url:
        enqueue(s, "webhook", idempotency_key=f"webhook:{doc.id}:{doc.status}:{doc.processing_run}",
                document_id=doc.id, max_attempts=5, payload={"status": doc.status})


def _images_to_pdf(paths: list[Path], dpis: list[int], out: Path) -> None:
    layouts = []
    for dpi in dpis:
        layouts.append(img2pdf.get_fixed_dpi_layout_fun((dpi, dpi)) if dpi else None)
    # img2pdf takes one layout for all pages; pages share the profile dpi in practice.
    layout = layouts[0] if layouts and layouts[0] else None
    with open(out, "wb") as f:
        f.write(img2pdf.convert([str(p) for p in paths], layout_fun=layout) if layout else
                img2pdf.convert([str(p) for p in paths]))


def _export_images(kept: list[PageOutput], fmt: str, out_dir: Path, quality: int) -> list[Path]:
    files: list[Path] = []
    if fmt == "tiff":
        imgs = [Image.open(o.image_path) for o in kept]
        path = out_dir / "document.tif"
        compression = "group4" if all(i.mode == "1" for i in imgs) else "tiff_lzw"
        imgs[0].save(path, save_all=True, append_images=imgs[1:], compression=compression,
                     dpi=(kept[0].dpi, kept[0].dpi))
        for i in imgs:
            i.close()
        return [path]
    for n, o in enumerate(kept, start=1):
        with Image.open(o.image_path) as im:
            if fmt == "jpeg":
                path = out_dir / f"page-{n:03d}.jpg"
                (im.convert("L") if im.mode == "1" else im).save(path, quality=quality, optimize=True,
                                                                 dpi=(o.dpi, o.dpi))
            else:
                path = out_dir / f"page-{n:03d}.png"
                im.save(path, optimize=True, dpi=(o.dpi, o.dpi))
        files.append(path)
    return files


def zip_artifacts(paths: list[tuple[Path, str]], out: Path) -> Path:
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as z:
        for p, name in paths:
            z.write(p, arcname=name)
    return out


def remaining_deliveries(s, doc_id: int, run: int) -> dict[str, int]:  # noqa: ANN001
    rows = s.execute(
        select(Delivery.status, func.count(Delivery.id)).where(Delivery.document_id == doc_id, Delivery.run == run)
        .group_by(Delivery.status)
    ).all()
    return {status: n for status, n in rows}


__all__ = ["Processor", "decide_qr", "process_page_sync", "schedule_webhook", "utcnow"]
