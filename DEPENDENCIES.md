# Danksagungen & Abhängigkeiten

mandari steht auf den Schultern von Giganten. Ohne die fantastische Arbeit der Open-Source-Community wäre dieses Projekt nicht möglich. Hier listen wir die Projekte auf, die mandari möglich machen, mit Lizenz und Zweck.

Die Versionen stehen nicht hier, sondern in `mandari/pyproject.toml`, `ingestor/pyproject.toml`,
`shared/pyproject.toml` und `mandari/package.json`; die gesperrten Stände in `mandari/uv.lock`,
`ingestor/uv.lock` und `mandari/package-lock.json`. Die vollständige, versionsgenaue Liste samt
transitiver Pakete hängt als Stückliste an jedem Release (Abschnitt
[Stückliste (SBOM) je Release](#stückliste-sbom-je-release)).

## Kern-Technologien

| Technologie | Beschreibung | Lizenz | Link |
|-------------|--------------|--------|------|
| **Python 3.12 bis 3.14** | Programmiersprache | PSF License | [python.org](https://python.org) |
| **Django 6.1** | Web-Framework | BSD-3-Clause | [djangoproject.com](https://djangoproject.com) |
| **PostgreSQL 16** | Relationale Datenbank | PostgreSQL License | [postgresql.org](https://postgresql.org) |
| **Redis 7** | Cache, Sessions, Channel Layer | RSALv2 / SSPLv1 (ab Redis 7.4; bis 7.2 BSD-3-Clause) | [redis.io](https://redis.io) |
| **Elasticsearch 8** | Volltextsuche | SSPL / Elastic License 2.0 | [elastic.co](https://www.elastic.co) |
| **Docker** | Container-Plattform | Apache 2.0 | [docker.com](https://docker.com) |

## Python: Anwendung (`mandari/`)

| Paket | Zweck | Lizenz |
|-------|-------|--------|
| **Django** | Web-Framework (Backend, Templates, ORM, Background Tasks) | BSD-3-Clause |
| **django-htmx** | HTMX-Integration für Django | MIT |
| **django-cotton** | Template-Komponenten (`templates/cotton/`) | MIT |
| **django-vite** | Einbindung der Vite-Bündel | Apache-2.0 |
| **django-ninja** | Session-API v1 und gemeinsame Schnittstellenbausteine | MIT |
| **django-unfold** | Admin-Theme | MIT |
| **whitenoise** | Auslieferung statischer Dateien | MIT |
| **daphne**, **channels**, **channels-redis** | ASGI-Server, WebSockets, Redis-Channel-Layer | BSD-3-Clause |
| **django-safemigrate** | Migrationen ohne Unterbrechung | BSD-3-Clause |
| **django-oauth-toolkit** | mandari als OpenID-Connect-Anbieter; bringt oauthlib (BSD-3-Clause) und jwcrypto (LGPL-3.0-or-later) mit | BSD-3-Clause |
| **psycopg** (mit Pool) | PostgreSQL-Adapter | LGPL-3.0 |
| **dj-database-url** | Datenbank-Konfiguration per URL | BSD-3-Clause |
| **sqlalchemy**, **asyncpg** | Datenbankzugriff des Ingestor-Codes im Anwendungsimage | MIT, Apache-2.0 |
| **redis** | Redis-Client (Cache, Sessions, Drossel) | MIT |
| **elasticsearch** | Python-Client der Volltextsuche | Apache-2.0 |
| **httpx** | HTTP-Client (KI-Anbindung, Abrufe) | BSD-3-Clause |
| **anthropic** | Claude API für die Zusammenfassungen im Bürgerportal, auch als Message Batch (optional) | MIT |
| **boto3** | S3-kompatibler Objektspeicher für die Dokumentablage (optional) | Apache-2.0 |
| **pillow** | Bildverarbeitung | MIT-CMU (HPND) |
| **av** | Einzelbilder aus HLS-Übertragungen für die Live-Übertragung; bringt FFmpeg (LGPL) im Wheel mit | BSD-3-Clause |
| **cryptography** | Feldverschlüsselung (AES-256-GCM) | Apache-2.0 / BSD-3-Clause |
| **webauthn** | Sicherheitsschlüssel und Passkeys (WebAuthn/FIDO2) | BSD-3-Clause |
| **css-inline**, **html2text** | E-Mails: CSS-Inliner und Textfassung | MIT, GPL-3.0-or-later |
| **pypdf** | PDF-Textebene | BSD-3-Clause |
| **pypdfium2** | PDF-Seitenanalyse und Rendern (PDFium) | Apache-2.0 / BSD-3-Clause |
| **xhtml2pdf**, **reportlab** | PDF-Erzeugung | Apache-2.0, BSD-3-Clause |
| **python-docx** | DOCX-Erzeugung | MIT |
| **defusedxml** | Sicheres XML-Parsing (Aufgaben-Import) | PSF-2.0 |
| **segno** | QR-Codes | BSD-3-Clause |
| **jsonschema** | Prüfung der Verträge für Ereignisse und Befehle (JSON Schema 2020-12) | MIT |
| **rdflib** | Datenkatalog nach DCAT-AP.de als Turtle, RDF/XML und JSON-LD | BSD-3-Clause |
| **opentelemetry-*** | Tracing (aktiv nur mit `OTEL_EXPORTER_OTLP_ENDPOINT`) | Apache-2.0 |
| **prometheus-client** | Anwendungsmetriken unter `/metrics/` | Apache-2.0 |
| **pydantic-settings**, **python-dotenv**, **rich**, **tzdata**, **pyyaml** | Konfiguration, Konsolenausgabe, Zeitzonen, Prüfung der Compose-Rollen | MIT, BSD-3-Clause, MIT, Apache-2.0, MIT |
| **mandari-oparl** (`shared/`) | eigenes Paket: OParl-Typen, Abruf- und Texterkennungsbausteine | AGPL-3.0-or-later |

Aus Sicherheitsgründen sind einige transitive Pakete mit Mindestversion festgelegt (urllib3, requests,
anyio, zipp); die Begründung steht als Kommentar in `mandari/pyproject.toml`.

## Python: Ingestor (`ingestor/`) und `shared/`

| Paket | Zweck | Lizenz |
|-------|-------|--------|
| **httpx** | Asynchrone Abrufe der OParl-Schnittstellen | BSD-3-Clause |
| **pydantic**, **pydantic-settings** | Datenvalidierung und typisierte Konfiguration | MIT |
| **sqlalchemy**, **asyncpg** | Datenbankzugriff | MIT, Apache-2.0 |
| **redis** | Drossel je Server, Status | MIT |
| **typer**, **rich** | Kommandozeile und Ausgabe | MIT |
| **beautifulsoup4** | Scraper-Adapter (HTML ohne OParl) | MIT |
| **prometheus-client**, **aiohttp** | Metriken und Statusendpunkt | Apache-2.0 |
| **pypdf** | Textebene in `mandari_dokumente` | BSD-3-Clause |
| **tzdata** | Zeitzonendaten für den Takt je Quelle | Apache-2.0 |

## Frontend

| Bibliothek | Beschreibung | Lizenz | Link |
|------------|--------------|--------|------|
| **HTMX** | Hypermedia-Framework für dynamische UIs | 0BSD | [htmx.org](https://htmx.org) |
| **Alpine.js** | Leichtgewichtiges JS-Framework | MIT | [alpinejs.dev](https://alpinejs.dev) |
| **Tailwind CSS** | Utility-First CSS Framework | MIT | [tailwindcss.com](https://tailwindcss.com) |
| **Lucide Icons** | Open-Source Icons | ISC | [lucide.dev](https://lucide.dev) |
| **TipTap** | Editor für Anträge und Notizen | MIT | [tiptap.dev](https://tiptap.dev) |
| **Yjs** (mit y-websocket, y-indexeddb) | Gemeinsames Bearbeiten in Echtzeit | MIT | [yjs.dev](https://yjs.dev) |
| **pdf.js** | PDF-Anzeige | Apache-2.0 | [mozilla.github.io/pdf.js](https://mozilla.github.io/pdf.js/) |
| **jsdiff** | Textvergleich | BSD-3-Clause | [github.com/kpdecker/jsdiff](https://github.com/kpdecker/jsdiff) |
| **Leaflet** | Karten | BSD-2-Clause | [leafletjs.com](https://leafletjs.com) |
| **Chart.js** | Diagramme | MIT | [chartjs.org](https://chartjs.org) |
| **FullCalendar** | Kalender-Komponente | MIT | [fullcalendar.io](https://fullcalendar.io) |
| **marked** | Markdown-Darstellung | MIT | [marked.js.org](https://marked.js.org) |
| **SortableJS** | Sortieren per Ziehen | MIT | [sortablejs.github.io](https://sortablejs.github.io/Sortable/) |
| **Inter** | Schriftart | OFL-1.1 | [rsms.me/inter](https://rsms.me/inter/) |

Leaflet, Chart.js, FullCalendar, marked, SortableJS und Inter liegen als lokale Kopien unter
`mandari/static/vendor/`; der Rest kommt über `mandari/package.json`.

## Infrastruktur und externe Dienste

| Dienst | Beschreibung | Lizenz | Link |
|--------|--------------|--------|------|
| **Caddy** | Reverse Proxy mit automatischem TLS | Apache 2.0 | [caddyserver.com](https://caddyserver.com) |
| **PgBouncer** (optional) | Verbindungspool vor PostgreSQL | ISC | [pgbouncer.org](https://www.pgbouncer.org) |
| **Tesseract OCR** | Texterkennung, als Unterprozess aus `mandari_dokumente` | Apache 2.0 | [github.com/tesseract-ocr](https://github.com/tesseract-ocr/tesseract) |
| **Poppler** (`pdftoppm`) | Rendern einzelner Seiten, als Unterprozess | GPL-2.0/-3.0 | [poppler.freedesktop.org](https://poppler.freedesktop.org) |
| **Let's Encrypt** | Kostenlose TLS-Zertifikate | - | [letsencrypt.org](https://letsencrypt.org) |
| **OpenStreetMap** | Kartendaten | ODbL | [openstreetmap.org](https://openstreetmap.org) |

## Stückliste (SBOM) je Release

Jedes Release bringt eine maschinenlesbare Stückliste im Format CycloneDX mit, versionsgenau und mit
allen transitiven Paketen. Der Job „SBOM (CycloneDX)“ in `.github/workflows/release.yml` ruft
`scripts/build_sbom.sh` auf und hängt drei Dateien an das GitHub-Release:

| Datei | Inhalt |
|---|---|
| `sbom-mandari-python.cdx.json` | Django-Anwendung aus `mandari/uv.lock` |
| `sbom-ingestor-python.cdx.json` | Ingestor aus `ingestor/uv.lock` |
| `sbom-mandari-npm.cdx.json` | Frontend aus `mandari/package-lock.json` (ohne Entwicklungspakete) |

Lokal: `sh scripts/build_sbom.sh [ausgabeverzeichnis]` (braucht `cyclonedx-bom`, `uv` und `npm`).
Entwicklungsabhängigkeiten (pytest, ruff, mypy u. a.) sind nicht Teil der Auslieferung und fehlen in der
Stückliste bewusst.

## Lizenzangaben (REUSE)

Jede Quelldatei trägt einen `SPDX-License-Identifier`-Header; alles ohne Header (Bilder, Daten,
Konfiguration, lokale Kopien von Fremdbibliotheken) ist in `REUSE.toml` zugeordnet. Die Lizenztexte
liegen unter `LICENSES/`. Die Zuordnung: Quellcode AGPL-3.0-or-later, Dokumentation CC-BY-4.0,
Konfiguration und Lockfiles CC0-1.0, Markenzeichen `LicenseRef-Mandari-Brand`, Fremdbibliotheken mit
ihrer jeweiligen Lizenz. Prüfung lokal mit `pip install reuse && reuse lint`; in der CI läuft sie im
Workflow „REUSE“ (`.github/workflows/reuse.yml`) bei jedem Pull Request und jedem Push auf `dev` und
`main`. `reuse spdx` erzeugt daraus eine SPDX-Stückliste der Lizenzen, ergänzend zur CycloneDX-Stückliste.

## Standards & Spezifikationen

### OParl 1.1

mandari implementiert den deutschen **OParl-Standard** für offene Ratsinformationssysteme. OParl definiert eine einheitliche API für den anonymen, lesenden Zugriff auf öffentliche parlamentarische Daten.

- Website: [oparl.org](https://oparl.org)
- Spezifikation: [Online-Ansicht](https://oparl.org/spezifikation/online-ansicht/)

### DCAT-AP.de 3.0

Den Katalog der offenen Ratsinformationen für Datenportale beschreibt mandari nach **DCAT-AP.de**, dem deutschen
Anwendungsprofil von DCAT-AP für Metadaten offener Verwaltungsdaten. Die CI prüft ihn mit
[pySHACL](https://github.com/RDFLib/pySHACL) (Apache 2.0) gegen die SHACL-Regeln von DCAT-AP 3.0 und DCAT-AP.de 3.0.

- Website: [dcat-ap.de](https://www.dcat-ap.de/)
- Spezifikation: [DCAT-AP.de 3.0](https://www.dcat-ap.de/def/dcatde/3.0/spec/)

## Inspirationen & verwandte Projekte

mandari ist nicht allein. Es gibt eine lebendige Community von Projekten, die sich für Transparenz in der Kommunalpolitik einsetzen:

### Politik bei uns *(eingestellt)*

Pionier der deutschen RIS-Transparenz. Ursprünglich gestartet von **Marian Steinbach** als "Offenes Köln", später von der Open Knowledge Foundation Deutschland (OKFDE) weiterentwickelt. **Unsere wichtigste Inspiration.**

- GitHub: [politik-bei-uns/politik-bei-uns-web-old](https://github.com/politik-bei-uns/politik-bei-uns-web-old)

### Meine Stadt Transparent *(eingestellt)*

Nachfolger von "München Transparent". Open-Source RIS-Frontend gefördert vom **Prototype Fund**. Entwickelt von Tobias Hößl, Bernd Oswald und Konstantin Schütze im OK Lab München.

- GitHub: [meine-stadt-transparent/meine-stadt-transparent](https://github.com/meine-stadt-transparent/meine-stadt-transparent)

### Abgeordnetenwatch

Non-Profit Plattform für politische Transparenz auf Bundes- und Landesebene (nicht kommunal). Ermöglicht Bürgern, Abgeordnete öffentlich zu befragen und deren Abstimmungsverhalten einzusehen.

- Website: [abgeordnetenwatch.de](https://abgeordnetenwatch.de)

### KRAken

Kommunaler Recherche-Assistent für die Stadt Bonn. Vollständige KI-Pipeline von OParl-Download über PDF-Konvertierung, LLM-Klassifikation bis zur semantischen Vektorsuche. Inkl. FastAPI-Server und MCP-Server für Claude-Integration. Ein Projekt von **Mach!Den!Staat!** unter GPL-3.0.

- PyPI: [stadt-bonn-oparl](https://pypi.org/project/stadt-bonn-oparl/)

### RATISA

KI-gestütztes Web-Interface für das Kölner Ratsinformationssystem. Natural Language Search, Dokumenten-Zusammenfassung mit Google Gemini AI und MCP-Server für externe AI-Tools.

- GitHub: [ErtanOz/RATISA_Koeln-Ratinformation-System-Assistent-docker](https://github.com/ErtanOz/RATISA_Koeln-Ratinformation-System-Assistent-docker)

## Lizenzhinweis

mandari selbst steht unter der **AGPL-3.0-or-later**.

Die eingebundenen Bibliotheken haben ihre eigenen Lizenzen (MIT, BSD, Apache usw.), die mit der AGPL vereinbar sind und kommerzielle Nutzung erlauben. Elasticsearch und Redis sind eigenständige Dienste in getrennten Containern und stehen unter den oben genannten Lizenzen ihrer Hersteller.

Siehe [LICENSE](LICENSE) für die vollständige mandari-Lizenz.
