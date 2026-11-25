# Scanbox – Benutzerhandbuch

Scanbox ersetzt die HP-Scan-Software für den **HP ScanJet Pro N4000 snw1**. Du bedienst es im Browser,
am besten auf dem Handy oder Tablet, das du zum Scanner mitnimmst. Scanbox lässt sich als App installieren:
Browsermenü → „Zum Startbildschirm hinzufügen“ bzw. „App installieren“.

*English summary at the end.*

---

## 1. Anmelden

* **SSO** (z. B. Synology): Knopf „Anmelden mit …“ – falls eingerichtet.
* **Lokales Konto**: Benutzername und Passwort. Nach mehreren Fehlversuchen ist die Anmeldung einige Minuten
  gesperrt; die Meldung nennt die Wartezeit.
* Sprache (Deutsch/English) lässt sich schon auf der Anmeldeseite umschalten.
* **Erster Start**: Solange es kein Administratorkonto gibt, zeigt die Seite „Scanbox einrichten“. Den
  Einrichtungscode findet der NAS-Admin im Container-Log (Container Manager → Container → scanbox →
  Protokoll). Danach ist die Einrichtung geschlossen. Details: [DEPLOYMENT.md](DEPLOYMENT.md).

## 2. Scannen

Die Startseite **Scannen** zeigt oben den **Scannerstatus**:

| Anzeige | Bedeutung / was tun |
|---|---|
| Bereit – Papier eingelegt (grün) | Scan starten. |
| Bereit – kein Papier im Einzug (gelb) | Papier mit der Schrift nach unten, Oberkante zuerst einlegen. |
| Scanner beschäftigt (blau) | Ein anderer Scan läuft. Dein Scan wird in die **Warteschlange** gestellt (Position wird angezeigt). |
| Papierstau / Klappe offen / Mehrfacheinzug (rot) | Am Gerät beheben (Klappe öffnen, Blatt entfernen, schließen). Starten ist so lange gesperrt. |
| Scanner nicht erreichbar (grau) | Scanner einschalten / Netzwerk prüfen. Nach dem Aufwachen kann es bis zu einer Minute dauern. |
| Kein Scanner eingerichtet | Ein Admin muss unter *Admin → Scanner* die Adresse eintragen. |

So scannst du:

1. **Profil** wählen (das zuletzt benutzte ist vorausgewählt).
2. Optional einen **Titel** eingeben und „**QR-Wert in den Dateinamen übernehmen**“ an- oder abhaken.
3. Optional unter **Schnelleinstellungen** Einseitig/Beidseitig, Auflösung oder Farbe nur für diesen Scan ändern.
   Es werden nur Optionen angeboten, die der Scanner wirklich kann.
4. **Scan starten**. Ist der Knopf ausgegraut, steht direkt darunter der Grund.

Rechts (auf dem Handy darunter) erscheint der laufende Auftrag. Die **Seiten tauchen live** als
Vorschaubilder auf, sobald sie im Scanbox-Speicher liegen – was du siehst, ist sicher gespeichert.

### Mehrere Stapel

Der Einzug fasst 50 Blätter. Für größere Dokumente:

1. Ersten Stapel scannen.
2. **Weiteren Stapel scannen** tippen, nächsten Stapel einlegen – die Seiten werden angehängt.
3. Wenn alles drin ist: **Abschließen** (verarbeiten, Texterkennung, Zustellung).

Weitere Aktionen nach einem Stapel: **Prüfen / bearbeiten** (öffnet den Editor) und **Verwerfen**.

### Fehler beheben

Scanbox verliert keine Seiten. Bei einem Problem erscheint ein farbiger Kasten mit dem Grund und den
passenden Knöpfen, z. B.:

> **Papierstau – 2 Seite(n) gespeichert.** Stau beseitigen, nicht gescannte Blätter wieder einlegen, dann:
> **[Restliche Seiten scannen]** **[Mit vorhandenen Seiten abschließen]** **[Verwerfen]**

* **Kein Papier im Einzug**: Papier einlegen → *Erneut scannen* / *Restliche Seiten scannen*.
* **Scanner antwortet nicht / nicht erreichbar**: Gerät prüfen → *Erneut versuchen*.
* **Kein QR-Code gefunden** (wenn das Profil „Anhalten und nachfragen“ verlangt): *QR-Wert eingeben* oder
  *Ohne QR fortfahren*.
* **Zustellung fehlgeschlagen** (z. B. Mailserver nicht erreichbar): *Zustellung erneut versuchen* – ohne neu
  zu scannen. Automatische Wiederholungen laufen vorher schon im Hintergrund.
