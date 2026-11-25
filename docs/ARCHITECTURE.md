# Scanbox – Architecture

Scanbox is a self-hosted, LAN-only web app that drives an HP ScanJet Pro N4000 snw1
over **eSCL** and turns scans into processed, OCR'd, named and delivered documents.

The primary quality goal is **reliability**: a scan is never lost, failures are visible
and recoverable, and the app survives restarts mid-job.

```
 Browser (Svelte SPA / PWA)                     HP ScanJet N4000 (eSCL over HTTP)
        │  REST + SSE (/api/...)                         ▲
        ▼                                                │ httpx (timeouts, retries)
 ┌─────────────────────────── one container ────────────┼──────────────────────┐
 │ FastAPI (uvicorn, 1 worker process)                   │                      │
 │  ├─ api/*            REST routers, auth, CSRF         │                      │
 │  ├─ events           in-process SSE bus               │                      │
 │  ├─ scanner          status monitor + scan runner ────┘                      │
 │  ├─ jobs             persistent task queue (SQLite) + lanes                  │
 │  ├─ processing       OpenCV / zxing-cpp / img2pdf / OCRmyPDF (subprocess)    │
 │  ├─ destinations     download · folder · email · paperless (plugins)         │
 │  ├─ walkup           HP walk-up scan (feature flag, polling)                 │
 │  └─ retention        cache expiry, disk guard                                │
 │ SQLite (WAL, FTS5)  /data/scanbox.db     Files: /data/raw, /data/processed   │
 └──────────────────────────────────────────────────────────────────────────────┘
```

## 1. Decisions (and deviations from the brief)

| Topic | Decision | Why |
|---|---|---|
| ORM | **SQLAlchemy 2.0** typed ORM (no SQLModel) + Alembic | SQLModel adds a layer with lagging SQLAlchemy 2 support; API schemas are separate Pydantic models anyway (clean OpenAPI). |
| DB access | sync SQLAlchemy sessions, short transactions | SQLite is in-process and fast; sync avoids aiosqlite quirks. Hot async paths wrap calls with `asyncio.to_thread` where they could block. |
| Process model | **one** uvicorn worker process | The job queue, scanner lock and SSE bus are in-process. Heavy work (OCR, Ghostscript) runs as niced subprocesses, so the API stays responsive. |
| IDs | integer primary keys | Simple FTS5 rowid mapping. Every query is filtered by owner, so guessable IDs leak nothing (tested). |
| Settings | env > DB > default | Values set via env are **locked** (shown read-only in the admin UI) so docker-compose stays authoritative. |
| Secrets at rest | Fernet (AES-128-CBC + HMAC), key derived (HKDF) from `SCANBOX_SECRET_KEY` / `SCANBOX_SECRET_KEY_FILE` | Encrypts SMTP password, OIDC client secret, Paperless tokens. |
| Scan format | request **JPEG** pages; all processing in-app | We control quality and are not tied to HP's PDF output. |
| B/W | default `bw_strategy=software`: scan `Grayscale8` JPEG, threshold in software (Otsu or a fixed value) | JPEG cannot carry 1-bit data, so what `BlackAndWhite1`+JPEG returns is device-specific. `bw_strategy=scanner` exists to try it on the real device (see HARDWARE_CHECKLIST). |
| Frontend | Svelte 5 + Vite SPA, served as static files by FastAPI | One container, no Node at runtime. |
| Walk-up | HP-specific, behind `SCANBOX_WALKUP_ENABLED`, **polling only** | The scanner never needs to reach into the Docker network. See `docs/WALKUP.md`. |

## 2. Repository layout

