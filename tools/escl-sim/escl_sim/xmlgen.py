"""eSCL response documents (ScannerStatus, ScanImageInfo)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from lxml import etree

from .caps import NS_PWG, NS_SCAN

if TYPE_CHECKING:  # pragma: no cover
    from .state import Job

NSMAP = {"scan": NS_SCAN, "pwg": NS_PWG}


def _s(name: str) -> str:
    return f"{{{NS_SCAN}}}{name}"


def _p(name: str) -> str:
    return f"{{{NS_PWG}}}{name}"


def _sub(parent, tag: str, text: object | None = None):
    el = etree.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def _job_info(parent, job: Job, now: float) -> None:
    info = _sub(parent, _s("JobInfo"))
    _sub(info, _p("JobUri"), job.path)
    _sub(info, _p("JobUuid"), job.uuid)
    _sub(info, _s("Age"), int(now - job.created))
    _sub(info, _p("ImagesCompleted"), job.images_completed)
    _sub(info, _p("ImagesToTransfer"), job.images_to_transfer)
    _sub(info, _p("JobState"), job.state)
    reasons = _sub(info, _p("JobStateReasons"))
    for r in job.reasons:
        _sub(reasons, _p("JobStateReason"), r)


def scanner_status(version: str, state: str, adf_state: str, jobs: list[Job], now: float) -> bytes:
    root = etree.Element(_s("ScannerStatus"), nsmap=NSMAP)
    _sub(root, _p("Version"), version)
    _sub(root, _p("State"), state)
    _sub(root, _s("AdfState"), adf_state)
    jobs_el = _sub(root, _s("Jobs"))
    for job in jobs:
        _job_info(jobs_el, job, now)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def scan_image_info(job: Job) -> bytes:
    root = etree.Element(_s("ScanImageInfo"), nsmap=NSMAP)
    _sub(root, _p("JobUri"), job.path)
    _sub(root, _p("JobUuid"), job.uuid)
    info = job.last_image
    _sub(root, _s("ActualWidth"), info.width if info else 0)
    _sub(root, _s("ActualHeight"), info.height if info else 0)
    _sub(root, _s("ActualBytesPerLine"), info.bytes_per_line if info else 0)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
