# Scanbox

Self-hosted, LAN-only web app that replaces the HP Scan desktop app for an **HP ScanJet Pro N4000 snw1**
(or any eSCL/AirScan scanner). Scanbox runs as a single Docker container on a Synology NAS and works from
desktop, tablet and phone browsers.

**Reliability first:** every page is written to disk the moment it arrives. Jobs survive restarts. Failures
are visible, with a clear recovery path, and a delivery can be retried without rescanning.

## Features

* **Scanning over eSCL** (no drivers): live scanner status, profiles built from the scanner's real capabilities,
  live page thumbnails while scanning, multiple stacks per document, clear handling of empty feeder, jam,
  multipick, busy/offline and timeouts.
* **Processing** (non-destructive, always re-runnable from the originals): auto-orientation, deskew,
  auto-crop, blank-page removal, colour/grey/B&W, brightness/contrast/threshold, denoise, sharpen.
* **Page editor**: drag-and-drop reorder (touch too), delete/restore, rotate, crop, insert pages from other
  scans, split and merge documents, before/after preview.
* **QR codes**: detected only inside a per-profile zone. The value can go into the filename (`{qr}`) and to
  Paperless (ASN/custom field/tag), can be corrected by hand, and can split stacks at separator sheets.
* **OCR**: OCRmyPDF + Tesseract (German/English) → searchable PDF/A. Other outputs: PDF, JPEG, PNG, TIFF, text.
* **Destinations** (several per profile, each tracked and retryable): download, NAS folders, email (Gmail SMTP
  with size handling), Paperless-ngx (API or consume folder).
* **Users**: Synology SSO (OIDC + PKCE), local admin fallback, admin/user roles, strictly per-user history,
  shared **inbox** for scans started at the scanner (claimed atomically by one user).
* **Walk-up scan** (button on the scanner → inbox): HP-specific, experimental, behind a feature flag (`docs/WALKUP.md`).
* **History & search** (full text over metadata + OCR text), re-send/re-process while the cache exists, REST
  API with per-user tokens and webhooks (Home Assistant).
* **Admin**: scanner connection test, SSO/SMTP/Paperless settings, output folders, retention, data usage +
  "clear cache", diagnostics, logs. PWA, dark mode, German/English.

## Quick start (Synology)

See **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**: secrets, `docker-compose.yml`, reverse proxy, SSO client, volumes.

```sh
docker pull ghcr.io/christianh99/scanbox:latest
```

## Development

Requirements: Python 3.12, Node 22+, optionally Docker.

```sh
# backend + simulator
python -m venv backend/.venv
backend/.venv/bin/pip install -e "backend[dev]" -e "tools/escl-sim[test]"   # Windows: backend/.venv/Scripts/...
backend/.venv/bin/python -m escl_sim --port 8090 &                          # fake scanner
SCANBOX_DATA_DIR=./dev-data SCANBOX_SCANNER_URL=http://127.0.0.1:8090 SCANBOX_COOKIE_SECURE=false \
SCANBOX_ADMIN_PASSWORD=adminadmin SCANBOX_MIN_FREE_MB=0 SCANBOX_LOG_JSON=false \
  backend/.venv/bin/python -m scanbox                                       # http://127.0.0.1:8080/api/docs

# frontend (Vite dev server proxies /api to :8080)
cd frontend && npm ci && npm run dev

# everything in Docker (app + simulator):
docker compose -f docker-compose.dev.yml up --build                         # http://localhost:8080 admin/adminadmin
```

Tests:

```sh
cd backend && .venv/bin/python -m pytest            # OCR tests skip without tesseract
docker build --target test -t scanbox:test . && docker run --rm scanbox:test   # full suite incl. OCR
cd frontend && npm run check && npm test && npm run test:e2e                   # e2e needs the dev stack
cd frontend && LAYOUT_SCREENSHOTS=1 npx playwright test e2e/layout.spec.ts     # layout audit + screenshots
```

The layout audit (`frontend/e2e/layout.spec.ts`) opens every page, menu and dialog at phone, tablet, laptop
and desktop sizes (German, plus English in dark mode). It fails on overlapping controls, text overflowing
its button, cut-off navigation labels and horizontal scrolling. Intentional overlays (the checkbox and drag
handle on page thumbnails) are marked with `data-layout-overlay`.

CI (GitHub Actions) runs lint, the backend suite in the Docker test image, frontend checks, Playwright e2e
against the app + simulator, and publishes the image to GHCR (`latest`, version, `sha-…`).

## Documentation

| Document | Content |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Design, data model, state machine, reliability, processing order, QR zones |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Synology Container Manager, reverse proxy, SSO, env vars, backups |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | How to use Scanbox (German + English summary) |
| [docs/DESTINATIONS.md](docs/DESTINATIONS.md) | Email and Paperless details |
| [docs/WALKUP.md](docs/WALKUP.md) | Walk-up scan research, protocol, probe tool |
| [docs/SIMULATOR.md](docs/SIMULATOR.md) | eSCL simulator, presets, fault injection |
| [docs/HARDWARE_CHECKLIST.md](docs/HARDWARE_CHECKLIST.md) | What still has to be verified on the real scanner, SSO, Gmail, Paperless |
| [docs/openapi.json](docs/openapi.json) | REST API contract (also live at `/api/docs`) |

## API example (Home Assistant)

Create a token under *Settings → API tokens*, then:

```sh
curl -X POST https://scanbox.home.lan/api/scans \
  -H "Authorization: Bearer sbx_…" -H "Content-Type: application/json" -H "Idempotency-Key: $(uuidgen)" \
  -d '{"profile_id": 1, "webhook_url": "http://homeassistant.local:8123/api/webhook/scanbox"}'
```

API-triggered scans are processed and delivered without review. When the document is done (or needs attention),
the webhook receives a JSON summary with download links.

## License

MIT
