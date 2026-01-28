# Mandari Work - Feature-Spezifikation

> **Lizenz**: AGPL-3.0 (Open Source)
> **Status**: Entwicklung
> **Basiert auf**: `_old/mandari_work` (Rewrite)

---

## 1. Architektur-Übersicht

### 1.1 Produktbereiche

```
Mandari Platform (AGPL-3.0)
├── Insight     → Öffentliches RIS Portal (existiert)
├── Work        → Portal für politische Organisationen
└── Session     → Sitzungsmanagement für Gremien (Zukunft)
```

### 1.2 Duales Gruppierungssystem

Organisationen können **gleichzeitig** zwei Hierarchien angehören:

```
┌─────────────────────────────────────────────────────────────┐
│                    PARTEI-HIERARCHIE                        │
│                                                             │
│  PartyGroup (z.B. "Volt Deutschland")                       │
│      ├── PartyGroup (z.B. "Volt NRW")                       │
│      │       └── Organization (Volt Münster)  ◄─────────┐   │
│      └── PartyGroup (z.B. "Volt Bayern")                │   │
│              └── Organization (Volt München)            │   │
└─────────────────────────────────────────────────────────│───┘
                                                          │
┌─────────────────────────────────────────────────────────│───┐
│                   REGIONALE HIERARCHIE                  │   │
│                                                         │   │
│  OParlBody (z.B. "Stadt Münster")                       │   │
│      └── Organizations:                                 │   │
│          ├── Volt Münster  ◄────────────────────────────┘   │
│          ├── SPD Münster                                    │
│          ├── CDU Münster                                    │
│          └── Grüne Münster                                  │
└─────────────────────────────────────────────────────────────┘
```

**Vorteile:**
- Parteiweites Teilen von Anträgen (z.B. Volt-weite Vorlagen)
- Regionales Teilen (z.B. Koalitionsarbeit in einer Kommune)
- Flexible Berechtigungsstrukturen
- Hierarchische Vererbung von Einstellungen

---

## 2. Datenmodell

### 2.1 Kern-Entitäten

```python
# Partei-Hierarchie (selbstreferenzierend)
class PartyGroup(Model):
    id: UUID
    name: str                    # "Volt Deutschland"
    slug: str                    # "volt-de"
    parent: FK[PartyGroup]       # Hierarchie
    logo: ImageField
    primary_color: str
    website: URL
    settings: JSON               # Vererbbare Einstellungen

# Die eigentliche Organisation (Mandant)
class Organization(Model):
    id: UUID
    name: str                    # "Volt Münster"
    slug: str                    # "volt-muenster"

    # Duale Zugehörigkeit
    party_group: FK[PartyGroup]  # Partei-Hierarchie (optional)
    body: FK[OParlBody]          # Regionale Zugehörigkeit (optional)

    # Branding
    logo: ImageField
    primary_color: str
    secondary_color: str

    # Kontakt
    contact_email: str
    website: URL
    address: str

    # Einstellungen
    settings: JSON
    require_2fa: bool

    # E-Mail (eigener SMTP)
    smtp_host: str (encrypted)
    smtp_port: int
    smtp_username: str (encrypted)
    smtp_password: str (encrypted)

    # Status
    is_active: bool
    created_at: datetime

# Mitgliedschaft
class Membership(Model):
    user: FK[User]
    organization: FK[Organization]

    # Rollen & Berechtigungen
    roles: M2M[Role]
    individual_permissions: M2M[Permission]
    denied_permissions: M2M[Permission]

    # Verknüpfung zu OParl
    oparl_person: FK[OParlPerson]  # Optional

    # Status
    is_active: bool
    joined_at: datetime

# Rollen
class Role(Model):
    organization: FK[Organization]
    name: str
    permissions: M2M[Permission]
    is_system_role: bool          # Nicht löschbar
    priority: int                 # Für Konfliktauflösung
    require_2fa: bool
    color: str                    # UI-Darstellung

# Berechtigungen
class Permission(Model):
    codename: str                 # "motions.create"
    name: str                     # "Anträge erstellen"
    category: str                 # "motions"
```

### 2.2 Sharing-Modell

