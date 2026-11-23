# Verify on real hardware / services

Everything was built and tested against the **eSCL simulator** (`tools/escl-sim`), a mock OIDC provider,
a local SMTP server and a mock Paperless API. The items below can only be confirmed with the real
HP ScanJet Pro N4000, Synology SSO Server, Gmail and Paperless-ngx. For each item: how to check it, and
what to change if it differs.

Useful tools:
* `python tools/probe_scanner.py --scanner http://<scanner-ip>` (read-only; standard library only; writes a log file to send back)
* Admin → Diagnostics → *Download logs* (structured JSON log with `document_id` / `task_id`)
* Raw eSCL: `curl http://<ip>/eSCL/ScannerStatus` while scanning

## A. Scanner (eSCL) – priority order

| # | What to verify | How | If different |
|---|---|---|---|
| A1 | **Basic scan** simplex and duplex, 300 dpi colour: page count and front/back order | Profile "Dokument", 3 sheets | Duplex ordering lives in `scanner/runner.py::_store_page` (side = index parity). |
| A2 | **Empty feeder**: rejected at `POST /ScanJobs` (409) or only at the first `NextDocument`? | Start a scan with no paper | Both paths end in `feeder_empty` (status check before and after the job). Report the HTTP codes from the log. |
| A3 | **Paper jam / multipick mid-job**: what `NextDocument` returns afterwards; `JobState`, `JobStateReasons` and `AdfState` in `ScannerStatus` | Feed a creased page, or two sheets stuck together, with multipick action StopJob | Pages before the error must be kept (`needs_attention`, "n pages saved"). We rely on `AdfState` + `JobState`. If HP uses other reason names, map them in `ADF_ERRORS` / `_check_job_end`. |
| A4 | **Multipick ContinueJob**: is the sheet delivered, and is the second one lost? | Profile with multipick action ContinueJob | A warning event "multipick detected – check pages" is added if the job reasons mention multipick. |
| A5 | **B/W**: what `BlackAndWhite1` + `image/jpeg` returns (409? grey JPEG? thresholded?) | Profile colour mode B/W with *B/W method = scanner* | Default is `bw_strategy=software` (scan grey, threshold in software), so B/W works either way. Note the result in `docs/ARCHITECTURE.md` §1. |
| A6 | **Optional ScanSettings elements** accepted, ignored, or rejected with 409: `BlankPageDetection(AndRemoval)`, `BlankPageSensitivity`, `MultipickDetection(Action)`, `AutoCrop`, `Brightness`…`CompressionFactor` | Enable each scanner-side option in a profile, one at a time | Rejected → leave the option unset (`null`) in profiles. Element names/order are in `escl/xml.py::build_scan_settings`. |
| A7 | **Page size "auto"** (legal-length region, 2550×4200): does the ADF stop at the paper end, or pad white? Does scanner `AutoCrop` trim? | Profile page size *auto*, short receipt | If it pads, enable *software auto-crop* in the profile, or use fixed A4. |
| A8 | **Long pages** (up to MaxHeight 36600) | Custom size with a long height | Only needed for receipts or rolls. |
| A9 | **Scanner busy** (e.g. HP Smart scanning at the same time): 503 + Retry-After, or blocking? | Start a scan from the HP app, then from Scanbox | Scanbox retries for 60 s, then reports `scanner_busy`. |
| A10 | **Dropped connection** (unplug network mid-scan): is the in-flight page lost, resent or aborted? | Pull the LAN cable for a moment | Scanbox stops the batch with `connection_lost` and keeps received pages. It deliberately does *not* retry `NextDocument`: the device may count the page as sent, and a retry could silently skip a sheet. |
| A11 | **Speed**: transfer time per page at 600 dpi colour (client read timeout is 120 s) | 600 dpi colour, 5 pages | Raise `PAGE_TIMEOUT` in `escl/client.py` if needed. |
| A12 | **Blank page detection on the scanner**: removes pages, or only flags them? | Stack with blank backs, duplex, blank removal = scanner | Default is software detection (always works, pages stay restorable). |
| A13 | **Location header** host name (`HPB0795E.local`) | Log line `scan.job_created` | Scanbox always rewrites to the configured address (mDNS doesn't resolve in Docker). |
| A14 | **Status polling** every 3 s while the UI is open doesn't disturb scanning | Scan with the UI open | Interval in `scanner/service.py::_interval`. |
| A15 | **Real QR stickers**: detection rate at 200/300 dpi, B/W and colour, slightly rotated | Profile "Dokument mit QR", several documents | Detection uses the zone after orientation+deskew, before crop. If detection is weak, raise the dpi or widen the zone. |
| A17 | **Truncated JPEG** ("Corrupt JPEG data: found marker 0xd9 instead of RST2"): seen once on the first page of a duplex scan, with the last ~4 pixel rows grey | Check the log and the bottom edge of the first pages of a few scans | Harmless with *Clean background* on (edge cleanup whitens them). If it happens often or cuts off more, report it: it may be a transfer issue. |
| A18 | **Background cleanup and blank detection on your paper**: recycled/grey paper, coloured forms, thin paper with strong show-through | Scan such pages, compare *Original* and *Result* in the page editor | Adjust *Strength* in the profile (higher removes more show-through, but also very light print). Blank threshold is in the profile too. |
| A16 | **Auto-orientation** (Tesseract OSD) on upside-down pages, and on pages with little text | Feed a page upside down | Low-confidence OSD results (<1.5) are ignored on purpose. Manual rotation is always possible. |

## B. Walk-up scan (scanner button) – experimental, feature-flagged

Follow `docs/WALKUP.md` §9–10 and send the log of:

```
python tools/probe_scanner.py --scanner http://<ip> --out probe.log
python tools/probe_scanner.py --scanner http://<ip> --register --name "Scanbox-Probe" --watch 300 --cleanup --out probe-walkup.log
```

While `--watch` runs: on the scanner panel select "Scanbox-Probe", then 1) cancel, 2) start a simplex scan, 3) start a duplex scan with another resolution/colour, 4) wait for the timeout. Key questions:
which base URL/port serves `/eSCL/WalkupSubscriptions`; whether the entry and its shortcut labels appear on the panel (label length);
the event bodies (`HostSelected`, `ScanRequested`, `ScanPagesComplete`); whether a ScanJob with `ContextID` is required;
what happens after a device reboot or after SubscriptionTimeout (150 s).
Until confirmed, keep `SCANBOX_WALKUP_ENABLED=false`. The rest of the app doesn't depend on it.

