# Deployment on a Synology NAS (DS723+, DSM 7.2, Container Manager)

Scanbox runs as **one container** (`ghcr.io/christianh99/scanbox`) behind DSM's reverse proxy.
It is meant for the LAN only: don't forward its port on your router.

```
Browser ──HTTPS──▶ DSM reverse proxy (scanbox.home.lan:443) ──HTTP──▶ 127.0.0.1:8085 ─▶ container :8080
                                                                     │
Scanner (eSCL, HTTP) ◀────────────────── container polls/scans ──────┘
```

## 1. Prepare folders

Create `/volume1/docker/scanbox/data` (File Station or SSH) and give the container user write access:

```sh
sudo mkdir -p /volume1/docker/scanbox/data
id your-dsm-user       # → uid / gid for `user:` in docker-compose.yml
sudo chown -R <uid>:<gid> /volume1/docker/scanbox
```

The container runs as the UID/GID set in `user:` (default in the compose file: `1026:100`). That user needs
write access to `/data` and to every output folder you mount.

No password file is needed: the admin account is created in the browser on first start (§5).

**Secret key (optional).** Scanbox encrypts stored secrets (SMTP password, Paperless tokens, OIDC client
secret) with a key. Without one it generates `/data/secret.key` on first start, so the key sits next to the
database and ends up in the same backups. To keep it apart, create it yourself and uncomment
`SCANBOX_SECRET_KEY_FILE` and the `secrets:` entries in the compose file:

```sh
sudo mkdir -p /volume1/docker/scanbox/secrets
# 48+ random chars; keep a backup. Changing it invalidates sessions and stored secrets (SMTP/Paperless).
openssl rand -base64 48 | sudo tee /volume1/docker/scanbox/secrets/secret_key >/dev/null
sudo chmod 600 /volume1/docker/scanbox/secrets/*
sudo chown -R <uid>:<gid> /volume1/docker/scanbox/secrets
```

## 2. Pull the image

The GitHub Actions workflow publishes `ghcr.io/christianh99/scanbox` with these tags:

* `latest`: the newest release.
* `1.2.3` / `1.2`: a specific release (built from the git tag `v1.2.3`), or the newest patch of `1.2`.
* `sha-<short>`: the release built from one specific commit.

Images exist for releases only. Admin → Diagnostics shows which version is running.

* **Public package** (GitHub → Packages → scanbox → Package settings → Change visibility → Public): Container Manager can pull it without credentials.
* **Private package**: create a GitHub personal access token (classic) with only the **`read:packages`** scope. Then in Container Manager → *Registry* → *Settings* → *Add*:
  * Registry name `GHCR`, URL `https://ghcr.io`, user name = your GitHub user name, password = the token.
  * Alternative over SSH: `sudo docker login ghcr.io -u <github-user>` (paste the token as the password).

## 3. Create the project

Container Manager → *Project* → *Create* → path `/volume1/docker/scanbox`, *Create docker-compose.yml* → paste
[`docker-compose.yml`](../docker-compose.yml) and adapt:

| Variable | Meaning |
|---|---|
| `SCANBOX_BASE_URL` | The HTTPS URL users open, e.g. `https://scanbox.home.lan`. Used for the OIDC redirect URI and links in webhooks. |
| `SCANBOX_SCANNER_URL` | Optional, **normally left unset**: set the scanner address in Admin → *Scanner* instead (see §7). If set here, the admin UI field becomes read-only. |
| `SCANBOX_SECRET_KEY_FILE` / `SCANBOX_SECRET_KEY` | Optional. Session signing + encryption of stored secrets. Without it a key is generated in `/data/secret.key`. |
| `SCANBOX_ADMIN_PASSWORD(_FILE)`, `SCANBOX_ADMIN_USERNAME` (`admin`) | Optional, **normally left unset**: the admin is created in the browser on first start (§5). For automated setups (CI, scripts) the password can be set here instead (≥ 8 characters, re-hashed on change). Then the setup page does not appear and the password cannot be changed in the app. Without a password, `SCANBOX_ADMIN_USERNAME` is only the suggested name on the setup page. |
| `SCANBOX_OIDC_ISSUER`, `_CLIENT_ID`, `_CLIENT_SECRET(_FILE)`, `_SCOPES`, `_ADMIN_CLAIM`, `_ADMIN_VALUE` | Optional, **normally left unset**: configure SSO in Admin → *SSO* instead (see §6). Each variable that is set pins its value and makes that field read-only in the UI. |
| `SCANBOX_OUTPUT_ROOTS` | `Name=/path;Other=/path2` – folders users can deliver to (paths inside the container, mount them as volumes). |
| `SCANBOX_PAPERLESS_URL`, `SCANBOX_PAPERLESS_CONSUME_DIR` | Paperless-ngx (API URL and/or mounted consume folder). |
| `SCANBOX_OCR_CONCURRENCY` (1), `SCANBOX_OCR_JOBS` (2), `SCANBOX_NICE_LEVEL` (10) | CPU budget: OCR documents in parallel, Tesseract threads per document, process priority. |
| `SCANBOX_RAW_RETENTION_DAYS` (30), `SCANBOX_PROCESSED_RETENTION_DAYS` (14), `SCANBOX_MIN_FREE_MB` (1024) | Cache retention and disk guard (also adjustable in the admin UI if not set here). |
| `SCANBOX_WALKUP_ENABLED` | Experimental HP walk-up scan (see `docs/WALKUP.md`). |
| `SCANBOX_MAX_QUEUED_SCANS` (5) | Bound on queued scans. |
| `SCANBOX_LOG_LEVEL` (INFO), `SCANBOX_LOG_JSON` (true) | Logging. Logs also go to `/data/logs/scanbox.log` (downloadable in Admin → Diagnostics). |
| `SCANBOX_FORWARDED_ALLOW_IPS` (`127.0.0.1,172.16.0.0/12`) | Proxies whose `X-Forwarded-For` is trusted (DSM nginx reaches the container via the Docker bridge). Never set `*`: clients could then forge their IP and bypass the login rate limit. |
| `SCANBOX_COOKIE_SECURE` (true) | Keep `true` behind HTTPS. Only set `false` for plain-HTTP testing. |

**Variables set in the environment are locked**: the admin UI shows them read-only. Anything not set here can be changed in the admin UI.

Resource limits in the compose file: `cpus: 2`, `memory: 4g`. Idle usage is ~150 MB RAM. OCR of a 20-page document at 300 dpi takes about 1–2 minutes on the R1600 with `SCANBOX_OCR_JOBS=2`.

## 4. Reverse proxy (HTTPS)

DSM → *Control Panel* → *Login Portal* → *Advanced* → *Reverse Proxy* → *Create*:

* Source: protocol `HTTPS`, hostname `scanbox.home.lan`, port `443`, enable HSTS.
* Destination: protocol `HTTP`, hostname `localhost`, port `8085`.
* *Custom Header* → *Create* → **WebSocket** (adds `Upgrade`/`Connection`). Add `X-Forwarded-Proto: $scheme` and `X-Forwarded-For: $proxy_add_x_forwarded_for` too.
* *Advanced settings*: proxy read timeout **≥ 300 s**, because live updates use a long-lived Server-Sent-Events connection. DSM's nginx otherwise buffers or cuts it. Scanbox sends `X-Accel-Buffering: no` to disable buffering.
* Certificate: *Control Panel* → *Security* → *Certificate* → assign one to `scanbox.home.lan` (a local CA or Let's Encrypt via DNS challenge).
* DNS: point `scanbox.home.lan` to the NAS (router, Pi-hole or DSM DNS Server).

## 5. First start: create the admin account

Scanbox has no admin account after the first start. It writes a one-time **setup code** to the container
log instead:

1. Container Manager → *Container* → `scanbox` → *Log*. Look for the entry `auth.setup_required`, e.g.
   `"setup_code": "K7QM-3XRD-WP9H"`.
2. Open `https://scanbox.home.lan`. The page shows *Set up Scanbox* instead of the login form.
3. Enter the code, a username (default `admin`) and a password (at least 8 characters). You are signed in
   as admin right away.

Setup closes for good once the admin exists. The code is then useless, and later starts don't create a new
one. Until then, every start of the container creates a new code and the previous one stops working. Only
someone who can read the container log can claim the admin account. Complete the setup right after the
first start anyway.

The admin's password is stored only as an argon2 hash in the database. Change it in **Settings → Password**:
this needs the current password and signs out all other sessions of the account.

**Forgot the password?** Set `SCANBOX_ADMIN_PASSWORD` (and `SCANBOX_ADMIN_USERNAME` if the account is not
called `admin`) in the compose file and restart: that sets the password. Remove the variable again and
restart. The account keeps the password, and you can change it in the app again.

**Upgrading from a version with a password file:** remove `SCANBOX_ADMIN_PASSWORD_FILE`, the
`scanbox_admin_password` secret and the file itself, then restart. The admin keeps its current password
(the hash is already in the database); change it in *Settings* from now on.

## 6. Synology SSO Server (OIDC)

1. Install **SSO Server** from the Package Center. Under *General* set the server URL, e.g. `https://sso.home.lan`. The issuer is then `https://sso.home.lan/webman/sso` (check `<issuer>/.well-known/openid-configuration` in a browser).
2. *Application* → *Add* → **OIDC**:
   * Application name `Scanbox`
   * Redirect URI **`https://scanbox.home.lan/api/auth/oidc/callback`** (must match `SCANBOX_BASE_URL` exactly)
3. Sign in to Scanbox as the local admin → **Admin → SSO**: enter the issuer, client ID and client secret (stored encrypted), then save. The page also shows the exact redirect URI to register. Changes take effect immediately; no restart needed. The *Synology SSO* button appears on the login page once issuer and client ID are set.
   (Alternatively pin them via the `SCANBOX_OIDC_*` variables; those fields are then read-only in the UI.)
4. Admin role: Scanbox makes a user admin when the claim *Admin claim* (default `groups`) contains one of the comma-separated values in *Admin value*. Whether Synology's SSO Server sends a `groups` claim depends on the DSM version and the user directory. If it doesn't, use a claim it does send, e.g. *Admin claim* `preferred_username` with *Admin value* `christian,anna`. Every SSO login writes the *names* of the received claims to the log (`auth.login … claims=[…]`, Admin → Diagnostics → download logs), so you can see which claim to use.
5. The local admin always works as a fallback (`/login` → username/password).

## 7. Scanner network settings

* Give the scanner a fixed IP (DHCP reservation), then enter its address in **Admin → *Scanner*** → *Test connection* → *Use this address*. The test shows the model, serial number and status; the address is saved and used immediately. Change it there whenever the scanner moves.
* The container talks to the scanner over HTTP on the LAN (bridge networking is fine; the scanner never connects to the container).
* The HP web UI (EWS) must have **eSCL / AirScan** enabled (usually on by default).

## 8. Backups and updates

* Back up `/volume1/docker/scanbox/data` (SQLite database + cache) with Hyper Backup. The DB uses WAL mode; snapshot backups are consistent. For file-level backups, stop the project first or back up `scanbox.db*` together.
* Update: Container Manager → *Image* → pull `latest` (or a version tag) → *Project* → *Action* → *Build/Restart*. Database migrations run automatically on start. Interrupted jobs are recovered: pages already received are kept, and processing and deliveries resume without duplicates.

## 9. Troubleshooting

| Symptom | Check |
|---|---|
| "Scanner offline" | Admin → Scanner → Test connection; is the IP still right? Can the NAS reach it: `curl http://<ip>/eSCL/ScannerStatus`? |
| SSO button → error | Admin → Diagnostics → recent errors (`oidc.*`). Redirect URI typo? Issuer without `/webman/sso`? Clock skew? |
| Setup code rejected | The container restarted since the code was logged: use the newest `auth.setup_required` entry. Too many wrong codes block setup for 15 minutes. |
| Live updates stall | Reverse proxy timeout / buffering (§4). The UI reconnects automatically and refetches. |
| Container unhealthy | `/readyz` failed: DB not writable (permissions of `/data`) or disk below the threshold. |
| Permission denied writing to a folder | `user:` UID/GID must have write access to the mounted share. |
