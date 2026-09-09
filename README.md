<p align="center">
  <img src="docs/assets/logo.svg" alt="mandari" width="120" />
</p>

<h1 align="center">mandari</h1>

<p align="center">
  <strong>Open-Source-Plattform für kommunalpolitische Transparenz</strong><br>
  Bürgerportal, Fraktions-Arbeitsbereich und Verwaltungs-RIS in einer Anwendung.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL%20v3-blue.svg" alt="Lizenz" /></a>
  <a href="https://github.com/mandariOSS/mandari/releases"><img src="https://img.shields.io/github/v/release/mandariOSS/mandari?include_prereleases" alt="Release" /></a>
  <a href="https://github.com/mandariOSS/mandari/actions/workflows/pr-check.yml"><img src="https://img.shields.io/github/actions/workflow/status/mandariOSS/mandari/pr-check.yml?label=Tests" alt="Tests" /></a>
  <a href="https://api.reuse.software/info/github.com/mandariOSS/mandari"><img src="https://img.shields.io/badge/REUSE-konform-green.svg" alt="REUSE" /></a>
  <a href="https://status.mandari.de"><img src="https://img.shields.io/badge/Status-live-brightgreen.svg" alt="Status" /></a>
</p>

<p align="center">
  <a href="https://docs.mandari.de">Dokumentation</a> •
  <a href="#installation">Installation</a> •
  <a href="https://github.com/mandariOSS/mandari/discussions">Diskussionen</a> •
  <a href="CONTRIBUTING.md">Mitwirken</a>
</p>

---

## Was mandari macht

mandari macht Ratsinformationen aus deutschen Kommunen zugänglich. Die Plattform liest Daten
über den [OParl-Standard](https://oparl.org) aus bestehenden Ratsinformationssystemen (ALLRIS,
SessionNet, regisafe und andere) und bietet drei Zugänge:

| Portal | Für wen | Was es bietet |
|--------|---------|---------------|
| **Insight** | Bürgerinnen und Bürger | Sitzungen, Vorlagen und Beschlüsse durchsuchen, Themen abonnieren, Beschlüsse verfolgen, Fragen an den Rat stellen |
| **Work** | Fraktionen und Gruppen | Sitzungsvorbereitung, Anträge mit gemeinsamer Bearbeitung, Aufgaben, interne Fraktionssitzungen mit Protokoll |
| **Session** | Kommunalverwaltungen | Vollständiges Ratsinformationssystem: Sitzungen, Vorlagen, Tagesordnungen, Abstimmungen, Protokolle, Sitzungsgelder, Audit-Log |

Jede Kommune im Session-Portal veröffentlicht ihre öffentlichen Daten automatisch wieder als
OParl-Schnittstelle — die Daten bleiben offen und maschinenlesbar.

### Eigenschaften

- **Offen** — AGPL-3.0, keine Herstellerbindung, Daten jederzeit exportierbar
- **Selbst betreibbar** — ein Server genügt; Docker Compose oder Kubernetes
- **Mehrmandantenfähig** — mehrere Organisationen und Kommunen in einer Installation
- **Verschlüsselt** — AES-256-GCM für vertrauliche Inhalte, Schlüssel je Mandant
- **Datenschutzkonform** — Hosting in Deutschland möglich, Löschkonzept und Muster-AVV liegen bei
- **Dokumentierte Schnittstellen** — OParl 1.1, Session-API v1 mit OpenAPI, Fraktions-API

## Installation

### Ein Server mit Docker (empfohlen)

Voraussetzung: Linux-Server (Ubuntu 22.04 oder neuer, Debian 12, Rocky 9), 4 GB
Arbeitsspeicher, eine Domain, die auf den Server zeigt. Docker installiert der Installer
bei Bedarf mit.

```bash
git clone https://github.com/mandariOSS/mandari.git
cd mandari
./install.sh
```

Der Installer fragt Domain, Zeitzone und Administrationskonto ab, erzeugt alle Schlüssel,
startet die Dienste in der richtigen Reihenfolge, führt die Migrationen aus und richtet ein
nächtliches Backup ein. HTTPS-Zertifikate holt Caddy automatisch.

