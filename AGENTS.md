# Mandari: Kontext für KI-Assistenten

> Gemeinsamer Kontext für alle KI-Assistenten (Claude, Copilot, Codex, Gemini usw.).
> Claude-spezifische Ergänzungen stehen in CLAUDE.md, das diese Datei einbindet.

## Verbindliche Arbeitsregeln

Vollständig in [docs/WEITERENTWICKLUNG.md](docs/WEITERENTWICKLUNG.md) und
[docs/RELEASE_POLITIK.md](docs/RELEASE_POLITIK.md). Gehen Anweisungen auseinander, gelten diese beiden Seiten.

1. **Aufgaben stehen nur in GitHub** (Issues, Meilensteine, Projekt-Board). Konzept- und Plandateien sind
   Hintergrund, keine Aufgabenliste. Keine Arbeit ohne Issue; ausgenommen sind Dependabot,
   Release-Vorbereitung und Rückportierungen.
2. **Release alle zwei Wochen** (Dienstag, Stichtag Donnerstag davor). Jeder Meilenstein ist ein Zug. Was
   nicht fertig ist, fährt mit dem nächsten; der Termin bleibt.
3. **Prioritäten in dieser Reihenfolge:** Sicherheit innerhalb der Frist, Störungen und kritische Fehler,
   Zusagen mit Termin, der laufende Meilenstein, Rückstand. Begonnenes vor Neuem.
4. **Ein Issue zur Zeit** je Agent, höchstens sechs gleichzeitig „In Arbeit“. Eigene rote PRs und offene
   Befunde gehen neuer Arbeit vor. Neue Wünsche kommen in den nächsten Zug, nicht still in den laufenden.
5. **Branch von `dev`, PR gegen `dev`, Merge-Queue.** `Closes #…` nur, wenn der PR das Issue ganz erledigt,
   sonst `Teil von #…`. CHANGELOG-Zeile unter `[Unreleased]` im selben PR.
6. **Ältere Versionen:** Korrekturen nie direkt auf `release/v*` oder `main`. Erst in `dev` mergen, dann bei
   Sicherheitskorrekturen ab „mittel“ und kritischen Fehlern zurückportieren:
   `python scripts/rueckportieren.py <PR> --grund <grund>` (Abschnitt 9 in WEITERENTWICKLUNG.md). Keine
   Migrationen und keine neuen Funktionen in Pflegelinien.
7. **Sicherheitslücken nie öffentlich:** kein öffentliches Issue, neutrale PR-Titel und -Texte.
8. **Dokumentation:** `docs/` beschreibt, was im Code existiert. Pläne gehören in Issues oder ADRs.
   Überholtes wird entfernt, nicht als veraltet markiert.

## Projekt-Überblick

Mandari ist eine Open-Source-Plattform für kommunalpolitische Transparenz in Deutschland. Sie basiert auf dem **OParl-Standard** für Ratsinformationssysteme (RIS) und besteht aus drei Produkten:

1. **mandari Insight** (Bürgerportal) - Bürger:innen-Zugang zu Ratsinformationen
2. **mandari Work** - Arbeitsbereich für politische Organisationen (Fraktionen)
3. **mandari Session** - Sitzungsdienst und Ratsinformationssystem der Verwaltung

---

## Technologie-Stack

| Komponente | Technologie |
|------------|-------------|
| **Backend** | Django 6.1 (Python 3.12 bis 3.14) |
| **Frontend** | Django Templates + HTMX + Alpine.js |
| **CSS** | Tailwind CSS |
| **Datenbank** | PostgreSQL 16 |
| **Cache, Sessions, WebSocket** | Redis 7 (Cache, `cached_db`-Sessions, Channel Layer, Drossel je Host) |
| **Volltextsuche** | Elasticsearch 8 |
| **Hintergrundarbeit** | `manage.py events_worker` in den Diensten `worker`, `worker-heavy` (Warteschlangen `ocr`, `ai`) und `worker-live` (`live`); `minutes-orchestrator` für die GPU-Rechenknoten der Protokollierung; Ingestor als eigener Dienst |
| **Admin** | django-unfold |
| **Static Files** | WhiteNoise |
| **Verschlüsselung** | AES-256-GCM (tenant-spezifisch) |

---

## Projektstruktur

