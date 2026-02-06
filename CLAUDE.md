# Mandari 2.0 - AI Assistant Context

## Projekt-Überblick

Mandari ist eine Open-Source-Plattform für kommunalpolitische Transparenz in Deutschland. Sie basiert auf dem **OParl-Standard** für Ratsinformationssysteme (RIS) und bietet drei Portale:

1. **Public Portal** - Bürger:innen-Zugang zu Ratsinformationen
2. **Work Portal** - Arbeitsbereich für politische Organisationen (Fraktionen)
3. **Session RIS** - Verwaltungs-RIS für kommunale Sachbearbeiter

---

## Git-Workflow

**Entwicklung erfolgt ausschließlich auf dem `dev` Branch.**

```bash
# Standard-Workflow
git checkout dev
git pull origin dev
# ... Änderungen ...
git add <files>
git commit -m "feat/fix/chore: Beschreibung"
git push origin dev
```

---

## Technologie-Stack

| Komponente | Technologie |
|------------|-------------|
| **Backend** | Django 6.0 (Python 3.12+) |
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
├── .private/                   # Interne Planungsdokumente (im Repo)
│   ├── MASTER_FEATURE_LIST.md  # Feature-Übersicht & Roadmap
│   ├── ARCHITECTURE_*.md       # Architektur-Pläne
│   ├── CI_CD_*.md              # Deployment-Pläne
│   └── PLAN_*.md               # Feature-Implementierungspläne
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
│   ├── insight_search/         # Meilisearch-Integration
│   ├── insight_ai/             # KI-Pipelines
│   ├── templates/              # Django Templates
│   ├── static/                 # Statische Dateien
│   └── mandari/settings.py     # Django-Einstellungen
├── ingestor/                   # Rust-basierter OParl-Ingestor
├── CLAUDE.md                   # Diese Datei
└── LICENSE                     # EUPL-1.2
```

---

## Kern-Apps im Detail

### 1. `apps.accounts` - Authentifizierung

**Zweck**: Benutzer-Konten und Sicherheit

**Modelle**:
- `User` - Custom User (E-Mail als Login, UUID)
- `TwoFactorDevice` - TOTP-basierte 2FA
- `TrustedDevice` - "Dieses Gerät merken" (30 Tage)
- `UserSession` - Aktive Sitzungen mit IP/Standort
- `LoginAttempt` - Rate Limiting (5 Versuche/15 Min)
- `PasswordResetToken` - Einmalig, 24h gültig
- `SecurityNotification` - Sicherheitswarnungen

**Wichtige Views**:
- `LoginView` - Login mit Rate Limiting
- `RegisterView` - Nur per Einladung
- Password Reset Flow

---

### 2. `apps.tenants` - Multi-Tenant & Berechtigungen

**Zweck**: Organisationen, Rollen und Berechtigungen

**Modelle**:

```
Organization (Tenant)
├── PartyGroup (Partei-Hierarchie: Volt DE → Volt NRW → Volt MS)
├── OParlBody (Kommune-Verknüpfung)
├── encryption_key (tenant-spezifisch)
└── smtp_* (eigene E-Mail-Konfiguration)

Role
├── permissions (M2M → Permission)
├── is_admin (Vollzugriff)
└── priority (für Konflikte)