```python
class ShareScope(Enum):
    USER = "user"                 # Einzelner User
    ROLE = "role"                 # Alle mit dieser Rolle
    ORGANIZATION = "organization" # Ganze Organisation
    PARTY_GROUP = "party_group"   # Partei-Ebene (z.B. alle Volt)
    REGIONAL = "regional"         # Alle Orgs einer Kommune

class ShareLevel(Enum):
    VIEW = "view"
    COMMENT = "comment"
    EDIT = "edit"
    ADMIN = "admin"

class MotionShare(Model):
    motion: FK[Motion]

    # Ziel (nur eines gesetzt)
    scope: ShareScope
    user: FK[User]                # wenn scope=USER
    role: FK[Role]                # wenn scope=ROLE
    organization: FK[Organization] # wenn scope=ORGANIZATION
    party_group: FK[PartyGroup]   # wenn scope=PARTY_GROUP
    body: FK[OParlBody]           # wenn scope=REGIONAL

    level: ShareLevel
    created_by: FK[User]
    created_at: datetime
```

---

## 3. Feature-Module

### 3.1 Dashboard (`/work/<org>/dashboard/`)

| Feature | Beschreibung | Priorität |
|---------|--------------|-----------|
| Anstehende Sitzungen | Nächste 2 Wochen, mit Vorbereitungsstatus | Hoch |
| Offene Aufgaben | Persönliche To-Dos nach Priorität | Hoch |
| Letzte Aktivitäten | Timeline der Org-Aktivitäten | Mittel |
| Benachrichtigungen | Ungelesene Notifications | Hoch |
| Schnellzugriffe | Häufig genutzte Aktionen | Mittel |
| Statistiken | Anträge, Sitzungen, Mitglieder | Niedrig |

**Zukünftige Features:**
- Widget-System (anpassbare Dashboard-Layouts)
- Cross-Org Dashboard (für Nutzer in mehreren Orgs)
- KI-Zusammenfassungen der Woche

### 3.2 Meine Sitzungen (`/work/<org>/meetings/`)

| Feature | Beschreibung | Priorität |
|---------|--------------|-----------|
| Sitzungsliste | Filtrar nach Zeitraum, Gremium, Status | Hoch |
| Kalenderansicht | Monats-/Wochenansicht | Hoch |
| Sitzungsvorbereitung | Notizen, Positionen pro TOP | Hoch |
| Positionserfassung | Zustimmung/Ablehnung/Enthaltung | Hoch |
| Dokumente anhängen | Zu TOPs zuordnen | Mittel |
| Rednerliste | Speech-Notes mit Timer | Mittel |
| ICS-Export | Kalender-Synchronisation | Mittel |

**Modelle:**
```python
class MeetingPreparation(Model):
    organization: FK[Organization]
    membership: FK[Membership]
    meeting: FK[OParlMeeting]

    notes_encrypted: EncryptedTextField
    is_prepared: bool
    prepared_at: datetime

class AgendaItemPosition(Model):
    preparation: FK[MeetingPreparation]
    agenda_item: FK[OParlAgendaItem]

    position: Enum[FOR, AGAINST, ABSTAIN, DISCUSS, DEFER, NONE]
    notes_encrypted: EncryptedTextField
    documents: M2M[Document]

class AgendaItemNote(Model):
    organization: FK[Organization]
    agenda_item: FK[OParlAgendaItem]

    visibility: Enum[PRIVATE, ORGANIZATION, PARTY, REGIONAL, PUBLIC]
    content_encrypted: EncryptedTextField

    author: FK[Membership]
    is_decision: bool  # Als Beschluss markiert
```

**Zukünftige Features:**
- Gemeinsame Positionsabstimmung in der Fraktion
- Automatische Erinnerungen vor Sitzungen
- KI-Zusammenfassung der Vorlagen
- Protokoll-Integration

### 3.3 Antragsdatenbank (`/work/<org>/motions/`)

| Feature | Beschreibung | Priorität |
|---------|--------------|-----------|
| CRUD Anträge | Erstellen, Bearbeiten, Löschen | Hoch |
| Typen | Antrag, Anfrage, Stellungnahme, Änderungsantrag | Hoch |
| Status-Workflow | Entwurf → Prüfung → Freigabe → Eingereicht | Hoch |
| Versionierung | Änderungshistorie mit Diff | Hoch |
| Dokumentanhänge | PDF, Word mit Textextraktion | Hoch |
| Teilen | Granulare Freigabe (User/Role/Org/Party/Regional) | Hoch |
| Kommentare | Inline-Kommentare (wie Google Docs) | Mittel |
| Vorlagen | Wiederverwendbare Templates | Mittel |
| Export | PDF, Word | Mittel |
| Tags | Kategorisierung | Niedrig |

