# Changelog

Alle nennenswerten Änderungen an mandari stehen hier, nach
[Keep a Changelog 1.1](https://keepachangelog.com/de/1.1.0/) und
[Semantic Versioning](https://semver.org/lang/de/). Regeln: `docs/RELEASE_POLITIK.md`.

## [Unreleased]

### Hinzugefügt
- Scraper-Quellen: robots-Sperren als Fehlerklasse `robots_blocked` im Betriebsmonitor mit Empfehlung; Download-Header je Quelle (`download_headers`, z. B. Referer/Cookie) für Dateicache und Textextraktion (#116).
- CSP-Report-Endpunkt `/csp-report/` mit Protokoll und Zähler `mandari_csp_violations_total` (#172).
- Getrennte Liveness- und Readiness-Prüfungen `/health/live/` und `/health/ready/` (#231).
- Verwaiste Konten werden nach Frist automatisch gelöscht (`cleanup_orphaned_accounts`, #238).
- Session-Einladung und Provisioning-Einladung im gemeinsamen Mail-Layout, „Einladung erneut senden“ (#239).
- Protokollkonzept mit journald, zeitbasierter Aufbewahrung und Überlaufwarnung (#263).
- Release- und Support-Politik sowie SLA-Entwurf (#96, #93).
- E2E-Tests der Editor-Kollaboration mit zwei Browsern gegen einen ASGI-Testserver (#289).

### Geändert
- Session-Portal in der axe-Prüfung der CI (Dashboard, Sitzungen, Vorlagen); Filter beschriftet, Kontrast von Seitenleiste, Datumsbadge und Toast-Schließen-Button behoben (#44, #176).
- Barrierefreiheit: Alpine-Modals mit `role=dialog`, `aria-modal`, Fokusfalle (`x-trap.inert.noscroll`) und Escape; axe-core in der CI zusätzlich auf Insight-Startseite, Dokumentenliste und Aufgaben; Prüfprotokoll `docs/BARRIEREFREIHEIT_PRUEFPROTOKOLL.md` (#176).
- Alle 80 Inline-Event-Handler in den Templates durch `data-*`-Aktionen ersetzt (Vorbereitung CSP-Enforce, #172).
- Elasticsearch-Indizes legt ausschließlich Django an; der Ingestor schreibt nur noch (#215).
- Upload-Prüfung nach Dateityp und Größe auf allen Upload-Pfaden mit CI-Gate (#260).
- Python 3.14, Node 26, pdfjs-dist 6 (#255, #254, #253).
- PostgreSQL-Grundeinstellungen werden mit der Compose-Datei ausgeliefert (#258).
- Datenbankverbindungen laufen über einen Pool (#257).

### Behoben
- Editor: Speichern ohne Kollaborationsverbindung überschreibt keinen neueren Stand mehr still; Konflikthinweis mit „Neu laden“ / „Trotzdem speichern“ (#184).
- Editor: Ein verbundener Client konnte nach der Reload-Aufforderung mit einem späten `yjs_save` den ohne Verbindung gespeicherten Stand überschreiben; Server und Client verwerfen ihn jetzt (#298).
- Fünf Lösch-Routen antworteten auf GET mit 500 statt 405; Federführende und Mitwirkende sahen ihre Anträge nicht (#249).

### Sicherheit
- Einheitliche Validierung hochgeladener Dateien; Nicht-Bild-Anhänge werden als Download ausgeliefert (GHSA-6p5c-wv4v-8g24, #260).

## [0.10.0] – in Vorbereitung (Tag folgt mit der Veröffentlichung des Advisories)

### Hinzugefügt
- Zwei-Faktor-Pflicht mit TOTP und WebAuthn, vertrauenswürdige Geräte, Sitzungsübersicht (#236).
- Selbstregistrierung mit E-Mail-Bestätigung und Freigabe durch die Organisation (#237).
- Kryptokonzept (#262), Skalierungs- und Verfügbarkeitskonzept, BPMN-Modell des Antragslaufs.

### Geändert
- Versionsangaben in `pyproject.toml`, `package.json` und Git-Tag werden gegeneinander geprüft.

### Sicherheit
- Alle offenen Abhängigkeitslücken und CodeQL-Meldungen geschlossen; `pip-audit` und `npm audit` blockieren in der CI (#241).

## [0.9.0-beta] – 2026-07-19

Erste öffentliche Beta: Bürgerportal (Insight) mit OParl-Ingestor, mandari work für
Fraktionen, mandari session für Verwaltungen.

[Unreleased]: https://github.com/mandariOSS/mandari/compare/v0.9.0-beta...dev
[0.10.0]: https://github.com/mandariOSS/mandari/compare/v0.9.0-beta...dev
[0.9.0-beta]: https://github.com/mandariOSS/mandari/releases/tag/v0.9.0-beta