Membership (User ↔ Organization)
├── roles (M2M → Role)
├── individual_permissions (zusätzlich)
└── denied_permissions (Ausschluss)
```

**Berechtigungssystem** (3 Ebenen):
1. **Rollen-Berechtigungen** - Aus zugewiesenen Rollen
2. **Individuelle** - Pro Membership hinzugefügt
3. **Verweigert** - Pro Membership ausgeschlossen (höchste Priorität)

**50+ Berechtigungen** in 14 Kategorien:
- Dashboard, Fraktionssitzungen, Agenda, Abstimmung/Rederecht
- Anträge, Protokolle, RIS-Sitzungen, Aufgaben
- RIS-Daten, AGs, Dokumente, Mitglieder
- Organisation, Gäste, Support

**Standard-Rollen** (automatisch erstellt):
- Fraktionsvorsitz (Vollzugriff)
- Stellv. Vorsitz
- Fraktionsmitglied (Stimmrecht)
- Sachkundige/r Bürger/in
- Bezirksvertreter/in
- Fraktionspersonal
- Administrator

---

### 3. `apps.work` - Fraktions-Arbeitsbereich

**Zweck**: Interner Arbeitsbereich für politische Organisationen

#### `work.faction` - Interne Fraktionssitzungen

**Modelle**:
- `FactionMeeting` - Sitzung mit Status, Protokoll
- `FactionAgendaItem` - TOPs (hierarchisch: 1, 1.1, 1.2)
- `FactionAttendance` - Anwesenheit mit RSVP
- `FactionProtocolEntry` - Protokolleinträge
- `FactionDecision` - Beschlüsse

**Status-Workflow**:
`draft` → `invited` → `scheduled` → `in_progress` → `completed` → `cancelled`

#### `work.meetings` - RIS-Sitzungsvorbereitung

**Modelle**:
- `MeetingPreparation` - Vorbereitung zu RIS-Sitzung
- `AgendaItemNote` - Private/Organisations-Notizen zu TOPs
- `PaperComment` - Kommentare zu Vorlagen (gremienübergreifend)

**Sichtbarkeit**:
- `private` - Nur der Autor
- `organization` - Alle in der Organisation
- `consulting` - Alle beratenden Gremien (für PaperComment)

#### `work.motions` - Anträge

**Modelle**:
- `Motion` - Antrag mit Versionierung
- `MotionRevision` - Versionshistorie
- `MotionComment` - Diskussion
- `MotionShare` - Mit anderen Orgs teilen
- `MotionTemplate` - Vorlagen

#### `work.tasks` - Aufgaben

**Modelle**:
- `Task` - Aufgabe mit Zuweisung, Frist
- `TaskComment` - Kommentare
- `related_faction_meeting` - Verknüpfung zu Sitzung

---

### 4. `apps.session` - Verwaltungs-RIS

**Zweck**: RIS für kommunale Verwaltung

**Modelle**:
- `SessionTenant` - Kommune als Mandant
- `SessionMeeting` - Ratssitzungen
- `SessionPaper` - Vorlagen/Drucksachen
- `SessionApplication` - Anträge von Fraktionen
- `SessionAgendaItem` - TOPs mit Abstimmung
- `SessionProtocol` - Protokoll mit Genehmigung
- `SessionAttendance` - Anwesenheit
- `SessionAllowance` - Sitzungsgelder
- `SessionAuditLog` - Vollständiges Audit-Log

---

### 5. `insight_core` - OParl-Datenmodelle

**Zweck**: Öffentliche RIS-Daten nach deutschem OParl-Standard (Version 1.1)

---

## OParl-Standard im Detail

OParl ist der deutsche Standard für offene Ratsinformationssysteme. Er definiert eine einheitliche API für den anonymen, lesenden Zugriff auf öffentliche parlamentarische Daten.

**Spezifikation**: https://oparl.org/spezifikation/online-ansicht/

### OParl-Entitäten und ihre Beziehungen

```
OParlSource (Mandari-spezifisch)
    └── OParlBody (Kommune)
            ├── OParlOrganization (Gremium/Fraktion)
            │       └── OParlMembership ←→ OParlPerson
            │
            ├── OParlMeeting (Sitzung)
            │       ├── organizations (M2M → OParlOrganization)
            │       └── OParlAgendaItem (TOP)
            │               └── OParlConsultation → OParlPaper
            │
            ├── OParlPaper (Vorlage/Drucksache)
            │       ├── OParlFile (Dokumente)
            │       └── OParlConsultation (Beratungen)
            │
            ├── OParlPerson (Ratsmitglied)
            ├── OParlLocation (Orte)
            └── OParlLegislativeTerm (Wahlperiode)
```

### Entitäten-Details

| Entität | Deutsche Bezeichnung | Beschreibung |
|---------|---------------------|--------------|
| `OParlSource` | Quelle | RIS-API einer Kommune (z.B. `oparl.stadt-muenster.de`) |
| `OParlBody` | Kommune | Körperschaft (Stadt, Kreis, Gemeinde) |
| `OParlOrganization` | Gremium | Ausschuss, Fraktion, Beirat, Kommission |
| `OParlPerson` | Person | Ratsmitglied, Sachkundige/r Bürger/in |
| `OParlMembership` | Mitgliedschaft | Person in Gremium (mit Rolle, Stimmrecht) |
| `OParlMeeting` | Sitzung | Rats-/Ausschusssitzung |
| `OParlAgendaItem` | TOP | Tagesordnungspunkt |
| `OParlPaper` | Vorlage | Drucksache, Antrag, Anfrage, Bericht |
| `OParlFile` | Datei | PDF-Dokument, Anlage |
| `OParlConsultation` | Beratung | Verknüpfung Paper → AgendaItem → Meeting |
| `OParlLocation` | Ort | Sitzungsort mit Koordinaten |
| `OParlLegislativeTerm` | Wahlperiode | Zeitraum einer Legislatur |

### Zentrale Verknüpfungs-Entität: Consultation

Die `OParlConsultation` ist das **Bindeglied** zwischen Vorlagen und Sitzungen:

```
Paper (Vorlage "Antrag auf Spielplatz")
    │
    ├── Consultation (role="Vorberatung", authoritative=False)
    │       └── AgendaItem (TOP 5 im Jugendhilfeausschuss)
    │               └── Meeting (Sitzung 12.03.2026)
    │
    └── Consultation (role="Entscheidung", authoritative=True)
            └── AgendaItem (TOP 8 im Rat)
                    └── Meeting (Ratssitzung 25.03.2026)