**Modelle:**
```python
class Motion(Model):
    id: UUID
    organization: FK[Organization]

    motion_type: Enum[MOTION, INQUIRY, STATEMENT, AMENDMENT]
    title: str
    content_encrypted: EncryptedTextField
    summary: str  # Öffentliche Kurzfassung

    status: Enum[DRAFT, REVIEW, APPROVED, SUBMITTED, COMPLETED, REJECTED]

    # Verknüpfungen
    related_paper: FK[OParlPaper]
    related_meeting: FK[OParlMeeting]
    parent_motion: FK[Motion]  # Für Änderungsanträge

    # Metadaten
    author: FK[Membership]
    template: FK[MotionTemplate]
    tags: JSON

    # Kollaboration
    yjs_document: Binary  # Real-time Editor State

    # Audit
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime

class MotionDocument(Model):
    motion: FK[Motion]
    file: FileField
    filename: str
    mime_type: str
    text_content: str  # Extrahiert für Suche
    uploaded_by: FK[Membership]

class MotionRevision(Model):
    motion: FK[Motion]
    version: int
    content_encrypted: EncryptedTextField
    changed_by: FK[Membership]
    change_summary: str

class MotionComment(Model):
    motion: FK[Motion]
    parent: FK[MotionComment]  # Threading

    # Position im Dokument
    selection_start: int
    selection_end: int

    content: str
    author: FK[Membership]
    is_resolved: bool
```

**Sharing-Features:**
```python
# Beispiel: Antrag mit Volt-weiter Vorlage
motion = Motion.objects.create(
    organization=volt_muenster,
    title="Klimaneutrale Verwaltung bis 2030",
    ...
)

# 1. Teilen mit allen Volt-Organisationen
MotionShare.objects.create(
    motion=motion,
    scope=ShareScope.PARTY_GROUP,
    party_group=volt_deutschland,
    level=ShareLevel.VIEW
)

# 2. Teilen mit Koalitionspartnern in Münster
MotionShare.objects.create(
    motion=motion,
    scope=ShareScope.REGIONAL,
    body=stadt_muenster,
    level=ShareLevel.COMMENT
)
```

**Zukünftige Features:**
- Real-time kollaboratives Editing (Yjs)
- KI-gestützte Antragsformulierung
- Automatische Ähnlichkeitserkennung (hat andere Fraktion ähnlichen Antrag?)
- Antrags-Templates partei-/regionsweit teilen
- Abstimmungs-Tracking nach Einreichung

### 3.4 Fraktionssitzungen (`/work/<org>/faction/`)

| Feature | Beschreibung | Priorität |
|---------|--------------|-----------|
| Sitzungen erstellen | Titel, Datum, Ort, Video-Link | Hoch |
| Wiederkehrende Termine | Wöchentlich, 2-wöchentlich, monatlich | Hoch |
| Ausnahmen | Feiertage, Sonderfälle | Mittel |
| Eigene Tagesordnung | TOPs mit Verknüpfung zu öffentlichen TOPs | Hoch |
| Einladungen | E-Mail mit ICS | Mittel |
| Anwesenheit | Check-in/Check-out | Mittel |
| Protokoll | Mit Genehmigungsworkflow | Hoch |
| Beschlüsse | Dokumentation interner Entscheidungen | Hoch |

