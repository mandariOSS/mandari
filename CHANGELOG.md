# Changelog

Alle nennenswerten Änderungen an mandari stehen hier, nach
[Keep a Changelog 1.1](https://keepachangelog.com/de/1.1.0/) und
[Semantic Versioning](https://semver.org/lang/de/). Regeln: `docs/RELEASE_POLITIK.md`.

## [Unreleased]

### Hinzugefügt
- Öffentliche Demo-Instanz (Schalter `DEMO_INSTANCE`, #99): kein Mailversand, keine KI-Aufrufe, Passwort, zweiter Faktor, Registrierung und Kontolöschung gesperrt, Hinweis auf jeder Seite; feste Zugangsdaten über `DEMO_PASSWORD` nur in der Demo-Instanz.
- Präsentationsumgebung `setup_demo_praesentation` (Profile `nrw`/`hamburg`): eine Drucksache von der Fraktion über den Sitzungsdienst bis ins Bürgerportal und die OParl-Schnittstelle – zweiter Demo-Mandant mit eigener Nummernfolge, Leitstelle mit Mandantenwechsel, verbundene Einreichung Work → Session, Beratungsfolge, Ö/NÖ-Tagesordnung, Abstimmung, Protokoll, Beschlusskontrolle und Sitzungsgeld im Vier-Augen-Prinzip; Mandant A wird im Prozess ins Bürgerportal gespiegelt, ein erneuter Lauf setzt die Probe zurück, `--reset` räumt auf; E2E-Test des Drehbuchs (`docs/DEMO_PRAESENTATION.md`).
- Session: Nummernkreise für Vorlagen und Drucksachen mit Mustern (`{wp}-{lfd:4}`, `V/{lfd:4}/{jahr}`, `AN/…`), Zählerbereich je Jahr oder Wahlperiode, Vergabe beim Anlegen oder bei der Freigabe, Unternummern für Ergänzung/Neufassung/Antwort (`22-0593.1`), Presets für Hamburger Bezirke und NRW-Kommunen, Startwert für den Umstieg aus Altsystemen; Bezeichnung „Drucksache“/„Vorlagen-Nr.“ je Mandant (`docs/SESSION_NUMMERNKREISE.md`, #150).
- Session: Mandantenwechsel in der Seitenleiste für Nutzer mehrerer Mandanten (z. B. Leitstelle für mehrere Bezirke); Verwaltungsnutzer landen nach dem Login direkt in ihrem Mandanten.
- CI-Prüfungen gegen verwaiste Template-Blöcke und Alpine-Komponenten ohne Definition (`scripts/check_template_blocks.py`, `scripts/check_alpine_components.py`).
- Betriebsmonitor bewertet Scraper-Quellen nach Parse-Quote und Entitäten-Zufluss des letzten Laufs, Alarm über `check_source_health` (#53).
- Insight Geo-Verortung: Hausnummern-Punkte aus OSM (`import_streets --with-addresses`), Umkreissuche über eine indexierte Verortungstabelle statt JSONB-Vollscan (`backfill_paper_locations`), Nachbarschafts-Autocomplete aus dem eigenen Straßenverzeichnis, Admin-Korrektur „bestätigen/entfernen“ mit Sperre gegen Wiederanlage, `check_body_geodata` für Kommunen ohne OSM-Zuordnung (#54).
- Scraper-Quellen: robots-Sperren als Fehlerklasse `robots_blocked` im Betriebsmonitor mit Empfehlung; Download-Header je Quelle (`download_headers`, z. B. Referer/Cookie) für Dateicache und Textextraktion (#116).
- Compose-Rollenprofile data/web/worker für den Mehr-Server-Betrieb, Singleton-Sperren für Cron-Jobs, Orchestrator und Ingestor-Daemon, optional PgBouncer (#55).
- CSP-Report-Endpunkt `/csp-report/` mit Protokoll und Zähler `mandari_csp_violations_total` (#172).
- Getrennte Liveness- und Readiness-Prüfungen `/health/live/` und `/health/ready/` (#231).
- Verwaiste Konten werden nach Frist automatisch gelöscht (`cleanup_orphaned_accounts`, #238).
- Session-Einladung und Provisioning-Einladung im gemeinsamen Mail-Layout, „Einladung erneut senden“ (#239).
- Protokollkonzept mit journald, zeitbasierter Aufbewahrung und Überlaufwarnung (#263).
- Release- und Support-Politik sowie SLA-Entwurf (#96, #93).
- E2E-Tests der Editor-Kollaboration mit zwei Browsern gegen einen ASGI-Testserver (#289).

### Geändert
- Dokument-Cache nur noch für gelistete Kommunen: `cache_files` und der Datei-Proxy speichern Dokumente ausgeblendeter Kommunen (Piloten, Tests) nicht mehr; `prune_file_cache --unlisted` räumt deren Bestand ab. Extrahierte Texte bleiben erhalten.
- Insight Geo-Verortung: Gebiete oberhalb der Gemeinde (Regierungsbezirk, Kreis – erkennbar am amtlichen Schlüssel mit weniger als acht Stellen, z. B. `053`) brauchen nur Grenze und Kartenausschnitt; `import_streets` überspringt sie, `check_body_geodata` zeigt die Ebene (#54).
- Abhängigkeiten gesammelt aktualisiert: Biome 2.5 (Konfiguration migriert, Importe neu sortiert), esbuild 0.28, Alpine.js 3.17.4, django-vite 3.2, css-inline 0.21.3, django-stubs 6.1.1; Ingestor: SQLAlchemy 2.0.54, pypdf 6.19, APScheduler 3.11.3, idna 3.20, pytest-asyncio 1.4. Untergrenzen in `requirements.txt` an das Lockfile angeglichen.
- Demo-Umgebung: `setup_demo_environment --reset` entfernt eine aufgesetzte Präsentationsumgebung mit (sonst bliebe der Bürgerportal-Spiegel als aktive Quelle ohne Mandant zurück); `docs/DEMO_ENVIRONMENT.md` an den Code angeglichen (vier Verwaltungsnutzer, Kommune nicht gelistet).
- Session: Ö/NÖ wirkt sofort – ein TOP, eine Vorlage oder Anlage, die nicht-öffentlich wird, verschwindet im selben Moment aus dem Bürgerportal und zeigt dort keinen Inhalt mehr; Entwürfe erscheinen erst nach der Freigabe in der OParl-API; nicht-öffentliche Unterpunkte oder Vorlagen auf öffentlichen TOPs werden abgewiesen.
- Django 6.1: Mailversand auf `MAILERS` umgestellt; SMTP-Zugänge aus SiteSettings und organisationseigenes SMTP werden zur Laufzeit als Backend aufgebaut (`apps/common/mail_backends.py`), die Umgebungsvariablen `EMAIL_*` bleiben (#80).
- Session-Portal in der axe-Prüfung der CI (Dashboard, Sitzungen, Vorlagen); Filter beschriftet, Kontrast von Seitenleiste, Datumsbadge und Toast-Schließen-Button behoben (#44, #176).
- Barrierefreiheit: Alpine-Modals mit `role=dialog`, `aria-modal`, Fokusfalle (`x-trap.inert.noscroll`) und Escape; axe-core in der CI zusätzlich auf Insight-Startseite, Dokumentenliste und Aufgaben; Prüfprotokoll `docs/BARRIEREFREIHEIT_PRUEFPROTOKOLL.md` (#176).
- htmx ohne Inline-JavaScript: `hx-on`-Handler durch `data-autosave`/`data-after-request` ersetzt, `allowEval` und Fremdanfragen abgeschaltet (Vorbereitung CSP ohne `unsafe-eval`, #172).
- Alle 80 Inline-Event-Handler in den Templates durch `data-*`-Aktionen ersetzt (Vorbereitung CSP-Enforce, #172).
- Elasticsearch-Indizes legt ausschließlich Django an; der Ingestor schreibt nur noch (#215).
- Upload-Prüfung nach Dateityp und Größe auf allen Upload-Pfaden mit CI-Gate (#260).
- Python 3.14, Node 26, pdfjs-dist 6 (#255, #254, #253).
- PostgreSQL-Grundeinstellungen werden mit der Compose-Datei ausgeliefert (#258).
- Datenbankverbindungen laufen über einen Pool (#257).

### Behoben
- `extract_locations` und `extract_texts` geben die Datenbankverbindungen ihrer Worker-Threads zurück und laden die Vorgänge stapelweise; bisher war der Pool nach fünf Stapeln leer, und große Kommunen sprengten das Speicherlimit (#54).
- `check_body_geodata` zählt Straßen und Adressen getrennt; der gemeinsame Join erzeugte ein Kreuzprodukt, dessen temporäre Dateien die Platte des Datenbankservers füllten. Neu: `temp_file_limit` (`POSTGRES_TEMP_FILE_LIMIT`, Vorgabe 5 GB) begrenzt temporäre Dateien je Sitzung (#54).
- Datenbank-Pool lief nach abgebrochenen Anfragen leer: Legte ein Client auf, verschickte Django unter ASGI bei einem Teil der Anfragen nie `request_finished`, und die Verbindung blieb für immer ausgeliehen. Eine Scanner-Welle legte so am 22.09.2026 die Anwendung für gut 25 Stunden lahm. Neue Middleware gibt die Verbindung im Thread der View zurück; Readiness-Prüfung, Admin-Sync und der Sync-Watchdog im Webprozess geben die Verbindungen ihrer Threads zurück (der Watchdog belegte bisher dauerhaft einen Pool-Platz); begrenzte Warteschlange (`DB_POOL_MAX_WAITING`, Vorgabe 20) und kürzere Wartezeit (`DB_POOL_TIMEOUT`, jetzt 5 s); ein leerer Pool liefert 503 mit `Retry-After`, ohne dafür selbst die Datenbank zu brauchen; `/health/live/` wird bei festgefahrenem Pool rot, `deploy/scripts/restart-unhealthy.sh` startet dann neu; die Caddyfile-Vorlage weist offensichtliche Scanner-Pfade ab (#344).
- Fehlerseiten (400/403/404/500) behielten ihre Datenbankverbindung: Unter ASGI rendert Django sie über `response_for_exception` mit `thread_sensitive=False` in den langlebigen Threads des Standard-Executors, die nie eine Anfrage abschließen. Zehn Executor-Threads konnten so alle zehn Pool-Plätze dauerhaft belegen — eine 404-Welle eines Scanners genügte. Alle Fehler-Handler geben ihre Verbindungen jetzt am Ende zurück (#344).
- Metrik-Sammler holten sich beim Start eine Datenbankverbindung, die nie zurückkam: `prometheus_client` ruft beim Registrieren `collect()` auf, wenn ein Sammler kein `describe()` hat; der Import geschah im Hauptthread von Daphne. Die Sammler beschreiben sich jetzt ohne Abruf, und `asgi.py` gibt nach dem Start zurück, was der Hauptthread geöffnet hat (#344).
- `DatabaseErrorMiddleware` griff nie: Django wandelt Ausnahmen der View schon innerhalb des Middleware-Stapels in eine Antwort um, ihr `try/except` sah sie nicht. Sie arbeitet jetzt über `process_exception`, der 500-Handler erkennt dieselben Fälle (#344).
- Session: Beim Umwandeln eines Antrags in eine Vorlage wurden Titel und Vorlagenart aus dem Formular stillschweigend verworfen; die Vorlagenart ist jetzt nach der Antragsart vorausgewählt und bestimmt den Nummernkreis.
- Work: Dokument-Import und „Aufgabe anlegen“ waren ohne Funktion (Skript im nicht existierenden Block `extra_js`); RIS-Karte brach mit JavaScript-Fehler ab; Rollen-Tab nur noch mit Berechtigung.
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
