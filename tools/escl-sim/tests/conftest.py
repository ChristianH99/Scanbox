from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from escl_sim import SimState, create_app


def scan_settings(
    *,
    duplex: bool = False,
    color: str = "RGB24",
    dpi: int = 150,
    fmt: str = "image/jpeg",
    source: str = "Feeder",
    region: tuple[int, int, int, int] | None = (2480, 3508, 0, 0),
    extra: str = "",
) -> str:
    """A ScanSettings body like sane-airscan / Scanbox would send."""
    region_xml = ""
    if region is not None:
        w, h, x, y = region
        region_xml = f"""
  <pwg:ScanRegions pwg:MustHonor="true">
    <pwg:ScanRegion>
      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
      <pwg:XOffset>{x}</pwg:XOffset>
      <pwg:YOffset>{y}</pwg:YOffset>
      <pwg:Width>{w}</pwg:Width>
      <pwg:Height>{h}</pwg:Height>
    </pwg:ScanRegion>
  </pwg:ScanRegions>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                   xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <scan:Intent>Document</scan:Intent>{region_xml}
  <pwg:InputSource>{source}</pwg:InputSource>
  <scan:Duplex>{"true" if duplex else "false"}</scan:Duplex>
  <scan:ColorMode>{color}</scan:ColorMode>
  <scan:XResolution>{dpi}</scan:XResolution>
  <scan:YResolution>{dpi}</scan:YResolution>
  <pwg:DocumentFormat>{fmt}</pwg:DocumentFormat>
  <scan:DocumentFormatExt>{fmt}</scan:DocumentFormatExt>{extra}
</scan:ScanSettings>"""


@pytest.fixture
def state() -> SimState:
    return SimState()


@pytest.fixture
async def client(state: SimState):
    app = create_app(state)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sim") as c:
        yield c


async def create_job(client: httpx.AsyncClient, **kw) -> str:
    r = await client.post("/eSCL/ScanJobs", content=scan_settings(**kw), headers={"Content-Type": "text/xml"})
    assert r.status_code == 201, (r.status_code, r.text)
    return r.headers["Location"]


async def fetch_all(client: httpx.AsyncClient, job_uri: str) -> tuple[list[bytes], httpx.Response]:
    """Pull pages until a non-200 answer; return pages and the final response."""
    pages = []
    for _ in range(200):
        r = await client.get(f"{job_uri}/NextDocument")
        if r.status_code != 200:
            return pages, r
        pages.append(r.content)
    raise AssertionError("job never ended")


def image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img