**Modelle:**
```python
class FactionMeeting(Model):
    id: UUID
    organization: FK[Organization]

    title: str
    start: datetime
    end: datetime

    location: str
    is_virtual: bool
    video_link: URL

    status: Enum[PLANNED, ONGOING, COMPLETED, CANCELLED]

    # Einladung
    invitation_sent: bool
    invitation_sent_at: datetime

    # Protokoll
    protocol_encrypted: EncryptedTextField
    protocol_approved: bool
    protocol_approved_at: datetime

    # Verknüpfung zu öffentlicher Sitzung
    related_meeting: FK[OParlMeeting]

    created_by: FK[Membership]

class FactionMeetingSchedule(Model):
    organization: FK[Organization]
    name: str

    recurrence: Enum[WEEKLY, BIWEEKLY, MONTHLY]
    weekday: int  # 0=Mo
    time: time
    duration_minutes: int

    default_location: str
    is_active: bool

class FactionAgendaItem(Model):
    meeting: FK[FactionMeeting]

    number: str
    title: str
    description_encrypted: EncryptedTextField

    # Verknüpfung zu öffentlichem TOP
    related_agenda_item: FK[OParlAgendaItem]

    # Beschluss
    decision_encrypted: EncryptedTextField

    order: int

class FactionAttendance(Model):
    meeting: FK[FactionMeeting]
    membership: FK[Membership]

    status: Enum[INVITED, CONFIRMED, DECLINED, PRESENT, ABSENT]
    checked_in_at: datetime
    checked_out_at: datetime
```

**Zukünftige Features:**
- Automatische TOP-Übernahme aus öffentlichen Sitzungen
- Protokoll-Templates
- Abstimmungsfunktion in der Sitzung
- Integration mit Video-Konferenz-Tools

### 3.5 Aufgaben (`/work/<org>/tasks/`)

| Feature | Beschreibung | Priorität |
|---------|--------------|-----------|
| Persönliche Aufgaben | Eigene To-Dos | Hoch |
| Organisations-Aufgaben | Zuweisbar an Mitglieder | Hoch |
| Prioritäten | Hoch, Mittel, Niedrig | Hoch |
| Fälligkeitsdatum | Mit Erinnerungen | Mittel |
| Verknüpfungen | Zu Sitzungen, Anträgen, TOPs | Mittel |
| Wiederkehrend | Regelmäßige Aufgaben | Niedrig |
| Kanban-Board | Visuelle Aufgabenverwaltung | Niedrig |

**Modelle:**
```python
class Task(Model):
    id: UUID
    organization: FK[Organization]

    title: str
    description: str

    priority: Enum[HIGH, MEDIUM, LOW]
    is_completed: bool
    completed_at: datetime
    due_date: date

    created_by: FK[Membership]
    assigned_to: FK[Membership]

    # Verknüpfungen
    related_meeting: FK[OParlMeeting]
    related_motion: FK[Motion]
    related_faction_meeting: FK[FactionMeeting]
```

### 3.6 RIS-Ansicht (`/work/<org>/ris/`)

| Feature | Beschreibung | Priorität |
|---------|--------------|-----------|
| Vorgänge | Gefiltert auf verknüpfte OParl-Body | Hoch |
| Sitzungen | Termine mit Vorbereitungsstatus | Hoch |
| Gremien | Nur relevante Gremien | Hoch |
| Suche | Volltextsuche im RIS | Hoch |
| Karte | Vorgänge mit Ortsbezug | Mittel |
| Schnell-Notiz | Direkt zu Vorgängen notieren | Mittel |
| Zu Antrag hinzufügen | Vorlage als Basis für Antrag | Mittel |

**Implementation:**
- Wrapper um `insight_core` Views
- Automatische Filterung auf `organization.body`
- Zusätzliche Work-Features (Notizen, Antragsverknüpfung)

### 3.7 Organisation (`/work/<org>/organization/`)

| Feature | Beschreibung | Priorität |
|---------|--------------|-----------|
| Profil | Name, Logo, Farben | Hoch |
| Mitgliederverwaltung | Einladen, Entfernen, Rollen | Hoch |
| Rollenverwaltung | Erstellen, Berechtigungen | Hoch |
| Gremienverknüpfung | OParl-Organisationen zuweisen | Mittel |
| Benachrichtigungen | E-Mail-Einstellungen | Mittel |
| SMTP-Einstellungen | Eigener E-Mail-Server | Niedrig |
| API-Schlüssel | Für Integrationen | Niedrig |
| Datenschutz | Löschfristen, Export | Mittel |

### 3.8 Support (`/work/<org>/support/`)

| Feature | Beschreibung | Priorität |
|---------|--------------|-----------|
| Ticket erstellen | Bug, Feature, Frage | Hoch |
| Ticket-Verlauf | Kommunikation mit Support | Hoch |
| Knowledge Base | FAQ, Anleitungen | Mittel |
| Status-Tracking | Offen, In Bearbeitung, Gelöst | Hoch |