```
mandari/
├── mandari/                    # Haupt-Django-Projekt
│   ├── apps/                   # Django Apps
│   │   ├── accounts/           # Benutzer-Authentifizierung
│   │   ├── common/             # Shared Utilities
│   │   ├── events/             # Ereignistechnik: Journal, Aufträge, Zeitpläne, Worker (events_worker)
│   │   ├── minutes/            # Aufzeichnung, Transkription, KI-Protokoll (GPU-Rechenknoten)
│   │   ├── provisioning/       # Provisioning-API (Work-Organisationen anlegen und abgleichen)
│   │   ├── session/            # Verwaltungs-RIS
│   │   ├── tenants/            # Multi-Tenant & RBAC
│   │   └── work/               # Fraktions-Arbeitsbereich
│   │       ├── dashboard/      # Übersicht
│   │       ├── faction/        # Interne Fraktionssitzungen
│   │       ├── meetings/       # RIS-Sitzungsvorbereitung
│   │       ├── motions/        # Anträge
│   │       ├── notifications/  # Benachrichtigungen
│   │       ├── organization/   # Org-Einstellungen
│   │       ├── ris/            # RIS-Datenansicht
│   │       ├── support/        # Support-Tickets
│   │       └── tasks/          # Aufgabenverwaltung
│   ├── hub/                    # Datendrehscheibe: Verträge, Befehle, RIS-Modell, Sichten, Adapter, offene API
│   ├── insight_core/           # OParl-Datenmodelle & Services
│   │   └── services/           # Text-Extraktion, OCR, Suche
│   ├── insight_sync/           # Sync-Steuerung (Watchdog, Protokoll); der Sync läuft im Ingestor
│   ├── insight_search/         # Elasticsearch-Integration
│   ├── insight_ai/             # KI-Pipelines
│   ├── frontend/               # TypeScript-Quellen (Alpine-Komponenten, Editor), gebaut mit Vite
│   ├── templates/              # Django Templates
│   ├── static/                 # Statische Dateien
│   └── mandari/settings.py     # Django-Einstellungen
├── ingestor/                   # Python-basierter OParl-Ingestor (httpx, SQLAlchemy)
├── shared/                     # Gemeinsame Pakete für Anwendung und Ingestor
│   ├── mandari_oparl/          # OParl-Typen, Kennungen, robots.txt, Drossel je Host
│   └── mandari_dokumente/      # Texterkennung (pypdf, optional Mistral, Tesseract)
├── AGENTS.md                   # Diese Datei (gemeinsamer Kontext)
├── CLAUDE.md                   # Claude-spezifische Ergänzungen
└── LICENSE                     # AGPL-3.0-or-later
```

---

## Kern-Apps im Detail

### 1. `apps.accounts` - Authentifizierung

**Zweck**: Benutzer-Konten und Sicherheit

**Modelle**:
- `User` - Custom User (E-Mail als Login, UUID als PK)
- `TwoFactorDevice` - TOTP-basierte 2FA mit Backup-Codes
- `UserSession` - Aktive Sitzungen mit IP/Standort-Tracking
- `LoginAttempt` - Rate Limiting (5 Versuche pro 15 Minuten)
- `SecurityNotification` - Sicherheitswarnungen an Benutzer

**Wichtige Views**:
- `LoginView` - Login mit Rate Limiting
- `RegisterView` - Registrierung per Einladung
- `SelfRegisterView` - Selbstregistrierung mit E-Mail-Bestätigung, wenn die Organisation sie zulässt
- Password Reset Flow (4 Schritte)

---

### 2. `apps.tenants` - Multi-Tenant & Berechtigungen

**Zweck**: Organisationen, Rollen und Berechtigungen verwalten

**Datenmodell-Hierarchie**:

```
Organization (Tenant/Mandant)
├── PartyGroup (Partei-Hierarchie: Bundespartei → Landesverband → Kreisverband)
├── OParlBody (Kommune-Verknüpfung für regionale Zuordnung)
├── encryption_key (tenant-spezifischer Schlüssel)
└── smtp_* (eigene E-Mail-Konfiguration pro Org)

Role (Rolle innerhalb einer Organisation)
├── permissions (M2M → Permission)
├── is_admin (Vollzugriff-Flag)
└── priority (für Konfliktauflösung)

Membership (Verknüpfung User ↔ Organization)
├── roles (M2M → Role)
├── individual_permissions (zusätzliche Berechtigungen)
└── denied_permissions (explizit verweigerte Berechtigungen)
```

**Drei-Ebenen Berechtigungssystem**:
1. **Rollen-Berechtigungen** - Aus allen zugewiesenen Rollen gesammelt
2. **Individuelle Berechtigungen** - Pro Membership zusätzlich gewährt
3. **Verweigerte Berechtigungen** - Pro Membership explizit ausgeschlossen (höchste Priorität!)

Rechte und Standardrollen sind in `apps/common/permissions.py` definiert (`PERMISSIONS`,
`PERMISSION_CATEGORIES`, `DEFAULT_ROLES`); `python manage.py setup_roles` legt die Rollen an.

