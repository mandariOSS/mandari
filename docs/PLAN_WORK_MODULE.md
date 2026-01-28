# Mandari Work Module - Implementierungsplan

## 1. Produktübersicht

### 1.1 Produktbereiche

```
Mandari Platform
├── Insight (OSS)          → Öffentliches RIS Portal (bereits implementiert)
├── Work (Proprietär)      → SaaS Portal für Fraktionen/Organisationen
└── Session (Proprietär)   → Sitzungsmanagement für Gremien (Zukunft)
```

### 1.2 Zielgruppen Work-Modul

| Zielgruppe | Beschreibung | Hauptfunktionen |
|------------|--------------|-----------------|
| Fraktionsmitglieder | Ratsmitglieder einer Partei/Fraktion | Dashboard, Sitzungsvorbereitung, Anträge |
| Fraktionsgeschäftsführer | Administrative Leitung | Mitgliederverwaltung, Termine, Protokolle |
| Fraktionsvorsitzende | Politische Leitung | Übersicht, Entscheidungen, Freigaben |

---

## 2. Ordnerstruktur

### 2.1 Neue Projektstruktur

```
mandari2.0/
├── mandari/                          # Django Hauptprojekt
│   ├── mandari/                      # Settings & Config
│   │   ├── settings/
│   │   │   ├── base.py
│   │   │   ├── development.py
│   │   │   ├── staging.py
│   │   │   └── production.py
│   │   ├── urls.py
│   │   └── wsgi.py
│   │
│   ├── insight_core/                 # [OSS] Öffentliches RIS (existiert)
│   ├── insight_sync/                 # [OSS] OParl Synchronisation
│   ├── insight_search/               # [OSS] Volltextsuche
│   ├── insight_ai/                   # [OSS] KI-Features
│   │
│   ├── apps/                         # Neue App-Struktur
│   │   ├── __init__.py
│   │   │
│   │   ├── common/                   # Gemeinsame Utilities
│   │   │   ├── encryption.py         # Feldverschlüsselung
│   │   │   ├── mixins.py             # Basis-Mixins
│   │   │   ├── permissions.py        # Permission-System
│   │   │   └── utils.py
│   │   │
│   │   ├── accounts/                 # Benutzerverwaltung
│   │   │   ├── models.py             # User, 2FA, Sessions
│   │   │   ├── views.py              # Login, Register, Profile
│   │   │   ├── forms.py
│   │   │   └── urls.py
│   │   │
│   │   ├── tenants/                  # Multi-Tenant System
│   │   │   ├── models.py             # Tenant, Membership, Roles
│   │   │   ├── middleware.py         # Tenant-Context
│   │   │   ├── mixins.py             # TenantMixin
│   │   │   └── urls.py
│   │   │
│   │   └── work/                     # Work-Modul Apps
│   │       ├── __init__.py
│   │       │
│   │       ├── dashboard/            # Persönliches Dashboard
│   │       │   ├── views.py
│   │       │   └── urls.py
│   │       │
│   │       ├── meetings/             # Sitzungsmanagement
│   │       │   ├── models.py         # Preparation, Notes, Positions
│   │       │   ├── views.py
│   │       │   └── urls.py
│   │       │
│   │       ├── motions/              # Antragsdatenbank
│   │       │   ├── models.py         # Motion, Document, Revision
│   │       │   ├── views.py
│   │       │   └── urls.py
│   │       │
│   │       ├── faction/              # Fraktionssitzungen
│   │       │   ├── models.py         # FactionMeeting, Schedule
│   │       │   ├── views.py
│   │       │   └── urls.py
│   │       │
│   │       ├── tasks/                # To-Do System
│   │       │   ├── models.py
│   │       │   ├── views.py
│   │       │   └── urls.py
│   │       │
│   │       ├── ris/                  # RIS-Kopie (Read-Only)
│   │       │   ├── views.py          # Wrapped insight_core views
│   │       │   └── urls.py
│   │       │
│   │       ├── organization/         # Organisationseinstellungen
│   │       │   ├── views.py
│   │       │   └── urls.py
│   │       │
│   │       └── support/              # Support-System
│   │           ├── models.py         # Ticket, Message
│   │           ├── views.py
│   │           └── urls.py
│   │
│   ├── templates/
│   │   ├── base.html                 # Globale Basis
│   │   ├── base_ris.html             # RIS Layout (existiert)
│   │   ├── base_work.html            # Work Layout (neu)
│   │   │
│   │   ├── pages/                    # Insight Templates (existiert)
│   │   │
│   │   ├── accounts/                 # Auth Templates
│   │   │   ├── login.html
│   │   │   ├── register.html
│   │   │   └── profile.html
│   │   │
│   │   └── work/                     # Work Templates
│   │       ├── dashboard/
│   │       ├── meetings/
│   │       ├── motions/
│   │       ├── faction/
│   │       ├── tasks/
│   │       ├── ris/
│   │       ├── organization/
│   │       └── support/
│   │
│   └── static/
│       ├── css/
│       ├── js/
│       └── vendor/
│
├── docs/
├── infrastructure/
└── _old/                             # Referenz (nicht deployen)
```

