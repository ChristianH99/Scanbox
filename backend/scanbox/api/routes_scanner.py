"""Scanner status, capabilities and connection test; profiles."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..context import AppContext
from ..db import get_session
from ..escl.client import EsclError
from ..models import Profile
from ..profiles import ProfileSettings, validate_against_caps
from ..security.auth import Principal, require_admin
from .deps import CtxDep, PrincipalDep
from .schemas import (
    CapabilitiesOut,
    ErrorResponse,
    InputCapsOut,
    ProfileIn,
    ProfileOut,
    RangeOut,
    ScannerStatusOut,
    TestConnectionIn,
    TestConnectionOut,
)

router = APIRouter(prefix="/api", tags=["scanner"])


@router.get("/scanner/status", response_model=ScannerStatusOut)
async def scanner_status(refresh: bool = False, ctx: AppContext = CtxDep, _p: Principal = PrincipalDep) -> ScannerStatusOut:
    assert ctx.scanner
    if refresh or ctx.scanner.status.checked_at is None:
        if not ctx.scanner.lock.locked():
            await ctx.scanner.poll_status()
    return ScannerStatusOut(**ctx.scanner.status.to_dict())


@router.get("/scanner/capabilities", response_model=CapabilitiesOut)
def scanner_capabilities(ctx: AppContext = CtxDep, _p: Principal = PrincipalDep) -> CapabilitiesOut:
    assert ctx.scanner
    caps = ctx.scanner.capabilities
    if caps is None:
        return CapabilitiesOut(available=False)

    def ic(c) -> InputCapsOut | None:  # noqa: ANN001
        if c is None:
            return None
        return InputCapsOut(color_modes=c.color_modes, content_types=c.content_types,
                            document_formats=c.document_formats, resolutions=c.resolutions, intents=c.intents,
                            max_width=c.max_width, max_height=c.max_height)

    modes = (caps.adf_simplex or caps.adf_duplex).color_modes if (caps.adf_simplex or caps.adf_duplex) else []
    app_modes = []
    if "RGB24" in modes:
        app_modes.append("color")
    if "Grayscale8" in modes:
        app_modes += ["gray", "bw"]
    elif "BlackAndWhite1" in modes:
        app_modes.append("bw")
    if "scan:AutoColorDetection" in modes:
        app_modes.append("auto")
    return CapabilitiesOut(
        available=True,
        fetched_at=ctx.settings.get("scanner.capabilities_fetched_at"),
        make_and_model=caps.make_and_model,
        serial_number=caps.serial_number,
        sources=[x for x in caps.sources if x != "platen"],
        adf_simplex=ic(caps.adf_simplex),
        adf_duplex=ic(caps.adf_duplex),
        feeder_capacity=caps.feeder_capacity,
        auto_crop=caps.auto_crop,
        blank_page_detection=caps.blank_page_detection,
        multipick_actions=caps.multipick_actions,
        carrier_sheet=caps.carrier_sheet,
        walkup_supported=caps.walkup_supported,
        ranges={k: RangeOut(**v.__dict__) for k, v in caps.ranges.items()},
        color_modes=app_modes,
    )


@router.post("/scanner/test", response_model=TestConnectionOut, tags=["admin"])
async def scanner_test(body: TestConnectionIn, ctx: AppContext = CtxDep,
                       _a: Principal = Depends(require_admin)) -> TestConnectionOut:
    assert ctx.scanner
    try:
        info = await ctx.scanner.test_connection(body.address)
    except ValueError as e:
        return TestConnectionOut(ok=False, error=str(e), error_code="invalid_address")
    except EsclError as e:
        return TestConnectionOut(ok=False, error=e.message, error_code=e.code)
    return TestConnectionOut(ok=True, **info)


@router.post("/scanner/refresh", response_model=CapabilitiesOut, tags=["admin"])
async def scanner_refresh(ctx: AppContext = CtxDep, a: Principal = Depends(require_admin)) -> CapabilitiesOut:
    assert ctx.scanner
    try:
        await ctx.scanner.refresh_capabilities()
    except EsclError as e:
        raise HTTPException(502, detail={"code": e.code, "message": e.message}) from e
    await ctx.scanner.poll_status()
    return scanner_capabilities(ctx, a)


# ------------------------------------------------------------------ profiles
profiles = APIRouter(prefix="/api/profiles", tags=["profiles"])


def _out(p: Profile, ctx: AppContext) -> ProfileOut:
    ps = ProfileSettings.model_validate(p.settings)
    return ProfileOut(id=p.id, name=p.name, description=p.description, builtin=p.builtin_key is not None,
                      shared=p.owner_id is None,
                      owner_id=p.owner_id, settings=ps,
                      problems=validate_against_caps(ps, ctx.scanner.capabilities if ctx.scanner else None))


def _check_destinations(ps: ProfileSettings, ctx: AppContext) -> None:
    """Reject destination configs that can never work (wrong fields, unknown output folder, bad
    sub-folder template) when the profile is saved – not only when a scan is delivered. Things that
    may become valid later (SMTP not yet configured, Paperless offline) are checked at delivery."""
    from pydantic import ValidationError

    from ..destinations.builtin import FolderConfig, check_subfolder
    from ..destinations.service import REGISTRY

    roots = ctx.settings.get("output.roots") or {}
    errors: list[str] = []
    for i, d in enumerate(ps.destinations, 1):
        plugin = REGISTRY.get(d.type)
        try:
            cfg = plugin.Config.model_validate(d.config or {}) if plugin is not None else None
        except ValidationError as e:
            errors.append(f"Ziel {i} ({d.type}): " + "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                                                               for x in e.errors()))
            continue
        if isinstance(cfg, FolderConfig):
            if cfg.root not in roots:
                errors.append(f"Ziel {i}: Ausgabeordner '{cfg.root}' unbekannt (verfügbar: {', '.join(roots) or '–'})")
            errors += [f"Ziel {i}: {m}" for m in check_subfolder(cfg.subfolder)]
    if errors:
        raise HTTPException(422, detail={"code": "invalid_destination", "message": " · ".join(errors)})


def _get_profile(s: Session, profile_id: int, p: Principal, *, write: bool) -> Profile:
    prof = s.get(Profile, profile_id)
    if prof is None or (prof.owner_id is not None and prof.owner_id != p.id):
        raise HTTPException(404, detail={"code": "not_found", "message": "Profil nicht gefunden"})
    if write and prof.owner_id is None and not p.is_admin:
        raise HTTPException(403, detail={"code": "forbidden", "message": "Eingebaute Profile nur durch Admins änderbar"})
    return prof


@profiles.get("", response_model=list[ProfileOut])
def list_profiles(s: Session = Depends(get_session), ctx: AppContext = CtxDep, p: Principal = PrincipalDep) -> list[ProfileOut]:
    rows = s.scalars(select(Profile).where(or_(Profile.owner_id.is_(None), Profile.owner_id == p.id))
                     .order_by(Profile.owner_id.is_not(None), Profile.sort_order, Profile.name)).all()
    return [_out(r, ctx) for r in rows]


@profiles.post("", response_model=ProfileOut, status_code=201)
def create_profile(body: ProfileIn, shared: bool = False, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
                   p: Principal = PrincipalDep) -> ProfileOut:
    if shared and not p.is_admin:
        raise HTTPException(403, detail={"code": "forbidden", "message": "Nur Admins dürfen geteilte Profile anlegen"})
    _check_destinations(body.settings, ctx)
    prof = Profile(owner_id=None if shared else p.id, name=body.name.strip(), description=body.description,
                   settings=body.settings.model_dump())
    s.add(prof)
    s.flush()
    return _out(prof, ctx)


@profiles.get("/{profile_id}", response_model=ProfileOut, responses={404: {"model": ErrorResponse}})
def get_profile(profile_id: int, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
                p: Principal = PrincipalDep) -> ProfileOut:
    return _out(_get_profile(s, profile_id, p, write=False), ctx)


@profiles.put("/{profile_id}", response_model=ProfileOut)
def update_profile(profile_id: int, body: ProfileIn, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
                   p: Principal = PrincipalDep) -> ProfileOut:
    prof = _get_profile(s, profile_id, p, write=True)
    _check_destinations(body.settings, ctx)
    prof.name = body.name.strip()
    prof.description = body.description
    prof.settings = body.settings.model_dump()
    return _out(prof, ctx)


@profiles.delete("/{profile_id}", status_code=204)
def delete_profile(profile_id: int, s: Session = Depends(get_session), p: Principal = PrincipalDep) -> None:
    prof = _get_profile(s, profile_id, p, write=True)
    if prof.builtin_key:
        raise HTTPException(409, detail={"code": "builtin", "message": "Eingebaute Profile können nicht gelöscht werden"})
    s.delete(prof)