Ohne Rückfragen:

```bash
DOMAIN=ris.meine-kommune.de ADMIN_EMAIL=admin@meine-kommune.de \
ADMIN_PASSWORD='EinLangesPasswort' ./install.sh --unattended
```

Mehrere Installationen auf einem Host brauchen unterschiedliche Projektnamen:

```bash
COMPOSE_PROJECT_NAME=mandari-zweit ./install.sh
```

### Kubernetes

```bash
git clone https://github.com/mandariOSS/mandari.git
cd mandari
./install-k8s.sh
```

Der Installer prüft die Cluster-Verbindung, installiert Helm falls nötig, bietet fehlende
Bausteine (ingress-nginx, cert-manager) zur Installation an und spielt das Chart ein.
Details, Werte-Tabelle und Betriebshinweise: [`deploy/kubernetes/README.md`](deploy/kubernetes/README.md).

Direkt mit Helm:

```bash
helm upgrade --install mandari deploy/kubernetes/helm/mandari \
  --namespace mandari --create-namespace \
  --set domain=ris.meine-kommune.de --wait
```

Ohne Helm liegen fertige Manifeste unter [`deploy/kubernetes/manifests/`](deploy/kubernetes/manifests/).

### Betrieb

```bash
./update.sh     # Aktualisieren mit Sicherung und Rückfallebene
./backup.sh     # Datenbank, Medien und Konfiguration sichern
```

Vollständige Betriebsanleitung: <https://docs.mandari.de/betrieb/>

## Aufbau

```
mandari/
├── mandari/          Django-Anwendung (Insight, Work, Session)
│   ├── apps/         Fachmodule: accounts, tenants, work, session, common
│   ├── insight_core/ OParl-Datenmodelle, Suche, SEO
│   ├── frontend/     TypeScript (Vite), Alpine-Komponenten
│   └── templates/    Django-Templates und Komponentenbibliothek
├── ingestor/         OParl-Synchronisation (httpx, SQLAlchemy, APScheduler)
├── deploy/kubernetes/ Helm-Chart und Manifeste
├── docs/             Technische Notizen, Standards, Architekturentscheidungen
└── scripts/          Qualitätsprüfungen und Werkzeuge
```

Der Compose-Stack umfasst Caddy (TLS), PostgreSQL 16, Redis 7, Elasticsearch 8 und drei
Anwendungscontainer: `ghcr.io/mandarioss/mandari`, `.../website`, `.../ingestor`. Alle
Dienste laufen hinter einer Domain; Caddy verteilt `/insight/`, `/work/`, `/session/`,
`/accounts/`, `/admin/` und `/api/` an die Anwendung, alles Übrige an die Marketing-Website.

Der Betriebsstatus ist öffentlich einsehbar: <https://status.mandari.de>.

### Weitere Repositories