### 2.2 URL-Struktur

```
/                                     # Landing Page
/insight/                             # Öffentliches RIS Portal
/insight/<kommune>/                   # Kommune-spezifisch

/accounts/                            # Auth-Bereich
/accounts/login/
/accounts/register/
/accounts/profile/
/accounts/2fa/

/work/                                # Tenant-Auswahl (wenn mehrere)
/work/<tenant-slug>/                  # Work Portal Entry
/work/<tenant-slug>/dashboard/        # Dashboard
/work/<tenant-slug>/meetings/         # Meine Sitzungen
/work/<tenant-slug>/motions/          # Antragsdatenbank
/work/<tenant-slug>/faction/          # Fraktionssitzungen
/work/<tenant-slug>/tasks/            # To-Dos
/work/<tenant-slug>/ris/              # RIS Kopie
/work/<tenant-slug>/organization/     # Einstellungen
/work/<tenant-slug>/support/          # Support
```

---

## 3. Multi-Tenant Architektur

### 3.1 Tenant-Modell

```python
class Tenant(models.Model):
    """Organisation/Fraktion als Mandant."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)

    # Identifikation
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, max_length=100)

    # Hierarchie (optional)
    parent = models.ForeignKey('self', null=True, blank=True,
                               on_delete=models.SET_NULL,
                               related_name='children')

    # Verknüpfung zu OParl
    body = models.ForeignKey('insight_core.OParlBody',
                            null=True, blank=True,
                            on_delete=models.SET_NULL)
    organizations = models.ManyToManyField('insight_core.OParlOrganization',
                                           blank=True)

    # Branding
    primary_color = models.CharField(max_length=7, default='#6366f1')
    logo = models.ImageField(upload_to='tenants/logos/', blank=True)

    # Einstellungen
    settings = models.JSONField(default=dict)
    require_2fa = models.BooleanField(default=False)

    # Status
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Verschlüsselungs-Key (pro Tenant)
    encryption_key = models.BinaryField(editable=False)
```

### 3.2 Membership & Rollen

```python
class Role(models.Model):
    """Rolle mit Berechtigungen."""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    permissions = models.JSONField(default=list)  # Liste von Permission-Codes
    is_admin = models.BooleanField(default=False)

    class Meta:
        unique_together = ['tenant', 'name']

class Membership(models.Model):
    """Benutzer-Zugehörigkeit zu Tenant."""
    user = models.ForeignKey('accounts.User', on_delete=models.CASCADE)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)

    # Rollen
    roles = models.ManyToManyField(Role, blank=True)

    # Verknüpfung zu OParl-Person (optional)
    oparl_person = models.ForeignKey('insight_core.OParlPerson',
                                     null=True, blank=True,
                                     on_delete=models.SET_NULL)

    # Status
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['user', 'tenant']
```

### 3.3 Permission-System