* Ist die Live-Verbindung unterbrochen, zeigt ein gelber Balken oben „Live-Verbindung unterbrochen“. Scanbox
  verbindet sich selbst neu und lädt dann alles frisch.

## 3. Dokument bearbeiten (Editor)

Öffnen über *Prüfen / bearbeiten*, über den Verlauf oder über einen Link im Auftrag.

**Seitenübersicht**

* **Antippen** einer Seite öffnet den Seiteneditor.
* **Auswählen** über das Kästchen oben rechts an jeder Seite (mehrere möglich, „Alle auswählen“).
* **Umsortieren**: am **Griff** (⋮⋮ unten rechts) ziehen – funktioniert mit Maus und Finger. Alternativ den Griff
  per Tab fokussieren und mit den **Pfeiltasten** verschieben, oder die Auswahl mit *Nach vorne* / *Nach hinten*
  bewegen.
* **Links/Rechts drehen** (90°) für alle ausgewählten Seiten.
* **Löschen / Wiederherstellen**: Gelöschte Seiten bleiben blass sichtbar und lassen sich zurückholen.
  Leere Seiten werden schon während des Scans erkannt und mit **Leer** markiert (blass). Sie werden beim
  Abschließen entfernt. Im Seiteneditor kannst du „Leere Seite trotzdem behalten“ wählen.
* **Teilen**: Seiten auswählen, an denen jeweils ein neues Dokument beginnen soll → *Teilen*.
* **Seiten einfügen**: Seiten aus einem anderen deiner Dokumente kopieren oder verschieben (werden angehängt).
* **Zusammenführen**: andere Dokumente an dieses anhängen; die Quelldokumente werden danach verworfen.

**Seiteneditor** (Vollbild)

* Ansicht **Ergebnis**, **Original** oder **Vergleich** (nebeneinander). Die Vorschau wird mit genau den
  Schritten der echten Verarbeitung erzeugt.
* **Ausrichtung** ±90°, **Feindrehung** per Schieberegler (oder automatisch begradigen).
* **Zuschnitt zeichnen**: Rechteck auf der Seite aufziehen, Ecken anpassen; oder automatischen Zuschnitt
  an/aus.
* **Farbmodus** (Farbe/Graustufen/Schwarzweiß), **Helligkeit**, **Kontrast**, **Schwellwert** (bei S/W),
  **Entrauschen**, **Schärfen**. „Wie Profil“ heißt: Profilvorgabe verwenden.
* **Speichern** übernimmt die Änderungen. Originale werden nie verändert – alles bleibt rückgängig zu machen.

Ist ein Dokument schon **fertig**, kannst du trotzdem bearbeiten und danach **Erneut verarbeiten**.

**Angaben**: Titel (mit OCR-**Vorschlag** zum Übernehmen), Dokumentdatum, Profil (bestimmt Verarbeitung,
Dateiname und Ziele beim Abschließen).

**Zustellung**: Status je Ziel, Fehlermeldung, *Erneut versuchen*; darunter die erzeugten Dateien zum
Herunterladen und der erkannte Text. **Verlauf** zeigt jeden Schritt mit Uhrzeit.

## 4. QR-Codes

Ein Profil kann einen **QR-Code** auslesen (z. B. eine Belegnummer oben rechts). Der Wert kann in den
Dateinamen (`{qr}`) und nach Paperless (ASN) übernommen werden.

Im Dokument zeigt das Feld **QR-Code**:

* den aktuellen Wert und seine Herkunft (erkannt / manuell / bestätigt: kein QR),
* ein Eingabefeld zum **Korrigieren** (*Speichern*) bzw. *Kein QR-Code*,
* *Erkennung anzeigen*: die erste Seite mit dem **Suchbereich** (blau gestrichelt) und allen gefundenen Codes
  (grün = gewählt, rot = weitere). *Übernehmen* setzt einen gefundenen Wert ein. Zum Testen kannst du einen
  anderen Bereich (gelb) aufziehen und *In diesem Bereich suchen* – das Profil ändert sich dadurch nicht.

**Suchbereich im Profil festlegen** (*Profile → Profil → QR-Code auslesen*): Vorlage *Oben rechts*,
*Oben links* oder *Ganze Seite* wählen – oder direkt auf der Beispielseite (dein letzter Scan) ein Rechteck
aufziehen. Der Bereich wird relativ zur Seite gespeichert und gilt für jede Seitengröße. Außerdem einstellbar:
welche Seiten durchsucht werden, welcher Code bei mehreren gilt, ein Muster (Regex) und was passiert, wenn
kein Code gefunden wird (weiter / Warnung / anhalten und nachfragen).

## 5. Eingang (Scans am Gerät)