| Repository | Inhalt |
|------------|--------|
| [`mandariOSS/mandari`](https://github.com/mandariOSS/mandari) | Anwendung und Ingestor (dieses Repository) |
| [`mandariOSS/docs`](https://github.com/mandariOSS/docs) | Dokumentation unter docs.mandari.de |
| [`mandariOSS/marketing-website`](https://github.com/mandariOSS/marketing-website) | Website mandari.de (Wagtail) |

## Schnittstellen

| Schnittstelle | Pfad | Zugriff |
|---------------|------|---------|
| OParl-Aggregation | `/oparl/` | öffentlich, anonym |
| OParl je Kommune | `/session/<kommune>/api/oparl/` | öffentlich, anonym |
| Session-API v1 | `/api/v1/session/<kommune>/` | Token oder Sitzung, [OpenAPI](https://docs.mandari.de/session/api-v1/) |
| Fraktions-API | `/api/public/v1/` | Token |

Für den SaaS-Betrieb kann ein Kundenportal Organisationen über `/api/provisioning/` anlegen.
Ohne gesetzten `PROVISIONING_API_KEY` ist diese Schnittstelle vollständig abgeschaltet — bei
selbst betriebenen Installationen entsteht also keine zusätzliche Angriffsfläche.

## Technik

| Bereich | Technologie |
|---------|-------------|
| Backend | Django 6.0, Python 3.12 |
| Frontend | Django-Templates, HTMX, Alpine.js, Tailwind CSS, Vite |
| Ingestor | Python 3.12 (httpx, SQLAlchemy, APScheduler) |
| Datenbank | PostgreSQL 16 |
| Suche | Elasticsearch 8 (abschaltbar) |
| Cache und Kanäle | Redis 7 |
| Proxy | Caddy 2 (Docker) oder Ingress (Kubernetes) |

### Qualitätssicherung

Jede Änderung durchläuft dieselben Prüfungen: rund 1.500 Tests mit Coverage-Schwelle, mypy
im strikten Modus mit schrumpfender Ausnahmeliste, ruff, djlint, TypeScript, Biome,
End-to-End-Tests im Browser mit axe-core für Barrierefreiheit, ein Schema-Abgleich zwischen
Anwendung und Ingestor, `pip-audit` gegen das Lockfile und eine CycloneDX-Stückliste je
Release. Einzelheiten: [`docs/ENGINEERING_STANDARDS.md`](docs/ENGINEERING_STANDARDS.md) und
<https://docs.mandari.de/entwicklung/qualitaetsgates/>.

## Dokumentation

Vollständige Dokumentation: **<https://docs.mandari.de>**

| Bereich | Inhalt |
|---------|--------|
| [Betrieb](https://docs.mandari.de/betrieb/) | Installation, Konfiguration, Quellen anbinden, Monitoring, Logging, Updates, Backups |
| [Insight](https://docs.mandari.de/insight/) | Bürgerportal und OParl-Aggregations-API |
| [Work](https://docs.mandari.de/work/) | Fraktionsarbeit, Fraktions-API, Anträge einreichen |
| [Session](https://docs.mandari.de/session/) | Verwaltungs-RIS, OParl-API, Session-API v1, Beschlusskontrolle |
| [Datenschutz](https://docs.mandari.de/datenschutz/) | Technische Maßnahmen, Löschkonzept, Muster-AVV, Crawler |
| [Entwicklung](https://docs.mandari.de/entwicklung/) | Qualitätsgates, Abhängigkeiten, Mitarbeit |

## Mitwirken

Beiträge sind willkommen — von Fehlermeldungen über Dokumentation bis zu Code.
Lies vorher [CONTRIBUTING.md](CONTRIBUTING.md) und den [Verhaltenskodex](CODE_OF_CONDUCT.md).

- [Fehler melden](https://github.com/mandariOSS/mandari/issues/new?template=bug_report.md)
- [Funktion vorschlagen](https://github.com/mandariOSS/mandari/issues/new?template=feature_request.md)
- [Diskussionen](https://github.com/mandariOSS/mandari/discussions)

## Sicherheit

Sicherheitslücken bitte **nicht** öffentlich melden, sondern an **security@mandari.de**.
Der Ablauf steht in [SECURITY.md](SECURITY.md).

## Lizenz

[AGPL-3.0-or-later](LICENSE). mandari darf frei genutzt, verändert und weitergegeben werden;
Änderungen an einer öffentlich betriebenen Instanz müssen ebenfalls unter AGPL-3.0
veröffentlicht werden. Das Repository ist [REUSE](https://reuse.software)-konform: Jede Datei
trägt eine maschinenlesbare Lizenz- und Urheberangabe, Lizenztexte liegen unter `LICENSES/`.

Die Wortmarke „mandari“ und das Logo sind davon ausgenommen (`LicenseRef-Mandari-Brand`).
Wer eine eigene Installation unter eigenem Namen betreibt, ersetzt die Dateien unter
`mandari/static/brand/`.

## Dank

- [OParl](https://oparl.org) für den Standard, auf dem alles aufbaut
- Allen [Mitwirkenden](https://github.com/mandariOSS/mandari/graphs/contributors)

---

<p align="center">
  <sub>Copyright 2025–2026 Sven Konopka and contributors. Lizenziert unter <a href="LICENSE">AGPL-3.0-or-later</a>.</sub>
</p>