```python
# Verfügbare Permissions
PERMISSIONS = {
    # Dashboard
    'dashboard.view': 'Dashboard anzeigen',

    # Sitzungen
    'meetings.view': 'Sitzungen anzeigen',
    'meetings.prepare': 'Sitzungen vorbereiten',
    'meetings.manage': 'Sitzungen verwalten',

    # Anträge
    'motions.view': 'Anträge anzeigen',
    'motions.create': 'Anträge erstellen',
    'motions.edit': 'Anträge bearbeiten',
    'motions.delete': 'Anträge löschen',
    'motions.approve': 'Anträge freigeben',

    # Fraktionssitzungen
    'faction.view': 'Fraktionssitzungen anzeigen',
    'faction.create': 'Fraktionssitzungen erstellen',
    'faction.manage': 'Fraktionssitzungen verwalten',

    # To-Dos
    'tasks.view': 'Aufgaben anzeigen',
    'tasks.manage': 'Aufgaben verwalten',

    # Organisation
    'organization.view': 'Einstellungen anzeigen',
    'organization.members': 'Mitglieder verwalten',
    'organization.roles': 'Rollen verwalten',
    'organization.settings': 'Einstellungen ändern',

    # Support
    'support.view': 'Support anzeigen',
    'support.admin': 'Support administrieren',
}

# Standard-Rollen
DEFAULT_ROLES = {
    'admin': {
        'name': 'Administrator',
        'permissions': list(PERMISSIONS.keys()),
        'is_admin': True,
    },
    'member': {
        'name': 'Mitglied',
        'permissions': [
            'dashboard.view',
            'meetings.view', 'meetings.prepare',
            'motions.view', 'motions.create',
            'faction.view',
            'tasks.view',
            'support.view',
        ],
    },
    'viewer': {
        'name': 'Lesezugriff',
        'permissions': [
            'dashboard.view',
            'meetings.view',
            'motions.view',
            'faction.view',
            'support.view',
        ],
    },
}
```

### 3.4 Tenant-Middleware & Mixin

```python
# middleware.py
class TenantMiddleware:
    """Extrahiert Tenant aus URL und setzt Context."""

    def __call__(self, request):
        tenant_slug = self.get_tenant_slug(request.path)

        if tenant_slug:
            try:
                request.tenant = Tenant.objects.get(slug=tenant_slug, is_active=True)
            except Tenant.DoesNotExist:
                raise Http404("Organisation nicht gefunden")
        else:
            request.tenant = None

        return self.get_response(request)

# mixins.py
class TenantMixin(LoginRequiredMixin):
    """Basis-Mixin für alle Tenant-Views."""

    def dispatch(self, request, *args, **kwargs):
        if not hasattr(request, 'tenant') or not request.tenant:
            return redirect('work:tenant_select')

        # Membership prüfen
        try:
            self.membership = Membership.objects.get(
                user=request.user,
                tenant=request.tenant,
                is_active=True
            )
        except Membership.DoesNotExist:
            raise PermissionDenied("Kein Zugang zu dieser Organisation")

        return super().dispatch(request, *args, **kwargs)

    def has_permission(self, permission: str) -> bool:
        """Prüft ob User eine Permission hat."""
        if self.membership.roles.filter(is_admin=True).exists():
            return True

        for role in self.membership.roles.all():
            if permission in role.permissions:
                return True
        return False

    def get_queryset(self):
        """Filtert automatisch nach Tenant."""
        qs = super().get_queryset()
        if hasattr(qs.model, 'tenant'):
            return qs.filter(tenant=self.request.tenant)
        return qs
```

---

## 4. Datenverschlüsselung

### 4.1 Verschlüsselungsstrategie

| Datentyp | Verschlüsselung | Grund |
|----------|-----------------|-------|
| Notizen zu Tagesordnung | AES-256-GCM | Politisch sensibel |
| Positionen/Abstimmungen | AES-256-GCM | Intern vertraulich |
| Antragsinhalt (Entwurf) | AES-256-GCM | Bis zur Veröffentlichung |
| Fraktionsprotokolle | AES-256-GCM | Intern vertraulich |
| Support-Tickets | AES-256-GCM | Kundendaten |
| Normale Metadaten | Keine | Öffentlich/Unkritisch |

### 4.2 Implementierung