```

**Wichtige Felder**:
- `authoritative` - Ist dies die entscheidende Beratung?
- `role` - Rolle der Beratung (Vorberatung, Entscheidung, Kenntnisnahme)

### OParl-Synchronisation

**Module**: `insight_sync`

```bash
# Incremental Sync (nur Änderungen seit letztem Sync)
python manage.py sync_oparl

# Full Sync (alle Daten neu laden)
python manage.py sync_oparl --full

# Einzelne Quelle synchronisieren
python manage.py sync_oparl --source https://oparl.stadt-muenster.de/system

# Im Hintergrund (Django 6.0 Tasks)
python manage.py sync_oparl --background
```

**Sync-Workflow**:
1. `OParlSource` → System-Endpoint abrufen
2. `OParlBody` → Bodies der Quelle laden
3. Pro Body: Organizations, Persons, Meetings, Papers parallel laden
4. Verknüpfungen (Memberships, Consultations, Files) auflösen
5. `raw_json` speichern für spätere Analyse

### Datenfluss im Projekt

```
OParl API (extern)
    │
    ▼
insight_sync (Synchronisation)
    │
    ▼
insight_core (Datenmodelle)
    │
    ├──► Public Portal (Bürger:innen-Ansicht)
    │
    └──► Work Portal
            ├── work.ris (RIS-Datenansicht)
            └── work.meetings (Sitzungsvorbereitung)
                    └── Verknüpfung mit OParlMeeting, OParlAgendaItem, OParlPaper
```

### OParl-Felder in Django-Models

Jedes OParl-Model hat diese Standard-Felder:

```python
class OParlXxx(models.Model):
    id = models.UUIDField(primary_key=True)        # Interne ID
    external_id = models.TextField(unique=True)     # OParl-URL als ID

    # OParl-Zeitstempel
    oparl_created = models.DateTimeField()          # Erstellungsdatum im RIS
    oparl_modified = models.DateTimeField()         # Letzte Änderung im RIS

    # Rohdaten für Debugging/Erweiterung
    raw_json = models.JSONField()

    # Mandari-Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
```

### Mandari-Erweiterungen (nicht OParl-Standard)

| Model | Feld | Zweck |
|-------|------|-------|
| `OParlSource` | `sync_config`, `last_sync` | Sync-Konfiguration |
| `OParlBody` | `display_name`, `logo` | Frontend-Anpassung |
| `OParlBody` | `latitude`, `longitude`, `bbox_*` | Geo-Daten für Karten |
| `OParlBody` | `osm_relation_id` | OpenStreetMap-Verknüpfung |
| `OParlBody` | `slug` | SEO-freundliche URLs |
| `OParlPaper` | `summary`, `locations` | KI-generierte Felder |
| `OParlFile` | `local_path`, `text_content` | Lokale Speicherung, OCR |
| `OParlFile` | `text_extraction_status/method/error` | Extraktions-Tracking |
| `LocationMapping` | - | Ortsname → Koordinaten-Mapping |
| `TileCache` | - | OSM-Kartenkacheln |

### Services (insight_core/services/)

| Service | Zweck |
|---------|-------|
| `document_extraction.py` | PDF-Textextraktion (pypdf → Mistral → Tesseract) |
| `mistral_ocr.py` | Mistral AI OCR-Integration |
| `text_extraction_queue.py` | Async Queue für Batch-Extraktion |
| `search_service.py` | Meilisearch Multi-Index-Suche |

### SEO & Sitemaps

| Datei | Zweck |
|-------|-------|
| `insight_core/seo.py` | SEO-Context-Generatoren für alle Entitäten |
| `insight_core/sitemaps.py` | Hierarchische Sitemaps pro Kommune |
| `insight_core/signals.py` | Auto-Indexierung bei Model-Änderungen |
| `insight_search/synonyms.py` | 100+ deutsche Kommunal-Synonyme |

### Zugriff auf OParl-Daten im Code

```python
# Alle Gremien einer Kommune
body = OParlBody.objects.get(short_name="Münster")
organizations = body.organizations.filter(organization_type="committee")