**80 Einzelrechte** in 15 Bereichen:
- Dashboard, Fraktionssitzungen, Tagesordnung, Stimm- und Rederecht, Anträge
- Protokolle, Ratssitzungen (RIS), Aufgaben, RIS-Daten, Arbeitsgruppen
- Dokumente, Mitglieder, Organisation, Gastzugänge (Dokumente), Support

**Zehn Standardrollen:**
- Administrator (alle Rechte außer dem Stimmrecht; das kommt über eine weitere Rolle oder als Einzelrecht)
- Fraktionsvorsitz und Stellv. Vorsitz (Sitzungsleitung, weitreichende Verwaltungsrechte)
- Fraktionsmitglied (Stimm- und Rederecht)
- Sachkundige/r Bürger/in und Bezirksvertreter/in (beratend, kein Stimmrecht)
- Fraktionspersonal (Organisation und Vorbereitung, kein Stimmrecht)
- Geschäftsführung (Verwaltung der Organisation, kein Stimmrecht)
- AG-Sprecher/in (leitet eine Arbeitsgruppe)
- Parteimitglied (öffentlicher Teil der Fraktionssitzung, noch nicht freigeschaltet)

---

### 3. `apps.work` - Fraktions-Arbeitsbereich

**Zweck**: Interner Arbeitsbereich für politische Organisationen

#### `work.faction` - Interne Fraktionssitzungen

**Modelle**:
- `FactionMeeting` - Sitzung mit Status, Ort, Video-Link, Protokoll
- `FactionAgendaItem` - TOPs mit hierarchischer Struktur (1, 1.1, 1.2, 2, ...)
- `FactionAttendance` - Anwesenheit mit RSVP (eingeladen → zugesagt → anwesend)
- `FactionProtocolEntry` - Protokolleinträge (Notiz, Beschluss, Aufgabe)
- `FactionDecision` - Formelle Beschlüsse

**Status-Workflow für Sitzungen**:
```
draft → invited → scheduled → in_progress → completed
                                         ↘ cancelled
```

#### `work.meetings` - RIS-Sitzungsvorbereitung

**Modelle**:
- `MeetingPreparation` - Interne Vorbereitung zu öffentlicher RIS-Sitzung
- `AgendaItemNote` - Private oder organisationsweite Notizen zu TOPs
- `PaperComment` - Kommentare zu Vorlagen (gremienübergreifend nutzbar)

**Sichtbarkeits-Stufen**:
- `private` - Nur der Autor sieht den Inhalt
- `organization` - Alle Mitglieder der Organisation
- `consulting` - Alle beratenden Gremien (nur für PaperComment)

#### `work.motions` - Anträge

**Modelle**:
- `Motion` - Antrag mit Versionierung und Workflow
- `MotionRevision` - Versionshistorie mit Änderungs-Tracking
- `MotionComment` - Interne Diskussion zum Antrag
- `MotionShare` - Antrag mit anderen Organisationen teilen
- `MotionTemplate` - Wiederverwendbare Vorlagen

#### `work.tasks` - Aufgabenverwaltung

**Modelle**:
- `Task` - Aufgabe mit Zuweisung, Frist, Status
- `TaskComment` - Kommentare/Updates zur Aufgabe
- `related_faction_meeting` - Optionale Verknüpfung zu Fraktionssitzung

---

### 4. `apps.session` - Verwaltungs-RIS

**Zweck**: Ratsinformationssystem für kommunale Verwaltung

**Kern-Modelle**:
- `SessionTenant` - Kommune als Mandant
- `SessionMeeting` - Ratssitzungen mit vollem Workflow
- `SessionPaper` - Vorlagen/Drucksachen
- `SessionApplication` - Anträge von Fraktionen (mit Auto-Aktenzeichen)
- `SessionAgendaItem` - TOPs mit Abstimmungsergebnis
- `SessionProtocol` - Protokoll mit Genehmigungsworkflow
- `SessionAttendance` - Anwesenheitserfassung
- `SessionAllowance` - Sitzungsgelder-Abrechnung
- `SessionAuditLog` - Vollständiges Audit-Log aller Aktionen

---

### 5. `insight_core` - OParl-Datenmodelle

**Zweck**: Öffentliche RIS-Daten nach deutschem OParl-Standard (Version 1.1)

---

## OParl-Standard im Detail

OParl ist der deutsche Standard für offene Ratsinformationssysteme. Er definiert eine einheitliche REST-API für den anonymen, lesenden Zugriff auf öffentliche parlamentarische Daten.

**Offizielle Spezifikation**: https://oparl.org/spezifikation/online-ansicht/

### Was ist OParl?

