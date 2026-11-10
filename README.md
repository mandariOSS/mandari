<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/logo-light.svg">
    <img src="docs/assets/logo.svg" alt="mandari" width="280" />
  </picture>
</p>

<p align="center">
  <strong>mandari: das offene Ratsinformationssystem</strong><br>
  Bürgerportal, Fraktions-Arbeitsbereich und Sitzungsdienst der Verwaltung in einer Anwendung, mit OParl-Schnittstelle.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0--or--later-blue.svg" alt="Lizenz" /></a>
  <a href="https://github.com/mandariOSS/mandari/releases"><img src="https://img.shields.io/github/v/release/mandariOSS/mandari?include_prereleases" alt="Release" /></a>
  <a href="https://github.com/mandariOSS/mandari/actions/workflows/pr-check.yml?query=branch%3Amain"><img src="https://img.shields.io/github/actions/workflow/status/mandariOSS/mandari/pr-check.yml?branch=main&amp;label=Tests" alt="Tests" /></a>
  <a href="https://github.com/mandariOSS/mandari/actions/workflows/reuse.yml?query=branch%3Amain"><img src="https://img.shields.io/github/actions/workflow/status/mandariOSS/mandari/reuse.yml?branch=main&amp;label=REUSE" alt="REUSE" /></a>
  <a href="https://status.mandari.de"><img src="https://status.mandari.de/api/v1/endpoints/f%C3%BCr-b%C3%BCrger%3Ainnen_b%C3%BCrgerportal/health/badge.svg" alt="Status des Bürgerportals" /></a>
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

### Offene Daten der Installation auf mandari.de