```python
# apps/common/encryption.py
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import os

class TenantEncryption:
    """Verschlüsselung pro Tenant."""

    def __init__(self, tenant):
        self.tenant = tenant
        self._key = None

    @property
    def key(self):
        if self._key is None:
            # Key aus Tenant laden oder generieren
            if not self.tenant.encryption_key:
                self.tenant.encryption_key = AESGCM.generate_key(bit_length=256)
                self.tenant.save(update_fields=['encryption_key'])
            self._key = self.tenant.encryption_key
        return self._key

    def encrypt(self, plaintext: str) -> bytes:
        """Verschlüsselt Text mit AES-256-GCM."""
        aesgcm = AESGCM(self.key)
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
        return nonce + ciphertext

    def decrypt(self, ciphertext: bytes) -> str:
        """Entschlüsselt mit AES-256-GCM."""
        aesgcm = AESGCM(self.key)
        nonce = ciphertext[:12]
        return aesgcm.decrypt(nonce, ciphertext[12:], None).decode()


class EncryptedTextField(models.BinaryField):
    """Django Field für verschlüsselte Texte."""

    def __init__(self, *args, **kwargs):
        kwargs['editable'] = True
        super().__init__(*args, **kwargs)

    def contribute_to_class(self, cls, name):
        super().contribute_to_class(cls, name)
        setattr(cls, f'get_{name}_decrypted',
                lambda self: self._decrypt_field(name))
        setattr(cls, f'set_{name}_encrypted',
                lambda self, value: self._encrypt_field(name, value))
```

---

## 5. Feature-Module

### 5.1 Dashboard

**Funktionen:**
- Übersicht über anstehende Sitzungen (nächste 2 Wochen)
- Offene To-Dos mit Priorität
- Letzte Aktivitäten in der Organisation
- Schnellzugriff auf häufig genutzte Funktionen
- Benachrichtigungen (ungelesen)

**Views:**
```python
class DashboardView(TenantMixin, TemplateView):
    template_name = 'work/dashboard/index.html'
    permission_required = 'dashboard.view'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['upcoming_meetings'] = self.get_upcoming_meetings()
        context['my_tasks'] = self.get_my_tasks()
        context['recent_activity'] = self.get_recent_activity()
        context['notifications'] = self.get_notifications()
        return context
```

### 5.2 Meine Sitzungen

**Funktionen:**
- Liste aller öffentlichen Sitzungen (aus OParl)
- Filter: Zeitraum, Gremium, Status
- Vorbereitung pro Sitzung:
  - Eigene Notizen (verschlüsselt)
  - Position pro TOP (Zustimmung/Ablehnung/Enthaltung)
  - Dokumente anhängen
  - Rednerliste
- Kalender-Ansicht
- ICS-Export

**Models:**
```python
class MeetingPreparation(models.Model):
    """Sitzungsvorbereitung eines Mitglieds."""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    membership = models.ForeignKey(Membership, on_delete=models.CASCADE)
    meeting = models.ForeignKey('insight_core.OParlMeeting',
                               on_delete=models.CASCADE)

    # Verschlüsselte Notizen
    notes_encrypted = EncryptedTextField(blank=True)

    # Status
    is_prepared = models.BooleanField(default=False)
    prepared_at = models.DateTimeField(null=True)

    class Meta:
        unique_together = ['membership', 'meeting']


class AgendaItemPosition(models.Model):
    """Position zu einem Tagesordnungspunkt."""
    preparation = models.ForeignKey(MeetingPreparation,
                                   on_delete=models.CASCADE,
                                   related_name='positions')
    agenda_item = models.ForeignKey('insight_core.OParlAgendaItem',
                                   on_delete=models.CASCADE)

    # Position
    POSITION_CHOICES = [
        ('for', 'Zustimmung'),
        ('against', 'Ablehnung'),
        ('abstain', 'Enthaltung'),
        ('discuss', 'Diskussionsbedarf'),
        ('none', 'Keine Position'),
    ]
    position = models.CharField(max_length=20, choices=POSITION_CHOICES,
                               default='none')

    # Verschlüsselte Begründung
    notes_encrypted = EncryptedTextField(blank=True)

    # Dokumente
    documents = models.ManyToManyField('motions.MotionDocument', blank=True)


class AgendaItemNote(models.Model):
    """Kollaborative Notiz zu einem TOP (für alle sichtbar)."""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    agenda_item = models.ForeignKey('insight_core.OParlAgendaItem',
                                   on_delete=models.CASCADE)

    # Sichtbarkeit
    VISIBILITY_CHOICES = [
        ('private', 'Nur ich'),
        ('organization', 'Meine Organisation'),
        ('public', 'Alle Organisationen'),
    ]
    visibility = models.CharField(max_length=20, choices=VISIBILITY_CHOICES,
                                 default='organization')

    # Inhalt (verschlüsselt)
    content_encrypted = EncryptedTextField()

    # Metadaten
    author = models.ForeignKey(Membership, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    is_decision = models.BooleanField(default=False)  # Als Beschluss markiert
```