```
backend/                 Python package `scanbox` (pyproject.toml, alembic)
  scanbox/
    main.py              app factory, lifespan (startup recovery, workers)
    config.py            env config (pydantic-settings)
    db.py, models.py     engine (WAL, FK, busy_timeout), ORM models
    migrations/          alembic
    settings_store.py    admin/user settings (env > DB > default, secret encryption)
    events.py            SSE pub/sub
    security/            sessions, CSRF, OIDC, local admin, API tokens, rate limit, crypto
    escl/                eSCL client + XML (capabilities, status, scan settings)
    scanner/             ScannerService: status monitor, capabilities cache, scan runner, lock
    jobs/                state machine, persistent task queue, worker lanes, recovery
    processing/          edits, pipeline, qr, orientation, deskew, blank, ocr, output, split
    destinations/        base (interface), registry, download, folder, email, paperless
    walkup/              HP walk-up destination registration + event polling
    filenames.py         template rendering + sanitising + conflict handling
    retention.py         cache expiry, usage, disk guard
    api/                 routers + schemas
  tests/
frontend/                Svelte 5 + Vite + TS + Tailwind SPA (PWA, i18n de/en)
tools/escl-sim/          eSCL scanner simulator with fault injection (own Dockerfile)
tools/probe_scanner.py   walk-up / eSCL discovery and capture tool
docs/                    architecture, deployment, user guide, walk-up, API, hardware checklist
Dockerfile               multi-stage: frontend build → python deps → runtime (+ test target)
docker-compose.yml       Synology Container Manager
docker-compose.dev.yml   app + simulator + mock services for development
```

## 3. Data model (SQLite)

All timestamps are UTC. Files live under `SCANBOX_DATA_DIR` (default `/data`):

```
/data/scanbox.db (+ -wal, -shm)
/data/raw/<document_id>/<page_uuid>.jpg      original pages, written once, never modified
/data/processed/<document_id>/<run>/...      outputs of a processing run (cache)
/data/cache/render/<page_uuid>/<edit-hash>.jpg  preview renders (disposable)
/data/tmp/                                     temp files (same filesystem → atomic rename)
```

| Table | Purpose |
|---|---|
| `users` | `sub` (OIDC subject, unique), `username`, `display_name`, `email`, `is_admin`, `is_local`, `password_hash` (argon2, local accounts only), `locale`, `theme`, `disabled` |
| `sessions` | server-side sessions: hashed id, user, `csrf_token`, expiry, last seen |
| `api_tokens` | per-user bearer tokens (SHA-256 hash stored, shown once), name, last used, revoked |
| `settings` | admin key/value (JSON); secret values encrypted |
| `user_settings` | per-user key/value (email templates, Paperless token (encrypted), counters) |
| `contacts` | per-user address book (name, email, default flag) |
| `profiles` | `owner_id` (NULL = built-in), `name`, `settings` (JSON, validated by `ProfileSettings`) |
| `documents` | one scan document: `owner_id` (NULL = inbox), `origin` (`app`/`walkup`/`api`), `status`, `profile_id`, `profile_snapshot`, `title`, `filename`, `qr_value`, `qr_source`, `add_qr_to_filename`, `page_count`, `ocr_text`, `raw_expired`, `processed_expired`, `inbox_expires_at`, `webhook_url`, `idempotency_key`, `error_code`, `error_message`, timestamps |
| `batches` | one ADF stack (one eSCL job) per row: `seq`, `escl_job_uri`, `status`, settings, `pages_received`, error |
| `pages` | `document_id`, `batch_id`, `position`, `side`, `raw_path`, `raw_sha256`, size, dpi, `edits` (JSON), `analysis` (JSON: orientation, skew, blank score, QR hits), `is_blank`, `deleted` |
| `tasks` | persistent queue: `kind` (`scan`/`process`/`deliver`/`webhook`), `document_id`, `delivery_id`, `status` (`pending`/`running`/`done`/`failed`/`cancelled`), `attempts`, `max_attempts`, `next_run_at`, `locked_at`, `last_error`, `idempotency_key` (unique) |
| `deliveries` | one row per destination per document: `destination_type`, `config` (snapshot), `status`, `attempts`, `last_error`, `result` (path / message-id / Paperless task id), `idempotency_key` |
| `artifacts` | outputs of processing: `kind` (`pdf`, `txt`, `image`), `path`, `size`, `sha256`, `run` |
| `job_events` | timeline per document ("what happened"): level, code, message, timestamp |
| `documents_fts` | FTS5 (`title`, `filename`, `qr_value`, `profile_name`, `ocr_text`), rowid = documents.id |

**Visibility rules** (enforced in one place, `api/deps.py::visible_documents()`):

* `owner_id = me` → my history.
* `owner_id IS NULL AND origin = 'walkup' AND status NOT IN (expired, discarded)` → shared inbox, visible to all signed-in users.
* Nothing else, for anyone (including admins). Admins see aggregate data (usage, queue, errors) but not other users' documents.
* **Claim** = `UPDATE documents SET owner_id=:me, claimed_at=now WHERE id=:id AND owner_id IS NULL`. SQLite serialises writers, so exactly one claimant sees `rowcount == 1`; the other gets `409 already_claimed`. An inbox item cannot be edited, processed or deleted before it is claimed.