Wenn der Admin „Scan am Gerät“ aktiviert hat, erscheint Scanbox im Display des Scanners. Solche Scans landen im
gemeinsamen **Eingang** – sichtbar für alle Benutzer (Zähler im Menü).

1. *Eingang* öffnen, Scan anhand von Uhrzeit/Seitenzahl erkennen.
2. **Übernehmen** – ab jetzt gehört der Scan dir und ist für andere nicht mehr sichtbar.
   Hat jemand anderes schneller übernommen, erscheint ein Hinweis.
3. Im Editor das passende **Profil** wählen, ggf. bearbeiten und **Abschließen**.

Nicht übernommene Scans laufen nach der Aufbewahrungsfrist ab.

## 6. Verlauf und Suche

*Verlauf* listet deine Dokumente (neueste zuerst, lädt beim Scrollen nach).

* **Volltextsuche** über Titel, Dateiname, QR-Wert, Profil und den erkannten Text.
* **Filter**: Status, Profil, Ziel, Zeitraum.
* Schnellaktionen: **Herunterladen**, **Erneut senden** (erzeugt abgelaufene Dateien neu aus den Originalen).
* Hinweise „Dateien abgelaufen“ / „Originale abgelaufen“ zeigen, was durch die Aufbewahrungsfrist schon
  gelöscht wurde.

## 7. Profile

Ein Profil bündelt alles, was beim Scannen und Verarbeiten passiert. Gemeinsame Profile (eingebaut oder vom
Admin freigegeben) kannst du **duplizieren** und dann anpassen. Passt ein Profil nicht zum Scanner (z. B.
nicht unterstützte Auflösung), zeigt Scanbox das in Gelb an.

* **Scannen**: Seiten (einseitig/beidseitig), Auflösung, Farbe (S/W wird empfohlen in Scanbox erzeugt),
  Papierformat, Zweck, Inhalt; erweiterte Scanner-Optionen (Leerseiten, Mehrfacheinzug, Helligkeit …) nur,
  wenn der Scanner sie kann.
* **Verarbeitung**: automatisch drehen, begradigen, zuschneiden, Leerseiten entfernen (Schwelle), Farbe,
  **Hintergrund bereinigen** (macht das Papier reinweiß: entfernt Grauschleier, Knicke, durchscheinende
  Rückseiten und dunkle Scanränder; *Automatisch* = an, außer bei Foto-Profilen; Stärke einstellbar,
  im Seiteneditor pro Seite abschaltbar), Entrauschen/Schärfen, automatisch an QR-Wechsel oder
  Trennblättern **teilen**.
* **Texterkennung & Ausgabe**: OCR (Deutsch/Englisch), Titelvorschlag, Format (PDF/A, PDF, JPEG, PNG, TIFF,
  Text), Komprimierung, Zielgröße.
* **Benennung**: Dateinamen-Vorlage mit Platzhaltern zum Antippen – `{date}` `{time}` `{datetime}` `{qr}`
  `{title}` `{counter}` `{profile}` `{user}` `{pages}`; Datumsformat z. B. `{date:%Y%m%d}`. Ein Beispiel wird
  live angezeigt.
* **Vor dem Verarbeiten prüfen**: aus = Scanbox schließt nach dem Scan automatisch ab.

## 8. Ziele (Destinations)

Pro Profil beliebig viele Ziele, die beim Abschließen beliefert werden:

* **Download** – Datei steht in Scanbox bereit.
* **NAS-Ordner** – Ausgabeordner (vom Admin angelegt) + optionaler Unterordner mit Platzhaltern,
  z. B. `{user}/{date:%Y}` oder `{year}/{month}/{qr}`. Es gibt dieselben Platzhalter wie beim Dateinamen;
  Werte mit `/` legen keine zusätzlichen Ebenen an. Bestehende Dateien werden nie überschrieben (`Name (2).pdf`).
  Ein unbekannter Ausgabeordner oder ein ungültiger Unterordner wird schon beim Speichern des Profils abgelehnt.
* **E-Mail** – Empfänger aus dem **Adressbuch** (Einstellungen) oder frei eingegeben, Betreff/Text-Vorlagen,
  Verhalten bei zu großen Anhängen (komprimieren, aufteilen, Fehler).
* **Paperless-ngx** – per API (Titel, Tags, Korrespondent, Dokumenttyp, QR → ASN) oder Consume-Ordner.
  Dafür deinen persönlichen **Paperless-Token** unter *Einstellungen* hinterlegen.

## 9. Einstellungen

Sprache, Design (System/Hell/Dunkel), Anzeigename, **Passwort** (nur lokale Konten: braucht das aktuelle
Passwort und meldet alle anderen Sitzungen ab), Adressbuch (Standard-Empfänger mit Haken), E-Mail-Vorlagen,
Paperless-Token (wird nur gespeichert, nie wieder angezeigt) und **API-Tokens**.