### 5.3 Antragsdatenbank

**Funktionen:**
- CRUD für Anträge
- Typen: Antrag, Anfrage, Stellungnahme, Änderungsantrag
- Status-Workflow: Entwurf → Prüfung → Freigegeben → Eingereicht → Erledigt
- Versionierung
- Dokumente (PDF, Word) mit Textextraktion
- Teilen mit anderen Mitgliedern
- Kommentare/Diskussion
- Vorlagen
- Export (PDF, Word)

**Models:**
```python
class Motion(models.Model):
    """Antrag/Anfrage."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)

    # Typ
    TYPE_CHOICES = [
        ('motion', 'Antrag'),
        ('inquiry', 'Anfrage'),
        ('statement', 'Stellungnahme'),
        ('amendment', 'Änderungsantrag'),
    ]
    motion_type = models.CharField(max_length=20, choices=TYPE_CHOICES)

    # Inhalt
    title = models.CharField(max_length=500)
    content_encrypted = EncryptedTextField()  # Inhalt verschlüsselt
    summary = models.TextField(blank=True)     # Kurzfassung (öffentlich)

    # Status
    STATUS_CHOICES = [
        ('draft', 'Entwurf'),
        ('review', 'In Prüfung'),
        ('approved', 'Freigegeben'),
        ('submitted', 'Eingereicht'),
        ('completed', 'Erledigt'),
        ('rejected', 'Abgelehnt'),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                             default='draft')

    # Verknüpfungen
    related_paper = models.ForeignKey('insight_core.OParlPaper',
                                     null=True, blank=True,
                                     on_delete=models.SET_NULL)
    related_meeting = models.ForeignKey('insight_core.OParlMeeting',
                                       null=True, blank=True,
                                       on_delete=models.SET_NULL)

    # Metadaten
    author = models.ForeignKey(Membership, on_delete=models.CASCADE,
                              related_name='authored_motions')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True)

    # Tags
    tags = models.JSONField(default=list)


class MotionDocument(models.Model):
    """Dokument zu einem Antrag."""
    motion = models.ForeignKey(Motion, on_delete=models.CASCADE,
                              related_name='documents')

    file = models.FileField(upload_to='motions/documents/')
    filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100)

    # Extrahierter Text (für Suche)
    text_content = models.TextField(blank=True)

    uploaded_by = models.ForeignKey(Membership, on_delete=models.CASCADE)
    uploaded_at = models.DateTimeField(auto_now_add=True)


class MotionRevision(models.Model):
    """Versionierung von Anträgen."""
    motion = models.ForeignKey(Motion, on_delete=models.CASCADE,
                              related_name='revisions')

    version = models.PositiveIntegerField()
    content_encrypted = EncryptedTextField()

    changed_by = models.ForeignKey(Membership, on_delete=models.CASCADE)
    changed_at = models.DateTimeField(auto_now_add=True)
    change_summary = models.CharField(max_length=500, blank=True)

    class Meta:
        unique_together = ['motion', 'version']
        ordering = ['-version']
```

### 5.4 Fraktionssitzungen

**Funktionen:**
- Interne Sitzungen erstellen
- Wiederkehrende Termine (wöchentlich, monatlich)
- Ausnahmen von Regeltermin
- Eigene Tagesordnung
- Einladungen per E-Mail
- Anwesenheitsverfolgung
- Protokoll mit Genehmigungsworkflow
- Beschlüsse dokumentieren

