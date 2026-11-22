#!/usr/bin/env python3
"""Scanbox walk-up probe for HP scanners (eSCL / LEDM). Standard library only.

Read-only by default. Run from any machine on the same LAN as the scanner:

    python probe_scanner.py --scanner http://192.168.1.50
    python probe_scanner.py --scanner http://192.168.1.50 --register --watch 300 --cleanup
    python probe_scanner.py --scanner https://192.168.1.50 --insecure --register --watch 300 --cleanup

What it does
  * default: GETs a list of known/candidate endpoints over HTTP :80, :8080 and
    HTTPS :443 and logs status, headers, timing and pretty-printed bodies.
  * --register: creates ONE clearly named test destination ("...Probe...").
  * --watch N: long-polls for walk-up events for N seconds while you press the
    buttons on the scanner; logs every event and the settings it carries.
  * --cleanup: removes only the destination(s) created by this tool (also on Ctrl+C).
It never changes any other device setting and never resets anything.

Send the resulting .log file to the developer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from xml.dom import minidom

VERSION = "1.0"
SCAN_NS = "http://schemas.hp.com/imaging/escl/2011/05/03"
PWG_NS = "http://www.pwg.org/schemas/2010/12/sm"
WUS_NS = "http://www.hp.com/schemas/imaging/con/ledm/walkupscan/2010/09/28"
WUSREST_NS = "http://www.hp.com/schemas/imaging/con/rest/walkupscan/2009/09/21"
DD_NS = "http://www.hp.com/schemas/imaging/con/dictionaries/1.0/"
DD3_NS = "http://www.hp.com/schemas/imaging/con/dictionaries/2009/04/06"

# (path, note). All GET. "guess" = not seen in any source, probed out of curiosity.
ENDPOINTS = [
    ("/eSCL/ScannerCapabilities", "eSCL caps (WalkupScanSupport)"),
    ("/eSCL/ScanCapabilities", "eSCL caps alt path"),
    ("/eSCL/ScannerStatus", "eSCL status"),
    ("/eSCL/eSCLConfig", "HP eSCL config (8122e capture)"),
    ("/eSCL/eSclManifest.xml", "LEDM-style eSCL manifest"),
    ("/eSCL/WalkupSubscriptions", "eSCL walk-up subscriptions (KEY)"),
    ("/eSCL/ScanJobs", "eSCL job collection"),
    ("/eSCL/ScanShortcuts", "guess"),
    ("/eSCL/Shortcuts", "guess"),
    ("/eSCL/Destinations", "guess (dest namespace)"),
    ("/eSCL/ScanDestinations", "guess"),
    ("/eSCL/WalkupScanDestinations", "guess"),
    ("/DevMgmt/DiscoveryTree.xml", "LEDM discovery tree"),
    ("/DevMgmt/ProductConfigDyn.xml", "LEDM product config"),
    ("/EventMgmt/EventMgmtManifest.xml", "LEDM event manifest"),
    ("/EventMgmt/EventTable", "LEDM event table"),
    ("/WalkupScanToComp/WalkupScanToCompManifest.xml", "LEDM ScanToComp manifest"),
    ("/WalkupScanToComp/WalkupScanToCompCaps", "LEDM ScanToComp caps"),
    ("/WalkupScanToComp/WalkupScanToCompDestinations", "LEDM ScanToComp destinations"),
    ("/WalkupScanToComp/WalkupScanToCompEvent", "LEDM ScanToComp event"),
    ("/WalkupScan/WalkupScanManifest.xml", "LEDM WalkupScan (1st gen) manifest"),
    ("/WalkupScan/WalkupScanDestinations", "LEDM WalkupScan (1st gen) destinations"),
    ("/Scan/ScanCaps", "LEDM scan caps"),
    ("/Scan/Status", "LEDM scan status"),
]

MAX_BODY_LOG = 64 * 1024


# --------------------------------------------------------------------------- logging


class Log:
    def __init__(self, path: str, redact: bool):
        self.path = path
        self.redact = redact
        self.secrets: set[str] = set()
        self.lines: list[str] = []
        self.t0 = time.monotonic()

    def _stamp(self) -> str:
        return f"[{datetime.now().strftime('%H:%M:%S')} +{time.monotonic() - self.t0:7.1f}s]"

    def file(self, text: str = "") -> None:
        for line in text.splitlines() or [""]:
            self.lines.append(f"{self._stamp()} {line}")

    def say(self, text: str = "") -> None:
        print(text, flush=True)
        self.file(text)

    def add_secret(self, value: str | None) -> None:
        if value and len(value.strip()) >= 4:
            self.secrets.add(value.strip())

    def write(self) -> None:
        text = "\n".join(self.lines) + "\n"
        if self.redact:
            for s in sorted(self.secrets, key=len, reverse=True):
                text = re.sub(re.escape(s), "<REDACTED>", text, flags=re.IGNORECASE)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)


# --------------------------------------------------------------------------- HTTP


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class Http:
    def __init__(self, log: Log, insecure: bool, timeout: float):
        self.log = log
        self.timeout = timeout
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=ctx))

    def request(self, method, url, body=None, headers=None, timeout=None, log_body=True, quiet=False):
        """Returns dict(status, headers, body, elapsed, error). Never raises."""
        hdrs = {"User-Agent": f"scanbox-probe/{VERSION}", "Cache-Control": "no-cache"}
        if body is not None:
            hdrs["Content-Type"] = "text/xml"
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        t = time.monotonic()
        res = {"status": None, "headers": {}, "body": b"", "elapsed": 0.0, "error": None}
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as r:
                res["status"] = r.status
                res["headers"] = dict(r.headers.items())
                res["body"] = r.read()
        except urllib.error.HTTPError as e:
            res["status"] = e.code
            res["headers"] = dict(e.headers.items()) if e.headers else {}
            try:
                res["body"] = e.read()
            except Exception:  # noqa: BLE001
                res["body"] = b""
        except (TimeoutError, socket.timeout):
            res["error"] = "timeout"
        except urllib.error.URLError as e:
            res["error"] = f"{type(e.reason).__name__}: {e.reason}"
        except (ConnectionError, OSError, ssl.SSLError) as e:
            res["error"] = f"{type(e).__name__}: {e}"
        res["elapsed"] = time.monotonic() - t
        self._log(method, url, hdrs, body, res, log_body, quiet)
        return res

    def _log(self, method, url, hdrs, body, res, log_body, quiet):
        status = res["status"] if res["status"] is not None else res["error"]
        summary = f"{method} {url} -> {status} ({res['elapsed'] * 1000:.0f} ms, {len(res['body'])} bytes)"
        (self.log.file if quiet else self.log.say)("  " + summary)
        self.log.file("    request headers: " + json.dumps({k: v for k, v in hdrs.items()}))
        if body:
            self.log.file("    request body:\n" + indent(pretty(body)))
        if res["headers"]:
            self.log.file("    response headers: " + json.dumps(res["headers"]))
        if log_body and res["body"]:
            self.log.file("    response body:\n" + indent(pretty(res["body"], res["headers"])))


def pretty(body: bytes, headers: dict | None = None) -> str:
    ctype = ""
    for k, v in (headers or {}).items():
        if k.lower() == "content-type":
            ctype = v.lower()
    if body[:3] == b"\xff\xd8\xff" or "image/" in ctype or "pdf" in ctype or "octet" in ctype:
        return f"<binary {len(body)} bytes, starts {body[:16].hex()}>"
    text = body[:MAX_BODY_LOG].decode("utf-8", "replace")
    if text.lstrip().startswith("<") and "html" not in ctype:
        try:
            out = minidom.parseString(body[:MAX_BODY_LOG]).toprettyxml(indent="  ")
            return "\n".join(line for line in out.splitlines() if line.strip())
        except Exception:  # noqa: BLE001
            pass
    if len(body) > MAX_BODY_LOG:
        text += f"\n<... truncated, {len(body)} bytes total>"
    return text


def indent(text: str, pad: str = "      ") -> str:
    return "\n".join(pad + line for line in text.splitlines())


def xml_root(body: bytes):
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        return None


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def find_local(root, name):
    return [el for el in root.iter() if local(el.tag) == name]


def text_local(root, name):
    for el in find_local(root, name):
        if el.text and el.text.strip():
            return el.text.strip()
    return None


def path_of(uri: str) -> str:
    p = urllib.parse.urlsplit(uri.strip())
    return p.path if p.scheme else uri.strip()


# --------------------------------------------------------------------------- probe


class Probe:
    def __init__(self, args, log: Log):
        self.a = args
        self.log = log
        self.http = Http(log, args.insecure, args.timeout)
        self.results: dict[str, dict[str, object]] = {}  # base -> path -> status
        self.created: list[dict] = []
        self.findings: list[str] = []
        self.walkup_support: dict[str, str] = {}
        self.events_seen: list[str] = []

    # ---- bases

    def bases(self) -> list[str]:
        u = urllib.parse.urlsplit(self.a.scanner if "://" in self.a.scanner else "http://" + self.a.scanner)
        host = u.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        out = []
        if u.port:
            out.append(f"{u.scheme}://{host}:{u.port}")
        out += [f"http://{host}:80", f"http://{host}:8080", f"https://{host}:443"]
        seen, uniq = set(), []
        for b in out:
            if b not in seen:
                seen.add(b)
                uniq.append(b)
        return uniq

    def reachable_bases(self) -> list[str]:
        self.log.say("\n== 1. Reachability ==")
        ok = []
        for base in self.bases():
            r = self.http.request("GET", base + "/eSCL/ScannerStatus", timeout=6)
            if r["status"] is not None:
                ok.append(base)
            elif base.startswith("https") and r["error"] and "CERTIFICATE" in r["error"].upper():
                self.findings.append(f"{base}: TLS certificate not trusted - rerun with --insecure to include HTTPS.")
        if not ok:
            self.findings.append("Scanner not reachable on :80, :8080 or :443. Check IP/hostname and that you are on the same LAN.")
        return ok

    # ---- read-only sweep

    def sweep(self, bases: list[str]) -> None:
        self.log.say("\n== 2. Read-only endpoint sweep ==")
        for base in bases:
            self.log.say(f"\n-- {base}")
            res: dict[str, object] = {}
            for path, note in ENDPOINTS:
                self.log.file(f"  ({note})")
                r = self.http.request("GET", base + path)
                res[path] = r["status"] if r["status"] is not None else r["error"]
                if r["status"] == 200:
                    self._learn(base, path, r["body"])
            # follow manifests from the discovery tree
            dt = self.http.request("GET", base + "/DevMgmt/DiscoveryTree.xml", quiet=True, log_body=False)
            root = xml_root(dt["body"]) if dt["status"] == 200 else None
            if root is not None:
                for el in find_local(root, "ManifestURI"):
                    p = path_of(el.text or "")
                    if p and p not in res:
                        r = self.http.request("GET", base + p)
                        res[p] = r["status"] if r["status"] is not None else r["error"]
            self.results[base] = res

    def _learn(self, base: str, path: str, body: bytes) -> None:
        root = xml_root(body)
        if root is None:
            return
        for name in ("SerialNumber", "UUID", "MacAddress", "HardwareAddress", "SerialNumberString"):
            for el in find_local(root, name):
                self.log.add_secret(el.text)
        if path in ("/eSCL/ScannerCapabilities", "/eSCL/ScanCapabilities"):
            mm = text_local(root, "MakeAndModel")
            if mm:
                self.findings.append(f"{base}: model {mm}, eSCL {text_local(root, 'Version')}")
            ws = find_local(root, "WalkupScanSupport")
            if ws:
                vals = {local(c.tag): (c.text or "").strip() for c in ws[0]}
                self.walkup_support = vals
                self.findings.append(f"{base}: WalkupScanSupport {vals}")

    # ---- variant selection

    def pick(self):
        """(variant, base) preferred for registration."""
        for base, res in self.results.items():
            if res.get("/eSCL/WalkupSubscriptions") in (200, 405):
                return "escl", base
        for base, res in self.results.items():
            if res.get("/WalkupScanToComp/WalkupScanToCompDestinations") == 200:
                return "ledm_comp", base
        for base, res in self.results.items():
            if res.get("/WalkupScan/WalkupScanDestinations") == 200:
                return "ledm_ws", base
        return None, None

    # ---- registration

    def register(self, variant: str, base: str) -> dict | None:
        self.log.say(f"\n== 3. Register test destination '{self.a.name}' ({variant} on {base}) ==")
        if variant == "escl":
            body = escl_subscription(self.a.name, self.a.shortcut)
            r = self.http.request("POST", base + "/eSCL/WalkupSubscriptions", body=body)
        else:
            body = ledm_destination(self.a.name, self.a.hostname, variant == "ledm_comp")
            path = "/WalkupScanToComp/WalkupScanToCompDestinations" if variant == "ledm_comp" else "/WalkupScan/WalkupScanDestinations"
            r = self.http.request("POST", base + path, body=body)
        loc = next((v for k, v in r["headers"].items() if k.lower() == "location"), None)
        if r["status"] in (200, 201) and loc:
            d = {"variant": variant, "base": base, "uri": path_of(loc), "status": r["status"]}
            self.created.append(d)
            self.log.say(f"  registered: {d['uri']} (HTTP {r['status']})")
            self.findings.append(f"Registration {variant}: HTTP {r['status']}, Location {loc}")
            self.log.say("  >>> Check the scanner display now: does the destination appear? (note it for the developer)")
            self.list_after(variant, base)
            return d
        self.findings.append(f"Registration {variant} FAILED: HTTP {r['status']} {r['error'] or ''}")
        self.log.say("  registration failed - see log")
        return None

    def list_after(self, variant: str, base: str) -> None:
        self.log.say("  listing registrations after registering (format capture):")
        path = {
            "escl": "/eSCL/WalkupSubscriptions",
            "ledm_comp": "/WalkupScanToComp/WalkupScanToCompDestinations",
            "ledm_ws": "/WalkupScan/WalkupScanDestinations",
        }[variant]
        self.http.request("GET", base + path)

    # ---- idle test

    def idle_test(self, d: dict, seconds: int) -> None:
        self.log.say(f"\n== 3b. Idle test: NOT polling for {seconds}s to see if the registration expires ==")
        self.log.say("  (look at the scanner display: does the destination disappear during this time?)")
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            time.sleep(min(30, left))
            self.log.say(f"  ... {max(0, int(end - time.monotonic()))}s left")
        if d["variant"] == "escl":
            r = self.http.request("GET", f"{d['base']}{d['uri']}/Event?timeout=1", timeout=20)
            alive = r["status"] in (200, 204)
        else:
            r = self.http.request("GET", f"{d['base']}{d['uri']}")
            alive = r["status"] == 200
        self.findings.append(f"Idle test {seconds}s without polling: registration {'ALIVE' if alive else 'GONE'} (HTTP {r['status']})")
        self.list_after(d["variant"], d["base"])

    # ---- watch

    def watch(self, d: dict, seconds: int) -> None:
        self.log.say(f"\n== 4. Watching events for {seconds}s ==")
        self.log.say("  NOW: on the scanner, choose the computer/destination "
                     f"'{self.a.name}', pick a shortcut, change settings (resolution, duplex, colour) and press Start.")
        self.log.say("  Please try: (a) select + cancel, (b) select + start simplex, (c) duplex/colour change. Ctrl+C to stop early.")
        if d["variant"] == "escl":
            self._watch_escl(d, seconds)
        else:
            self._watch_ledm(d, seconds)

    def _watch_escl(self, d: dict, seconds: int) -> None:
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 1:
            t = int(min(60, max(1, left)))
            r = self.http.request("GET", f"{d['base']}{d['uri']}/Event?timeout={t}", timeout=t + 20)
            st = r["status"]
            if st == 204:
                continue
            if st == 200:
                root = xml_root(r["body"])
                ev = text_local(root, "Event") if root is not None else None
                sc = text_local(root, "Shortcut") if root is not None else None
                settings = {}
                if root is not None:
                    for ss in find_local(root, "ScanSettings"):
                        settings = {local(c.tag): (c.text or "").strip() for c in ss.iter() if len(c) == 0 and c is not ss}
                self.events_seen.append(f"{ev} shortcut={sc} settings={settings}")
                self.log.say(f"  EVENT {ev} shortcut={sc} settings={settings}")
                if ev == "ScanRequested" and self.a.scan_on_request:
                    self.scan_job(d, settings)
                continue
            if st in (403, 404, 410):
                self.findings.append(f"Event poll answered {st}: subscription lost during watch; re-registering")
                self.log.say(f"  subscription lost (HTTP {st}) - re-registering")
                new = self.register("escl", d["base"])
                if not new:
                    return
                d.update(new)
                continue
            wait = 2.0
            ra = next((v for k, v in r["headers"].items() if k.lower() == "retry-after"), None)
            if ra and ra.isdigit():
                wait = min(60, int(ra))
            self.log.say(f"  unexpected {st or r['error']} - waiting {wait}s")
            time.sleep(wait)

    def _watch_ledm(self, d: dict, seconds: int) -> None:
        base = d["base"]
        r = self.http.request("GET", base + "/EventMgmt/EventTable")
        etag = next((v for k, v in r["headers"].items() if k.lower() == "etag"), "")
        seen = set()
        root = xml_root(r["body"]) if r["status"] == 200 else None
        if root is not None:
            seen = {text_local(e, "AgingStamp") for e in find_local(root, "Event")}
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 1:
            ds = int(min(600, max(10, left * 10)))
            hdr = {"If-None-Match": etag} if etag else {}
            r = self.http.request("GET", f"{base}/EventMgmt/EventTable?timeout={ds}", headers=hdr, timeout=ds / 10 + 20)
            if r["status"] == 304:
                continue
            if r["status"] != 200:
                self.log.say(f"  event table {r['status'] or r['error']}")
                time.sleep(2)
                continue
            etag = next((v for k, v in r["headers"].items() if k.lower() == "etag"), etag)
            root = xml_root(r["body"])
            if root is None:
                continue
            for e in find_local(root, "Event"):
                stamp = text_local(e, "AgingStamp")
                if stamp in seen:
                    continue
                seen.add(stamp)
                cat = text_local(e, "UnqualifiedEventCategory")
                payloads = [(text_local(p, "ResourceURI"), text_local(p, "ResourceType")) for p in find_local(e, "Payload")]
                self.log.say(f"  EVENT {cat} {stamp} {payloads}")
                self.events_seen.append(f"{cat} {payloads}")
                if cat != "ScanEvent":
                    continue
                comp = next((path_of(u) for u, t in payloads if u and t and "CompEvent" in t), None)
                self.http.request("GET", base + d["uri"])
                if comp:
                    last = None
                    for _ in range(90):
                        cr = self.http.request("GET", base + comp, quiet=True)
                        croot = xml_root(cr["body"]) if cr["status"] == 200 else None
                        ct = text_local(croot, "WalkupScanToCompEventType") if croot is not None else None
                        if ct != last:
                            self.log.say(f"    comp event -> {ct}")
                            self.events_seen.append(f"comp {ct}")
                            if ct in ("ScanRequested", "ScanNewPageRequested"):
                                self.http.request("GET", base + d["uri"])  # settings now present?
                            last = ct
                        if ct in ("ScanPagesComplete", None):
                            break
                        time.sleep(1)

    # ---- optional scan job (eSCL)

    def scan_job(self, d: dict, settings: dict) -> None:
        self.log.say("  --scan-on-request: running an eSCL scan job with the requested settings")
        ctx = d["uri"].rstrip("/").rsplit("/", 1)[-1]
        res = settings.get("XResolution", "300")
        dup = settings.get("Duplex", "false")
        color = settings.get("ColorMode", "RGB24")
        src = settings.get("InputSource", "Feeder")
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="{SCAN_NS}" xmlns:pwg="{PWG_NS}">
  <pwg:Version>2.9</pwg:Version>
  <scan:ContextID>{ctx}</scan:ContextID>
  <scan:Intent>Document</scan:Intent>
  <pwg:InputSource>{src}</pwg:InputSource>
  <scan:Duplex>{dup}</scan:Duplex>
  <scan:ColorMode>{color}</scan:ColorMode>
  <scan:XResolution>{res}</scan:XResolution>
  <scan:YResolution>{settings.get('YResolution', res)}</scan:YResolution>
  <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
  <scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt>
</scan:ScanSettings>""".encode()
        r = self.http.request("POST", d["base"] + "/eSCL/ScanJobs", body=body)
        loc = next((v for k, v in r["headers"].items() if k.lower() == "location"), None)
        if r["status"] != 201 or not loc:
            self.findings.append(f"Scan job after walk-up request failed: HTTP {r['status']}")
            return
        job = path_of(loc)
        pages, total, busy = 0, 0, 0
        while busy < 120:
            pr = self.http.request("GET", d["base"] + job + "/NextDocument", timeout=90, log_body=False)
            if pr["status"] == 200:
                pages += 1
                total += len(pr["body"])
                if self.a.save_dir:
                    os.makedirs(self.a.save_dir, exist_ok=True)
                    with open(os.path.join(self.a.save_dir, f"page-{int(time.time())}-{pages}.jpg"), "wb") as fh:
                        fh.write(pr["body"])
                busy = 0
                continue
            if pr["status"] == 503:
                busy += 1
                time.sleep(1)
                continue
            break
        self.http.request("GET", d["base"] + "/eSCL/ScannerStatus")
        self.findings.append(f"Scan job with ContextID: {pages} page(s), {total} bytes")
        self.log.say(f"  scan job done: {pages} page(s), {total} bytes. Watch the display: does it show 'complete'?")

    # ---- cleanup

    def cleanup(self) -> None:
        self.log.say("\n== 5. Cleanup ==")
        for d in list(self.created):
            r = self.http.request("DELETE", d["base"] + d["uri"])
            self.log.say(f"  removed {d['uri']}: HTTP {r['status'] or r['error']}")
            self.created.remove(d)
        # leftovers of earlier probe runs with exactly the same (probe) name
        for base, res in self.results.items():
            for path, key in (
                ("/eSCL/WalkupSubscriptions", "HostIdentifier"),
                ("/WalkupScanToComp/WalkupScanToCompDestinations", "Name"),
                ("/WalkupScan/WalkupScanDestinations", "Name"),
            ):
                if res.get(path) != 200:
                    continue
                r = self.http.request("GET", base + path, quiet=True)
                root = xml_root(r["body"]) if r["status"] == 200 else None
                if root is None:
                    continue
                for el in root.iter():
                    names = [c for c in el if local(c.tag) == key]
                    if not names or (names[0].text or "").strip() != self.a.name:
                        continue
                    uri = next((c.text for c in el if local(c.tag) in ("ResourceURI", "SubscriptionURI")), None)
                    if uri:
                        r2 = self.http.request("DELETE", base + path_of(uri))
                        self.log.say(f"  removed leftover '{self.a.name}' {path_of(uri)}: HTTP {r2['status'] or r2['error']}")