## 10. API-Tokens und Home Assistant

*Einstellungen → API-Tokens*: Namen eingeben, optional Gültigkeit in Tagen, **Erstellen**. Der Token wird
**nur einmal** angezeigt – sofort kopieren. Scanbox zeigt direkt ein passendes Beispiel.

```bash
curl -X POST https://scanbox.example.lan/api/scans \
  -H "Authorization: Bearer sbx_xxxx_yyyy" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(date +%s)" \
  -d '{"profile_id": 1}'
```

Home Assistant (`configuration.yaml`):

```yaml
rest_command:
  scanbox_scan:
    url: "https://scanbox.example.lan/api/scans"
    method: POST
    headers:
      Authorization: "Bearer sbx_xxxx_yyyy"
      Content-Type: "application/json"
    payload: '{"profile_id": 1}'
```

Danach z. B. einen Knopf/NFC-Tag mit der Aktion `rest_command.scanbox_scan` verknüpfen. Per API gestartete
Scans werden automatisch abgeschlossen und zugestellt; optional `"webhook_url"` für eine Rückmeldung angeben.
Tokens lassen sich jederzeit **widerrufen**.

## 11. Administration (nur Admins)

* **Scanner**: Adresse eintragen, **Verbindung testen** (zeigt Modell, Seriennummer, Status), *Diese Adresse
  verwenden*, Fähigkeiten neu laden.
* **SSO**: Issuer, Client-ID/-Secret, Scopes, Admin-Claim/-Wert. Die **Redirect-URI** wird zum Kopieren
  angezeigt.
* **E-Mail**: SMTP-Server, *Verbindung testen*, *Testmail senden*.
* **Paperless**: URL, Consume-Ordner, *Verbindung testen* (mit deinem eigenen Token).
* **Speicher**: Ausgabeordner (Name → Pfad), Aufbewahrungsfristen, OCR-Parallelität, Speicherbelegung,
  **Cache jetzt leeren** (mit Bestätigung).
* **Scan am Gerät**: an/aus, Profil, Name im Display, Status, Neustart.
* **Benutzer**: Admin-Recht und Sperre setzen. Admins sehen keine Dokumente anderer Benutzer.
* **Diagnose**: Werkzeuge, Warteschlangen, Hintergrundprozesse, letzte Fehler, Logs herunterladen.

Mit 🔒 **ENV** markierte Einstellungen sind per Umgebungsvariable (docker-compose) gesetzt und nur lesbar.
Passwörter/Secrets werden nie angezeigt, nur „gesetzt“/„nicht gesetzt“.

---

## English summary

* **Sign in** with SSO or the local account. Switch language on the login page or in *Settings*. On first
  start, *Set up Scanbox* creates the admin with the one-time code from the container log.
* **Password**: local accounts change it in *Settings* (needs the current password, signs out other sessions).
* **Scan**: the status card tells you whether the scanner is ready, busy (your scan is queued), offline or
  needs attention (jam, cover open, multi-feed). Pick a profile, optionally a title and quick overrides
  (sides, resolution, colour – only what the scanner supports), tap **Start scan**. Pages appear live as
  they are stored. After a stack: **Scan another stack**, **Review / edit**, **Finish** or **Discard**.
* **Errors never lose pages**: e.g. “Paper jam – 2 pages saved” with **Scan remaining pages** / **Finish with
  what we have**. Failed deliveries can be retried without rescanning.
* **Editor**: reorder by dragging the handle (mouse or touch) or with arrow keys, select pages to rotate,
  delete/restore, split, insert from or merge with other documents. The page editor offers rotation, fine
  deskew, crop drawing, auto-crop, colour mode, brightness/contrast/threshold, denoise/sharpen with
  before/after/compare preview. Originals are never modified.
* **QR codes**: see/correct the detected value, view the search zone and hits on the page; define the zone
  in the profile with presets or by drawing a rectangle.
* **Inbox**: walk-up scans started at the scanner are visible to everyone – **Claim** one, pick a profile,
  finish it.
* **History**: full-text search, filters (status, profile, destination, dates), download, send again.
* **Profiles & destinations**: scanning, processing, OCR, output, filename template with tokens and live
  example; destinations download, NAS folder, email (address book) and Paperless-ngx.
* **API tokens**: create in *Settings* (shown once), use `Authorization: Bearer …` with `POST /api/scans`;
  see the curl and Home Assistant examples above.
* **Admin**: scanner, SSO, SMTP, Paperless, storage/retention, walk-up, users, diagnostics. Settings marked
  ENV are set via environment variables and read-only.