## C. Synology SSO Server (OIDC)

| # | What | How |
|---|---|---|
| C1 | Issuer URL (`https://<sso-host>/webman/sso`) and discovery document | Open `<issuer>/.well-known/openid-configuration` |
| C2 | Login round trip incl. PKCE (S256) | Click "Synology SSO" on `/login` |
| C3 | Token endpoint client authentication (`client_secret_basic` vs `client_secret_post`) | Picked automatically from discovery; check the log on failure |
| C4 | Which claims arrive (groups? preferred_username? email?) | Log line `auth.login … claims=[…]` after an SSO login |
| C5 | Admin mapping works (`SCANBOX_OIDC_ADMIN_CLAIM` / `_VALUE`) | The admin menu is visible for the intended users only |

## D. Gmail (SMTP)

| # | What | How |
|---|---|---|
| D1 | `smtp.gmail.com`, port 587, STARTTLS, user = full Gmail address, password = **app password** (needs 2-step verification) | Admin → Email → *Test connection*, then *Send test mail* |
| D2 | From address = the Gmail account (Gmail rewrites other From addresses) | Check a received mail |
| D3 | 25 MB limit: a large scan is compressed or split (`[1/2]`, `[2/2]`) | Scan ~40 colour pages at 300 dpi to email with *compress then split* |

## E. Paperless-ngx (once installed)

| # | What | How |
|---|---|---|
| E1 | API upload with token; tags, correspondent, document type, created date | Profile with Paperless destination, mode API; check the document in Paperless |
| E2 | QR value → ASN (numeric QR) and custom field / tag fallback (non-numeric) | Use both kinds of QR codes |
| E3 | Task polling fields (`related_document`, status casing) of your Paperless version | Delivery details in the document view show `task_id` / `document_id` |
| E4 | Duplicate handling (Paperless ≥ 3 accepts duplicates by default; Scanbox checks by checksum before re-uploading) | *Resend* a document: no second copy |
| E5 | Consume folder mode: permissions of the shared folder; Paperless ignores the dot-prefixed temp files | Mode "consume" |
| E6 | "Skip our OCR" sends the image-only PDF, so Paperless does its own OCR | Option `skip_ocr` |

## F. NAS / Docker

| # | What | How |
|---|---|---|
| F1 | UID/GID of `user:` can write `/data` and every output root | Folder destination test scan |
| F2 | Hard links are used for no-overwrite atomic writes; on shares without hard links it falls back to check+rename | Folder destination to each mounted share |
| F3 | Reverse proxy keeps the SSE connection open (> 60 s idle) | Leave the scan page open; the status updates live |
| F4 | CPU/RAM while OCRing 30 pages with OCR_JOBS=2 (other services such as Immich stay responsive) | DSM Resource Monitor |
| F5 | Healthcheck: container is healthy; scanner offline does **not** make it unhealthy | Switch off the scanner |
