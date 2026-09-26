# Mandari 2.0 - AI Assistant Context

> Gemeinsamer Kontext für alle KI-Assistenten (Claude, Copilot, Codex, Gemini usw.).
> Claude-spezifische Ergänzungen stehen in CLAUDE.md, das diese Datei einbindet.

## Projekt-Überblick

Mandari ist eine Open-Source-Plattform für kommunalpolitische Transparenz in Deutschland. Sie basiert auf dem **OParl-Standard** für Ratsinformationssysteme (RIS) und bietet drei Portale:

1. **Public Portal** - Bürger:innen-Zugang zu Ratsinformationen
2. **Work Portal** - Arbeitsbereich für politische Organisationen (Fraktionen)
3. **Session RIS** - Verwaltungs-RIS für kommunale Sachbearbeiter

---

## Technologie-Stack

| Komponente | Technologie |
|------------|-------------|
| **Backend** | Django 6.1 (Python 3.12+) |
| **Frontend** | Django Templates + HTMX + Alpine.js |
| **CSS** | Tailwind CSS |
| **Datenbank** | PostgreSQL 16 |
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
│   ├── insight_core/           # OParl-Datenmodelle & Services
│   │   └── services/           # Text-Extraktion, OCR, Suche
│   ├── insight_sync/           # OParl-Synchronisation
│   ├── insight_search/         # Elasticsearch-Integration
│   ├── insight_ai/             # KI-Pipelines
│   ├── templates/              # Django Templates
│   ├── static/                 # Statische Dateien
│   └── mandari/settings.py     # Django-Einstellungen
├── ingestor/                   # Python-basierter OParl-Ingestor (httpx, SQLAlchemy)
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
- `TrustedDevice` - "Dieses Gerät merken" (30 Tage Gültigkeit)
- `UserSession` - Aktive Sitzungen mit IP/Standort-Tracking
- `LoginAttempt` - Rate Limiting (5 Versuche pro 15 Minuten)
- `PasswordResetToken` - Einmalig verwendbar, 24h gültig
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
├── PartyGroup (Partei-Hierarchie: Volt DE → Volt NRW → Volt MS)
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

**50+ Berechtigungen** organisiert in 14 Kategorien:
- Dashboard, Fraktionssitzungen, Agenda, Abstimmung/Rederecht
- Anträge, Protokolle, RIS-Sitzungen, Aufgaben
- RIS-Daten, Arbeitsgruppen, Dokumente, Mitglieder
- Organisation, Gäste, Support

**Standard-Rollen** (werden automatisch erstellt):
- Fraktionsvorsitz (Vollzugriff)
- Stellvertretender Vorsitz
- Fraktionsmitglied (mit Stimmrecht)
- Sachkundige/r Bürger/in (beratend)
- Bezirksvertreter/in (beratend)
- Fraktionspersonal (administrativ)
- Administrator (System)

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

**Modul**: `insight_sync`

```bash
# Incremental Sync (nur Änderungen seit letztem Sync)
python manage.py sync_oparl

# Full Sync (alle Daten komplett neu laden)
python manage.py sync_oparl --full

# Einzelne Quelle synchronisieren
python manage.py sync_oparl --source https://oparl.stadt-muenster.de/system

# Im Hintergrund ausführen (Django 6.0 Background Tasks)
python manage.py sync_oparl --background

# Mit mehr parallelen Requests (schneller, aber mehr Last)
python manage.py sync_oparl --concurrent 20
```

**Sync-Workflow**:
1. `OParlSource` → System-Endpoint abrufen
2. `OParlBody` → Bodies der Quelle laden
3. Pro Body: Organizations, Persons, Meetings, Papers parallel laden
4. Verknüpfungen (Memberships, Consultations, Files) auflösen
5. `raw_json` speichern für spätere Analyse

**Automatisierung via Cron**:
```bash
# Incremental alle 15 Minuten
*/15 * * * * cd /path/to/mandari && python manage.py sync_oparl

# Full Sync täglich um 3:00 Uhr
0 3 * * * cd /path/to/mandari && python manage.py sync_oparl --full
```

### Datenfluss im Projekt