def escl_subscription(name: str, shortcuts: list[str]) -> bytes:
    sc = "".join(f"\n\t\t<Shortcut>{esc(s)}</Shortcut>" for s in shortcuts)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<WalkupScanSubscription xmlns="{SCAN_NS}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:schemaLocation="{SCAN_NS} eSCL.xsd">\n'
        f"\t<HostIdentifier>{esc(name)}</HostIdentifier>\n\t<Connection>Network</Connection>\n"
        f"\t<Shortcuts>{sc}\n\t</Shortcuts>\n</WalkupScanSubscription>\n"
    ).encode()


def ledm_destination(name: str, hostname: str, to_comp: bool) -> bytes:
    ns, root, xsd = (WUS_NS, "WalkupScanToCompDestination", "WalkupScanToComp.xsd") if to_comp else (
        WUSREST_NS, "WalkupScanDestination", "WalkupScanDestinations.xsd")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<{root} xmlns="{ns}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="{ns} {xsd}">\n'
        f'\t<Hostname xmlns="{DD3_NS}">{esc(hostname)}</Hostname>\n'
        f'\t<Name xmlns="{DD_NS}">{esc(name)}</Name>\n'
        f"\t<LinkType>Network</LinkType>\n</{root}>\n"
    ).encode()


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- main