## 4. Document state machine

```
            ┌──────────────► failed ◄──────────────────┐
            │                  ▲                       │
 queued ──► scanning ──► stored ──► processing ──► delivering ──► done
   │          │   ▲         │  ▲        │                │  ▲
   │          │   └─(add    │  │        ▼                ▼  │ retry delivery
   │          │     batch)──┘  └── needs_attention ◄─────┘──┘
   └──────────┴──► discarded (user)     expired (retention, inbox only)
```

| State | Meaning |
|---|---|
| `queued` | Created; waiting for the scanner lane (only one job talks to the scanner). |
| `scanning` | A batch is running; pages are persisted the moment they arrive. |
| `stored` | All pages are on disk. The user reviews/edits, adds another batch, or finishes. Walk-up inbox items rest here. |
| `processing` | Pipeline + OCR running. |
| `delivering` | One `deliver` task per destination. |
| `done` | All deliveries succeeded. |
| `needs_attention` | Partial success that a human must resolve: scan failed midway (pages kept), QR missing with `on_missing=block`, a delivery exhausted its retries. The document shows exactly what happened (`job_events`). |
| `failed` | Nothing usable (e.g. scan failed before the first page). It can still be retried (rescanned). |
| `discarded` / `expired` | Terminal; files removed, metadata kept. |

A queued or running scan can be stopped with `POST /api/documents/{id}/cancel`: the eSCL job is deleted, and pages already received are kept (→ `stored`, or `discarded` if there were none).

Transitions are validated by `jobs/states.py::transition()`. An invalid transition requested via the API is answered with `409 invalid_state`. Each transition writes a `job_events` row and publishes an SSE event.

### Startup recovery

On startup, before workers start:

* `tasks.status='running'` → the process died mid-task.
  * `scan` → the eSCL job cannot be resumed. The batch is marked `interrupted`. Pages already on disk are kept. The document goes to `needs_attention` (if pages > 0) or `failed`.
  * `process` → re-queued (processing is a pure function of raw pages + edits → idempotent).
  * `deliver` → per-destination recovery. `folder`: checks whether the target file exists with the same SHA-256 (then done) or re-writes it atomically. `paperless`: re-uploads; Paperless rejects exact duplicates by checksum, which counts as success. `email`: **not** re-sent automatically, because it may already have gone out. The delivery goes to `needs_attention` with "may have been sent – retry manually".
* Orphan temp files in `/data/tmp` are removed.

### Idempotency

* `tasks.idempotency_key` is unique (`process:<doc>:<run>`, `deliver:<delivery_id>:<run>`), so a double-click or retry never enqueues twice.
* `POST /api/scans` accepts an `Idempotency-Key` header (API/Home Assistant). The same key from the same user returns the existing document.
* Folder writes use temp file + `os.replace`. A conflicting name gets a suffix, never an overwrite.

## 5. Task queue and lanes

`jobs/queue.py` is a SQLite-backed queue. Workers claim a task with
`UPDATE tasks SET status='running', locked_at=now WHERE id=(SELECT ... LIMIT 1) AND status='pending'`.
Retries use exponential backoff with jitter (`next_run_at`).

| Lane | Concurrency | Kinds | Notes |
|---|---|---|---|
| scanner | **1** (plus `asyncio.Lock` on the scanner) | `scan` | Also used by walk-up. A second scan request while busy stays `queued` and the UI shows "scanner busy – queued (position n)". |
| process | `SCANBOX_OCR_CONCURRENCY` (default **1**) | `process` | OCRmyPDF runs as a subprocess under `nice -n 10`, with `--jobs` = `SCANBOX_OCR_JOBS` (default 2). |
| deliver | 2 | `deliver`, `webhook` | Network I/O only. |

Queues are bounded: `POST /api/scans` is refused with `429` when more than `SCANBOX_MAX_QUEUED_SCANS` (default 5) scans are queued.
Graceful shutdown: stop claiming, wait up to 20 s for running tasks, then kill subprocesses. Interrupted tasks are recovered on next start.