# Kommende Sitzungen eines Gremiums
from django.utils import timezone
meetings = OParlMeeting.objects.filter(
    organizations=organization,
    start__gte=timezone.now(),
    cancelled=False
).order_by("start")

# Papers zu einem AgendaItem (über Consultation)
agenda_item = OParlAgendaItem.objects.get(...)
papers = agenda_item.get_papers()  # Helper-Methode

# Oder direkt über Consultation
papers = OParlPaper.objects.filter(
    consultations__agenda_item_external_id=agenda_item.external_id
).distinct()

# Aktive Mitgliedschaften einer Person
person = OParlPerson.objects.get(...)
active_memberships = person.memberships.filter(
    end_date__isnull=True
) | person.memberships.filter(
    end_date__gte=timezone.now().date()
)
```

---

## Coding-Konventionen

### Python/Django

```python
# Models mit Type Hints
from django.db import models
from apps.common.encryption import EncryptedTextField

class MyModel(models.Model):
    title: str = models.CharField(max_length=200)
    content_encrypted = EncryptedTextField()
    organization = models.ForeignKey("tenants.Organization", on_delete=models.CASCADE)

    class Meta:
        ordering = ["-created_at"]

# Views mit Mixins
from apps.common.mixins import WorkViewMixin
from django.views.generic import TemplateView

class MyView(WorkViewMixin, TemplateView):
    template_name = "work/my_template.html"
    permission_required = "my_permission"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["items"] = self.get_queryset()
        return ctx
```

### Wichtige Mixins

```python
# OrganizationMixin - Setzt request.organization und request.membership
# PermissionRequiredMixin - Prüft Berechtigungen
# HTMXMixin - HTMX-Unterstützung (is_htmx, htmx_redirect)
# WorkViewMixin - Kombiniert alle obigen
```

### Templates

```html
{% extends "work/base_work.html" %}

{% block content %}
<div x-data="myComponent()" class="...">
    <!-- Alpine.js Component -->
</div>
{% endblock %}

{% block extra_js %}
<script>
function myComponent() {
    return {
        items: [],
        init() { /* ... */ }
    }
}
</script>
{% endblock %}
```

### Frontend-Stack

- **HTMX** für dynamische Interaktionen ohne JS
- **Alpine.js** für komplexere Client-Logik
- **Tailwind CSS** für Styling
- **Lucide Icons** (via `<i data-lucide="icon-name">`)

---

## Entwicklungs-Befehle

```bash
# Server starten
cd mandari
python manage.py runserver

# Migrationen
python manage.py makemigrations
python manage.py migrate

# Rollen/Berechtigungen synchronisieren
python manage.py setup_roles
python manage.py fix_permissions

# OParl-Daten synchronisieren
python manage.py sync_oparl --full
python manage.py extract_texts  # OCR für PDFs

# Meilisearch konfigurieren (Synonyme, Typo-Toleranz)
python manage.py setup_meilisearch

# Statische Dateien
python manage.py collectstatic

# Shell
python manage.py shell_plus  # django-extensions
```

---

## Umgebungsvariablen

```bash
# Erforderlich
SECRET_KEY=django-insecure-xxx
DATABASE_URL=postgresql://user:pass@localhost:5432/mandari
ENCRYPTION_MASTER_KEY=base64-encoded-256-bit-key

# Optional
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
SITE_URL=https://mandari.example.com

# E-Mail (oder via SiteSettings im Admin)
EMAIL_HOST=smtp.example.com
EMAIL_PORT=587
EMAIL_HOST_USER=user
EMAIL_HOST_PASSWORD=pass
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL=noreply@example.com

# Meilisearch (Volltextsuche)
MEILISEARCH_URL=http://localhost:7700
MEILISEARCH_KEY=masterKey
MEILISEARCH_AUTO_INDEX=True

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
/sitemap.xml                    # SEO: Sitemap-Index
/sitemap-pages.xml              # SEO: Statische Seiten
/sitemap-insight-<slug>.xml     # SEO: Pro Kommune
/insight/                       # Insight Portal
    /vorgaenge/<uuid>/          # Paper-Detail
    /termine/<uuid>/            # Meeting-Detail
    /gremien/<uuid>/            # Organization-Detail
    /personen/<uuid>/           # Person-Detail
/admin/                         # Django Admin (Unfold)
/accounts/                      # Login, Logout, Password Reset
/work/<org_slug>/               # Work Portal
    /dashboard/                 # Übersicht
    /ris/                       # RIS-Daten
    /meetings/                  # Sitzungsvorbereitung
    /faction/                   # Fraktionssitzungen
    /motions/                   # Anträge
    /tasks/                     # Aufgaben
    /settings/                  # Organisationseinstellungen