```
Externe OParl-API (z.B. oparl.stadt-muenster.de)
    │
    ▼
insight_sync (Synchronisation)
    │  - Paginierte Listen abrufen
    │  - Objekte in DB speichern
    │  - Verknüpfungen auflösen
    ▼
insight_core (Django ORM Models)
    │
    ├──► Public Portal
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
| `OParlBody` | `slug` | SEO-freundliche URLs |
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
| `document_extraction.py` | PDF-Textextraktion (pypdf → Mistral → Tesseract) |
| `mistral_ocr.py` | Mistral AI OCR-Integration |
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

Verbindlich: [`docs/ENGINEERING_STANDARDS.md`](docs/ENGINEERING_STANDARDS.md) und die ADRs unter `docs/adr/`.
Kurzfassung: Templates sind Struktur, nicht Programm.

- Kein `<script>`/`<style>` in Seiten-Templates (Allowlist: Layouts, E-Mails, PDF, PWA). JavaScript lebt in
  `frontend/` (TypeScript, Vite → `static/dist/`, Layouts laden per `{% vite_asset %}`); Alpine-Komponenten
  werden in `frontend/js/main.ts` mit `Alpine.data()` registriert und im Template nur per `x-data="name"`
  referenziert. Server-Daten gehen per `{{ data|json_script:"id" }}` an den Client. Icons brauchen kein
  `lucide.createIcons()` mehr (MutationObserver). Entwicklung: `npm run watch` + `DJANGO_VITE_DEV_MODE=1`.
- Höchstens 300 Zeilen je Template; HTMX-Fragmente als Django-6-Template-Partials (`{% partialdef %}`).
- Wiederkehrendes Markup (Buttons, Cards, Badges, Formularfelder, Modals) als django-cotton-Komponente aus
  `templates/cotton/` statt kopierter Klassenketten: `<c-ui.button type="submit" full>`, `<c-ui.card title="…">`,
  `<c-form.field :field="form.email" label="E-Mail" type="email" required />`, `<c-form.errors :form="form" />`.
  Vorschau aller Komponenten unter `/dev/ui/` (nur DEBUG); Tests in `apps/common/tests/test_components.py`.
- `scripts/check_frontend_ratchet.py` misst Inline-Code und Template-Größe; die Werte dürfen nur sinken.
- Template-Kommentare mit `{# … #}` nur einzeilig; mehrzeilig `{% comment %}…{% endcomment %}`. Ein
  mehrzeiliges `{# #}` erscheint als Text auf der Seite (`scripts/check_template_comments.py` prüft das).

```html
{% extends "work/base_work.html" %}

{% block content %}
{{ board_data|json_script:"task-board-data" }}
<div x-data="taskBoard" class="...">
    {% partialdef task-list inline %}
    <ul id="task-list">…</ul>
    {% endpartialdef %}
</div>
{% endblock %}
```

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
python manage.py fix_permissions   # Berechtigungen aus Code sync

# OParl-Daten
python manage.py sync_oparl --full  # Vollständiger Sync
python manage.py extract_texts      # OCR für PDFs

# Elasticsearch konfigurieren (Synonyme, Typo-Toleranz)
python manage.py setup_elasticsearch

# Statische Dateien (Production)
python manage.py collectstatic

# Interaktive Shell
python manage.py shell
```

---

## Umgebungsvariablen

```bash
# Erforderlich
SECRET_KEY=django-insecure-change-in-production
DATABASE_URL=postgresql://user:pass@localhost:5432/mandari
ENCRYPTION_MASTER_KEY=base64-encoded-256-bit-key  # Für Verschlüsselung

# Optional
DEBUG=True  # Niemals True in Production!
ALLOWED_HOSTS=localhost,127.0.0.1
SITE_URL=https://mandari.example.com

# E-Mail (alternativ: SiteSettings im Admin)
EMAIL_HOST=smtp.example.com
EMAIL_PORT=587
EMAIL_HOST_USER=user
EMAIL_HOST_PASSWORD=pass
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL=noreply@example.com

# Logging / Tracing (apps/common/observability.py)
LOG_FORMAT=json            # json (Produktion) oder text (Entwicklung)
LOG_LEVEL=INFO             # DEBUG wird in Produktion auf INFO gekappt
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318   # leer = kein Tracing
OTEL_SERVICE_NAME=mandari-web

# Elasticsearch (Volltextsuche)
ELASTICSEARCH_URL=http://localhost:9200
ELASTICSEARCH_AUTO_INDEX=True

# Text-Extraktion
TEXT_EXTRACTION_ENABLED=True
TEXT_EXTRACTION_ASYNC=True
TEXT_EXTRACTION_MAX_SIZE_MB=50

# Mistral OCR (optional, für bessere PDF-OCR)
MISTRAL_API_KEY=sk-...
MISTRAL_OCR_RATE_LIMIT=60
```

---

## URL-Struktur

```
/                               # Public Portal (insight_core)
/robots.txt                     # SEO: robots.txt
/sitemap-insight-index.xml      # SEO: Sitemap-Index
/sitemap-insight-<slug>.xml     # SEO: Pro Kommune
/insight/                       # Insight Portal
    /vorgaenge/<uuid>/          # Paper-Detail
    /termine/<uuid>/            # Meeting-Detail
    /gremien/<uuid>/            # Organization-Detail
    /personen/<uuid>/           # Person-Detail
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

Das System verwendet AES-256-GCM für sensible Daten mit einer Schlüssel-Hierarchie:

```
Master Key (Umgebungsvariable)
    └── Tenant Key (pro Organisation, verschlüsselt gespeichert)
            └── Feld-Daten (mit Tenant Key verschlüsselt)
```

**Verwendung in Models**:

```python
from apps.common.encryption import EncryptedTextField, EncryptionMixin

class SensitiveModel(EncryptionMixin, models.Model):
    # Feldname muss auf _encrypted enden
    secret_encrypted = EncryptedTextField(verbose_name="Geheime Daten")
    organization = models.ForeignKey("tenants.Organization", ...)

    def get_encryption_organization(self):
        return self.organization

# Automatisch generierte Methoden:
obj.set_secret_encrypted("Klartext")      # Verschlüsseln und speichern
plain = obj.get_secret_decrypted()        # Entschlüsseln und zurückgeben
```

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
| `templates/components/` | Wiederverwendbare UI-Komponenten |

---

## Architektur-Prinzipien

1. **Multi-Tenancy** - Strikte Datenisolation pro Organisation
2. **RBAC** - Feingranulare Berechtigungen (50+) mit 3-Ebenen-System
3. **Verschlüsselung** - Sensible Daten mit AES-256-GCM verschlüsselt
4. **OParl-Standard** - Kompatibilität mit deutschen Ratsinformationssystemen
5. **HTMX-First** - Minimales JavaScript, maximale Interaktivität
6. **Audit-Trail** - Vollständige Nachverfolgbarkeit aller Aktionen

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
- **Nicht** `onclick="..."` in Templates - Alpine.js `@click` verwenden

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
2. `python manage.py fix_permissions` ausführen
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
- **Dokumentation**: `docs/`
- **Django Dokumentation**: https://docs.djangoproject.com/
- **HTMX**: https://htmx.org/
- **Alpine.js**: https://alpinejs.dev/
- **Tailwind CSS**: https://tailwindcss.com/
