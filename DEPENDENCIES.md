# Danksagungen & Abhängigkeiten

Mandari steht auf den Schultern von Giganten. Ohne die fantastische Arbeit der Open-Source-Community wäre dieses Projekt nicht möglich. Hier listen wir alle Projekte auf, die Mandari möglich machen.

## Kern-Technologien

| Technologie | Beschreibung | Lizenz | Link |
|-------------|--------------|--------|------|
| **Python 3.12+** | Programmiersprache | PSF License | [python.org](https://python.org) |
| **Django 6.0** | Web-Framework | BSD-3-Clause | [djangoproject.com](https://djangoproject.com) |
| **PostgreSQL 16** | Relationale Datenbank | PostgreSQL License | [postgresql.org](https://postgresql.org) |
| **Redis 7** | In-Memory Cache | BSD-3-Clause | [redis.io](https://redis.io) |
| **Meilisearch** | Volltextsuche | MIT License | [meilisearch.com](https://meilisearch.com) |
| **Docker** | Container-Plattform | Apache 2.0 | [docker.com](https://docker.com) |

## Frontend & UI

| Bibliothek | Beschreibung | Lizenz | Link |
|------------|--------------|--------|------|
| **HTMX** | Hypermedia-Framework für dynamische UIs | BSD-2-Clause | [htmx.org](https://htmx.org) |
| **Alpine.js** | Leichtgewichtiges JS-Framework | MIT License | [alpinejs.dev](https://alpinejs.dev) |
| **Tailwind CSS** | Utility-First CSS Framework | MIT License | [tailwindcss.com](https://tailwindcss.com) |
| **Lucide Icons** | Schöne Open-Source Icons | ISC License | [lucide.dev](https://lucide.dev) |
| **MapLibre GL** | Open-Source Kartenbibliothek | BSD-3-Clause | [maplibre.org](https://maplibre.org) |
| **Chart.js** | Diagramme & Visualisierungen | MIT License | [chartjs.org](https://chartjs.org) |
| **FullCalendar** | Kalender-Komponente | MIT License | [fullcalendar.io](https://fullcalendar.io) |
| **EasyMDE** | Markdown-Editor | MIT License | [GitHub](https://github.com/Ionaru/easy-markdown-editor) |

## Python-Bibliotheken

| Paket | Verwendung | Lizenz | Link |
|-------|-----------|--------|------|
| **django-htmx** | HTMX-Integration für Django | MIT | [PyPI](https://pypi.org/project/django-htmx/) |
| **django-unfold** | Modernes Admin-Theme | MIT | [GitHub](https://github.com/unfoldadmin/django-unfold) |
| **whitenoise** | Static File Serving | MIT | [Docs](https://whitenoise.readthedocs.io/) |
| **psycopg** | PostgreSQL-Adapter | LGPL | [Docs](https://www.psycopg.org/psycopg3/) |
| **meilisearch** | Meilisearch Python Client | MIT | [GitHub](https://github.com/meilisearch/meilisearch-python) |
| **Pillow** | Bildverarbeitung | HPND | [Docs](https://pillow.readthedocs.io/) |
| **cryptography** | Verschlüsselung (AES-256-GCM) | BSD/Apache 2.0 | [Docs](https://cryptography.io/) |
| **pytesseract** | OCR für PDFs | Apache 2.0 | [GitHub](https://github.com/tesseract-ocr/tesseract) |
| **xhtml2pdf** | PDF-Generierung | Apache 2.0 | [GitHub](https://github.com/xhtml2pdf/xhtml2pdf) |
| **Gunicorn** | WSGI Server | MIT | [gunicorn.org](https://gunicorn.org/) |

Vollständige Liste: [mandari/requirements.txt](mandari/requirements.txt)

## Infrastruktur

| Dienst | Beschreibung | Lizenz | Link |
|--------|--------------|--------|------|
| **Caddy** | Reverse Proxy mit automatischem TLS | Apache 2.0 | [caddyserver.com](https://caddyserver.com) |
| **Let's Encrypt** | Kostenlose SSL-Zertifikate | - | [letsencrypt.org](https://letsencrypt.org) |
| **OpenStreetMap** | Kartendaten | ODbL | [openstreetmap.org](https://openstreetmap.org) |

## Standards & Spezifikationen

### OParl 1.1

Mandari implementiert den deutschen **OParl-Standard** für offene Ratsinformationssysteme. OParl definiert eine einheitliche API für den anonymen, lesenden Zugriff auf öffentliche parlamentarische Daten.

- Website: [oparl.org](https://oparl.org)
- Spezifikation: [Online-Ansicht](https://oparl.org/spezifikation/online-ansicht/)

## Inspirationen & verwandte Projekte

Mandari ist nicht allein. Es gibt eine lebendige Community von Projekten, die sich für Transparenz in der Kommunalpolitik einsetzen:

### Politik bei uns *(eingestellt)*

Pionier der deutschen RIS-Transparenz. Ursprünglich gestartet von **Marian Steinbach** als "Offenes Köln", später von der Open Knowledge Foundation Deutschland (OKFDE) weiterentwickelt. **Unsere wichtigste Inspiration.**

- GitHub: [okfde/politik-bei-uns-web](https://github.com/okfde/politik-bei-uns-web)

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

- GitHub: [ErtanOz/RATISA_Koeln-Ratinformation-System-Assistent](https://github.com/ErtanOz/RATISA_Koeln-Ratinformation-System-Assistent)

## Lizenzhinweis

Mandari selbst steht unter der **AGPL-3.0 Lizenz**.

Die hier aufgeführten Abhängigkeiten haben ihre eigenen Lizenzen (MIT, BSD, Apache, etc.), die alle mit der AGPL kompatibel sind und kommerzielle Nutzung erlauben.

Siehe [LICENSE](LICENSE) für die vollständige Mandari-Lizenz.