INTRO = """
Scanbox walk-up probe {ver}
==========================
This tool talks to your scanner at {scanner} and records what it answers.
* Default mode is READ-ONLY (HTTP GET only).
* --register adds ONE test destination named '{name}' to the scanner panel;
  --cleanup removes it again (also on Ctrl+C). Nothing else is changed.
* Log file: {out}
"""


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Probe an HP scanner for walk-up (scan-to-computer) support.")
    p.add_argument("--scanner", required=True, help="http://<ip-or-host>[:port] (https://... also works)")
    p.add_argument("--out", default=None, help="log file (default probe-<timestamp>.log)")
    p.add_argument("--insecure", action="store_true", help="accept the scanner's self-signed HTTPS certificate")
    p.add_argument("--timeout", type=float, default=10.0, help="per-request timeout for normal calls (s)")
    p.add_argument("--register", action="store_true", help="create a test destination")
    p.add_argument("--name", default="Scanbox-Probe", help="test destination name (must contain 'probe')")
    p.add_argument("--hostname", default=socket.gethostname(), help="hostname sent in LEDM registrations")
    p.add_argument("--shortcut", action="append", default=None, help="eSCL shortcut label (repeatable)")
    p.add_argument("--watch", type=int, default=0, metavar="N", help="poll events for N seconds (needs --register)")
    p.add_argument("--idle-test", type=int, default=0, metavar="S",
                   help="after registering, do NOT poll for S seconds, then check if the registration survived")
    p.add_argument("--scan-on-request", action="store_true",
                   help="on ScanRequested, run an eSCL scan job (pages are counted, not kept unless --save-dir)")
    p.add_argument("--save-dir", default=None, help="save pages from --scan-on-request here")
    p.add_argument("--cleanup", action="store_true", help="remove destinations created by this tool when done")
    p.add_argument("--redact", action="store_true", help="redact serial number / UUID / MAC in the log file")
    a = p.parse_args(argv)
    a.shortcut = a.shortcut or ["Document", "Photo"]
    if (a.register or a.cleanup) and "probe" not in a.name.lower():
        p.error("--name must contain 'probe' so the test destination is clearly recognisable")
    if a.watch and not a.register:
        p.error("--watch requires --register")
    out = a.out or f"probe-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    log = Log(out, a.redact)
    log.say(INTRO.format(ver=VERSION, scanner=a.scanner, name=a.name, out=out))
    log.file(f"python {sys.version.split()[0]} on {sys.platform}; args: {vars(a)}; run id {uuid.uuid4()}")
    pr = Probe(a, log)
    try:
        bases = pr.reachable_bases()
        if bases:
            pr.sweep(bases)
            variant, base = pr.pick()
            if variant is None:
                pr.findings.append("No walk-up registration endpoint found (eSCL WalkupSubscriptions / LEDM).")
            else:
                pr.findings.append(f"Preferred walk-up variant: {variant} at {base}")
            if a.register and variant:
                d = pr.register(variant, base)
                if d and a.idle_test:
                    pr.idle_test(d, a.idle_test)
                if d and a.watch:
                    pr.watch(d, a.watch)
    except KeyboardInterrupt:
        log.say("\nInterrupted (Ctrl+C).")
    finally:
        try:
            if a.cleanup:
                pr.cleanup()
        except KeyboardInterrupt:
            log.say("cleanup interrupted")
        summary(pr, log, a)
        log.write()
        print(f"\nLog written to: {os.path.abspath(out)}")
    return 0


def summary(pr: Probe, log: Log, a) -> None:
    log.say("\n== Summary ==")
    for base, res in pr.results.items():
        key = {k: v for k, v in res.items() if "Walkup" in k or "Capabilities" in k or "Discovery" in k or "EventTable" in k}
        log.say(f"  {base}: {key}")
    for f in pr.findings:
        log.say(f"  * {f}")
    if pr.events_seen:
        log.say("  events seen:")
        for e in pr.events_seen:
            log.say(f"    - {e}")
    if pr.created:
        log.say(f"  !! test destination still registered: {[d['uri'] for d in pr.created]}")
        log.say("     rerun with --cleanup (same --name) to remove it, or let it expire.")
    log.say("\nPlease send the log file to the developer. Thank you!")


if __name__ == "__main__":
    sys.exit(main())