## 6. Scanner integration (eSCL)

`escl/client.py` – `EsclClient(base_url)`:

* `get_capabilities()` → `ScannerCapabilities` (parsed with lxml, namespace-aware, tolerant of unknown elements).
* `get_status()` → `ScannerStatus` (`state`: Idle/Processing/Stopped/Testing, `adf_state`, jobs with `JobState` + `JobStateReasons`).
* `create_job(ScanSettings)` → job URI (`201` + `Location`). `409/503` → `ScannerBusy`.
* `next_document(job_uri)` → `bytes | None` (`200` page, `404` no more pages). `503` → retry with backoff.
* `cancel_job(job_uri)` (`DELETE`).
* Timeouts: connect 5 s, read 60 s for `NextDocument` (a page at 600 dpi can take a while), 10 s otherwise. Retries: 3× with backoff on connect errors / 503.

**Capabilities** are cached in the `settings` table (`scanner.capabilities` with fetch time), refreshed on startup, on "Test connection" and whenever the scanner goes from offline to online. The UI only offers options present in `GET /api/scanner/capabilities`.

**Scan runner** (`scanner/runner.py`), per batch:

1. Disk guard: free space ≥ `SCANBOX_MIN_FREE_MB` (default 1024), otherwise `insufficient_disk`.
2. `GET ScannerStatus`. `AdfState=ScannerAdfEmpty` → `feeder_empty` (the document goes to `needs_attention` if it already has pages, otherwise `failed`, retryable). Jam/hatch open → the matching error code.
3. `POST ScanJobs` with settings from the profile snapshot.
4. Loop `NextDocument`. Each page is written to `/data/tmp`, fsync'd, renamed into `/data/raw/<doc>/`, and **only then** inserted into `pages` and announced over SSE (`page.added`).
5. On `404` → re-check `ScannerStatus` / job info. `JobStateReasons` such as `JobCompletedSuccessfully` → batch done. A jam or multipick during the job → batch `failed` with the reason; pages kept.
6. Timeouts / dropped connections → bounded retries, then the batch fails with `scanner_timeout` / `scanner_offline`. The eSCL job is cancelled best-effort.

Error codes surfaced to users: `scanner_offline`, `scanner_busy`, `feeder_empty`, `paper_jam`, `cover_open`, `multipick`, `scanner_timeout`, `scanner_error`, `insufficient_disk`.

**Status monitor**: polls `ScannerStatus` every 3 s while a scan runs or SSE clients are connected, otherwise every 60 s (for `/readyz` and diagnostics). It pushes `scanner.status` only on change.

## 7. Processing pipeline (`processing/`)

Processing is a **pure function** of (raw page, page edits, profile snapshot). Originals are never modified. Each run writes a new `processed/<doc>/<run>/` directory, and the previous run is removed after success.

Per page, in this order:

| # | Step | Source | Notes |
|---|---|---|---|
| 1 | Decode raw JPEG | – | EXIF orientation ignored (the scanner doesn't set it). |
| 2 | **Orientation** (0/90/180/270) | `edits.rotate` if set by the user, else auto (Tesseract OSD `--psm 0`) when `auto_rotate` is on | OSD result cached in `pages.analysis`. |
| 3 | **Fine rotation / deskew** | `edits.angle` if set, else auto-deskew (Hough / minAreaRect on text) when `deskew` is on | Limited to ±15°. |
| 4 | **QR detection** | zone from profile | See §8. Runs on the *oriented, deskewed, uncropped* image. |
| 5 | **Crop** | `edits.crop` (relative rect) or auto-crop | Auto-crop finds the paper against the scanner background. |
| 6 | **Background cleanup** | profile `clean_background` (`auto` = on unless the profile scans photos), `edits.clean_background` | See below. |
| 7 | **Colour + enhancement** | `edits` | colour/grey/B&W conversion, brightness, contrast, threshold (B&W), denoise, sharpen. |
| 8 | **Blank detection** | profile | Ink coverage measured against the local paper tone (see below). Blank pages are flagged `is_blank` and excluded from output (but kept and restorable in the editor). |

**Background cleanup** (`imageops.normalize_background` + `clean_borders`), built from real N4000 scans:
1. *Paper mask*: pixels within 35 levels of the page's paper colour in every channel (after a 3×3 opening).
   Shading, grey tint, show-through and faint creases are paper; ink, coloured areas and highlighter are not.
2. *Paper surface*: on a ~75 dpi copy, a 15×15 max filter over paper pixels only, so thin dark features
   never win. Large non-paper areas (photos, coloured boxes) are inpainted from the surrounding paper.
   Pages with < 35 % paper (photos, dark paper) are left unchanged.
3. Divide the page by that surface, then apply a highlight curve. Tones below 60 % of the paper stay
   unchanged, and the range up to the white point (0.97 − 0.12 × strength) is stretched to white.
   Text, photos and colours keep their values.
4. *Edge artefacts*: per row/column, a dark run that starts at the image edge and ends within 3 % of the
   width/height is whitened. This covers the backdrop strip, dark corners, and the truncated grey JPEG rows
   the N4000 sometimes sends. Runs reaching further in are content and are kept.

B/W with an automatic threshold always thresholds the background-normalised page (Otsu, clamped to
110–215), so creases and shadows don't turn black.

**Blank detection** (`imageops.ink_coverage`): ink is darker than 70 % of the local paper tone after the same
background estimate. Specks (< 6 px), components touching the 3 % margin, and long hairlines (fold lines) are
ignored. Real pages: empty N4000 back side 0.0000, sparse flyer 0.12, simulator text pages 0.036–0.05
(threshold 0.004).
It runs twice:
* **At ingest**, right after each page is stored, in the background (one at a time) on a reduced copy
  without orientation/deskew. It sets `is_blank` and `analysis.ink` and publishes `page.updated`, so the
  review already shows *Blank* badges. This is best effort: a crash only loses the hint.
* **In the pipeline**, on the final rendered page. This is authoritative.

Preview renders are cached by a key that includes `RENDER_VERSION` (bump it when rendering changes).

Then, per document:

9. **Split** (optional) at separator pages (QR value change / QR pattern match) → child documents. Each part
   carries the QR value of the separator that started it (`qr_source = separator`, kept on re-processing even
   though the removed separator page is gone); the first part stays in the original document.
10. **Assemble**: `img2pdf` (lossless for JPEG) or Pillow for PNG/TIFF.
11. **OCR**: `ocrmypdf --output-type pdfa|pdf -l deu+eng --jobs N --optimize 1..3`, run under `nice`. Text sidecar → `documents.ocr_text` → FTS.
12. **Compression**: OCRmyPDF `--optimize`, plus Ghostscript re-render for target size (`/ebook`, `/screen`) if needed.
13. **Title/date suggestion** from OCR text (simple heuristics: first strong line, first date). Always editable.
14. **Filename** from template (§9).

The editor preview uses the same step functions (`GET /api/pages/{id}/image?variant=processed` for saved edits, `POST /api/pages/{id}/preview` with unsaved edits), so what you see is what gets produced. Renders are cached under a hash of raw page + edits + profile processing, which also serves as the HTTP `ETag`.

## 8. QR codes

* **Zone** stored in the profile as relative coordinates `{x, y, w, h}` ∈ [0,1], relative to the page **after orientation correction and deskew, before crop** (step 4 above). Presets: `top_right` = `{x:0.6, y:0, w:0.4, h:0.3}`, `top_left` = `{x:0, y:0, w:0.4, h:0.3}`, `full` = whole page.
* Why before crop: the zone is drawn on the preview of an oriented page, and auto-crop varies from page to page. Using the uncropped page keeps the zone stable for every scan of the same paper size.
* **Decoding**: zxing-cpp `read_barcodes(formats=QRCode|MicroQRCode, try_rotate=True, try_downscale=True)` on the zone. If nothing is found, retry on preprocessed variants (Otsu binarisation, 2× upscale, CLAHE contrast). Barcodes whose centre lies inside the zone count as "in zone".
* **Several hits**: `selection` = `reading_order` (top-to-bottom, then left-to-right, 2 % row tolerance) | `largest` (bounding-box area) | `regex` (first in reading order that matches `regex`).
* **Pages searched**: `first` | `all` (first page with a hit wins) | `each` (per-page values, used for splitting).
* **Regex filter**: values not matching are ignored. **On missing**: `continue` | `warn` (job event + UI badge) | `block` (document goes to `needs_attention` until the user enters a value or confirms "no QR").
* The page carrying the code is **kept**. The value can be corrected manually (`qr_source='manual'`). A manual value always wins over re-detection.
* `{qr}` in filename templates. The "Add QR value to filename" checkbox (default from profile) prefixes `{qr}_` when the template doesn't already contain `{qr}`.

## 9. Filenames

Tokens: `{date}` (YYYY-MM-DD), `{time}` (HH-MM-SS), `{datetime}` (YYYY-MM-DD_HH-MM-SS), `{qr}`, `{title}`, `{counter}` (per-user, zero-padded 4), `{profile}`, `{user}`, `{pages}`. `strftime` format specs are allowed for date tokens: `{date:%Y%m%d}`.

Sanitising: NFC normalisation; `/ \ : * ? " < > |` and control chars → `_`; leading dots/dashes stripped; Windows-reserved names (`CON`, `NUL`, …) suffixed; whitespace collapsed; max 150 characters (UTF-8 safe), extension appended after truncation; empty → `scan`. Sub-folders in folder destinations are sanitised **per segment**, `..` rejected, and the final path is verified with `resolve().is_relative_to(root)`. Conflicts get `name (2).pdf`, `name (3).pdf`, … (never overwrite).

## 10. Destinations

```python
class Destination(Protocol):
    type: ClassVar[str]                    # "download" | "folder" | "email" | "paperless"
    Config: ClassVar[type[BaseModel]]      # per-profile config, validated
    async def validate(self, cfg, env: DeliveryEnv) -> list[str]
    async def deliver(self, job: DeliveryJob, cfg, env: DeliveryEnv) -> DeliveryResult
    async def recover(self, job, cfg, env, previous: dict | None) -> RecoverAction  # after crash
```

`DeliveryJob` carries the document metadata (title, filename, QR, created, pages, user), artifact paths, and the delivery's idempotency key. `DeliveryEnv` gives access to admin settings, user settings (secrets decrypted), an httpx client factory and a temp dir.
Errors are raised as `DeliveryError(code, message, retryable)`. Retryable errors back off (30 s, 2 min, 10 min, 30 min, 1 h; max 6 attempts), then the delivery goes to `needs_attention`. A failed destination can be retried from the UI without rescanning, as long as the cache exists. Processed files are re-generated from raw if they have already expired.

* **download** – always available. Marks the artifact downloadable (no-op delivery).
* **folder** – admin-defined roots (`SCANBOX_OUTPUT_ROOTS` / admin UI: name → absolute path). The profile picks root + sub-folder template. Atomic writes.
* **email** – SMTP settings are admin-global. Recipients, subject and body templates are per user/profile. Size check against `max_attachment_mb` (default 25, Gmail). Too large → `compress` (Ghostscript `/ebook`) → `split` into several mails → otherwise fail with `attachment_too_large`.
* **paperless** – mode `consume` (write to a mounted consume folder, atomically) or `api` (`POST /api/documents/post_document/` with `Authorization: Token …`). URL is admin-set, token per user. Metadata: title, created, tags, correspondent, document_type. QR → `archive_serial_number` (must be an integer, otherwise falls back to a configurable custom field or tag) or a custom field. Option `skip_ocr` sends the image-only PDF.

## 11. Auth and security

* **OIDC** (Synology SSO Server) via Authlib: authorization code + PKCE (S256), `state` + `nonce`. Redirect URI `https://<host>/api/auth/oidc/callback`. Admin if the configured claim (`SCANBOX_OIDC_ADMIN_CLAIM`, default `groups`) contains `SCANBOX_OIDC_ADMIN_VALUE` (default `scanbox-admins`).
* **Local admin**: created on first start through the setup page. While no local admin exists (and no password is set in the environment), each start generates a one-time setup code (12 characters, ~60 bits), keeps it only in memory and writes it to the log (`auth.setup_required`). `POST /api/auth/setup` with the code, a username and a password creates the admin and signs it in. Setup then closes for good, and a restart invalidates an unused code. Wrong codes are rate-limited (10 per 15 min per IP, 30 overall).
* **Password change**: `POST /api/auth/password` needs the current password (failures count towards the login rate limit) and deletes all other sessions of the account.
* **Pinned admin**: with `SCANBOX_ADMIN_PASSWORD(_FILE)` set (CI, system test), the admin `SCANBOX_ADMIN_USERNAME` gets that password on every start, the setup page never appears, and the password cannot be changed in the app (`password_locked`). Passwords are hashed with argon2. Login is rate-limited: 5 failures per 15 min per IP+username, and 20 per IP.
* **Sessions**: random 256-bit id in cookie `scanbox_session` (HttpOnly, Secure, SameSite=Lax, Path=/). Only the SHA-256 hash is stored. Idle timeout 7 days, absolute 30 days.
* **CSRF**: double submit. Cookie `scanbox_csrf` (readable by JS) must equal header `X-CSRF-Token` for every unsafe method with cookie auth. Bearer-token requests are exempt (no ambient credentials).
* **Login errors** are passed to the SPA as `/login?error=<code>`: `sso_unavailable` (discovery failed), `sso_disabled`, `sso_failed` (state/nonce/token validation), `sso_<provider error>` (e.g. `sso_access_denied`) and `account_disabled`.
* **Proxy headers** are trusted only from `SCANBOX_FORWARDED_ALLOW_IPS` (default localhost + Docker bridge networks).
* **API tokens**: `sbx_<prefix>_<secret>`, SHA-256 stored, scoped to the user. Used by Home Assistant/scripts.
* **Webhooks**: an API scan can set `webhook_url`. On `done` / `needs_attention` / `failed`, Scanbox POSTs a JSON summary (+ download URL). Retries use the deliver lane.
* Security headers: CSP (self), `X-Content-Type-Options`, `Referrer-Policy`, `frame-ancestors 'none'`.
* Uploads/inputs: size limits on every body, Pydantic validation, no user-controlled paths except via the sanitiser.

## 12. Live updates (SSE)

`GET /api/events` (cookie or token auth). Each client gets a bounded `asyncio.Queue` (100). A slow client is disconnected and reconnects (EventSource auto-reconnects; `Last-Event-ID` is ignored – the client refetches state on reconnect). Events are filtered per user: document events go to the owner only, inbox events to everyone.

Events: `scanner.status`, `document.created|updated|deleted`, `page.added|updated`, `batch.updated`, `delivery.updated`, `inbox.changed`, `heartbeat` (every 20 s).

## 13. Retention and disk

Hourly job plus admin "Clear cache now":

* raw pages older than `raw_retention_days` (30) → files removed, `documents.raw_expired = true`.
* processed outputs older than `processed_retention_days` (14) → removed, `processed_expired = true`.
* unclaimed inbox items older than `raw_retention_days` → `expired`.
* render cache: LRU, capped at 500 MB.
* **Documents that are not finished** (`queued`/`scanning`/`stored`/`processing`/`delivering`/`needs_attention`) keep their raw files until resolved or until 2× the retention period.

`GET /api/admin/usage` reports bytes for raw / processed / cache / DB and free disk.

## 14. Walk-up scan

See `docs/WALKUP.md` (research results and protocol). The implementation is isolated in `scanbox/walkup/`, enabled by `SCANBOX_WALKUP_ENABLED=true` (or the admin toggle). If registration or polling fails, it logs, shows the state in diagnostics and backs off. Nothing else depends on it.

## 15. Observability

* structlog JSON logs (`job_id`, `document_id`, `task_id` bound via contextvars). The log is also written to a rotating `/data/logs/scanbox.log` (downloadable by admins).
* `GET /healthz` → process alive (always 200 if the event loop responds).
* `GET /readyz` → DB writable, data dir free space ≥ threshold, workers running; scanner reachability is reported but **does not** fail readiness (an offline scanner must not restart the container).

## 16. Testing strategy

* `tools/escl-sim` – an eSCL simulator serving the real capabilities XML, generating pages from sample images, with fault injection via `POST /_sim/faults`.
* Backend unit tests: eSCL XML/client, QR zone and selection, filename templating, pipeline steps, retention, state machine, permissions (inbox claim race, history isolation), CSRF/session.
* Integration: app + simulator in-process (ASGI transport) for scan → process → OCR → deliver (folder, aiosmtpd SMTP, mock Paperless). OCR tests are skipped locally if Tesseract is missing, and **always** run in the Docker `test` target used by CI.
* Mock OIDC provider (in-test ASGI app issuing signed ID tokens).
* Frontend: Vitest component tests + Playwright e2e against the container + simulator.
