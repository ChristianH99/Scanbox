# Walk-up scan ("Scan to Computer") – research and protocol

Status: **prototype**. The protocol layer (`backend/scanbox/walkup/protocol.py`) is tested only against
a fake device. Before we wire it in, the owner has to run `tools/probe_scanner.py` against the real
N4000 (see [§9](#9-probe-tool-toolsprobe_scannerpy) and [§10](#10-what-the-owner-must-verify-on-the-n4000)).

Confidence tags used below:

| Tag | Meaning |
|---|---|
| **[C]** | Confirmed by a packet capture of HP's own software or by working open-source code that is used against real devices |
| **[D]** | Documented: a vendor or third-party doc says so, but we have no capture |
| **[G]** | Guessed or inferred by us. Must be verified with the probe tool |

## 1. Summary and recommendation

* The N4000's `ScannerCapabilities` has `<scan:WalkupScanSupport>`, so it supports HP's **eSCL walk-up
  subscription extension** [G, strong]. The OfficeJet Pro 8120e/8122e has exactly the same element
  (same child names, different values) and was captured using
  `POST /eSCL/WalkupSubscriptions` and `GET /eSCL/WalkupSubscriptions/{id}/Event?timeout=100` with HP's
  Windows "Scan to Computer" [C].
* The protocol is **pure client polling**:
  * Register: `POST` with HostIdentifier and up to `MaxScanShortcuts` shortcut labels.
  * Wait: long-poll `GET …/Event`.
  * Read the event: it carries the chosen shortcut and the settings from the panel.
  * Scan: a normal eSCL `POST /eSCL/ScanJobs` with `<scan:ContextID>` set to the subscription id.

  The scanner never connects to us, so this works from a Docker bridge network on a Synology NAS. [C]
* **Recommendation:** implement the **eSCL variant** as the primary path for the N4000. Keep the LEDM
  variants (WalkupScanToComp / WalkupScan) as auto-detected fallbacks for other HP models. They cost
  little and were tested against the same fake. Ship it behind `SCANBOX_WALKUP_ENABLED` (default
  off) until the probe log from the real device confirms §10.
* **WS-Scan (WSD) is rejected.** It delivers `ScanAvailableEvent` only by WS-Eventing push to a
  `NotifyTo` HTTP endpoint on the client, and it has no pull or polling mode (§6). That breaks the
  "no callbacks into the container" constraint.

Ranking for the N4000:

| Rank | Variant | Why |
|---|---|---|
| 1 | **eSCL `/eSCL/WalkupSubscriptions`** | Advertised in the N4000 caps (`WalkupScanSupport`, eSCL 2.9). Same generation as the 8122e capture. |
| 2 | LEDM `WalkupScanToComp` + `/EventMgmt/EventTable` | Common on HP MFPs 2011-2020. The N4000 (2022+) probably has no LEDM `DiscoveryTree.xml` [G]; the probe checks. |
| 3 | LEDM `WalkupScan` (1st gen) | Very old devices only. |
| – | WSD ScanAvailableEvent | Push-only. Rejected. |

## 2. Sources

| # | Source | What we took from it |
|---|---|---|
| S1 | node-hp-scan-to, `protocol_doc/HP Officejet Pro 8122e.md` – https://github.com/manuc66/node-hp-scan-to/blob/master/protocol_doc/HP%20Officejet%20Pro%208122e.md | **Capture** of HP Windows Scan-to-Computer over HTTPS: subscription POST/DELETE, event long-poll (204/200), the three event bodies, ScanJobs with `ContextID`, and caps with `WalkupScanSupport` |
| S2 | node-hp-scan-to `src/scanListeners/EsclWalkupScanListener.ts`, `src/DeviceClient.ts` (`registerEsclWalkupSubscription`, `getEsclWalkupEvent`, `probeEsclWalkup`, `isSubscriptionExpired`), `src/hpModels/EsclWalkup*.ts` – https://github.com/manuc66/node-hp-scan-to (commit 0901da5, Nov 2026) | Working client logic: GET probe (200/405 means supported, 404 means missing); 200 or 201 on POST; 204 means idle; 403/404/410 mean the subscription is gone; 503 handling with `Retry-After`; ContextID; shortcuts immutable |
| S3 | node-hp-scan-to `CHANGELOG.md` (Unreleased), `README.md` "Protocol Support" | eSCL walk-up added for the OfficeJet Pro 8120e/8122e. Those devices no longer serve `DiscoveryTree.xml`. HTTPS-only eSCL with a self-signed certificate. Some firmware answers 200 to a repeated POST. |
| S4 | node-hp-scan-to `src/listening.ts`, `src/hpModels/{Destination,EventTable,Event,WalkupScanToComp*}.ts`, `test/asset/*.xml`, `protocol_doc/HP Officejet 6500 E710n-z.md` | LEDM WalkupScanToComp and WalkupScan: registration XML, EventTable long-poll (`?timeout=<deciseconds>`, `If-None-Match`, 304), comp-event state machine, destination settings (Shortcut, ScanPlexMode). Recorded XML is reused (MIT, © 2022 Emmanuel Counasse, license in `backend/tests/fixtures/walkup/LICENSE-node-hp-scan-to`) under `backend/tests/fixtures/walkup/`. |
| S5 | Microsoft, "ScanAvailableEvent element" – https://learn.microsoft.com/en-us/windows-hardware/drivers/image/scanavailableevent | WSD scan events need a WS-Eventing `Subscribe` with `wse:NotifyTo`, and the device pushes `ScanAvailableEvent` to that address |
| S6 | `docs/fixtures/scanner-capabilities.xml` (our N4000) | `WalkupScanSupport` values; `dest` namespace `http://schemas.hp.com/imaging/destination/2011/06/06`; IconURI on `:8080` |

Searched without useful results: public mentions of `WalkupScanSupport`, `WalkupScanSubscription` or
`WalkupScanEvent` outside node-hp-scan-to. We also found nothing N4000/N4600/3600/2600-specific about
walk-up; HP's community forum has only "Scan to Computer doesn't work on Mac" threads. sane-airscan and
hplip contain no walk-up/scan-to-computer implementation that we know of [G]: sane-airscan is a
pull-only SANE backend, and hplip has no network scan-to-computer.

## 3. eSCL walk-up protocol (primary)

All paths are relative to the eSCL base URL: `http(s)://<scanner>[:port]`. The 8122e serves eSCL only
on HTTPS :443 with a self-signed certificate [C, S1/S3]. The N4000 advertises its icon on `http://…:8080`
[C, S6]. Which port and scheme serve `/eSCL/WalkupSubscriptions` on the N4000 is **unknown** (probe).

### 3.1 Capabilities: `<scan:WalkupScanSupport>`

```xml
<scan:WalkupScanSupport>
  <scan:MaxNetworkDestinations>10</scan:MaxNetworkDestinations>   <!-- 8122e: 10 -->
  <scan:InactivityTimeout>150</scan:InactivityTimeout>            <!-- 8122e: 240 -->
  <scan:SubscriptionTimeout>150</scan:SubscriptionTimeout>        <!-- 8122e: 3600 -->
  <scan:MaxScanShortcuts>20</scan:MaxScanShortcuts>               <!-- 8122e: 5 -->
  <scan:PINSupport>false</scan:PINSupport>
</scan:WalkupScanSupport>
```

| Element | Meaning | Confidence |
|---|---|---|
| presence | The device implements `/eSCL/WalkupSubscriptions` | [G, strong]: the 8122e has both the element and the endpoint |
| `MaxNetworkDestinations` | Max simultaneous host subscriptions (PCs) | [G]: same name as LEDM `wus:MaxNetworkDestinations`, which means exactly that |
| `MaxScanShortcuts` | Max `<Shortcut>` entries per subscription | [G, strong]: S1 notes "Up to 5 shortcuts are supported" on the 8122e, and HP's client registers exactly 5 |
| `SubscriptionTimeout` | Seconds a subscription survives **without the client polling** its `/Event`. The panel then drops the host. | [G]: HP's client polls with `timeout=100` (< 150 and < 3600). node-hp-scan-to ignores the value and re-registers on 403/404/410. **Verify with `--idle-test`.** |
| `InactivityTimeout` | Seconds the panel waits during a session (after HostSelected or between pages) before giving up | [G]: analogous to LEDM `UserActionTimeout` (60 s) |
| `PINSupport` | Optional PIN for the host entry | [G]. Not used; `false` on the N4000. |

The `dest` namespace in the N4000 caps (`http://schemas.hp.com/imaging/destination/2011/06/06`) is
declared but **unused** in the capabilities document. It probably belongs to HP's scan-to-destination
extension (network folder, e-mail, SharePoint shortcuts configured in the EWS). It is not needed for
scan-to-computer [G]. The 8122e event declares an `httpdest` namespace
(`http://schemas.hp.com/imaging/httpdestination/2011/10/13`) that it does not use either [C, S1].

### 3.2 Register: `POST /eSCL/WalkupSubscriptions` [C, S1]

Request (`Content-Type: text/xml`). The root element uses the eSCL namespace as **default** namespace,
exactly as HP's client sends it:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<WalkupScanSubscription xmlns="http://schemas.hp.com/imaging/escl/2011/05/03"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://schemas.hp.com/imaging/escl/2011/05/03 eSCL.xsd">
  <HostIdentifier>Scanbox</HostIdentifier>          <!-- text shown on the panel -->
  <Connection>Network</Connection>
  <Shortcuts>
    <Shortcut>Document</Shortcut>                  <!-- free-form labels, ≤ MaxScanShortcuts -->
    <Shortcut>Photo</Shortcut>
  </Shortcuts>
</WalkupScanSubscription>
```

Response:

```http
HTTP/1.1 201 Created
Location: /eSCL/WalkupSubscriptions/108b7a10-43ef-44b0-97ab-ea2f1274de50
Content-Length: 0
```

* The subscription id (last path segment) is also the `ContextID` used later [C, S1].
* Some firmware answers **200** plus the existing `Location` when the same host is already subscribed [D, S2 comment].
* **Shortcuts cannot be edited.** To change them, DELETE and POST again [C, S1 notes].
* `Location` may be absolute (`https://ip:443/...`); use the path only [C, S1 ScanJobs response].

### 3.3 Unregister: `DELETE /eSCL/WalkupSubscriptions/{id}` → `200 OK` [C, S1]

### 3.4 List: `GET /eSCL/WalkupSubscriptions`

node-hp-scan-to only uses this as a probe: **200 or 405 means supported, 404 means missing**, and
401/403 mean the device is booting or refusing [C, S2]. **The body format of a 200 is unknown** [G]. The
protocol module parses it leniently: any element with a `HostIdentifier` child plus a ResourceURI-like
child (synthetic example: `backend/tests/fixtures/walkup/escl_subscriptions_list_SYNTHETIC.xml`). If the
device answers 405, registration falls back to "delete the URI we stored last time, then POST".

### 3.5 Events: `GET /eSCL/WalkupSubscriptions/{id}/Event?timeout=<seconds>` [C, S1]

This is a long poll. HP's client uses `timeout=100`.

| Response | Meaning |
|---|---|
| `204 No Content` | No event within `timeout` |
| `200` + `<scan:WalkupScanEvent>` | One event (see below) |
| `403` | Stale id after a device reboot (8120e) [C, S2] |
| `404` / `410` | Subscription gone, on other firmware [D, S2] |
| `503` (+`Retry-After`) | Busy. node-hp-scan-to deletes and re-creates the subscription after 3 × 503 in a row [C, S2]. |

The user selects the computer on the panel:

```xml
<scan:WalkupScanEvent xmlns:dest="http://schemas.hp.com/imaging/httpdestination/2011/10/13"
    xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"
    xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03">
  <scan:ResourceURI>/eSCL/WalkupSubscriptions/108b7a10-…</scan:ResourceURI>
  <scan:HostIdentifier>Scanbox</scan:HostIdentifier>
  <scan:Connection>Network</scan:Connection>
  <scan:Event>HostSelected</scan:Event>
</scan:WalkupScanEvent>
```

The user picks a shortcut and presses Start:

```xml
<scan:WalkupScanEvent …>
  <scan:ResourceURI>/eSCL/WalkupSubscriptions/108b7a10-…</scan:ResourceURI>
  <scan:HostIdentifier>Scanbox</scan:HostIdentifier>
  <scan:Connection>Network</scan:Connection>
  <scan:Event>ScanRequested</scan:Event>
  <scan:Shortcut>Paperless</scan:Shortcut>
  <scan:ScanSettings>
    <pwg:Version>2.4</pwg:Version>
    <scan:XResolution>300</scan:XResolution>
    <scan:YResolution>300</scan:YResolution>
    <scan:Duplex>false</scan:Duplex>
    <scan:Brightness>4</scan:Brightness>
    <scan:ContentOrientation>Portrait</scan:ContentOrientation>
  </scan:ScanSettings>
</scan:WalkupScanEvent>
```

The session is finished (`<scan:Event>ScanPagesComplete</scan:Event>`, same envelope).

* The 8122e has no duplex ADF and no colour choice on the panel, so the capture shows only resolution,
  duplex, brightness and orientation. Whether the N4000 also sends `ColorMode`, `InputSource`,
  `DocumentFormatExt` or `Intent` (it has a colour/B&W and duplex choice) is **unknown** [G]. The parser
  keeps every leaf element in `WalkupScanRequest.extra`.
* Whether a second `ScanRequested` arrives in the same session (for example "scan more pages") on an
  ADF-only device is unknown [G]. The module handles it either way.

### 3.6 Scan job

This is a normal eSCL job, plus `ContextID` [C, S1]:

```xml
<scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                   xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.9</pwg:Version>
  <scan:ContextID>108b7a10-43ef-44b0-97ab-ea2f1274de50</scan:ContextID>  <!-- subscription id -->
  <scan:Intent>Document</scan:Intent>
  <pwg:InputSource>Feeder</pwg:InputSource>
  <scan:Duplex>true</scan:Duplex>
  <scan:XResolution>300</scan:XResolution> <scan:YResolution>300</scan:YResolution>
  <scan:ColorMode>RGB24</scan:ColorMode>
  <scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt>
</scan:ScanSettings>
```

`ContextID` probably ties the job to the panel session, so the panel shows progress and ends with
`ScanPagesComplete` [G]. HP's client always sends it, and node-hp-scan-to does the same. There is no
explicit "acknowledge" or "complete" call in this protocol [C, S1 contains none]. The session ends on
the device side after the job.

## 4. LEDM variants (fallback, other HP devices)

### 4.1 Discovery

`GET /DevMgmt/DiscoveryTree.xml` lists `<ledm:SupportedIfc>` entries [C, S4]:

* `ledm:hpLedmWalkupScanToCompManifest` → WalkupScanToComp (2nd gen)
* `hpCnxWalkupScanManifest` → WalkupScan (1st gen)
* `ledm:hpLedmEventMgmtManifest` → `/EventMgmt/EventTable`

A 200 on `GET /WalkupScanToComp/WalkupScanToCompCaps` also identifies 2nd gen. The caps contain
`MaxNetworkDestinations`, `SupportsMultiItemScanFromPlaten`, `UserActionTimeout` (seconds) and
`ShortcutSupport` (fixed set: SavePDF, SaveJPEG, EmailPDF, EmailJPEG, GeneralScan).

### 4.2 Register

`POST /WalkupScanToComp/WalkupScanToCompDestinations` → `201` + `Location` [C, S4]:

```xml
<WalkupScanToCompDestination xmlns="http://www.hp.com/schemas/imaging/con/ledm/walkupscan/2010/09/28">
  <Hostname xmlns="http://www.hp.com/schemas/imaging/con/dictionaries/2009/04/06">nas</Hostname>
  <Name xmlns="http://www.hp.com/schemas/imaging/con/dictionaries/1.0/">Scanbox</Name>
  <LinkType>Network</LinkType>
</WalkupScanToCompDestination>
```

1st gen is the same with root `WalkupScanDestination`, namespace
`http://www.hp.com/schemas/imaging/con/rest/walkupscan/2009/09/21`, posted to
`/WalkupScan/WalkupScanDestinations`. Destinations are listed with GET on the collection and removed with
DELETE on the item. The panel shows only the fixed device shortcuts; you cannot register custom labels.

### 4.3 Events

Long-poll `GET /EventMgmt/EventTable?timeout=<deciseconds>` with `If-None-Match: <ETag>`. The response
is `304` when nothing changed, or `200` + a new `ETag` [C, S4]. A `ScanEvent` for our destination looks
like this:

```xml
<ev:Event>
  <dd:UnqualifiedEventCategory>ScanEvent</dd:UnqualifiedEventCategory>
  <dd:AgingStamp>10-457</dd:AgingStamp>
  <ev:Payload><dd:ResourceURI>/WalkupScanToComp/WalkupScanToCompDestinations/ab60…</dd:ResourceURI>
              <dd:ResourceType>wus:WalkupScanToCompDestination</dd:ResourceType></ev:Payload>
  <ev:Payload><dd:ResourceURI>/WalkupScanToComp/WalkupScanToCompEvent</dd:ResourceURI>
              <dd:ResourceType>wus:WalkupScanToCompEvent</dd:ResourceType></ev:Payload>
</ev:Event>
```

The table also contains old events, so the first read only primes the cursor (the AgingStamps seen so
far). After that, `GET /WalkupScanToComp/WalkupScanToCompEvent` moves through `HostSelected` →
`ScanRequested` → (after a job) `ScanNewPageRequested` | `ScanPagesComplete`. node-hp-scan-to polls it
every 1 s for up to `UserActionTimeout` [C, S4]. The chosen shortcut and duplex mode are read from
`GET <destination URI>` → `WalkupScanToCompSettings/Shortcut` and `scantype:ScanSettings/dd:ScanPlexMode`
[C, S4]. In 1st gen, the `ScanEvent` alone means "scan requested".

## 5. End-to-end sequence (eSCL, as the background service should run it)

```
startup / flag enabled
  probe()                                  GET caps, GET /eSCL/WalkupSubscriptions, (LEDM GETs)
  register(name, host, shortcuts, known_uri=<stored>)
      ├─ list → same HostIdentifier & same shortcuts → reuse
      ├─ list → same name, other shortcuts → DELETE + POST
      └─ list unsupported (405) → DELETE known_uri (best effort) → POST
  persist dest.uri (for known_uri next start)

poll task (never blocks on scanning):
  loop:
    ev = poll_event(state, timeout = recommended_poll_timeout(support))   # 90 s on N4000
      None                → loop (204)
      HostSelected        → note "user at panel" (UI hint), loop
      ScanRequested       → req = requested_settings(ev); enqueue walk-up scan(req); loop
      ScanPagesComplete   → mark session done, loop
    WalkupSubscriptionLost → register(... known_uri=old) → new PollState
    WalkupDeviceBusy       → sleep(retry_after or 2 s); after 3 in a row → DELETE + register
    WalkupUnreachable/Timeout → back off 5 s → 10 → 30 → 60 s; when back: register() again

scanner lane (existing runner):
  POST /eSCL/ScanJobs with ContextID=req.context_id, settings mapped (§7) → NextDocument loop
  → pages into the shared inbox (owner: "walk-up", shortcut → profile)
```

Keep-alive and re-registration:

* **Poll continuously.** The long-poll is also the keep-alive. With SubscriptionTimeout = 150 s, use a
  90 s long-poll and re-issue it immediately (a gap of under a second).
* **Never pause polling while a scan runs.** A 50-page duplex batch takes several minutes, longer than
  150 s. Run the poller as its own asyncio task, separate from the scanner lane.
* Re-register on `WalkupSubscriptionLost`, after the device was unreachable, and as a safety net every
  `SubscriptionTimeout × 10` seconds. That last check is a GET list where supported; otherwise rely on
  the 403/404 from the next poll.
* Rate-limit the loop to at least 1 s between polls, in case a firmware returns 200 or 204 immediately.

## 6. Why not WS-Scan (WSD)?

WSD Scan's walk-up mechanism works like this: the client sends a WS-Eventing `Subscribe` with
`ScanDestinations` (display string + ClientContext). The device keeps the subscription (with `Expires`
and Renew) and **pushes** `ScanAvailableEvent` (ClientContext + ScanIdentifier) as a SOAP message to the
`wse:NotifyTo` address [D, S5]. WS-Eventing (2004/08), as used by WS-Scan, has no pull or polling delivery
mode, and the device would need to reach an HTTP server inside the container. That is impossible from
a Docker bridge network without host networking and a published port, and it violates the
"polling only" constraint. Discovery (WS-Discovery multicast) does not work from a bridge network either.
**Rejected.**

## 7. Mapping panel settings to eSCL ScanSettings

`requested_settings()` returns a `WalkupScanRequest`. Suggested mapping in the service:

| Panel / event field | `WalkupScanRequest` | eSCL ScanSettings | Rule |
|---|---|---|---|
| `Shortcut` (label we registered) | `shortcut` | – | **Selects the Scanbox profile**: register one shortcut per walk-up-enabled profile (≤ `MaxScanShortcuts`, 20 on the N4000). Unknown label → default walk-up profile. |
| `Duplex` / LEDM `ScanPlexMode` | `duplex` | `<scan:Duplex>` | Panel wins (physical choice). The N4000 has `AdfDuplexInputCaps`. |
| `XResolution`/`YResolution` | `x_resolution`/`y_resolution` | same | Panel wins if present **and** in the supported list; otherwise use the profile. |
| `ColorMode` (if sent) | `color_mode` | `<scan:ColorMode>` | Panel wins if present; `BlackAndWhite1` → apply the profile's `bw_strategy` (scan `Grayscale8`, threshold in software). |
| `InputSource` (if sent) | `input_source` | `<pwg:InputSource>` | The N4000 is ADF-only, so default to `Feeder`. |
| `DocumentFormatExt` / LEDM SavePDF/SaveJPEG | `document_format` | – | Ignored for the scan: Scanbox always requests JPEG pages. Can inform the profile's output type. |
| `Brightness`, `Contrast` | `brightness`, `contrast` | same | **Ignore.** The scales are device-specific (8122e 0–8; N4000 0–2000). Processing does enhancement. |
| `ContentOrientation`, `Intent` | `content_orientation`, `intent` | – / `<scan:Intent>` | Informational. |
| subscription id | `context_id` | `<scan:ContextID>` | **Always send.** |

## 8. Failure modes

| Failure | Detection | Handling |
|---|---|---|
| Subscription expiry (no poll within SubscriptionTimeout, for example a NAS suspend or a long GC pause) | Next poll → 403/404/410 → `WalkupSubscriptionLost` | Re-register with `known_uri`. The panel entry is missing until then. |
| Device reboot or power-save deep sleep | Transport errors (`WalkupUnreachable`/`WalkupTimeout`), then 403 on the old id [C, S2] | Back off, then re-register. Optionally DELETE the stale URI (ignore errors). |
| Device busy | 503 (+Retry-After) | Wait. After 3 consecutive 503s, DELETE + re-register: node-hp-scan-to observed `/Event` wedging until the subscription was re-created [C, S2]. |
| Duplicate registrations (container restart without cleanup) | Listing shows several entries with our name, or the panel shows duplicates | `register()` reuses or deletes entries with the same name. Without listing it deletes the stored `known_uri` first. On shutdown, call `unregister()` (best effort). |
| `MaxNetworkDestinations` full (10 incl. Windows PCs running HP Scan) | POST fails (status unknown [G]) | Show it in diagnostics ("scanner has no free destination slots"). Never delete other hosts' entries. |
| Multiple Scanbox instances with the same name | Both poll one subscription (if the firmware dedupes by HostIdentifier, it returns 200 + the same Location), so each event reaches only one of them at random | Use a unique `SCANBOX_WALKUP_NAME` per instance; document "one instance per scanner". |
| User waits too long / scanner lane busy | The panel times out after `InactivityTimeout` (150 s) [G] | If the scanner lane cannot start the job within ~InactivityTimeout, fail the walk-up request with a visible message and do not queue it indefinitely. |
| HTTPS with a self-signed certificate | TLS error | Admin option "accept the scanner certificate" (verify=False), ideally pinned by SHA-256 fingerprint as node-hp-scan-to does. |
| Event for an unknown shortcut | `shortcut` not in our list | Use the default walk-up profile and log it. |
| Unexpected or unparsable XML | `WalkupProtocolError` | Log the body (truncated), back off, continue. Never crash the app. |

## 9. Probe tool (`tools/probe_scanner.py`)

This is a standalone script that needs only Python 3.8+ and no packages. Run it from any computer on the
same LAN as the scanner.

```bash
# 1. read-only survey (safe; only HTTP GET)
python probe_scanner.py --scanner http://<scanner-ip>
#    add --insecure if the summary says the HTTPS certificate is not trusted
python probe_scanner.py --scanner http://<scanner-ip> --insecure

# 2. register a test destination, then use the scanner panel for 5 minutes
python probe_scanner.py --scanner http://<scanner-ip> --insecure \
       --register --name "Scanbox-Probe" --watch 300 --cleanup --redact

# 3. (optional) does the registration expire when nobody polls? (waits 200 s)
python probe_scanner.py --scanner http://<scanner-ip> --insecure \
       --register --idle-test 200 --watch 120 --cleanup

# 4. (optional) also run a real scan job after "Start" (pages counted; saved with --save-dir)
python probe_scanner.py --scanner http://<scanner-ip> --insecure \
       --register --watch 300 --scan-on-request --save-dir pages --cleanup
```

* It tries `http://host:80`, `http://host:8080` and `https://host:443`, plus the port you gave.
* For each endpoint, the log records status, headers, timing and the pretty-printed XML body.
* `--register` refuses names without "probe" in them.
* `--cleanup` deletes the registrations this run created (also on Ctrl+C), plus leftovers with exactly
  the same `--name` from earlier runs. It never touches other hosts' entries or device settings.
* `--redact` replaces the serial number, UUID and MAC values in the log file.
* `--shortcut LABEL` (repeatable) sets the eSCL shortcut labels. The default is `Document`, `Photo`.
* When it finishes, it prints a summary and the log path. **Send the `.log` file to the developer.**

During `--watch`, please do the following on the scanner and note what the display shows:

1. Open the scan-to-computer menu. Does `Scanbox-Probe` appear, and with which shortcuts?
2. Select it and cancel.
3. Select it, choose `Document`, keep the defaults and press Start (with paper in the ADF).
4. Repeat with duplex on, a different resolution and colour/grey/B&W, if the panel offers them.

## 10. What the owner must verify on the N4000

1. Which base URL serves eSCL and `/eSCL/WalkupSubscriptions` (http :80 / :8080 / https :443), and
   whether HTTPS needs `--insecure`.
2. `GET /eSCL/WalkupSubscriptions` status (200/405/404), and **the body format of a 200** before and
   after registering.
3. `POST` succeeds (201 vs 200, form of `Location`), and the entry **appears on the panel** with the
   registered shortcut labels. Note any label length or character limits.
4. The event sequence and bodies for: select+cancel, select+start, duplex, resolution and colour changes.
   In particular: which `ScanSettings` elements the N4000 sends, and whether `InputSource`/`ColorMode` are present.
5. Whether `ScanPagesComplete` arrives by itself, or only after a ScanJob with `ContextID`
   (`--scan-on-request`). What the panel shows when no job is started (timeout after ~150 s?).
6. **SubscriptionTimeout semantics** (`--idle-test 200`): is the registration gone after more than 150 s
   without polling, and does the panel entry disappear?
7. Behaviour of a second POST with the same `HostIdentifier` (duplicate entry, or 200 + same Location).
   Run the probe twice without `--cleanup`, then once with `--cleanup`.
8. Status code of event polls on a stale id after a scanner reboot (403/404/410).
9. Whether `/DevMgmt/DiscoveryTree.xml` or any LEDM endpoint exists at all (expected: no).
10. Results of the guessed `/eSCL/*Destinations`/`*Shortcuts` GETs, to see what the `dest` namespace belongs to.

## 11. Protocol module API (for the service)

`scanbox.walkup.protocol` (stdlib + httpx + lxml only):

* `HpWalkupClient(client: httpx.AsyncClient, base_url: str, *, timeout_s=10.0)`
  * `probe() -> WalkupSupport`: GET only. Sets `.support`.
  * `register(name, hostname, *, shortcuts=None, known_uri=None, variant=None) -> RegisteredDestination`
  * `list_destinations(variant=None) -> list[RegisteredDestination]`: eSCL raises `WalkupUnsupported` on 405.
  * `unregister(dest) -> None`: 403/404/410 count as success.
  * `poll_event(state: PollState, timeout_s) -> WalkupEvent | None`
  * `wait_for_scan_request(state, event, timeout_s) -> WalkupEvent | None`: HostSelected → ScanRequested.
  * `wait_for_next_page(state, event, timeout_s) -> WalkupEvent | None`: LEDM "scan another page?".
  * `requested_settings(event) -> WalkupScanRequest`
* `recommended_poll_timeout(support) -> int`: 90 s for the N4000.
* Exceptions: `WalkupError` > `WalkupUnsupported`, `WalkupProtocolError` (> `WalkupSubscriptionLost`,
  `WalkupDeviceBusy`), `WalkupUnreachable`, `WalkupTimeout`.

Tests: `backend/tests/test_walkup_protocol.py` runs against `backend/tests/walkup_fake.py`
(`create_walkup_fake(variant="escl"|"escl_nolist"|"ledm"|"ledm_walkupscan"|"none")`, with helpers
`select_host`, `start_scan`, `press_scan`, `request_new_page`, `complete_session`, `reboot`,
`fail_next_events`).