**Models:**
```python
class FactionMeeting(models.Model):
    """Interne Fraktionssitzung."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)

    # Zeitpunkt
    title = models.CharField(max_length=200)
    start = models.DateTimeField()
    end = models.DateTimeField(null=True)

    # Ort
    location = models.CharField(max_length=500, blank=True)
    is_virtual = models.BooleanField(default=False)
    video_link = models.URLField(blank=True)

    # Status
    STATUS_CHOICES = [
        ('planned', 'Geplant'),
        ('ongoing', 'Läuft'),
        ('completed', 'Abgeschlossen'),
        ('cancelled', 'Abgesagt'),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                             default='planned')

    # Einladung
    invitation_sent = models.BooleanField(default=False)
    invitation_sent_at = models.DateTimeField(null=True)

    # Protokoll (verschlüsselt)
    protocol_encrypted = EncryptedTextField(blank=True)
    protocol_approved = models.BooleanField(default=False)
    protocol_approved_at = models.DateTimeField(null=True)

    # Verknüpfung zu öffentlicher Sitzung
    related_meeting = models.ForeignKey('insight_core.OParlMeeting',
                                       null=True, blank=True,
                                       on_delete=models.SET_NULL)

    created_by = models.ForeignKey(Membership, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)


class FactionMeetingSchedule(models.Model):
    """Wiederkehrende Termine."""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)

    name = models.CharField(max_length=200)

    # Wiederholung
    RECURRENCE_CHOICES = [
        ('weekly', 'Wöchentlich'),
        ('biweekly', 'Alle 2 Wochen'),
        ('monthly', 'Monatlich'),
    ]
    recurrence = models.CharField(max_length=20, choices=RECURRENCE_CHOICES)
    weekday = models.PositiveSmallIntegerField()  # 0=Mo, 6=So
    time = models.TimeField()
    duration_minutes = models.PositiveIntegerField(default=120)

    # Standardort
    default_location = models.CharField(max_length=500, blank=True)

    is_active = models.BooleanField(default=True)


class FactionAgendaItem(models.Model):
    """Tagesordnungspunkt für Fraktionssitzung."""
    meeting = models.ForeignKey(FactionMeeting, on_delete=models.CASCADE,
                               related_name='agenda_items')

    number = models.CharField(max_length=20)
    title = models.CharField(max_length=500)
    description_encrypted = EncryptedTextField(blank=True)

    # Verknüpfung zu öffentlichem TOP
    related_agenda_item = models.ForeignKey('insight_core.OParlAgendaItem',
                                           null=True, blank=True,
                                           on_delete=models.SET_NULL)

    # Beschluss
    decision_encrypted = EncryptedTextField(blank=True)

    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'number']


class FactionAttendance(models.Model):
    """Anwesenheit bei Fraktionssitzung."""
    meeting = models.ForeignKey(FactionMeeting, on_delete=models.CASCADE,
                               related_name='attendances')
    membership = models.ForeignKey(Membership, on_delete=models.CASCADE)

    STATUS_CHOICES = [
        ('invited', 'Eingeladen'),
        ('confirmed', 'Zugesagt'),
        ('declined', 'Abgesagt'),
        ('present', 'Anwesend'),
        ('absent', 'Abwesend'),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                             default='invited')

    checked_in_at = models.DateTimeField(null=True)
    checked_out_at = models.DateTimeField(null=True)

    class Meta:
        unique_together = ['meeting', 'membership']
```

### 5.5 To-Do System

**Funktionen:**
- Persönliche Aufgaben
- Organisations-Aufgaben (zuweisbar)
- Prioritäten (Hoch, Mittel, Niedrig)
- Fälligkeitsdatum
- Verknüpfung zu Sitzungen, Anträgen, TOPs
- Wiederkehrende Aufgaben
- Filter und Sortierung

**Models:**
```python
class Task(models.Model):
    """Aufgabe/To-Do."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)

    title = models.CharField(max_length=500)
    description = models.TextField(blank=True)

    # Priorität
    PRIORITY_CHOICES = [
        ('high', 'Hoch'),
        ('medium', 'Mittel'),
        ('low', 'Niedrig'),
    ]
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES,
                               default='medium')

    # Status
    is_completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True)

    # Zeitraum
    due_date = models.DateField(null=True, blank=True)

    # Zuweisung
    created_by = models.ForeignKey(Membership, on_delete=models.CASCADE,
                                  related_name='created_tasks')
    assigned_to = models.ForeignKey(Membership, on_delete=models.CASCADE,
                                   null=True, blank=True,
                                   related_name='assigned_tasks')

    # Verknüpfungen (optional)
    related_meeting = models.ForeignKey('insight_core.OParlMeeting',
                                       null=True, blank=True,
                                       on_delete=models.SET_NULL)
    related_motion = models.ForeignKey(Motion, null=True, blank=True,
                                      on_delete=models.SET_NULL)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-priority', 'due_date', '-created_at']
```

### 5.6 RIS-Kopie (Read-Only)

**Funktionen:**
- Gespiegelte Ansichten aus insight_core
- Automatischer Tenant-Kontext (nur verknüpfte OParl-Body/Orgs)
- Zusätzliche "Work"-Features:
  - Schnell-Notiz zu Vorgängen
  - Zur Antragsdatenbank hinzufügen
  - Zu Fraktionssitzung verknüpfen
