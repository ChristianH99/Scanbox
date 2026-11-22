# eSCL scanner simulator (`tools/escl-sim`)

`escl-sim` pretends to be the HP ScanJet Pro N4000 snw1 over eSCL. It serves the real
device's `ScannerCapabilities` byte-for-byte, generates deterministic sample pages
(text, QR codes, blanks, photos, skewed or rotated sheets) at the requested resolution,
and lets tests inject faults: jam, multipick, hatch open, slow scanning, latency,
dropped connections, offline, busy, and failing job creation.

The backend's eSCL client and integration tests are developed against it, so it aims
to behave like the device. Where real HP behaviour is unknown, the simulator makes a
documented guess (see [Guesses to verify on hardware](#guesses-to-verify-on-hardware)),
and most of those guesses can be switched in `SimConfig`.

## Running it

### In-process (backend tests)

```python
import httpx
from escl_sim import SimState, create_app

state = SimState()                      # "letter_de" stack (3 text pages) in the feeder
app = create_app(state)                 # a FastAPI app
async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://sim") as client:
    ...
state.load("invoice_qr")                # change the stack
state.faults.jam_after_pages = 2        # inject a fault (or state.set_faults(...))
```

* `SimState(stack="letter_de" | None | [PageSpec|dict, ...], **config)`. The `config` keys are listed under [SimConfig](#simconfig).
* `create_app(state, **config)` also accepts config overrides.
* Every delay uses `asyncio.sleep` and nothing runs on threads, so slow mode, latency and
  concurrent status polls all work through `ASGITransport`.
* Install for tests: `pip install -e tools/escl-sim[test]`. Runtime deps are fastapi, uvicorn, pillow, numpy,
  qrcode[pil] and lxml. httpx is only needed in-process, for the transport-error exceptions.

### Standalone

```bash
cd tools/escl-sim
python -m venv .venv && .venv/Scripts/pip install -e .[test]      # Linux: .venv/bin/pip
python -m escl_sim --host 0.0.0.0 --port 8090 [--stack invoice_qr|empty]
       [--background '#c8c8c6'] [--bw-jpeg gray_thresholded|reject]
       [--location absolute|relative] [--offline-mode hang|503] [--samples-dir DIR]
```

### Docker

```bash
docker build -t escl-sim tools/escl-sim
docker run --rm -p 8090:8090 -v ./my-samples:/samples:ro escl-sim --stack invoice_qr
```

A docker-compose service looks like this:

```yaml
  escl-sim:
    build: ./tools/escl-sim
    command: ["--stack", "letter_de", "--offline-mode", "503"]
    ports: ["8090:8090"]
```

Point Scanbox at `http://escl-sim:8090`. Interactive API docs are at `/_sim/docs`.

### Tests

```bash
cd tools/escl-sim && .venv/Scripts/python -m pytest -q      # ~4 s
```

The OCR test runs only when `tesseract` is on PATH.

## eSCL endpoints

| Request | Answer |
|---|---|
| `GET /eSCL/ScannerCapabilities` | `200 text/xml`, the fixture verbatim (`escl_sim/data/scanner-capabilities.xml` = `docs/fixtures/scanner-capabilities.xml`) |
| `GET /eSCL/ScannerStatus` | `200 text/xml` `scan:ScannerStatus` (see below) |
| `POST /eSCL/ScanJobs` | `201` + `Location: http://<Host header>/eSCL/ScanJobs/<uuid>` (absolute by default, path-only with `location_style="relative"`). Errors: see below. |
| `GET /eSCL/ScanJobs/<uuid>/NextDocument` | `200 image/jpeg` (or `application/pdf`), one image per call. Feed order is front, back, front, back. `404` when the job has no more pages. `503` + `Retry-After` while the next page is still scanning (slow mode). Unknown uuid: `404`. |
| `GET /eSCL/ScanJobs/<uuid>/ScanImageInfo` | `200 text/xml` with `pwg:JobUri`, `pwg:JobUuid`, `scan:ActualWidth`, `scan:ActualHeight` and `scan:ActualBytesPerLine` of the last delivered image |
| `DELETE /eSCL/ScanJobs/<uuid>` | `200`. An active job becomes `Canceled` / `JobCanceledByUser`, and sheets not yet fed stay in the tray. A finished job also answers `200` (unchanged), so cleanup is idempotent. Unknown uuid: `404`. |

Status codes for `POST /eSCL/ScanJobs`, in the order they are checked:

| Condition | Status |
|---|---|
| `fail_create_status` fault | that status |
| malformed XML, wrong root element, non-integer numbers, empty body | `400` |
| another job is active, or the `busy` fault | `503` |
| unsupported value: `InputSource` other than `Feeder` (this device has **no platen**), resolution not in the discrete list or X≠Y, colour mode, format, intent, content type, region below Min or with negative offsets, more than one region, Brightness/Contrast/Threshold/CompressionFactor/BlankPageSensitivity out of range, unknown `MultipickDetectionAction`, `BlackAndWhite1`+JPEG with `bw_jpeg_behaviour="reject"` | `409` |
| hatch open, or paper stuck after a jam or multipick | `409` |
| feeder empty | `409` |

### ScannerStatus

The document uses the fixture's namespaces (`scan` = `http://schemas.hp.com/imaging/escl/2011/05/03`,
`pwg` = `http://www.pwg.org/schemas/2010/12/sm`):

```xml
<scan:ScannerStatus xmlns:scan=… xmlns:pwg=…>
  <pwg:Version>2.9</pwg:Version>
  <pwg:State>Idle|Processing|Stopped</pwg:State>
  <scan:AdfState>…</scan:AdfState>
  <scan:Jobs>                       <!-- newest first, the last 10 finished jobs are kept -->
    <scan:JobInfo>
      <pwg:JobUri>/eSCL/ScanJobs/<uuid></pwg:JobUri>
      <pwg:JobUuid><uuid></pwg:JobUuid>
      <scan:Age>seconds</scan:Age>
      <pwg:ImagesCompleted>n</pwg:ImagesCompleted>
      <pwg:ImagesToTransfer>n</pwg:ImagesToTransfer>
      <pwg:JobState>Processing|Completed|Aborted|Canceled</pwg:JobState>
      <pwg:JobStateReasons><pwg:JobStateReason>…</pwg:JobStateReason></pwg:JobStateReasons>
    </scan:JobInfo>
  </scan:Jobs>
</scan:ScannerStatus>
```

* `pwg:State`:
  * `Processing` while a job is active or the `busy` fault is set.
  * `Stopped` while the hatch is open or paper is stuck.
  * `Idle` otherwise.
* `scan:AdfState`, highest priority first:
  1. `ScannerAdfHatchOpen`
  2. `ScannerAdfJam`
  3. `ScannerAdfMultipickDetected`
  4. `ScannerAdfProcessing` (job active and sheets left)
  5. `ScannerAdfLoaded`
  6. `ScannerAdfEmpty`
* Job state reasons:

  | Reason(s) | When |
  |---|---|
  | `JobScanning` | while the job is processing |
  | `JobCompletedSuccessfully` | job finished normally |
  | `JobCanceledByUser` | job deleted with `DELETE` |
  | `AbortedBySystem` + `MediaJam` | paper jam |
  | `AbortedBySystem` + `MultipickDetected` | multipick with `StopJob` |
  | `AbortedBySystem` + `DoorOpen` | hatch opened mid-job |
  | `AbortedBySystem` + `JobTimedOut` | no `NextDocument` for `job_timeout_s` (120 s) |
  | `AbortedBySystem` + `ConnectionLost` | `drop_mode="abort_job"` |

  `MultipickDetected` is also added to a job that kept going with `ContinueJob`. It stays
  in the list after the job finishes (e.g. `JobCompletedSuccessfully`, `MultipickDetected`).
* A job is `Processing` from creation. It becomes `Completed` as soon as the last image has
  been handed out and the feeder is empty, so the following `NextDocument` gets `404`.

### Scan settings handled

Elements are matched by local name, so `pwg:` and `scan:` prefixes both work. Unknown
elements are ignored.

| Element | Effect |
|---|---|
| `InputSource` | `Feeder` is the only accepted value |
| `Duplex` | `true` delivers front and back |
| `ColorMode` | see the table below |
| `XResolution`, `YResolution` | must be one of the discrete resolutions in the capabilities |
| `DocumentFormatExt`, `DocumentFormat` | `DocumentFormatExt` wins over `DocumentFormat`. The default is `image/jpeg`. |
| `ScanRegions/ScanRegion` | `Width`/`Height`/`XOffset`/`YOffset` in `escl:ThreeHundredthsOfInches` |
| `Intent`, `ContentType` | validated only |
| `BlankPageDetection`, `BlankPageDetectionAndRemoval`, `BlankPageSensitivity` | blank pages are dropped on the scanner side |
| `MultipickDetection`, `MultipickDetectionAction` | `ContinueJob` / `StopJob`, see Multipick under the fault table |
| `AutoCrop` | crop the image to the paper's bounding box |
| `Brightness`, `Contrast` | linear factor `value / 996` (996 is the device's Normal) |
| `Threshold` | B/W threshold, default 128 |
| `CompressionFactor` | JPEG quality = 95 − 5 × factor (Normal 4 → 75) |

Colour modes and output:

| ColorMode | Output |
|---|---|
| `RGB24` | RGB JPEG |
| `Grayscale8` | 8-bit L JPEG |
| `BlackAndWhite1` + JPEG | `bw_jpeg_behaviour="gray_thresholded"` (default): an L JPEG whose pixels are thresholded to 0/255 before compression (so values are only close to 0/255). `"reject"`: `409` at job creation. |
| `BlackAndWhite1` + PDF | 1-bit PDF |
| `scan:AutoColorDetection` | RGB for pages with colour content (the `photo` kind, colourful files), L otherwise |

Every JPEG carries JFIF density = dpi. A PDF is one page per `NextDocument`.

### Image geometry

* Pixel size = region × dpi / 300, rounded.
* With no `ScanRegion` the region is A4 (2480×3508 in 300ths). Width and height are clamped to
  `MaxWidth` 2550 and `MaxHeight` 36600. Width or height below `Min` → `409`.
* The ADF bed is `MaxWidth` wide.
  * The sheet is centred horizontally (`Justification XImagePosition=Center`).
  * The sheet's leading edge is 2 mm below the top of the scan.
  * The requested region is also centred, then shifted by `XOffset`/`YOffset`.
  * Result: an A4 region on A4 paper shows a 2 mm background strip at the top and loses 2 mm at the bottom.
  * A letter-width region on A4 paper shows background left and right.
  * Skew rotates the sheet around its centre, so background corners appear.
* The scanner background is `SimConfig.background`, default `(200,200,198)`. The paper is `(250,250,247)`.
  Together they make auto-crop and deskew testable.

## Control API (`/_sim`, never delayed or failed by faults)

| Request | Body / effect |
|---|---|
| `GET /_sim/state` | JSON snapshot: `scanner_state`, `adf_state`, `condition`, `feeder` (specs), `feeder_count`, `paper_path`, `lost_sheets`, `faults`, `config`, `jobs` (incl. `delivered` / `lost` page records), last 100 eSCL `requests` `[method, path, status]` (status `-1` = failed offline) |
| `POST /_sim/reset` | Clears jobs, faults, stuck paper, lost sheets and the request log, then reloads the initial stack. `config` is kept. |
| `POST /_sim/load` | `{"stack": "<preset>"}`, `{"pages": [PageSpec, …]}` or `{"empty": true}`. `"append": true` adds to the tray instead of replacing it. `422` on invalid specs. |
| `GET /_sim/faults` · `POST /_sim/faults` · `DELETE /_sim/faults` | Read, merge (partial update) or clear faults. Unknown keys → `422`. |
| `POST /_sim/clear_jam` | `{"reinsert": true}` (default). The operator removes the stuck sheet(s) and, if `reinsert` is true, puts them back on top of the stack. |
| `GET /_sim/config` · `POST /_sim/config` | Read or merge [SimConfig](#simconfig). |
| `GET /_sim/presets` | Every preset with its specs. |

Python API on `SimState`, for tests:

* `load(stack, append=False)`, `load_empty()`
* `set_faults(**kw)` or `faults.<name> = …`, `clear_faults()`
* `clear_jam(reinsert=True)`, `reset()`, `snapshot()`
* `config.<name> = …`, `clock` (replace with a fake to test `Age` and timeouts)
* Inspection: `jobs[uuid].delivered` / `.lost` / `.blank_removed`, `feeder`, `paper_path`, `lost_sheets`, `request_log`

## Faults

Event faults fire once and then reset themselves, unless `"sticky": true`. Conditions and
modes stay as they are until you change them.

| Fault | Kind | Behaviour |
|---|---|---|
| `jam_after_pages: n` | event | Counts sheets fed in the current job. When sheet n+1 would be fed, it jams instead. The job becomes `Aborted` (`AbortedBySystem`, `MediaJam`). AdfState is `ScannerAdfJam` and `State` is `Stopped`. The jammed sheet moves to `paper_path`. `NextDocument` → **`abort_status` (default `404`)**. New jobs → `409` until `clear_jam()`. Images delivered before the jam stay delivered. |
| `multipick_after_pages: n` | event | Sheet n+1 is pulled together with the next sheet. What happens depends on the job's settings (table below). |
| `hatch_open: bool` | condition | AdfState `ScannerAdfHatchOpen`, `State` `Stopped`, create → `409`. If set during a job, the next feed aborts the job (`AbortedBySystem`, `DoorOpen`) and `NextDocument` → `abort_status`. |
| `slow_ms: int`, `slow_style` | mode | Each sheet takes `slow_ms` to scan. `slow_style="retry_after"` (default): `NextDocument` answers `503` with `Retry-After: ceil(remaining s)` (at least `1`) until the page is ready. `"block"`: the request itself waits `slow_ms`, like a long poll. Duplex backs are ready together with their front. |
| `latency_ms: int` | mode | Every `/eSCL/*` request is delayed by this much. |
| `drop_connection_after_pages: n` | event | Once n images have been delivered, the next `NextDocument` response announces the full `Content-Length`, sends half the body and kills the connection. In-process the client gets `httpx.RemoteProtocolError` (a `TransportError`). With uvicorn the connection closes mid-body, and httpx raises `RemoteProtocolError` too. The job then depends on `drop_mode` (see below). |
| `offline: bool` | event | `/eSCL/*` requests fail. One-shot = only the next request. In-process (`offline_mode="raise"`, default) it raises `httpx.ConnectError`. Standalone defaults to `hang`, which holds the request for `hang_s` (300 s) so the client times out. `503` mode returns `503`. |
| `busy: bool` | event | Another user is at the device. `State` is `Processing`, and the next create → `503`. |
| `fail_create_status: int` | event | The next `POST /eSCL/ScanJobs` returns that status. |
| `sticky: bool` | – | Event faults keep firing. A sticky jam hits every job after n sheets, a sticky `offline` fails every request, and so on. |

Multipick outcomes:

| Job settings | Result |
|---|---|
| detection on (the default when the request doesn't say) and `StopJob` (the default action) | Nothing from this sheet is delivered. The job becomes `Aborted` (`AbortedBySystem`, `MultipickDetected`) and AdfState `ScannerAdfMultipickDetected`. Both sheets go to `paper_path`, and `NextDocument` → `abort_status`. |
| `ContinueJob` | The first sheet's image is delivered and the job continues with `MultipickDetected` in its reasons. The second sheet went through unscanned: it is in `lost_sheets` and its page is missing from the output. |
| detection off | Same as `ContinueJob` but silent: no reason is added and the page is lost. |

`drop_mode` controls what happens to the job after a dropped connection:

| `drop_mode` | Result |
|---|---|
| `lose_page` (default) | The device counts the image as sent and moves on. The next `NextDocument` returns the *following* page, so a client that simply retries **loses a page**. The lost page is listed in `jobs[...].lost`. |
| `redeliver` | The same page is served again. |
| `abort_job` | The job becomes `Aborted` (`ConnectionLost`). |

### What the eSCL client must handle

| Situation | HTTP | Then |
|---|---|---|
| empty feeder, hatch open, paper stuck, invalid settings | `409` on create | read `ScannerStatus` → `AdfState` gives the reason |
| busy (own active job, or another user) | `503` on create | retry with backoff |
| slow scanning | `503` + `Retry-After` on NextDocument | retry |
| end of job | `404` on NextDocument | `ScannerStatus` → `Completed` / `JobCompletedSuccessfully` |
| jam, multipick with StopJob, hatch opened mid-job, job timeout | `404` (configurable) on NextDocument | `ScannerStatus` → job `Aborted` + reason, and AdfState `ScannerAdfJam` / `ScannerAdfMultipickDetected` / `ScannerAdfHatchOpen` |
| multipick with ContinueJob | `200` | the reason `MultipickDetected` appears on the job, and one page is missing |
| dropped connection | `httpx.RemoteProtocolError` | check `ImagesCompleted` in the status: the page may be lost |
| offline | `httpx.ConnectError` (in-process), or timeout / `503` (standalone) | |

## Sample pages

### PageSpec

A PageSpec is one side of a sheet. For duplex scans the sheet's `back` is used, or a blank back if it has none.

| Field | Default | Meaning |
|---|---|---|
| `kind` | `"text"` | `text`: a business letter (sender line, address, date line, bold heading, 3–5 paragraphs, closing). `qr`: text plus a QR code. `multi_qr`: text plus `qr` plus `extra_qrs`. `blank`. `near_blank`: 5–9 small specks and a faint line. `photo`: a colourful gradient with shapes and a caption. `separator`: a big centred QR (≥ 50 mm), a "TRENNBLATT"/"SEPARATOR SHEET" title and the value. `file`: an image from `path`. |
| `qr` | `null` | QR value |
| `qr_position` | `"top_right"` | `top_right`, `top_left`, `top_center`, `bottom_right`, `bottom_left`, `bottom_center` or `center`. The QR sits 12 mm inside the page edges. `top_right` lies inside the architecture's `top_right` zone (x > 0.6, y < 0.3). |
| `qr_size_mm` | `25` | Target size including the quiet zone. Modules are scaled by whole pixels, so the real size is 22–25 mm. |
| `extra_qrs` | `[]` | `[{"value": "...", "position": "bottom_left", "size_mm": 25}]` |
| `rotation` | `0` | 0, 90, 180 or 270: **clockwise** rotation of the content as it appears in the scan. 90 and 270 lay the content out landscape and turn it sideways on the portrait sheet. |
| `skew_deg` | `0` | Clockwise skew of the sheet (crooked feeding), within ±45° |
| `paper` | `"a4"` | `a4`, `letter` or `a5` |
| `text` | `null` | Replaces the generated paragraphs; paragraphs are separated by blank lines |
| `lang` | `"de"` | `de` or `en` |
| `seed` | `0` | Picks the letter content deterministically |
| `path` | – | For `kind=file`. A relative path is resolved against `samples_dir` (`tools/escl-sim/samples`, `/samples` in Docker, or env `ESCL_SIM_SAMPLES_DIR`). The image is resized to the paper and rotated 90° if its orientation doesn't match. |
| `back` | `null` | A PageSpec for the back side |

Text uses the bundled DejaVu Sans fonts (`escl_sim/data/fonts`, licence in `LICENSE_DEJAVU`) at 11 pt
body and 15 pt heading, drawn at the scan resolution so it is OCR-able (the Tesseract test is
skipped when `tesseract` is missing; it was not run on the development host).
Rendering is deterministic and cached by (spec, dpi, output parameters). A 300 dpi A4 page
takes about 60 ms and a 600 dpi page about 200 ms.

### Presets

Each preset is a list of sheets.

| Name | Content |
|---|---|
| `letter_de` (default) | 3 German letters |
| `letter_en` | 3 English letters |
| `invoice_qr` | QR `INV-2024-0042` top-right on page 1, then 2 text pages |
| `multi_qr` | page 1: `ZONE-123` top-right and `OTHER-999` bottom-left; page 2: text |
| `with_blanks` | text, blank, text, near_blank |
| `skewed` | text +3°, text −4.5°, text +5° **upside down**, QR `SKEW-1` −6° |
| `sideways` | text rotated 90° and 270° |
| `duplex_mixed` | (text / text), (text / blank), (QR `DUPLEX-7` / near_blank), (text / no back → blank) |
| `separator_batch` | QR `DOC-A`, text, QR `DOC-B`, text, text (the QR value changes at the document boundary) |
| `separator_sheets` | separator `DOC-A`, text, separator `DOC-B`, text |
| `photo` | colour photo page, then a text page (for AutoColorDetection) |
| `long_stack` | 30 text pages (mixed de/en) |
| `mixed_paper` | A4, Letter and A5 text pages |

Blank detection: a side is blank when its share of ink pixels (< 160 grey, measured at 75 dpi)
is below `blank_threshold` (0.2 %) × `BlankPageSensitivity`/128. Text pages are at 2–3 %,
`near_blank` at about 0.006 %.

## SimConfig

| Key | Default | Meaning |
|---|---|---|
| `background` | `(200,200,198)` | scanner background RGB |
| `bw_jpeg_behaviour` | `gray_thresholded` | or `reject` |
| `location_style` | `absolute` | or `relative` (Location = path only) |
| `abort_status` | `404` | `NextDocument` status after an abort (jam, multipick, hatch, timeout), e.g. set to `409`, `500` or `503` to test other device behaviour |
| `drop_mode` | `lose_page` | or `redeliver`, `abort_job` |
| `job_timeout_s` | `120` | idle job abort (capabilities: `StoredJobRequestSupport/TimeoutInSeconds` = 120) |
| `multipick_detection_default` | `true` | detection when the request doesn't say |
| `multipick_action_default` | `StopJob` | action when the request doesn't say |
| `blank_threshold` | `0.002` | ink fraction for blank detection |
| `job_history` | `10` | finished jobs kept in the status |
| `samples_dir` | `tools/escl-sim/samples` | base for `kind=file` |
| `offline_mode` | `raise` (in-process); CLI default `hang` | `raise`, `503` or `hang` |
| `hang_s` | `300` | how long `hang` holds a request |

## Guesses to verify on hardware

Each guess below should be checked against the real N4000 snw1, for example with
`tools/probe_scanner.py` or `curl`, and the simulator default changed if the device differs.

1. **NextDocument after a jam, multipick or hatch abort** returns `404`. sane-airscan treats
   any NextDocument error as "check `ScannerStatus`", maps `404` to "no more docs" and the
   ADF state to the error, so `404` plus status is the safe assumption. Some devices answer
   `409`, `500` or `503` instead. Switch with `abort_status`.
2. **JobStateReason names** for faults: `MediaJam`, `MultipickDetected`, `DoorOpen`,
   `JobTimedOut` and `ConnectionLost` are guesses. They are eSCL-style CamelCase of
   PWG-like keywords. `JobCompletedSuccessfully`, `JobCanceledByUser`, `AbortedBySystem`
   and `JobScanning` are known eSCL values. **The client should rely on `AdfState` and
   `JobState`, not on reason strings.**
3. **`pwg:State` = `Stopped`** while jammed or with the hatch open. The device may report
   `Idle` instead.
4. **`ScannerAdfProcessing`** is reported while a job runs and sheets remain. The device may
   keep saying `ScannerAdfLoaded`. Note that sane-airscan maps `ScannerAdfProcessing` to
   "no docs" for Kyocera.
5. **Job creation errors**: invalid settings → `409`, malformed XML → `400`, busy → `503`,
   and empty feeder, jam or hatch → `409`. Some devices accept a job with an empty feeder and
   fail on the first NextDocument instead.
6. **Job completion timing**: the job becomes `Completed` right after the last image is
   handed out. The device might switch only after the client sees `404`.
7. **Jam semantics**: the jammed sheet isn't scanned, and new jobs get `409` until the paper
   path is cleared. On the device the user opens the hatch, and the hatch-close may need to happen
   before `AdfState` returns to Loaded or Empty.
8. **Multipick defaults**: detection on and `StopJob` when the request doesn't say. With
   `ContinueJob` the second sheet is lost. The device might deliver it, or deliver both
   sheets as one image.
9. **Dropped connection**: the default `lose_page` (the device moves on) is the pessimistic
   choice. The device might resend the page or abort the job.
10. **`application/pdf`**: one single-page PDF per `NextDocument`. HP might return one
    multi-page PDF for the whole ADF job.
11. **`application/octet-stream` and `application/vnd.hp.continuous-perforated-sheet+jpeg`**
    are accepted and answered with JPEG.
12. **`BlackAndWhite1` + JPEG**: unknown what HP returns. It might be a grey JPEG, a
    thresholded JPEG or an error. Both variants are simulated.
13. **ScanRegion and justification**: with `XImagePosition=Center`, the simulator centres the
    region on the paper path and adds `XOffset`. The device might treat `XOffset` as absolute
    from the left edge. Also, output height = region height: the device might stop at the
    paper's trailing edge instead (paper-length detection).
14. **`Retry-After`**: in slow mode the simulator answers `503` + `Retry-After` (whole
    seconds). The real device may block NextDocument until the page is ready, which is
    `slow_style="block"`.
15. **Location header**: an absolute URL built from the request's `Host` header. sane-airscan
    resolves it relative to the base URL and replaces the host, and Scanbox should do the same.
16. **`DELETE` of a finished job** answers `200`. The device might answer `404` or `409`.
17. **Blank page detection**: `BlankPageDetection` alone also removes blank pages. On the
    device it may only flag them. The sensitivity mapping is invented.
18. **Job timeout**: 120 s, taken from `StoredJobRequestSupport/TimeoutInSeconds`. The
    device's real idle timeout is unknown.