**Admin-Sicht (für Mandari-Team):**
- Alle Tickets aller Organisationen
- Zuweisung an Support-Mitarbeiter
- SLA-Tracking
- Statistiken

---

## 4. Berechtigungssystem

### 4.1 Permission-Kategorien

```python
PERMISSIONS = {
    # Dashboard
    "dashboard.view": "Dashboard anzeigen",

    # Sitzungen (öffentlich)
    "meetings.view": "Sitzungen anzeigen",
    "meetings.prepare": "Sitzungen vorbereiten",
    "meetings.notes": "Notizen erstellen",

    # Anträge
    "motions.view": "Anträge anzeigen",
    "motions.create": "Anträge erstellen",
    "motions.edit": "Eigene Anträge bearbeiten",
    "motions.edit_all": "Alle Anträge bearbeiten",
    "motions.delete": "Anträge löschen",
    "motions.approve": "Anträge freigeben",
    "motions.share": "Anträge teilen",

    # Fraktionssitzungen
    "faction.view": "Fraktionssitzungen anzeigen",
    "faction.create": "Fraktionssitzungen erstellen",
    "faction.manage": "Fraktionssitzungen verwalten",
    "faction.protocol": "Protokolle erstellen",

    # Aufgaben
    "tasks.view": "Aufgaben anzeigen",
    "tasks.create": "Aufgaben erstellen",
    "tasks.assign": "Aufgaben zuweisen",
    "tasks.manage": "Alle Aufgaben verwalten",

    # RIS
    "ris.view": "RIS-Daten anzeigen",
    "ris.notes": "RIS-Notizen erstellen",

    # Organisation
    "organization.view": "Einstellungen anzeigen",
    "organization.edit": "Einstellungen bearbeiten",
    "organization.members": "Mitglieder verwalten",
    "organization.roles": "Rollen verwalten",
    "organization.invite": "Mitglieder einladen",

    # Support
    "support.view": "Support anzeigen",
    "support.create": "Tickets erstellen",
}
```

### 4.2 Standard-Rollen

```python
DEFAULT_ROLES = {
    "admin": {
        "name": "Administrator",
        "permissions": ALL_PERMISSIONS,
        "is_system_role": True,
    },
    "board": {
        "name": "Vorstand",
        "permissions": [
            "dashboard.view",
            "meetings.*",
            "motions.*",
            "faction.*",
            "tasks.*",
            "ris.*",
            "organization.view",
            "organization.members",
            "support.*",
        ],
    },
    "member": {
        "name": "Mitglied",
        "permissions": [
            "dashboard.view",
            "meetings.view", "meetings.prepare", "meetings.notes",
            "motions.view", "motions.create", "motions.edit",
            "faction.view",
            "tasks.view", "tasks.create",
            "ris.view", "ris.notes",
            "support.view", "support.create",
        ],
    },
    "viewer": {
        "name": "Lesezugriff",
        "permissions": [
            "dashboard.view",
            "meetings.view",
            "motions.view",
            "faction.view",
            "tasks.view",
            "ris.view",
            "support.view",
        ],
    },
}
```

---

## 5. Verschlüsselung

### 5.1 Verschlüsselte Felder

| Modell | Feld | Grund |
|--------|------|-------|
| MeetingPreparation | notes_encrypted | Politisch sensibel |
| AgendaItemPosition | notes_encrypted | Interne Positionierung |
| AgendaItemNote | content_encrypted | Fraktionsinterna |
| Motion | content_encrypted | Bis zur Veröffentlichung |
| FactionMeeting | protocol_encrypted | Internes Protokoll |
| FactionAgendaItem | description/decision | Interne Beschlüsse |
| Organization | smtp_password | Zugangsdaten |

### 5.2 Schlüssel-Hierarchie

```
Master Key (ENCRYPTION_MASTER_KEY env var)
    │
    └── Organization Key (pro Organisation)
            │
            └── Verschlüsselte Felder
```

---

## 6. API-Endpunkte (HTMX Partials)

### 6.1 Dashboard
```
GET  /work/<org>/dashboard/                    # Haupt-Dashboard
GET  /work/<org>/dashboard/partials/meetings/  # Anstehende Sitzungen
GET  /work/<org>/dashboard/partials/tasks/     # Offene Aufgaben
GET  /work/<org>/dashboard/partials/activity/  # Aktivitäts-Timeline
```