- Erweiterte Suche

**Implementation:**
```python
# apps/work/ris/views.py
class WorkRISMixin(TenantMixin):
    """Erweitert RIS-Views um Work-Features."""

    def get_oparl_body(self):
        return self.request.tenant.body

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['can_add_note'] = self.has_permission('meetings.prepare')
        context['can_create_motion'] = self.has_permission('motions.create')
        return context


class WorkPaperListView(WorkRISMixin, PaperListView):
    template_name = 'work/ris/paper_list.html'


class WorkPaperDetailView(WorkRISMixin, PaperDetailView):
    template_name = 'work/ris/paper_detail.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Eigene Notizen laden
        context['my_notes'] = self.get_my_notes()
        # Verknüpfte Anträge laden
        context['related_motions'] = self.get_related_motions()
        return context
```

### 5.7 Organisationseinstellungen

**Funktionen:**
- Mitgliederverwaltung (einladen, entfernen, Rollen)
- Rollenverwaltung (erstellen, bearbeiten, Permissions)
- Organisationsprofil (Name, Logo, Farben)
- Gremienverknüpfung (OParl-Organisationen zuweisen)
- Benachrichtigungseinstellungen
- Datenschutz-Einstellungen
- API-Schlüssel (für Integrationen)

### 5.8 Support-System

**Funktionen:**
- Ticket erstellen
- Kategorien (Bug, Feature, Frage, Sonstiges)
- Prioritäten
- Dateianhänge
- Verlauf/Kommunikation
- Status-Tracking
- Knowledge Base (FAQ)

**Admin-Sicht (übergreifend):**
- Alle Tickets aller Tenants
- Zuweisung an Support-Mitarbeiter
- SLA-Tracking
- Statistiken

---

## 6. HTMX-Integration

### 6.1 Patterns

```html
<!-- Lazy Loading -->
<div hx-get="/work/{{ tenant.slug }}/meetings/upcoming/"
     hx-trigger="load"
     hx-swap="innerHTML">
    <div class="animate-pulse">Laden...</div>
</div>

<!-- Live Search -->
<input type="search"
       hx-get="/work/{{ tenant.slug }}/motions/search/"
       hx-trigger="keyup changed delay:300ms"
       hx-target="#motion-results">

<!-- Inline Edit -->
<div hx-get="/work/{{ tenant.slug }}/tasks/{{ task.id }}/edit/"
     hx-trigger="click"
     hx-swap="outerHTML">
    {{ task.title }}
</div>

<!-- Modal -->
<button hx-get="/work/{{ tenant.slug }}/motions/create/"
        hx-target="#modal-content"
        hx-trigger="click"
        @click="$dispatch('open-modal')">
    Neuer Antrag
</button>

<!-- Infinite Scroll -->
<div hx-get="/work/{{ tenant.slug }}/meetings/?page={{ next_page }}"
     hx-trigger="revealed"
     hx-swap="afterend">
</div>

<!-- Form Submit -->
<form hx-post="/work/{{ tenant.slug }}/tasks/create/"
      hx-target="#task-list"
      hx-swap="afterbegin"
      hx-on::after-request="this.reset()">
```

### 6.2 Template-Struktur

```
templates/work/
├── base_work.html              # Work-Layout mit Sidebar
├── components/
│   ├── sidebar.html            # Navigation
│   ├── header.html             # Top-Bar mit User-Menu
│   ├── modal.html              # Modal-Container
│   └── notifications.html      # Toast-Notifications
├── partials/                   # HTMX Partials
│   ├── meeting_card.html
│   ├── motion_row.html
│   ├── task_item.html
│   └── ...
└── [module]/
    ├── index.html
    ├── detail.html
    ├── form.html
    └── partials/
```

---

## 7. Performance-Optimierungen

### 7.1 Caching-Strategie

| Daten | Cache | TTL | Invalidierung |
|-------|-------|-----|---------------|
| OParl-Daten | Redis | 5 min | Bei Sync |
| Tenant-Settings | Redis | 1 h | Bei Änderung |
| User-Permissions | Redis | 15 min | Bei Rollen-Änderung |
| Dashboard-Stats | Redis | 5 min | Bei Aktivität |
| Suchindex | OpenSearch | Real-time | Signal |