OParl ermöglicht es Kommunen, ihre Ratsinformationen (Sitzungen, Vorlagen, Beschlüsse) als **Open Data** bereitzustellen. Bürger:innen, Journalist:innen und Entwickler:innen können so auf strukturierte Daten zugreifen.

**Kernprinzipien**:
- **Read-Only**: Nur lesender Zugriff (HTTP GET)
- **JSON**: Alle Daten im JSON-Format (UTF-8)
- **URL als ID**: Jedes Objekt hat eine eindeutige, kanonische URL
- **CORS**: Server müssen `Access-Control-Allow-Origin: *` setzen
- **Pagination**: Große Listen werden paginiert

### OParl-Entitäten und ihre Beziehungen

```
OParlSource (Mandari-spezifisch: RIS-API-Registrierung)
    └── OParlBody (Kommune/Körperschaft)
            │
            ├── OParlOrganization (Gremium/Fraktion)
            │       └── OParlMembership ←→ OParlPerson
            │
            ├── OParlMeeting (Sitzung)
            │       ├── organizations (M2M → welche Gremien tagen)
            │       └── OParlAgendaItem (Tagesordnungspunkt)
            │               └── OParlConsultation → OParlPaper
            │
            ├── OParlPaper (Vorlage/Drucksache)
            │       ├── OParlFile (PDF-Dokumente)
            │       └── OParlConsultation (in welchen Sitzungen beraten)
            │
            ├── OParlPerson (Ratsmitglied)
            ├── OParlLocation (Sitzungsorte)
            └── OParlLegislativeTerm (Wahlperiode)
```

### Entitäten-Referenz

| OParl-Entität | Deutsche Bezeichnung | Beschreibung | Wichtige Felder |
|---------------|---------------------|--------------|-----------------|
| `System` | System | API-Einstiegspunkt | `body`, `otherOparlVersions` |
| `Body` | Kommune | Körperschaft | `name`, `website`, `organization`, `meeting`, `paper` |
| `Organization` | Gremium | Ausschuss, Fraktion, Beirat | `name`, `organizationType`, `classification`, `startDate`, `endDate` |
| `Person` | Person | Ratsmitglied | `name`, `familyName`, `givenName`, `email`, `phone` |
| `Membership` | Mitgliedschaft | Person in Gremium | `person`, `organization`, `role`, `votingRight`, `startDate`, `endDate` |
| `Meeting` | Sitzung | Rats-/Ausschusssitzung | `name`, `start`, `end`, `location`, `organization`, `agendaItem` |
| `AgendaItem` | TOP | Tagesordnungspunkt | `number`, `name`, `public`, `result`, `resolutionText`, `consultation` |
| `Paper` | Vorlage | Drucksache/Antrag | `name`, `reference`, `paperType`, `date`, `mainFile`, `auxiliaryFile`, `consultation` |
| `Consultation` | Beratung | Paper in Meeting | `paper`, `agendaItem`, `meeting`, `organization`, `authoritative`, `role` |
| `File` | Datei | Dokument (PDF) | `name`, `fileName`, `mimeType`, `accessUrl`, `downloadUrl`, `size` |
| `Location` | Ort | Sitzungsort | `description`, `streetAddress`, `postalCode`, `locality`, `geojson` |
| `LegislativeTerm` | Wahlperiode | Legislaturperiode | `name`, `startDate`, `endDate` |

### Die zentrale Verknüpfung: Consultation

Die `Consultation` ist das **Bindeglied** zwischen Vorlagen (`Paper`) und Sitzungen (`Meeting`):

```
Paper (z.B. "Antrag: Neuer Spielplatz Südpark")
    │
    ├── Consultation 1:
    │       role = "Vorberatung"
    │       authoritative = False
    │       └── AgendaItem (TOP 7) → Meeting (Jugendhilfeausschuss, 12.03.)
    │
    └── Consultation 2:
            role = "Entscheidung"
            authoritative = True  ← Hier wird final entschieden!
            └── AgendaItem (TOP 12) → Meeting (Rat, 25.03.)
```

**Wichtige Felder**:
- `authoritative: true` = Dies ist die **entscheidende** Beratung
- `role` = Art der Beratung (Vorberatung, Entscheidung, Kenntnisnahme, federführend)

### OParl-Synchronisation in Mandari

**Dienst**: `ingestor/` (eigener Container). Der Ingestor-Daemon synchronisiert alle Quellen
regelmäßig selbst; einen Cron-Eintrag braucht es nicht. Manuelle Läufe im Ingestor-Container:

```bash
# Incremental Sync aller registrierten Quellen
docker compose exec ingestor python -m src.main sync --all

# Full Sync (alle Daten komplett neu laden)
docker compose exec ingestor python -m src.main sync --all --full

# Einzelne Quelle über ihre registrierte Adresse (eine unbekannte Adresse lehnt der Befehl ab)
docker compose exec ingestor python -m src.main sync --source https://oparl.stadt-muenster.de/system

# Nur eine Kommune dieser Quelle
docker compose exec ingestor python -m src.main sync --source https://oparl.stadt-muenster.de/system --filter https://oparl.stadt-muenster.de/bodies/0001

# Mit mehr parallelen Requests (schneller, aber mehr Last)
docker compose exec ingestor python -m src.main sync --all --concurrent 20
```

**Sync-Workflow**:
1. `OParlSource` → System-Endpoint abrufen
2. `OParlBody` → Bodies der Quelle laden
3. Pro Body: Organizations, Persons, Meetings, Papers parallel laden
4. Verknüpfungen (Memberships, Consultations, Files) auflösen
5. `raw_json` speichern für spätere Analyse

### Datenfluss im Projekt

```
Externe OParl-API (z.B. oparl.stadt-muenster.de)
    │
    ▼
Ingestor (ingestor/, eigener Container)
    │  - Paginierte Listen abrufen
    │  - Objekte in DB speichern
    │  - Verknüpfungen auflösen
    ▼
insight_core (Django ORM Models)
    │
    ├──► Insight (/insight/)
    │       └── Bürger:innen sehen Sitzungen, Vorlagen, Beschlüsse
    │
    └──► Work Portal
            ├── work.ris (RIS-Datenansicht für Fraktionen)
            └── work.meetings (Sitzungsvorbereitung)
                    └── Notizen/Positionen zu OParlAgendaItems
```

### Django-Models: OParl-Felder

Jedes OParl-Model hat diese Standard-Felder:

```python
class OParlXxx(models.Model):
    # Interne UUID (für Django)
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)

    # OParl-URL als externe ID (eindeutig!)
    external_id = models.TextField(unique=True, db_index=True)

    # OParl-Zeitstempel (vom RIS)
    oparl_created = models.DateTimeField(blank=True, null=True)
    oparl_modified = models.DateTimeField(blank=True, null=True)

    # Rohe JSON-Daten (für Debugging/Erweiterung)
    raw_json = models.JSONField(default=dict)

    # Mandari-Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
```

### Mandari-Erweiterungen (nicht im OParl-Standard)

| Model | Zusätzliches Feld | Zweck |
|-------|-------------------|-------|
| `OParlSource` | `sync_config`, `last_sync`, `is_active` | Sync-Verwaltung |
| `OParlBody` | `display_name`, `logo` | Frontend-Anpassung |
| `OParlBody` | `latitude`, `longitude`, `bbox_*` | Geo-Koordinaten für Karten |
| `OParlBody` | `osm_relation_id` | OpenStreetMap-Verknüpfung |
| `OParlBody` | `slug` | SEO-freundliche URLs, Bürgerportal `/insight/k/<slug>/` (`set_body_slugs`) |
| `OParlBody` | `file_cache_dir` | Festgeschriebenes Verzeichnis im Dokument-Cache (unabhängig vom Slug) |
| `OParlPaper` | `summary` | KI-generierte Zusammenfassung |
| `OParlPaper` | `locations` | Extrahierte Ortsreferenzen |
| `OParlFile` | `local_path` | Lokale Dateispeicherung |
| `OParlFile` | `text_content` | OCR-extrahierter Text |
| `OParlFile` | `text_extraction_status/method/error` | Extraktions-Tracking |
| `LocationMapping` | - | Ortsname → Koordinaten-Mapping |
| `TileCache` | - | Gecachte OSM-Kartenkacheln |

### Services (insight_core/services/)

| Service | Zweck |
|---------|-------|
| `document_extraction.py` | Abruf von Dokumenten und Texterkennung über `mandari_dokumente` (shared/: pypdf → Mistral → Tesseract seitenweise) |
| `text_extraction_job.py` | Aufträge `file.read_text_layer` (Warteschlange `text`, nur Textebene) und `file.extract_text` (Warteschlange `ocr`) und Zeitplan, bei `TEXT_EXTRACTION_RUNNER=worker` |
| `search_service.py` | Elasticsearch Multi-Index-Suche |

### SEO & Sitemaps

| Datei | Zweck |
|-------|-------|
| `insight_core/seo.py` | SEO-Context-Generatoren für alle Entitäten |
| `insight_core/views/sitemap.py` | Hierarchische Sitemaps pro Kommune |
| `insight_core/signals.py` | Auto-Indexierung bei Model-Änderungen |
| `insight_search/synonyms.py` | 100+ deutsche Kommunal-Synonyme |

### Code-Beispiele für OParl-Zugriff