### 6.2 Sitzungen
```
GET  /work/<org>/meetings/                     # Liste
GET  /work/<org>/meetings/calendar/            # Kalenderansicht
GET  /work/<org>/meetings/<id>/                # Detail
GET  /work/<org>/meetings/<id>/prepare/        # Vorbereitung
POST /work/<org>/meetings/<id>/positions/      # Position speichern
GET  /work/<org>/meetings/<id>/export/ics/     # ICS-Export
```

### 6.3 Anträge
```
GET  /work/<org>/motions/                      # Liste
GET  /work/<org>/motions/create/               # Formular
POST /work/<org>/motions/                      # Erstellen
GET  /work/<org>/motions/<id>/                 # Detail
GET  /work/<org>/motions/<id>/edit/            # Bearbeiten
GET  /work/<org>/motions/<id>/history/         # Versionen
GET  /work/<org>/motions/<id>/share/           # Teilen-Dialog
POST /work/<org>/motions/<id>/share/           # Freigabe erstellen
GET  /work/<org>/motions/<id>/export/pdf/      # PDF-Export
```

### 6.4 Fraktionssitzungen
```
GET  /work/<org>/faction/                      # Liste
POST /work/<org>/faction/                      # Erstellen
GET  /work/<org>/faction/<id>/                 # Detail
GET  /work/<org>/faction/<id>/agenda/          # Tagesordnung
POST /work/<org>/faction/<id>/attendance/      # Check-in
GET  /work/<org>/faction/<id>/protocol/        # Protokoll
GET  /work/<org>/faction/schedules/            # Serientermine
```

---

## 7. Zukünftige Features (Roadmap)

### Phase 1: Foundation (aktuell)
- [x] Duales Gruppierungssystem
- [ ] Basis-Authentifizierung
- [ ] Organisation CRUD
- [ ] Mitgliederverwaltung
- [ ] Rollen & Berechtigungen

### Phase 2: Core Work
- [ ] Dashboard
- [ ] Sitzungsvorbereitung
- [ ] RIS-Integration
- [ ] Basis-Antragsverwaltung

### Phase 3: Collaboration
- [ ] Antrags-Sharing (User/Org)
- [ ] Kommentare
- [ ] Fraktionssitzungen
- [ ] Aufgaben

### Phase 4: Advanced Sharing
- [ ] Partei-weites Sharing
- [ ] Regionales Sharing
- [ ] Vorlagen-Bibliothek
- [ ] Cross-Org Dashboard

### Phase 5: AI & Automation
- [ ] KI-Zusammenfassungen
- [ ] Ähnliche Anträge finden
- [ ] Automatische Kategorisierung
- [ ] Smart Notifications

### Phase 6: Integration
- [ ] Kalender-Sync (Google, Outlook)
- [ ] E-Mail-Integration
- [ ] API für externe Tools
- [ ] Webhook-System

---

## 8. Technische Anforderungen

### 8.1 Abhängigkeiten

```txt
# Core
Django>=5.0
django-htmx>=1.17
psycopg[binary]>=3.1

# Security
cryptography>=41.0
django-otp>=1.3
qrcode>=7.4

# Tasks
celery>=5.3
redis>=5.0

# Search
meilisearch>=0.28

# Export
python-docx>=1.0
reportlab>=4.0
weasyprint>=60.0
```

### 8.2 Umgebungsvariablen

```env
# Verschlüsselung
ENCRYPTION_MASTER_KEY=...

# Feature Flags
WORK_ENABLED=true
WORK_AI_FEATURES=false
WORK_SHARING_ENABLED=true
```

---

## 9. Migration von _old

### 9.1 Modell-Mapping

| _old | Neu | Änderungen |
|------|-----|------------|
| OrganizationGroup | PartyGroup | Umbenannt, klarere Semantik |
| OrganizationTenant | Organization | Vereinfacht |
| OrganizationMembership | Membership | Unverändert |
| Role, Permission | Role, Permission | Unverändert |
| FactionMeeting | FactionMeeting | Unverändert |
| Motion | Motion | Erweitertes Sharing |

### 9.2 Neue Features

- Duales Gruppierungssystem (Party + Regional)
- Erweitertes Sharing-Modell
- Vereinfachte URL-Struktur
- HTMX statt komplexem Frontend