Die Ratsinformationen der Kommunen auf [mandari.de](https://mandari.de/insight/) stehen als offene Daten nach OParl 1.1
bereit, lesend und ohne Anmeldung:

- Einstieg: `https://oparl.mandari.de/oparl/v1/system`
- Alle Kommunen mit ihren Adressen in einem Abruf: `https://oparl.mandari.de/oparl/` (Feld `bodyIndex`)
- Eine Kommune über ihren Namen, zum Beispiel Münster: `https://oparl.mandari.de/oparl/v1/body/muenster`, ihre Vorlagen
  unter `…/body/muenster/papers`, ihre Sitzungen unter `…/body/muenster/meetings`
- Beschreibung und Beispiele: [docs.mandari.de/insight/oparl-api](https://docs.mandari.de/insight/oparl-api/)

### Eigenschaften

- **Offen** — AGPL-3.0-or-later, keine Herstellerbindung, Daten jederzeit exportierbar
- **Selbst betreibbar** — ein Server genügt; Docker Compose oder Kubernetes
- **Mehrmandantenfähig** — mehrere Organisationen und Kommunen in einer Installation
- **Verschlüsselt** — AES-256-GCM für vertrauliche Inhalte, Schlüssel je Mandant
- **Datenschutzkonform** — Hosting in Deutschland möglich, Löschkonzept und Muster-AVV liegen bei
- **Dokumentierte Schnittstellen** — OParl 1.1, Session-API v1 mit OpenAPI, Fraktions-API

## Installation

### Ein Server mit Docker (empfohlen)

Voraussetzung: Linux-Server (Ubuntu 22.04 oder neuer, Debian 12, Rocky 9) mit mindestens 8 GB
Arbeitsspeicher (16 GB für mittlere Kommunen, Größenklassen in [docs/LASTTESTS.md](docs/LASTTESTS.md)), eine
Domain, die auf den Server zeigt. Die Speichergrenzen aller Dienste in `docker-compose.yml` ergeben zusammen
rund 10,5 GB; ausgeschöpft werden sie selten gleichzeitig. Docker installiert der Installer bei Bedarf mit.

```bash
git clone https://github.com/mandariOSS/mandari.git
cd mandari
./install.sh
```

Der Installer fragt Domain, Zeitzone und Administrationskonto ab, erzeugt alle Schlüssel,
startet die Dienste in der richtigen Reihenfolge, führt die Migrationen aus und richtet ein
nächtliches Backup ein, samt Passphrase (`.backup-passphrase`), mit der die Sicherung die
Konfiguration verschlüsselt mitnimmt. `.env` und `.backup-passphrase` gehören zusätzlich außerhalb
des Servers verwahrt. HTTPS-Zertifikate holt Caddy automatisch; eine Kontaktadresse für Let's Encrypt
ist optional.

Nach der Installation:

- Beim ersten Login als Administrator richtet mandari die Zwei-Faktor-Anmeldung ein (Pflicht,
  [docs/ACCOUNT_SECURITY.md](docs/ACCOUNT_SECURITY.md)).
- Ohne Mailserver verschickt mandari keine Mails. Einzutragen im Admin unter den Systemeinstellungen oder
  als `EMAIL_*` in der `.env` ([`.env.example`](.env.example)).
- Kommunen einzeln im Admin prüfen und aktivieren; abgerufen wird nach [docs/ABRUFREGELN.md](docs/ABRUFREGELN.md).

Ohne Rückfragen:

```bash
DOMAIN=ris.meine-kommune.de ADMIN_EMAIL=admin@meine-kommune.de \
ADMIN_PASSWORD='EinLangesPasswort' ./install.sh --unattended
```

Das Passwort braucht mindestens 12 Zeichen (Passwortrichtlinie), sonst bricht der Lauf vorher ab.
`ACME_EMAIL` ist optional.

Liegt im Verzeichnis bereits eine Installation, bricht der Lauf ohne Änderung ab (Exit-Code 1);
aktualisiert wird mit `./update.sh`. Neu aufsetzen lässt sie sich ohne Rückfragen nur ausdrücklich
mit `--reinstall-destroy-data` – das löscht alle Daten (siehe [DEPLOYMENT.md](DEPLOYMENT.md#installation-ohne-rückfragen)).

Mehrere Installationen auf einem Host sind derzeit nicht vorgesehen: Die Volumes tragen feste Namen
(`mandari_*_data`, [Issue #443](https://github.com/mandariOSS/mandari/issues/443)).

Ohne Installer, nur mit `docker-compose.yml`, `Caddyfile`, `scripts/init-website-db.sh` und `.env`: Ablauf in
[DEPLOYMENT.md](DEPLOYMENT.md#ohne-installsh). Die Anwendung migriert beim Start nicht; der Ablauf dort
legt das Schema vor dem ersten `docker compose up -d` an.

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
./backup.sh     # Datenbanken, Uploads, Dokument-Cache und verschlüsselte Konfiguration sichern
```

Vollständige Betriebsanleitung: <https://docs.mandari.de/betrieb/>

## Aufbau

```
mandari/
├── mandari/          Django-Anwendung (Insight, Work, Session)
│   ├── apps/         Fachmodule: accounts, tenants, work, session, events, minutes,
│   │                 provisioning, common
│   ├── hub/          Datendrehscheibe: Verträge, Befehle, RIS-Modell, Adapter
│   ├── insight_core/ OParl-Datenmodelle, Suche, SEO
│   ├── frontend/     TypeScript (Vite), Alpine-Komponenten
│   └── templates/    Django-Templates und Komponentenbibliothek
├── ingestor/         OParl-Synchronisation (httpx, SQLAlchemy, asyncio)
├── shared/           Gemeinsame Pakete von Anwendung und Ingestor (OParl, Texterkennung)
├── deploy/           Helm-Chart, Compose-Rollen für mehrere Server, Betriebsskripte
├── docs/             Technische Notizen, Standards, Architekturentscheidungen
└── scripts/          Qualitätsprüfungen und Werkzeuge
```

Der Compose-Stack umfasst Caddy (TLS), PostgreSQL 16, Redis 7, Elasticsearch 8 und Dienste aus drei
Anwendungs-Images:

| Dienst | Image | Aufgabe |
|--------|-------|---------|
| `mandari` | `ghcr.io/mandarioss/mandari` | Webanwendung (Insight, Work, Session, Admin, APIs) |
| `worker` | `ghcr.io/mandarioss/mandari` | Aufträge, Zeitpläne (früher Cron) und Datendrehscheibe (`manage.py events_worker`) |
| `worker-heavy` | `ghcr.io/mandarioss/mandari` | Texterkennung und KI (Warteschlangen `ocr`, `ai`) |
| `worker-live` | `ghcr.io/mandarioss/mandari` | Live-Übertragungen von Gremiensitzungen (Warteschlange `live`) |
| `minutes-orchestrator` | `ghcr.io/mandarioss/mandari` | GPU-Rechenknoten der Protokollierung, ruht ohne Einschalten im Admin |
| `ingestor` | `ghcr.io/mandarioss/ingestor` | Abgleich mit den Ratsinformationssystemen (OParl) |
| `website` | `ghcr.io/mandarioss/website` | Marketing-Website (Wagtail) |

Alle Dienste laufen hinter einer Domain. Caddy leitet die Pfade der Anwendung an `mandari` (`/insight/`,
`/work/`, `/session/`, `/accounts/`, `/admin/`, `/api/`, `/oparl/`, `/ws/`, `/static/`, `/media/` und
weitere, vollständig im Matcher `@mandari` im [`Caddyfile`](Caddyfile)), alles Übrige an die Website. Einzelheiten
zu Workern und Zeitplänen: [DEPLOYMENT.md](DEPLOYMENT.md).

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

Für den SaaS-Betrieb kann ein System des Betreibers Organisationen über `/api/provisioning/` anlegen;
Version 2 unter `/api/provisioning/v2/` setzt den Soll-Zustand deklarativ und wiederholbar
([docs/PROVISIONING_API.md](docs/PROVISIONING_API.md)). Ohne gesetzten `PROVISIONING_API_KEY` bzw.
`PROVISIONING_V2_API_KEY` ist die jeweilige Version vollständig abgeschaltet — bei
selbst betriebenen Installationen entsteht also keine zusätzliche Angriffsfläche.

## Technik

| Bereich | Technologie |
|---------|-------------|
| Backend | Django 6.1, Python 3.14 (Image; 3.12+ unterstützt) |
| Frontend | Django-Templates, HTMX, Alpine.js, Tailwind CSS, Vite |
| Ingestor | Python 3.14 (httpx, SQLAlchemy, asyncio) |
| Datenbank | PostgreSQL 16 |
| Suche | Elasticsearch 8 (im Helm-Chart abschaltbar, mit Docker Compose fest dabei) |
| Hintergrundarbeit | Worker aus dem Anwendungs-Image: Aufträge, Zeitpläne, Datendrehscheibe |
| Cache und Kanäle | Redis 7 |
| Proxy | Caddy 2 (Docker) oder Ingress (Kubernetes) |

### Qualitätssicherung

Jede Änderung durchläuft dieselben Prüfungen: über 13.000 Tests mit Coverage-Schwelle, mypy
im strikten Modus mit schrumpfender Ausnahmeliste, ruff, djlint, TypeScript, Biome,
End-to-End-Tests im Browser mit axe-core für Barrierefreiheit, ein Schema-Abgleich zwischen
Anwendung und Ingestor, `pip-audit` gegen das Lockfile, die REUSE-Lizenzprüfung, eine
Link-Prüfung der Dokumentation und eine CycloneDX-Stückliste je Release. Einzelheiten:
[`docs/ENGINEERING_STANDARDS.md`](docs/ENGINEERING_STANDARDS.md) und
<https://docs.mandari.de/entwicklung/qualitaetsgates/>.

## Dokumentation

Vollständige Dokumentation: **<https://docs.mandari.de>**

| Bereich | Inhalt |
|---------|--------|
| [Betrieb](https://docs.mandari.de/betrieb/) | Installation, Konfiguration, Quellen anbinden, Monitoring, Logging, Updates, Backups |
| [Insight](https://docs.mandari.de/insight/) | Bürgerportal und OParl-Aggregations-API |
| [Work](https://docs.mandari.de/work/) | Fraktionsarbeit, Fraktions-API, Anträge einreichen |
| [Session](https://docs.mandari.de/session/) | Verwaltungs-RIS, OParl-API, Session-API v1, Beschlusskontrolle |
| [Datenschutz](https://docs.mandari.de/datenschutz/) | Technische Maßnahmen, Löschkonzept, Muster-AVV, Ingestor und Opt-out |
| [Entwicklung](https://docs.mandari.de/entwicklung/) | Qualitätsgates, Abhängigkeiten, Mitarbeit |

Wann und wie mandari Ratsinformationssysteme abruft (Kennung, robots.txt, Takt je Server, Zeitfenster, Abruf-Adresse,
Kontakt), beschreibt [`docs/ABRUFREGELN.md`](docs/ABRUFREGELN.md) für Betreiber von Ratsinformationssystemen und für
Selbstbetreiber.

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
Änderungen an einer öffentlich betriebenen Instanz müssen ebenfalls unter AGPL-3.0-or-later
veröffentlicht werden. Das Repository folgt der [REUSE](https://reuse.software)-Spezifikation:
Jede Datei hat eine maschinenlesbare Lizenz- und Urheberangabe – im Dateikopf oder über
[`REUSE.toml`](REUSE.toml) –, die Lizenztexte liegen unter `LICENSES/`. `reuse lint` prüft das bei
jeder Änderung (Workflow „REUSE“).

Urheber von mandari ist Sven Konopka. Die Angabe steht zentral in `REUSE.toml` und gilt für jede
Datei des Projekts; ausgenommen sind nur die dort eigens zugeordneten Fremdbibliotheken. Wie
Beitragende genannt werden, steht in [CONTRIBUTING.md](CONTRIBUTING.md#urheberangaben).

Die Wortmarke „mandari“ und das Logo sind davon ausgenommen (`LicenseRef-Mandari-Brand`).
Wer eine eigene Installation unter eigenem Namen betreibt, ersetzt die Dateien unter
`mandari/static/brand/`.

## Dank

- [OParl](https://oparl.org) für den Standard, auf dem alles aufbaut
- Allen [Mitwirkenden](https://github.com/mandariOSS/mandari/graphs/contributors)

---

<p align="center">
  <sub>Copyright 2025–2026 Sven Konopka. Lizenziert unter <a href="LICENSE">AGPL-3.0-or-later</a>.</sub>
</p>