```python
from insight_core.models import (
    OParlBody, OParlOrganization, OParlMeeting,
    OParlAgendaItem, OParlPaper, OParlConsultation
)
from django.utils import timezone

# Alle Gremien einer Kommune
body = OParlBody.objects.get(short_name="Münster")
committees = body.organizations.filter(organization_type="committee")
factions = body.organizations.filter(organization_type="faction")

# Kommende Sitzungen
upcoming_meetings = OParlMeeting.objects.filter(
    body=body,
    start__gte=timezone.now(),
    cancelled=False
).order_by("start")[:10]

# Sitzungen eines bestimmten Gremiums
org = OParlOrganization.objects.get(name__icontains="Hauptausschuss")
org_meetings = OParlMeeting.objects.filter(
    organizations=org
).order_by("-start")

# Papers/Vorlagen zu einem Tagesordnungspunkt
agenda_item = OParlAgendaItem.objects.get(...)

# Methode 1: Helper-Methode
papers = agenda_item.get_papers()

# Methode 2: Über Consultation
papers = OParlPaper.objects.filter(
    consultations__agenda_item_external_id=agenda_item.external_id
).distinct()

# Alle Beratungen zu einem Paper
paper = OParlPaper.objects.get(reference="V/2026/0123")
consultations = paper.consultations.select_related('paper')
for c in consultations:
    print(f"{c.role} - authoritative: {c.authoritative}")

# Aktive Mitgliedschaften einer Person
from insight_core.models import OParlPerson
person = OParlPerson.objects.get(name__icontains="Müller")
active = person.memberships.filter(
    models.Q(end_date__isnull=True) |
    models.Q(end_date__gte=timezone.now().date())
)
```

### Verknüpfung mit Work-Portal

Das Work-Portal nutzt OParl-Daten für die Sitzungsvorbereitung:

```python
# work.meetings.models
class MeetingPreparation(models.Model):
    organization = models.ForeignKey("tenants.Organization", ...)

    # Verknüpfung zur OParl-Sitzung
    meeting = models.ForeignKey(
        "insight_core.OParlMeeting",
        on_delete=models.CASCADE
    )

class AgendaItemNote(models.Model):
    # Verknüpfung zum OParl-TOP
    agenda_item = models.ForeignKey(
        "insight_core.OParlAgendaItem",
        on_delete=models.CASCADE
    )
    # Interne Notizen (verschlüsselt)
    content_encrypted = EncryptedTextField()

class PaperComment(models.Model):
    # Kommentar zur OParl-Vorlage (gremienübergreifend!)
    paper = models.ForeignKey(
        "insight_core.OParlPaper",
        on_delete=models.CASCADE
    )
```

---

## Coding-Konventionen

### Python/Django Patterns

```python
# Models mit Type Hints und Verschlüsselung
from django.db import models
from apps.common.encryption import EncryptedTextField, EncryptionMixin

class MyModel(EncryptionMixin, models.Model):
    """Beispiel-Model mit verschlüsseltem Feld."""

    title = models.CharField(max_length=200)
    content_encrypted = EncryptedTextField(verbose_name="Inhalt")
    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="my_models"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Mein Model"
        verbose_name_plural = "Meine Models"

    def get_encryption_organization(self):
        """Erforderlich für EncryptionMixin."""
        return self.organization
```

```python
# Views mit Standard-Mixins
from apps.common.mixins import WorkViewMixin
from django.views.generic import TemplateView, ListView

class MyListView(WorkViewMixin, ListView):
    """View mit Organisation-Kontext und Berechtigung."""

    template_name = "work/my_list.html"
    permission_required = "my_permission"
    context_object_name = "items"

    def get_queryset(self):
        # WICHTIG: Immer nach Organisation filtern!
        return MyModel.objects.filter(organization=self.organization)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Meine Liste"
        return ctx
```

### Verfügbare Mixins

| Mixin | Zweck |
|-------|-------|
| `OrganizationMixin` | Setzt `request.organization` und `request.membership` |
| `PermissionRequiredMixin` | Prüft `permission_required` Attribut |
| `HTMXMixin` | Bietet `is_htmx` (HTMX-Anfrage erkennen); außerhalb von Views `request.htmx` aus django-htmx |
| `WorkViewMixin` | Kombiniert alle obigen für Work-Portal |

### Templates

Verbindlich: [`docs/ENGINEERING_STANDARDS.md`](docs/ENGINEERING_STANDARDS.md), Abschnitt 5 (Regeln, Muster
für Seiten ohne Inline-Code, E-Mails, Barrierefreiheit), und die ADRs unter `docs/adr/`. Kurz: Templates
sind Struktur, nicht Programm. Kein `<script>`/`<style>` in Seiten-Templates, höchstens 300 Zeilen,
wiederkehrendes Markup aus `templates/cotton/` (Vorschau unter `/dev/ui/`), JavaScript als TypeScript unter
`frontend/`. Template-Kommentare mit `{# … #}` nur einzeilig; ein mehrzeiliges `{# #}` erscheint als Text
auf der Seite (`scripts/check_template_comments.py`).

