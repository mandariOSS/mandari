<p align="center">
  <img src="docs/assets/logo.svg" alt="Mandari" width="120" />
</p>

<h1 align="center">Mandari</h1>

<p align="center">
  <strong>Open-Source-Plattform für kommunalpolitische Transparenz</strong><br>
  Macht Kommunalpolitik transparent, verständlich und zugänglich.
</p>

<p align="center">
  <a href="https://github.com/mandariOSS/mandari/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/License-AGPL%20v3-blue.svg" alt="License" />
  </a>
  <a href="https://github.com/mandariOSS/mandari/releases">
    <img src="https://img.shields.io/github/v/release/mandariOSS/mandari?include_prereleases" alt="Release" />
  </a>
  <a href="https://github.com/mandariOSS/mandari/actions/workflows/pr-check.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/mandariOSS/mandari/pr-check.yml?label=tests" alt="Tests" />
  </a>
  <a href="https://github.com/mandariOSS/mandari/stargazers">
    <img src="https://img.shields.io/github/stars/mandariOSS/mandari?style=flat" alt="Stars" />
  </a>
</p>

<p align="center">
  <a href="https://docs.mandari.de">Dokumentation</a> •
  <a href="#installation">Installation</a> •
  <a href="https://github.com/mandariOSS/mandari/discussions">Diskussionen</a> •
  <a href="CONTRIBUTING.md">Mitwirken</a>
</p>

---

## Über Mandari

Mandari ist eine Open-Source-Plattform, die Ratsinformationen aus deutschen Kommunen zugänglich macht. Basierend auf dem [OParl-Standard](https://oparl.org) funktioniert Mandari mit über 100 Ratsinformationssystemen.

### Features

- **OParl-kompatibel** — Funktioniert mit ALLRIS, regisafe, SessionNet u.v.m.
- **Volltextsuche** — Durchsuche Sitzungen, Vorlagen und Dokumente
- **Self-Hosted** — Volle Kontrolle über deine Daten
- **Multi-Tenant** — Mehrere Organisationen in einer Instanz
- **Verschlüsselung** — AES-256 für sensible Daten
- **Automatische Updates** — OParl-Sync läuft im Hintergrund

## Installation

### Voraussetzungen

- Linux Server (Ubuntu 22.04+)
- Docker & Docker Compose
- Domain mit DNS-Eintrag

### Quick Start

```bash
git clone https://github.com/mandariOSS/mandari.git
cd mandari
./install.sh
```

### Oder nur Docker Compose

```bash
mkdir mandari && cd mandari
curl -LO https://raw.githubusercontent.com/mandariOSS/mandari/main/docker-compose.yml
curl -LO https://raw.githubusercontent.com/mandariOSS/mandari/main/Caddyfile
curl -Lo .env https://raw.githubusercontent.com/mandariOSS/mandari/main/.env.example
nano .env  # Konfiguration anpassen
docker compose up -d
```

Detaillierte Anleitung: [docs/installation.md](docs/installation.md)

## Dokumentation

| Dokument | Beschreibung |
|----------|--------------|
| [Installation](docs/installation.md) | Server-Setup und Deployment |
| [Konfiguration](docs/configuration.md) | Einstellungen und Optionen |
| [Updates](docs/upgrading.md) | Auf neue Version aktualisieren |
| [Backup](docs/backup-restore.md) | Datensicherung |

## Technologie

| Komponente | Technologie |
|------------|-------------|
| Backend | Django 6.0, Python 3.12+ |
| Frontend | HTMX, Alpine.js, Tailwind |
| Datenbank | PostgreSQL 16 |
| Suche | Meilisearch |
| Cache | Redis 7 |
| Proxy | Caddy |

## Mitwirken

Beiträge sind willkommen! Bitte lies zuerst:

- [Contributing Guidelines](CONTRIBUTING.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)

### Möglichkeiten

- 🐛 [Bug melden](https://github.com/mandariOSS/mandari/issues/new?template=bug_report.md)
- 💡 [Feature vorschlagen](https://github.com/mandariOSS/mandari/issues/new?template=feature_request.md)
- 📖 Dokumentation verbessern
- 🌍 Übersetzungen hinzufügen

## Sicherheit

Sicherheitslücken bitte **nicht** öffentlich melden. Siehe [SECURITY.md](SECURITY.md) für den Prozess zur verantwortungsvollen Offenlegung.

## Lizenz

[AGPL-3.0](LICENSE) — Du kannst Mandari frei nutzen, modifizieren und verteilen, solange Änderungen ebenfalls unter AGPL-3.0 veröffentlicht werden.

## Danksagung

- [OParl](https://oparl.org) — Standard für offene Ratsinformationssysteme
- Alle [Contributors](https://github.com/mandariOSS/mandari/graphs/contributors)

---

<p align="center">
  <sub>Mit ❤️ für Demokratie und Transparenz</sub>
</p>

---

<p align="center">
  <sub>Copyright 2025 Sven Konopka and contributors. Licensed under <a href="LICENSE">AGPL-3.0-or-later</a>.</sub>
</p>