### 7.2 Datenbankoptimierung

```python
# Immer select_related/prefetch_related nutzen
meetings = (
    MeetingPreparation.objects
    .filter(tenant=tenant)
    .select_related('meeting', 'meeting__body', 'membership__user')
    .prefetch_related('positions__agenda_item')
)

# Indexes für häufige Abfragen
class Meta:
    indexes = [
        models.Index(fields=['tenant', 'status']),
        models.Index(fields=['tenant', 'created_at']),
        models.Index(fields=['tenant', 'meeting', 'membership']),
    ]
```

### 7.3 Lazy Loading

- Dashboard-Widgets einzeln laden
- Sitzungsliste paginiert
- Antragsinhalt erst bei Bedarf entschlüsseln
- Dokumente on-demand laden

---

## 8. Sicherheit

### 8.1 Authentifizierung

- Session-basiert (Django default)
- Optional: 2FA (TOTP) pro Tenant erzwingbar
- Passwort-Policies konfigurierbar
- Brute-Force-Schutz (Rate Limiting)
- Session-Timeout konfigurierbar

### 8.2 Autorisierung

- RBAC (Role-Based Access Control)
- Permission-Checks auf View-Ebene
- Object-Level Permissions möglich
- Audit-Log für kritische Aktionen

### 8.3 Datenschutz (DSGVO)

- Verschlüsselung sensibler Daten
- Datenexport (User)
- Löschung auf Anfrage
- Einwilligungsverwaltung
- Verarbeitungsverzeichnis

---

## 9. Implementierungsreihenfolge

### Phase 1: Foundation (2-3 Wochen)
1. App-Struktur erstellen (`apps/`)
2. `accounts` App (User, Auth)
3. `tenants` App (Tenant, Membership, Roles)
4. `common` App (Encryption, Mixins)
5. Basis-Templates (`base_work.html`, Sidebar)
6. Multi-Tenant Middleware

### Phase 2: Core Work (3-4 Wochen)
1. `work/dashboard` - Persönliches Dashboard
2. `work/meetings` - Sitzungsvorbereitung
3. `work/ris` - RIS-Kopie mit Tenant-Filter

### Phase 3: Extended Work (3-4 Wochen)
1. `work/motions` - Antragsdatenbank
2. `work/faction` - Fraktionssitzungen
3. `work/tasks` - To-Do System

### Phase 4: Admin & Support (2 Wochen)
1. `work/organization` - Einstellungen
2. `work/support` - Ticket-System
3. Admin-Bereich (übergreifend)

### Phase 5: Polish (1-2 Wochen)
1. HTMX-Optimierungen
2. Performance-Tuning
3. Tests
4. Dokumentation

---

## 10. Technische Anforderungen

### 10.1 Abhängigkeiten (zusätzlich)

```
# requirements.txt
cryptography>=41.0.0      # Verschlüsselung
django-htmx>=1.17.0       # HTMX-Integration
celery>=5.3.0             # Background Tasks
redis>=5.0.0              # Cache & Broker
opensearch-py>=2.4.0      # Suche
python-docx>=1.0.0        # Word-Export
reportlab>=4.0.0          # PDF-Export
```

### 10.2 Umgebungsvariablen

```env
# Encryption
ENCRYPTION_MASTER_KEY=...

# Multi-Tenant
DEFAULT_TENANT_SLUG=demo

# Feature Flags
ENABLE_WORK_MODULE=true
ENABLE_2FA=true
```

---

## 11. Offene Fragen

1. **Session-Modul**: Soll später in `/apps/session/` analog zu Work implementiert werden?
2. **Branding**: Soll Tenant-Branding (Farben, Logo) in Templates dynamisch geladen werden?
3. **Notifications**: E-Mail vs. Push vs. In-App - Welche Priorität?
4. **Mobile**: PWA-Support oder native App später?
5. **API**: REST API für externe Integrationen?
6. **Billing**: Abrechnungssystem für SaaS?

---

## 12. Nächste Schritte

1. [ ] Review dieses Plans
2. [ ] Entscheidung zu offenen Fragen
3. [ ] App-Struktur erstellen
4. [ ] Tenant-Models implementieren
5. [ ] Basis-Views mit HTMX
6. [ ] Erste lauffähige Version (Dashboard)