### Frontend-Technologien

| Technologie | Verwendung |
|-------------|------------|
| **HTMX** | Dynamische Interaktionen ohne JavaScript |
| **Alpine.js** | Komplexere Client-Logik (State, Events) |
| **Tailwind CSS** | Utility-First Styling |
| **Lucide Icons** | `<i data-lucide="icon-name"></i>` |

---

## Entwicklungs-Befehle

```bash
# Server starten
cd mandari
python manage.py runserver

# Datenbank-Migrationen
python manage.py makemigrations
python manage.py migrate

# Rollen/Berechtigungen synchronisieren
python manage.py setup_roles       # Standard-Rollen erstellen
python manage.py fix_permissions   # Probelauf: zeigt, was fehlt
python manage.py fix_permissions --fix  # ergänzt fehlende Rechte/Standardrollen, ändert keine vorhandenen

# OParl-Daten (Sync läuft im Ingestor, siehe oben)
python manage.py extract_texts      # Texterkennung einplanen (Aufträge file.read_text_layer/file.extract_text, nur mit TEXT_EXTRACTION_RUNNER=worker, lädt nicht)

# Elasticsearch konfigurieren (Synonyme, Typo-Toleranz)
python manage.py setup_elasticsearch

# Statische Dateien (Production)
python manage.py collectstatic

# Interaktive Shell
python manage.py shell
```

---

## Umgebungsvariablen

Alle Variablen mit Erläuterung stehen in [`.env.example`](.env.example), Betrieb, Texterkennung und
KI-Anbieter in [DEPLOYMENT.md](DEPLOYMENT.md). Für die Arbeit am Code wichtig:

- KI-Aufrufe gehen nur an Hosts aus `KI_ERLAUBTE_HOSTS`; leer heißt KI aus. Anbieter, Modell und Schlüssel
  stehen im Admin (KI-Einstellungen), nie fest im Code (Issue #950).
- `TEXT_EXTRACTION_RUNNER` wird in Anwendung und Ingestor gleich gesetzt.
- `DEBUG=True` nie in Produktion.

---

## URL-Struktur

```
/robots.txt                     # SEO: robots.txt
/sitemap-insight-index.xml      # SEO: Sitemap-Index
/sitemap-insight-<slug>.xml     # SEO: Pro Kommune
/insight/                       # Insight-Portal (Startseite: Kommunenauswahl)
    /vorgaenge/<uuid>/          # Paper-Detail
    /termine/<uuid>/            # Meeting-Detail
    /gremien/<uuid>/            # Organization-Detail
    /personen/<uuid>/           # Person-Detail
/api/, /oparl/, /data/dcat/     # Schnittstellen (docs/SCHNITTSTELLE.md)
/admin/                         # Django Admin (Unfold Theme)
/accounts/                      # Authentifizierung
    /login/
    /logout/
    /password-reset/
    /register/                  # Registrierung per Einladung
    /register/<org_slug>/       # Selbstregistrierung
/work/<org_slug>/               # Work Portal (pro Organisation)
    /dashboard/                 # Übersicht
    /ris/                       # RIS-Datenansicht
    /meetings/                  # Sitzungsvorbereitung
        /<meeting_id>/prepare/  # Vorbereitung einer Sitzung
    /faction/                   # Fraktionssitzungen
        /<meeting_id>/
    /motions/                   # Anträge
    /tasks/                     # Aufgaben
    /organization/              # Organisationseinstellungen
        /members/
        /roles/
/session/<tenant_slug>/         # Session RIS (Verwaltung)
```

---

## Verschlüsselung

Sensible Felder sind `EncryptedTextField` aus `apps/common/encryption.py` (Feldname endet auf `_encrypted`,
Modell mit `EncryptionMixin` und `get_encryption_organization()`). Schlüsselhierarchie, Verfahren und
Schlüsselwechsel: [docs/KRYPTOKONZEPT.md](docs/KRYPTOKONZEPT.md).

---

## Wichtige Dateien

| Datei | Beschreibung |
|-------|--------------|
| `mandari/settings.py` | Haupt-Django-Einstellungen |
| `mandari/urls.py` | Root-URL-Konfiguration |
| `apps/common/permissions.py` | Alle Berechtigungsdefinitionen |
| `apps/common/encryption.py` | Verschlüsselungs-Utilities |
| `apps/common/mixins.py` | Wiederverwendbare View-Mixins |
| `templates/work/base_work.html` | Basis-Template für Work Portal |
| `templates/cotton/` | Wiederverwendbare UI-Komponenten (django-cotton) |

---

## Architektur-Prinzipien

1. **Multi-Tenancy** - Strikte Datenisolation pro Organisation
2. **RBAC** - 80 Einzelrechte mit 3-Ebenen-System
3. **Verschlüsselung** - Sensible Daten mit AES-256-GCM verschlüsselt
4. **OParl-Standard** - Kompatibilität mit deutschen Ratsinformationssystemen
5. **HTMX-First** - Minimales JavaScript, maximale Interaktivität
6. **Audit-Trail** - Vollständige Nachverfolgbarkeit aller Aktionen

### Datendrehscheibe: Regeln für Beiträge

Verbindlich: [`docs/ENGINEERING_STANDARDS.md`](docs/ENGINEERING_STANDARDS.md), Abschnitt 1, und die
[ADR Schichtenmodell](docs/adr/20260929-schichtenmodell.md). Kurz: keine fremden Tabellen (RIS-Daten nur
über `hub.ris.selectors`), Ereignisse veröffentlicht nur der Eigentümer, Befehle statt Direktaufruf, und
jedes Ereignis und jeder Befehl hat ein Schema unter `hub/contracts/schemas/`.

---

## Best Practices für KI-Assistenten

### Machen (DO)

- **Immer `WorkViewMixin`** für alle Work-Portal-Views verwenden
- **Berechtigungen prüfen** via `permission_required` Attribut
- **`EncryptedTextField`** für sensible Daten (Notizen, Kommentare, etc.)
- **Queryset filtern** nach `organization` in allen Views
- **HTMX** für einfache Interaktionen, **Alpine.js** nur für komplexe Logik
- **Type Hints** in Python-Code verwenden
- **Deutsche Bezeichnungen** für verbose_name in Models

### Vermeiden (DON'T)

- **Niemals** Daten ohne Organisations-Filter abfragen
- **Keine hardcodierten** Secrets oder API-Keys
- **Nicht** `User.objects.all()` - immer über Membership gehen
- **Keine synchronen** externen API-Calls in Request-Cycle
- **Keine inline-styles** - immer Tailwind CSS Klassen
- **Kein** `onclick="..."` in Templates: Aktionen über `data-*`-Attribute (`frontend/js/actions.ts`)
  oder eine registrierte Alpine-Komponente (Engineering-Standards, Abschnitt 5.1)

### Typische Code-Patterns

```python
# View mit Berechtigung und Organisation-Filter
class MyView(WorkViewMixin, TemplateView):
    permission_required = "my_permission"

    def get_queryset(self):
        return MyModel.objects.filter(organization=self.organization)

# HTMX-Partial wählen
def get_template_names(self):
    if self.is_htmx:
        return ["work/partials/my_partial.html"]
    return ["work/my_full.html"]

# Verschlüsselte Daten speichern
def post(self, request, *args, **kwargs):
    obj = MyModel(organization=self.organization)
    obj.set_content_encrypted(request.POST.get("content"))
    obj.save()
```

---

## Häufige Aufgaben

### Neue Organisation erstellen

1. Django Admin → Tenants → Organizations → Add
2. Slug, Name, OParl Body zuweisen
3. `python manage.py setup_roles` ausführen

### Benutzer zu Organisation einladen

1. Work Portal → Einstellungen → Mitglieder → Einladen
2. E-Mail eingeben, Rollen wählen
3. System sendet Einladungslink per E-Mail

### Neue Berechtigung hinzufügen

1. `apps/common/permissions.py` → `PERMISSIONS` dict erweitern
2. `python manage.py fix_permissions --fix` ausführen (legt die Berechtigung an; vorhandene Rollen bleiben unverändert)
3. Rollen im Admin oder Code anpassen

### Neues Model mit Migration

```bash
# 1. Model in models.py definieren
# 2. Migration erstellen
python manage.py makemigrations <app_name> -n "beschreibung"
# 3. Migration anwenden
python manage.py migrate
```

---

## Referenzen

- **OParl-Spezifikation**: https://oparl.org/spezifikation/
- **Projekt-Repository**: https://github.com/mandariOSS/mandari
- **Dokumentation**: `docs/` (Index `docs/README.md`), öffentlich https://docs.mandari.de
- **Weiterentwicklung und Release-Politik**: `docs/WEITERENTWICKLUNG.md`, `docs/RELEASE_POLITIK.md`
- **Django Dokumentation**: https://docs.djangoproject.com/
- **HTMX**: https://htmx.org/
- **Alpine.js**: https://alpinejs.dev/
- **Tailwind CSS**: https://tailwindcss.com/