/session/<tenant_slug>/         # Session RIS (Verwaltung)
```

---

## Verschlüsselung

```python
# Hierarchie: Master Key → Tenant Key → Feld-Daten

# In Models
class MyModel(EncryptionMixin, models.Model):
    secret_encrypted = EncryptedTextField()

    def get_encryption_organization(self):
        return self.organization

# Verwendung
obj.set_secret_encrypted("geheimer Text")
plain = obj.get_secret_decrypted()
```

---

## Häufige Aufgaben

### Neue Organisation erstellen

1. Admin → Tenants → Organizations → Add
2. Slug, Name, OParl Body zuweisen
3. `python manage.py setup_roles` (falls Standard-Rollen fehlen)

### Benutzer einladen

1. Work Portal → Einstellungen → Mitglieder → Einladen
2. E-Mail eingeben, Rollen wählen
3. Einladungslink wird per E-Mail gesendet

### Neue Berechtigung hinzufügen

1. `apps/common/permissions.py` → `PERMISSIONS` dict erweitern
2. `python manage.py fix_permissions`
3. Rollen im Admin anpassen

### Migration erstellen

```bash
python manage.py makemigrations <app_name> -n "kurze_beschreibung"
python manage.py migrate
```

---

## Wichtige Dateien

| Datei | Beschreibung |
|-------|--------------|
| `mandari/settings.py` | Django-Einstellungen |
| `mandari/urls.py` | Haupt-URL-Konfiguration |
| `apps/common/permissions.py` | Berechtigungsdefinitionen |
| `apps/common/encryption.py` | Verschlüsselungslogik |
| `apps/common/mixins.py` | View-Mixins |
| `templates/work/base_work.html` | Basis-Template für Work Portal |

---

## Architektur-Prinzipien

1. **Multi-Tenancy** - Strikte Datenisolation pro Organisation
2. **RBAC** - Feingranulare Berechtigungen (50+)
3. **Verschlüsselung** - Sensible Daten AES-256 verschlüsselt
4. **OParl-Standard** - Kompatibilität mit deutschen RIS
5. **HTMX-First** - Minimales JavaScript, maximale Interaktivität
6. **Audit-Trail** - Vollständige Nachverfolgbarkeit

---

## Hinweise für KI-Assistenten

### DO

- Immer `WorkViewMixin` für Work-Portal-Views verwenden
- Berechtigungen via `permission_required` prüfen
- `EncryptedTextField` für sensible Daten
- Queryset immer mit `organization` filtern
- HTMX für einfache Interaktionen, Alpine.js für komplexe

### DON'T

- Niemals Daten ohne Organisations-Filter abfragen
- Keine hardcodierten Secrets
- Keine `User.objects.all()` - immer über Membership
- Keine synchronen externen API-Calls in Views

### Typische Patterns

```python
# View mit Berechtigung
class MyView(WorkViewMixin, TemplateView):
    permission_required = "my_permission"

    def get_queryset(self):
        return MyModel.objects.filter(organization=self.organization)

# HTMX-Partial
def get_template_names(self):
    if self.is_htmx:
        return ["work/my_partial.html"]
    return ["work/my_full.html"]

# Verschlüsselte Daten
obj.set_content_encrypted(request.POST.get("content"))
```

---

## .private/ - Interne Planungsdokumente

Das `.private/` Verzeichnis enthält interne Planungsdokumente:

| Datei | Inhalt |
|-------|--------|
| `MASTER_FEATURE_LIST.md` | Vollständige Feature-Liste & Roadmap |
| `ARCHITECTURE_OPTIMIZATION_PLAN.md` | Architektur-Optimierungen |
| `CI_CD_AND_INSTALL_PLAN.md` | CI/CD & Deployment-Konfiguration |
| `DJANGO_PERFORMANCE_OPTIMIZATION_PLAN.md` | Performance-Optimierungen |
| `PLAN_TEXT_EXTRACTION_SEO_SEARCH.md` | Text-Extraktion, SEO, Suche |
| `SPDX_AND_COPYRIGHT_PLAN.md` | Lizenz & Copyright Headers |

**Wichtig**: Bei neuen Features zuerst `.private/MASTER_FEATURE_LIST.md` prüfen!

---

## Support & Kontakt

- **OParl-Spezifikation**: https://oparl.org
- **Projekt-Repository**: https://github.com/mandariOSS/mandari
- **Dokumentation**: `docs/`
