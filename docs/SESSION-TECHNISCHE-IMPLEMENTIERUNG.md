# Mandari Session - Technische Implementierung

**Version:** 1.0
**Stand:** Januar 2026
**Dokumenttyp:** Technische Spezifikation

---

## Inhaltsverzeichnis

1. [Architektur-Übersicht](#1-architektur-übersicht)
2. [Multi-Tenant-Architektur](#2-multi-tenant-architektur)
3. [Verschlüsselungskonzept](#3-verschlüsselungskonzept)
4. [Frontend mit HTMX](#4-frontend-mit-htmx)
5. [User- und Rollensystem](#5-user--und-rollensystem)
6. [Spaces und Kontexte](#6-spaces-und-kontexte)
7. [Workflow-Engine](#7-workflow-engine)
8. [OParl 1.1 Vollintegration](#8-oparl-11-vollintegration)
9. [3-Säulen-Kommunikation](#9-3-säulen-kommunikation)
10. [Schnittstellen-Framework](#10-schnittstellen-framework)
11. [Performance-Optimierung](#11-performance-optimierung)
12. [Deployment und Betrieb](#12-deployment-und-betrieb)

---

## 1. Architektur-Übersicht

### 1.1 System-Architektur

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              MANDARI PLATFORM                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                   │
│  │   INSIGHT    │    │    WORK      │    │   SESSION    │                   │
│  │   (Bürger)   │    │ (Fraktionen) │    │ (Verwaltung) │                   │
│  │              │    │              │    │              │                   │
│  │  Django +    │    │  Django +    │    │  Django +    │                   │
│  │   HTMX       │    │   HTMX       │    │   HTMX       │                   │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘                   │
│         │                   │                   │                            │
│         └───────────────────┼───────────────────┘                            │
│                             │                                                │
│                             ▼                                                │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                      MANDARI CORE API (FastAPI)                       │   │
│  │                                                                       │   │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐    │   │
│  │  │ OParl   │  │ Session │  │  Work   │  │ Search  │  │   AI    │    │   │
│  │  │  API    │  │   API   │  │   API   │  │   API   │  │   API   │    │   │
│  │  └─────────┘  └─────────┘  └─────────┘  └─────────┘  └─────────┘    │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                             │                                                │
│         ┌───────────────────┼───────────────────┐                           │
│         ▼                   ▼                   ▼                           │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐                     │
│  │ PostgreSQL  │    │ Meilisearch │    │   Redis     │                     │
│  │ (mit RLS)   │    │  (Suche)    │    │(Cache/Queue)│                     │
│  └─────────────┘    └─────────────┘    └─────────────┘                     │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 Technologie-Stack

| Komponente | Technologie | Begründung |
|------------|-------------|------------|
| **Backend API** | FastAPI (Python 3.12+) | Async, schnell, OpenAPI |
| **Frontend** | Django + HTMX + Alpine.js | Server-side, instant response |
| **Datenbank** | PostgreSQL 16 + RLS | Multi-Tenant, Row-Level Security |
| **Suche** | Meilisearch | Typo-tolerant, schnell |
| **Cache** | Redis | Sessions, Queues, Cache |
| **Queue** | Celery + Redis | Background Jobs |
| **Storage** | S3-kompatibel (MinIO) | Dokumente verschlüsselt |

### 1.3 Designprinzipien

1. **Digital First, Human Friendly**: Modern, aber verwaltungsgerecht
2. **Instant Response**: Jede Aktion < 100ms gefühlt
3. **Progressive Enhancement**: Funktioniert auch ohne JavaScript
4. **Zero Trust**: Jede Anfrage wird verifiziert
5. **Encryption by Default**: Alle Daten verschlüsselt
6. **OParl Native**: Standard first, Erweiterungen second

---

## 2. Multi-Tenant-Architektur

### 2.1 Tenant-Modell

Jeder **Tenant** ist eine Kommune (Body im OParl-Schema).

```python
# models/tenant.py
class Tenant(Base):
    """Ein Tenant = Eine Kommune/Körperschaft"""
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Identifikation
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    short_name: Mapped[str | None] = mapped_column(String(50))

    # OParl-Verknüpfung
    oparl_body_id: Mapped[str | None] = mapped_column(Text, unique=True)

    # Konfiguration
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    features: Mapped[list[str]] = mapped_column(JSONB, default=list)

    # Verschlüsselung
    encryption_key_id: Mapped[str] = mapped_column(String(100))

    # Branding
    logo_url: Mapped[str | None] = mapped_column(Text)
    primary_color: Mapped[str] = mapped_column(String(7), default="#3B82F6")

    # Status
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
```

### 2.2 Row-Level Security (RLS)

PostgreSQL RLS garantiert Datentrennung auf Datenbankebene.

```sql
-- RLS für alle tenant-spezifischen Tabellen
ALTER TABLE oparl_meetings ENABLE ROW LEVEL SECURITY;

-- Policy: Nutzer sehen nur Daten ihres Tenants
CREATE POLICY tenant_isolation ON oparl_meetings
    USING (tenant_id = current_setting('app.current_tenant_id')::uuid);

-- Policy für Session-Admin: Alle Daten des Tenants
CREATE POLICY session_admin_access ON oparl_meetings
    FOR ALL
    USING (
        tenant_id = current_setting('app.current_tenant_id')::uuid
        AND current_setting('app.user_role') IN ('session_admin', 'superadmin')
    );
```

### 2.3 Tenant-Context im Request

```python
# middleware/tenant.py
class TenantMiddleware:
    """Setzt Tenant-Kontext für jeden Request"""

    async def __call__(self, request: Request, call_next):
        # Tenant aus Subdomain oder Header
        tenant_slug = self._extract_tenant(request)

        if not tenant_slug:
            raise HTTPException(400, "Tenant nicht identifiziert")

        # Tenant laden und cachen
        tenant = await self._get_tenant(tenant_slug)

        # In Request-State speichern
        request.state.tenant = tenant
        request.state.tenant_id = tenant.id

        # PostgreSQL-Session konfigurieren
        async with get_db_session() as db:
            await db.execute(
                text(f"SET app.current_tenant_id = '{tenant.id}'")
            )

            response = await call_next(request)

        return response

    def _extract_tenant(self, request: Request) -> str | None:
        # 1. Subdomain: musterstadt.session.mandari.de
        host = request.headers.get("host", "")
        if ".session.mandari.de" in host:
            return host.split(".")[0]

        # 2. Header für API-Calls
        return request.headers.get("X-Tenant-ID")
```

### 2.4 Tenant-spezifische Konfiguration

```python
# Tenant-Settings Schema
TENANT_SETTINGS_SCHEMA = {
    # Workflows
    "workflows": {
        "paper_approval_chain": ["author", "department_head", "mayor"],
        "protocol_approval": ["protocol_writer", "chair"],
        "require_digital_signature": False,
    },

    # Fristen
    "deadlines": {
        "invitation_days_before": 7,
        "paper_submission_days": 14,
        "protocol_approval_days": 21,
    },

    # Sitzungsgelder
    "allowances": {
        "enabled": True,
        "base_rate": 30.00,
        "mileage_rate": 0.30,
        "hkr_export_format": "xml_standard",
    },

    # Features
    "features": {
        "online_voting": True,
        "ai_protocol": True,
        "document_ocr": True,
    },

    # Branding
    "branding": {
        "primary_color": "#003366",
        "logo_position": "left",
        "custom_css": None,
    }
}
```

---

## 3. Verschlüsselungskonzept

### 3.1 Verschlüsselungsebenen

```
┌─────────────────────────────────────────────────────────────────┐
│                    VERSCHLÜSSELUNGSEBENEN                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. TRANSPORT (TLS 1.3)                                         │
│     └─ Alle Verbindungen verschlüsselt                          │
│                                                                  │
│  2. AT-REST (AES-256-GCM)                                       │
│     ├─ Datenbank: Transparent Data Encryption                   │
│     ├─ Dateien: Individuelle Dateiverschlüsselung               │
│     └─ Backups: Verschlüsselt                                   │
│                                                                  │
│  3. FIELD-LEVEL (für sensible Felder)                           │
│     ├─ Personendaten (E-Mail, Telefon, Adresse)                 │
│     ├─ Bankverbindungen                                         │
│     └─ Nichtöffentliche Dokumente                               │
│                                                                  │
│  4. END-TO-END (für Work-Integration)                           │
│     └─ Nichtöffentliche TOPs nur client-seitig entschlüsselbar  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 Schlüsselhierarchie

```python
# crypto/keys.py
class KeyHierarchy:
    """
    Schlüsselhierarchie:

    Master Key (HSM/Vault)
        └─ Tenant Key (pro Kommune)
            ├─ Data Key (für DB-Felder)
            ├─ File Key (für Dokumente)
            └─ Session Keys (temporär)
    """

    @staticmethod
    async def get_tenant_key(tenant_id: uuid.UUID) -> bytes:
        """Holt Tenant-Schlüssel aus Vault"""
        vault = get_vault_client()
        key_path = f"tenants/{tenant_id}/data_key"
        return await vault.get_secret(key_path)

    @staticmethod
    async def derive_field_key(tenant_key: bytes, field_name: str) -> bytes:
        """Leitet feldspezifischen Schlüssel ab"""
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=field_name.encode(),
            info=b"field_encryption"
        ).derive(tenant_key)
```

### 3.3 Feld-Level-Verschlüsselung

```python
# models/mixins.py
from cryptography.fernet import Fernet

class EncryptedFieldMixin:
    """Mixin für verschlüsselte Felder"""

    @declared_attr
    def _encrypted_fields(cls) -> list[str]:
        return []

    def encrypt_field(self, field_name: str, value: str) -> str:
        """Verschlüsselt einen Feldwert"""
        if not value:
            return value

        key = get_current_tenant_key()
        fernet = Fernet(key)
        return fernet.encrypt(value.encode()).decode()

    def decrypt_field(self, field_name: str, encrypted_value: str) -> str:
        """Entschlüsselt einen Feldwert"""
        if not encrypted_value:
            return encrypted_value

        key = get_current_tenant_key()
        fernet = Fernet(key)
        return fernet.decrypt(encrypted_value.encode()).decode()


# Verwendung
class OParlPerson(Base, EncryptedFieldMixin):
    __tablename__ = "oparl_persons"
    _encrypted_fields = ["email", "phone", "address"]

    # Verschlüsselte Felder
    _email_encrypted: Mapped[str | None] = mapped_column("email", Text)
    _phone_encrypted: Mapped[str | None] = mapped_column("phone", Text)

    @hybrid_property
    def email(self) -> str | None:
        return self.decrypt_field("email", self._email_encrypted)

    @email.setter
    def email(self, value: str | None):
        self._email_encrypted = self.encrypt_field("email", value)
```

### 3.4 Dokument-Verschlüsselung

```python
# services/document_encryption.py
class DocumentEncryptionService:
    """Verschlüsselung von Dokumenten im Storage"""

    def __init__(self, tenant_id: uuid.UUID):
        self.tenant_id = tenant_id
        self.storage = get_storage_client()

    async def encrypt_and_store(
        self,
        content: bytes,
        filename: str,
        confidentiality: str = "PUBLIC"
    ) -> str:
        """Verschlüsselt und speichert ein Dokument"""

        # Generiere einzigartigen Dokument-Schlüssel
        doc_key = os.urandom(32)

        # Verschlüssele Inhalt
        cipher = AESGCM(doc_key)
        nonce = os.urandom(12)
        encrypted_content = cipher.encrypt(nonce, content, None)

        # Verschlüssele Dokument-Schlüssel mit Tenant-Key
        tenant_key = await KeyHierarchy.get_tenant_key(self.tenant_id)
        encrypted_doc_key = self._wrap_key(doc_key, tenant_key)

        # Speichere mit Metadaten
        storage_path = f"{self.tenant_id}/{uuid.uuid4()}/{filename}.enc"

        metadata = {
            "encrypted_key": base64.b64encode(encrypted_doc_key).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "algorithm": "AES-256-GCM",
            "confidentiality": confidentiality,
            "original_filename": filename,
        }

        await self.storage.put_object(
            storage_path,
            encrypted_content,
            metadata=metadata
        )

        return storage_path

    async def decrypt_and_retrieve(self, storage_path: str) -> tuple[bytes, dict]:
        """Lädt und entschlüsselt ein Dokument"""

        # Lade verschlüsseltes Dokument
        encrypted_content, metadata = await self.storage.get_object(storage_path)

        # Entschlüssele Dokument-Schlüssel
        tenant_key = await KeyHierarchy.get_tenant_key(self.tenant_id)
        doc_key = self._unwrap_key(
            base64.b64decode(metadata["encrypted_key"]),
            tenant_key
        )

        # Entschlüssele Inhalt
        cipher = AESGCM(doc_key)
        nonce = base64.b64decode(metadata["nonce"])
        content = cipher.decrypt(nonce, encrypted_content, None)

        return content, metadata
```

---

## 4. Frontend mit HTMX

### 4.1 HTMX-Architektur

```
┌─────────────────────────────────────────────────────────────────┐
│                      HTMX RESPONSE FLOW                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Browser                    Server (Django)                      │
│  ┌─────┐                   ┌─────────────┐                      │
│  │     │ ── hx-get ──────► │             │                      │
│  │     │                   │   View      │                      │
│  │     │ ◄── HTML ──────── │             │                      │
│  │     │    Fragment       └─────────────┘                      │
│  │     │                                                         │
│  │     │    hx-swap="innerHTML"                                  │
│  │     │    ───────────────────►                                 │
│  │     │    DOM Update (kein Reload!)                            │
│  └─────┘                                                         │
│                                                                  │
│  VORTEILE:                                                       │
│  ✓ Server-side Rendering (SEO, Accessibility)                   │
│  ✓ Kein JavaScript-Framework nötig                               │
│  ✓ Instant Response (nur geänderte Teile)                       │
│  ✓ Progressive Enhancement                                       │
│  ✓ Einfache Implementierung                                      │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 Template-Struktur

```
mandari/templates/session/
├── base_session.html           # Basis-Layout mit Navigation
├── components/                  # Wiederverwendbare Komponenten
│   ├── _button.html
│   ├── _card.html
│   ├── _modal.html
│   ├── _table.html
│   ├── _pagination.html
│   ├── _toast.html
│   └── _loading.html
├── partials/                    # HTMX-Partials (Fragmente)
│   ├── meetings/
│   │   ├── _list.html
│   │   ├── _card.html
│   │   ├── _agenda_item.html
│   │   └── _attendance_row.html
│   ├── papers/
│   │   ├── _list.html
│   │   ├── _card.html
│   │   ├── _workflow_status.html
│   │   └── _approval_chain.html
│   └── ...
├── pages/                       # Vollständige Seiten
│   ├── dashboard.html
│   ├── meetings/
│   │   ├── list.html
│   │   ├── detail.html
│   │   ├── create.html
│   │   └── agenda_editor.html
│   └── ...
└── emails/                      # E-Mail-Templates
    ├── invitation.html
    └── reminder.html
```

### 4.3 HTMX-Pattern: Instant Search

```html
<!-- pages/meetings/list.html -->
{% extends "session/base_session.html" %}

{% block content %}
<div class="max-w-7xl mx-auto px-4 py-8">
    <h1 class="text-2xl font-bold mb-6">Sitzungen</h1>

    <!-- Suchfeld mit HTMX -->
    <div class="mb-6">
        <input type="search"
               name="q"
               placeholder="Sitzung suchen..."
               class="input input-bordered w-full max-w-md"
               hx-get="{% url 'session:meetings_list' %}"
               hx-trigger="input changed delay:300ms, search"
               hx-target="#meetings-list"
               hx-swap="innerHTML"
               hx-indicator="#search-spinner"
               hx-include="[name='filter_org'], [name='filter_status']">

        <!-- Loading Indicator -->
        <span id="search-spinner" class="htmx-indicator ml-2">
            <svg class="animate-spin h-5 w-5" viewBox="0 0 24 24">...</svg>
        </span>
    </div>

    <!-- Filter -->
    <div class="flex gap-4 mb-6">
        <select name="filter_org"
                hx-get="{% url 'session:meetings_list' %}"
                hx-trigger="change"
                hx-target="#meetings-list"
                hx-include="[name='q'], [name='filter_status']"
                class="select select-bordered">
            <option value="">Alle Gremien</option>
            {% for org in organizations %}
            <option value="{{ org.id }}">{{ org.name }}</option>
            {% endfor %}
        </select>

        <select name="filter_status"
                hx-get="{% url 'session:meetings_list' %}"
                hx-trigger="change"
                hx-target="#meetings-list"
                hx-include="[name='q'], [name='filter_org']"
                class="select select-bordered">
            <option value="">Alle Status</option>
            <option value="planned">Geplant</option>
            <option value="invited">Eingeladen</option>
            <option value="completed">Abgeschlossen</option>
        </select>
    </div>

    <!-- Meetings Liste (wird via HTMX ausgetauscht) -->
    <div id="meetings-list">
        {% include "session/partials/meetings/_list.html" %}
    </div>
</div>
{% endblock %}
```

### 4.4 HTMX-Pattern: Inline-Edit

```html
<!-- partials/papers/_workflow_status.html -->
<div id="workflow-{{ paper.id }}" class="bg-white rounded-lg shadow p-4">
    <div class="flex items-center justify-between mb-4">
        <h3 class="font-semibold">Workflow-Status</h3>
        <span class="badge badge-{{ workflow.status|lower }}">
            {{ workflow.get_status_display }}
        </span>
    </div>

    <!-- Freigabe-Kette -->
    <div class="space-y-3">
        {% for step in workflow.steps %}
        <div class="flex items-center gap-3 p-3 rounded
                    {% if step.completed %}bg-green-50{% elif step.current %}bg-blue-50{% else %}bg-gray-50{% endif %}">

            <!-- Status Icon -->
            <div class="w-8 h-8 rounded-full flex items-center justify-center
                        {% if step.completed %}bg-green-500 text-white
                        {% elif step.current %}bg-blue-500 text-white
                        {% else %}bg-gray-300{% endif %}">
                {% if step.completed %}
                    <i data-lucide="check" class="w-4 h-4"></i>
                {% elif step.current %}
                    <i data-lucide="clock" class="w-4 h-4"></i>
                {% else %}
                    <span class="text-sm">{{ forloop.counter }}</span>
                {% endif %}
            </div>

            <!-- Step Info -->
            <div class="flex-1">
                <p class="font-medium">{{ step.role_display }}</p>
                <p class="text-sm text-gray-500">{{ step.user.name }}</p>
            </div>

            <!-- Aktionen (nur für aktuellen Schritt und berechtigten User) -->
            {% if step.current and step.user == request.user %}
            <div class="flex gap-2">
                <button hx-post="{% url 'session:paper_approve' paper.id %}"
                        hx-target="#workflow-{{ paper.id }}"
                        hx-swap="outerHTML"
                        hx-confirm="Vorlage freigeben?"
                        class="btn btn-success btn-sm">
                    <i data-lucide="check" class="w-4 h-4 mr-1"></i>
                    Freigeben
                </button>

                <button hx-get="{% url 'session:paper_reject_modal' paper.id %}"
                        hx-target="#modal-container"
                        hx-swap="innerHTML"
                        @click="$dispatch('open-modal')"
                        class="btn btn-error btn-sm btn-outline">
                    <i data-lucide="x" class="w-4 h-4 mr-1"></i>
                    Ablehnen
                </button>
            </div>
            {% endif %}

            {% if step.completed %}
            <span class="text-xs text-gray-400">
                {{ step.completed_at|date:"d.m.Y H:i" }}
            </span>
            {% endif %}
        </div>
        {% endfor %}
    </div>
</div>
```

### 4.5 HTMX-Pattern: Drag & Drop Tagesordnung

```html
<!-- pages/meetings/agenda_editor.html -->
<div id="agenda-editor"
     x-data="agendaEditor()"
     class="bg-white rounded-lg shadow">

    <div class="p-4 border-b flex items-center justify-between">
        <h2 class="font-semibold">Tagesordnung bearbeiten</h2>
        <div class="flex gap-2">
            <button hx-post="{% url 'session:agenda_save' meeting.id %}"
                    hx-include="#agenda-form"
                    hx-target="#agenda-editor"
                    hx-swap="outerHTML"
                    class="btn btn-primary btn-sm">
                <i data-lucide="save" class="w-4 h-4 mr-1"></i>
                Speichern
            </button>
        </div>
    </div>

    <form id="agenda-form">
        <!-- Sortierbare Liste -->
        <div id="agenda-items"
             class="divide-y"
             x-ref="sortable"
             x-init="initSortable()"
             hx-post="{% url 'session:agenda_reorder' meeting.id %}"
             hx-trigger="sortable:end"
             hx-swap="none">

            {% for item in agenda_items %}
            <div class="agenda-item p-4 flex items-center gap-4 hover:bg-gray-50"
                 data-id="{{ item.id }}"
                 draggable="true">

                <!-- Drag Handle -->
                <div class="cursor-move text-gray-400 hover:text-gray-600">
                    <i data-lucide="grip-vertical" class="w-5 h-5"></i>
                </div>

                <!-- Nummer -->
                <input type="hidden" name="items[]" value="{{ item.id }}">
                <span class="w-8 text-center font-mono text-gray-500">
                    {{ item.number }}
                </span>

                <!-- Titel (editierbar) -->
                <div class="flex-1">
                    <input type="text"
                           name="title_{{ item.id }}"
                           value="{{ item.name }}"
                           class="w-full bg-transparent border-0 focus:ring-2 focus:ring-primary-500 rounded px-2 py-1">
                </div>

                <!-- Öffentlichkeit -->
                <label class="flex items-center gap-2 cursor-pointer">
                    <input type="checkbox"
                           name="public_{{ item.id }}"
                           {% if item.public %}checked{% endif %}
                           class="checkbox checkbox-sm">
                    <span class="text-sm">Öffentlich</span>
                </label>

                <!-- Verknüpfte Vorlage -->
                {% if item.paper %}
                <a href="{% url 'session:paper_detail' item.paper.id %}"
                   class="badge badge-info">
                    {{ item.paper.reference }}
                </a>
                {% else %}
                <button hx-get="{% url 'session:agenda_link_paper' item.id %}"
                        hx-target="#modal-container"
                        class="btn btn-ghost btn-xs">
                    <i data-lucide="link" class="w-4 h-4"></i>
                </button>
                {% endif %}

                <!-- Löschen -->
                <button hx-delete="{% url 'session:agenda_item_delete' item.id %}"
                        hx-target="closest .agenda-item"
                        hx-swap="outerHTML"
                        hx-confirm="TOP löschen?"
                        class="btn btn-ghost btn-xs text-error">
                    <i data-lucide="trash-2" class="w-4 h-4"></i>
                </button>
            </div>
            {% endfor %}
        </div>

        <!-- Neuen TOP hinzufügen -->
        <div class="p-4 border-t">
            <button hx-get="{% url 'session:agenda_item_new' meeting.id %}"
                    hx-target="#agenda-items"
                    hx-swap="beforeend"
                    class="btn btn-outline btn-sm w-full">
                <i data-lucide="plus" class="w-4 h-4 mr-2"></i>
                Tagesordnungspunkt hinzufügen
            </button>
        </div>
    </form>
</div>

<script>
function agendaEditor() {
    return {
        initSortable() {
            new Sortable(this.$refs.sortable, {
                animation: 150,
                handle: '.cursor-move',
                onEnd: (evt) => {
                    htmx.trigger(this.$refs.sortable, 'sortable:end');
                }
            });
        }
    }
}
</script>
```

### 4.6 Response-Optimierung

```python
# views/mixins.py
class HTMXViewMixin:
    """Mixin für HTMX-optimierte Views"""

    partial_template_name: str | None = None

    def get_template_names(self) -> list[str]:
        """Liefert Partial-Template für HTMX-Requests"""
        if self.request.headers.get("HX-Request") and self.partial_template_name:
            return [self.partial_template_name]
        return super().get_template_names()

    def render_to_response(self, context, **response_kwargs):
        """Fügt HTMX-spezifische Header hinzu"""
        response = super().render_to_response(context, **response_kwargs)

        # Trigger Events für Client
        if hasattr(self, 'htmx_trigger'):
            response['HX-Trigger'] = json.dumps(self.htmx_trigger)

        # URL-Update für Browser-History
        if hasattr(self, 'htmx_push_url'):
            response['HX-Push-Url'] = self.htmx_push_url

        return response


# Verwendung
class MeetingsListView(HTMXViewMixin, ListView):
    model = OParlMeeting
    template_name = "session/pages/meetings/list.html"
    partial_template_name = "session/partials/meetings/_list.html"
    context_object_name = "meetings"
    paginate_by = 20

    def get_queryset(self):
        qs = super().get_queryset().filter(
            tenant_id=self.request.tenant.id
        )

        # Filter anwenden
        if q := self.request.GET.get('q'):
            qs = qs.filter(name__icontains=q)

        if org := self.request.GET.get('filter_org'):
            qs = qs.filter(organization_id=org)

        if status := self.request.GET.get('filter_status'):
            qs = qs.filter(status=status)

        return qs.order_by('-start')
```

---

## 5. User- und Rollensystem

### 5.1 User-Modell

```python
# models/user.py
class User(Base):
    """Benutzer im System"""
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Auth
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)

    # Profil
    display_name: Mapped[str] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(Text)

    # Verknüpfung zu OParl-Person (optional)
    oparl_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("oparl_persons.id"), nullable=True
    )

    # Status
    is_active: Mapped[bool] = mapped_column(default=True)
    is_superadmin: Mapped[bool] = mapped_column(default=False)

    # 2FA
    totp_secret: Mapped[str | None] = mapped_column(Text)  # verschlüsselt
    totp_enabled: Mapped[bool] = mapped_column(default=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_login: Mapped[datetime | None]

    # Relationships
    tenant_memberships: Mapped[list["TenantMembership"]] = relationship(
        back_populates="user"
    )


class TenantMembership(Base):
    """Mitgliedschaft eines Users in einem Tenant"""
    __tablename__ = "tenant_memberships"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))

    # Rollen in diesem Tenant
    roles: Mapped[list[str]] = mapped_column(JSONB, default=list)

    # Einschränkungen
    allowed_organizations: Mapped[list[uuid.UUID] | None] = mapped_column(
        JSONB, nullable=True
    )  # None = alle

    is_active: Mapped[bool] = mapped_column(default=True)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="tenant_memberships")
    tenant: Mapped["Tenant"] = relationship()
```

### 5.2 Rollen-Definition

```python
# auth/roles.py
from enum import Enum
from dataclasses import dataclass

class Permission(str, Enum):
    """Atomare Berechtigungen"""

    # Sitzungen
    MEETING_VIEW = "meeting:view"
    MEETING_CREATE = "meeting:create"
    MEETING_EDIT = "meeting:edit"
    MEETING_DELETE = "meeting:delete"
    MEETING_INVITE = "meeting:invite"
    MEETING_START = "meeting:start"

    # Vorlagen
    PAPER_VIEW = "paper:view"
    PAPER_CREATE = "paper:create"
    PAPER_EDIT = "paper:edit"
    PAPER_DELETE = "paper:delete"
    PAPER_SUBMIT = "paper:submit"
    PAPER_APPROVE = "paper:approve"

    # Protokolle
    PROTOCOL_VIEW = "protocol:view"
    PROTOCOL_EDIT = "protocol:edit"
    PROTOCOL_APPROVE = "protocol:approve"
    PROTOCOL_PUBLISH = "protocol:publish"

    # Personen/Stammdaten
    PERSON_VIEW = "person:view"
    PERSON_EDIT = "person:edit"

    # Administration
    TENANT_ADMIN = "tenant:admin"
    USER_MANAGE = "user:manage"
    SETTINGS_EDIT = "settings:edit"

    # Nichtöffentlich
    NON_PUBLIC_VIEW = "non_public:view"
    CONFIDENTIAL_VIEW = "confidential:view"


@dataclass
class Role:
    """Rollendefinition mit Berechtigungen"""
    name: str
    display_name: str
    permissions: set[Permission]
    description: str


# Vordefinierte Rollen
ROLES = {
    "session_admin": Role(
        name="session_admin",
        display_name="Session-Administrator",
        permissions={p for p in Permission},  # Alle
        description="Vollzugriff auf Session"
    ),

    "meeting_manager": Role(
        name="meeting_manager",
        display_name="Sitzungsmanagement",
        permissions={
            Permission.MEETING_VIEW, Permission.MEETING_CREATE,
            Permission.MEETING_EDIT, Permission.MEETING_INVITE,
            Permission.MEETING_START,
            Permission.PAPER_VIEW, Permission.PROTOCOL_VIEW,
            Permission.PROTOCOL_EDIT, Permission.PERSON_VIEW,
            Permission.NON_PUBLIC_VIEW,
        },
        description="Kann Sitzungen planen und durchführen"
    ),

    "protocol_writer": Role(
        name="protocol_writer",
        display_name="Protokollführer",
        permissions={
            Permission.MEETING_VIEW, Permission.PAPER_VIEW,
            Permission.PROTOCOL_VIEW, Permission.PROTOCOL_EDIT,
            Permission.PERSON_VIEW, Permission.NON_PUBLIC_VIEW,
        },
        description="Kann Protokolle erstellen und bearbeiten"
    ),

    "paper_author": Role(
        name="paper_author",
        display_name="Vorlagenersteller",
        permissions={
            Permission.MEETING_VIEW, Permission.PAPER_VIEW,
            Permission.PAPER_CREATE, Permission.PAPER_EDIT,
            Permission.PAPER_SUBMIT, Permission.PERSON_VIEW,
        },
        description="Kann Vorlagen erstellen und einreichen"
    ),

    "paper_approver": Role(
        name="paper_approver",
        display_name="Vorlagenfreigabe",
        permissions={
            Permission.MEETING_VIEW, Permission.PAPER_VIEW,
            Permission.PAPER_APPROVE, Permission.PERSON_VIEW,
            Permission.NON_PUBLIC_VIEW,
        },
        description="Kann Vorlagen freigeben"
    ),

    "viewer": Role(
        name="viewer",
        display_name="Lesezugriff",
        permissions={
            Permission.MEETING_VIEW, Permission.PAPER_VIEW,
            Permission.PROTOCOL_VIEW, Permission.PERSON_VIEW,
        },
        description="Nur lesender Zugriff auf öffentliche Inhalte"
    ),

    "mandator": Role(
        name="mandator",
        display_name="Mandatsträger",
        permissions={
            Permission.MEETING_VIEW, Permission.PAPER_VIEW,
            Permission.PROTOCOL_VIEW, Permission.PERSON_VIEW,
            Permission.NON_PUBLIC_VIEW,
        },
        description="Ratsmitglied mit Zugriff auf nichtöffentliche Inhalte"
    ),
}
```

### 5.3 Permission-Check

```python
# auth/permissions.py
from functools import wraps

class PermissionChecker:
    """Prüft Berechtigungen eines Users"""

    def __init__(self, user: User, tenant: Tenant):
        self.user = user
        self.tenant = tenant
        self._permissions: set[Permission] | None = None

    @property
    def permissions(self) -> set[Permission]:
        """Lädt und cached alle Berechtigungen"""
        if self._permissions is None:
            self._permissions = self._load_permissions()
        return self._permissions

    def _load_permissions(self) -> set[Permission]:
        """Sammelt alle Berechtigungen aus Rollen"""
        if self.user.is_superadmin:
            return {p for p in Permission}

        membership = self._get_membership()
        if not membership:
            return set()

        perms = set()
        for role_name in membership.roles:
            if role := ROLES.get(role_name):
                perms.update(role.permissions)

        return perms

    def has_permission(self, permission: Permission) -> bool:
        """Prüft einzelne Berechtigung"""
        return permission in self.permissions

    def has_any_permission(self, *permissions: Permission) -> bool:
        """Prüft ob mindestens eine Berechtigung vorhanden"""
        return bool(self.permissions & set(permissions))

    def has_all_permissions(self, *permissions: Permission) -> bool:
        """Prüft ob alle Berechtigungen vorhanden"""
        return set(permissions) <= self.permissions

    def can_access_organization(self, org_id: uuid.UUID) -> bool:
        """Prüft Zugriff auf bestimmtes Gremium"""
        membership = self._get_membership()
        if not membership:
            return False

        # None = alle Gremien erlaubt
        if membership.allowed_organizations is None:
            return True

        return org_id in membership.allowed_organizations


# Decorator für Views
def require_permission(*permissions: Permission):
    """Decorator der Berechtigungen prüft"""
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            checker = PermissionChecker(request.user, request.tenant)

            if not checker.has_all_permissions(*permissions):
                raise PermissionDenied("Fehlende Berechtigung")

            return view_func(request, *args, **kwargs)
        return wrapped
    return decorator


# Verwendung in View
@require_permission(Permission.PAPER_APPROVE)
def approve_paper(request, paper_id):
    ...
```

---

## 6. Spaces und Kontexte

### 6.1 Space-Konzept

```
┌─────────────────────────────────────────────────────────────────┐
│                        TENANT (Kommune)                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐ │
│  │  GLOBAL SPACE   │  │  ORG SPACE      │  │  PERSONAL SPACE │ │
│  │                 │  │                 │  │                 │ │
│  │  • Dashboard    │  │  • Gremium-     │  │  • Meine        │ │
│  │  • Alle Sitz-   │  │    sitzungen    │  │    Aufgaben     │ │
│  │    ungen        │  │  • Gremium-     │  │  • Meine        │ │
│  │  • Alle Vor-    │  │    vorlagen     │  │    Vorlagen     │ │
│  │    lagen        │  │  • Mitglieder   │  │  • Favoriten    │ │
│  │  • Personen     │  │                 │  │  • Notizen      │ │
│  │  • Gremien      │  │                 │  │                 │ │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘ │
│                                                                  │
│  ZUGRIFF:                                                        │
│  ────────                                                        │
│  Global: Gemäß Rolle                                            │
│  Org: Nur bei Mitgliedschaft/Berechtigung                       │
│  Personal: Nur eigener User                                      │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 6.2 Navigation-Struktur

```python
# navigation/menu.py
def build_navigation(user: User, tenant: Tenant) -> list[dict]:
    """Baut Navigation basierend auf Berechtigungen"""

    checker = PermissionChecker(user, tenant)
    nav = []

    # Dashboard (immer)
    nav.append({
        "name": "Dashboard",
        "url": reverse("session:dashboard"),
        "icon": "layout-dashboard",
    })

    # Sitzungen
    if checker.has_permission(Permission.MEETING_VIEW):
        nav.append({
            "name": "Sitzungen",
            "url": reverse("session:meetings_list"),
            "icon": "calendar",
            "children": [
                {"name": "Übersicht", "url": reverse("session:meetings_list")},
                {"name": "Kalender", "url": reverse("session:meetings_calendar")},
            ] + (
                [{"name": "Neue Sitzung", "url": reverse("session:meeting_create")}]
                if checker.has_permission(Permission.MEETING_CREATE) else []
            )
        })

    # Vorlagen
    if checker.has_permission(Permission.PAPER_VIEW):
        nav.append({
            "name": "Vorlagen",
            "url": reverse("session:papers_list"),
            "icon": "file-text",
            "badge": get_pending_approvals_count(user, tenant),  # Wartende Freigaben
        })

    # Gremien
    if checker.has_permission(Permission.PERSON_VIEW):
        nav.append({
            "name": "Gremien",
            "url": reverse("session:organizations_list"),
            "icon": "users",
        })

    # Personen
    if checker.has_permission(Permission.PERSON_VIEW):
        nav.append({
            "name": "Personen",
            "url": reverse("session:persons_list"),
            "icon": "user",
        })

    # Administration
    if checker.has_permission(Permission.TENANT_ADMIN):
        nav.append({
            "name": "Einstellungen",
            "url": reverse("session:settings"),
            "icon": "settings",
            "children": [
                {"name": "Allgemein", "url": reverse("session:settings_general")},
                {"name": "Benutzer", "url": reverse("session:settings_users")},
                {"name": "Workflows", "url": reverse("session:settings_workflows")},
                {"name": "Schnittstellen", "url": reverse("session:settings_integrations")},
            ]
        })

    return nav
```

### 6.3 Kontext-Switcher

```html
<!-- components/_context_switcher.html -->
<div x-data="{ open: false }" class="relative">
    <!-- Aktueller Kontext -->
    <button @click="open = !open"
            class="flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-gray-100">
        {% if current_organization %}
            <span class="w-8 h-8 rounded-lg bg-primary-500 text-white flex items-center justify-center text-sm font-medium">
                {{ current_organization.short_name|slice:":2"|upper }}
            </span>
            <span class="font-medium">{{ current_organization.name }}</span>
        {% else %}
            <span class="w-8 h-8 rounded-lg bg-gray-200 flex items-center justify-center">
                <i data-lucide="globe" class="w-4 h-4 text-gray-600"></i>
            </span>
            <span class="font-medium">Alle Gremien</span>
        {% endif %}
        <i data-lucide="chevron-down" class="w-4 h-4 text-gray-400"></i>
    </button>

    <!-- Dropdown -->
    <div x-show="open"
         @click.away="open = false"
         x-transition
         class="absolute left-0 mt-2 w-64 bg-white rounded-lg shadow-lg border z-50">

        <!-- Globale Ansicht -->
        <a href="{% url 'session:context_clear' %}"
           class="flex items-center gap-3 px-4 py-3 hover:bg-gray-50 border-b">
            <span class="w-8 h-8 rounded-lg bg-gray-200 flex items-center justify-center">
                <i data-lucide="globe" class="w-4 h-4 text-gray-600"></i>
            </span>
            <span>Alle Gremien</span>
        </a>

        <!-- Meine Gremien -->
        <div class="py-2">
            <p class="px-4 py-1 text-xs font-medium text-gray-500 uppercase">Meine Gremien</p>
            {% for org in user_organizations %}
            <a href="{% url 'session:context_set' org.id %}"
               hx-post="{% url 'session:context_set' org.id %}"
               hx-swap="none"
               hx-on::after-request="window.location.reload()"
               class="flex items-center gap-3 px-4 py-2 hover:bg-gray-50 {% if org == current_organization %}bg-primary-50{% endif %}">
                <span class="w-8 h-8 rounded-lg bg-primary-100 text-primary-600 flex items-center justify-center text-sm font-medium">
                    {{ org.short_name|slice:":2"|upper }}
                </span>
                <span>{{ org.name }}</span>
            </a>
            {% endfor %}
        </div>
    </div>
</div>
```

---

## 7. Workflow-Engine

### 7.1 Workflow-Definition

```python
# workflows/engine.py
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

class WorkflowStatus(str, Enum):
    DRAFT = "draft"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"


@dataclass
class WorkflowStep:
    """Ein Schritt im Workflow"""
    name: str
    display_name: str
    role: str | None = None  # Rolle die diesen Schritt ausführen kann
    user_field: str | None = None  # Feld das den User bestimmt
    required: bool = True
    auto_complete: bool = False  # Automatisch abschließen wenn Bedingung erfüllt
    condition: Callable | None = None  # Bedingung für Schritt
    on_complete: Callable | None = None  # Callback nach Abschluss
    timeout_days: int | None = None  # Frist in Tagen


@dataclass
class WorkflowDefinition:
    """Definition eines Workflows"""
    name: str
    display_name: str
    entity_type: str  # paper, protocol, meeting
    steps: list[WorkflowStep] = field(default_factory=list)

    def get_step(self, name: str) -> WorkflowStep | None:
        return next((s for s in self.steps if s.name == name), None)


# Vordefinierte Workflows
WORKFLOWS = {
    "paper_simple": WorkflowDefinition(
        name="paper_simple",
        display_name="Einfache Vorlagenfreigabe",
        entity_type="paper",
        steps=[
            WorkflowStep(
                name="draft",
                display_name="Entwurf",
                role="paper_author",
            ),
            WorkflowStep(
                name="review",
                display_name="Prüfung",
                role="paper_approver",
                timeout_days=7,
            ),
            WorkflowStep(
                name="approved",
                display_name="Freigegeben",
                auto_complete=True,
            ),
        ]
    ),

    "paper_full": WorkflowDefinition(
        name="paper_full",
        display_name="Vollständige Vorlagenfreigabe",
        entity_type="paper",
        steps=[
            WorkflowStep(
                name="draft",
                display_name="Entwurf",
                role="paper_author",
            ),
            WorkflowStep(
                name="department_review",
                display_name="Fachliche Prüfung",
                user_field="department_head",
                timeout_days=5,
            ),
            WorkflowStep(
                name="legal_review",
                display_name="Rechtsprüfung",
                role="legal_department",
                condition=lambda p: p.requires_legal_review,
                timeout_days=7,
            ),
            WorkflowStep(
                name="finance_review",
                display_name="Kämmerei",
                role="finance_department",
                condition=lambda p: p.has_financial_impact,
                timeout_days=5,
            ),
            WorkflowStep(
                name="mayor_approval",
                display_name="Bürgermeister",
                user_field="mayor",
                timeout_days=3,
            ),
            WorkflowStep(
                name="approved",
                display_name="Freigegeben",
                auto_complete=True,
            ),
        ]
    ),

    "protocol_approval": WorkflowDefinition(
        name="protocol_approval",
        display_name="Protokollfreigabe",
        entity_type="protocol",
        steps=[
            WorkflowStep(
                name="draft",
                display_name="Entwurf",
                role="protocol_writer",
            ),
            WorkflowStep(
                name="chair_review",
                display_name="Prüfung Vorsitz",
                user_field="meeting.chair",
                timeout_days=3,
            ),
            WorkflowStep(
                name="ready",
                display_name="Bereit zur Genehmigung",
                auto_complete=True,
            ),
        ]
    ),
}
```

### 7.2 Workflow-Instance

```python
# models/workflow.py
class WorkflowInstance(Base):
    """Laufende Workflow-Instanz"""
    __tablename__ = "workflow_instances"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))

    # Workflow-Definition
    workflow_name: Mapped[str] = mapped_column(String(100))

    # Verknüpfte Entität
    entity_type: Mapped[str] = mapped_column(String(50))  # paper, protocol
    entity_id: Mapped[uuid.UUID]

    # Status
    status: Mapped[str] = mapped_column(String(50), default="draft")
    current_step: Mapped[str] = mapped_column(String(100))

    # Timestamps
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None]

    # Steps History
    steps: Mapped[list["WorkflowStepInstance"]] = relationship(
        back_populates="workflow",
        order_by="WorkflowStepInstance.order"
    )


class WorkflowStepInstance(Base):
    """Einzelner Schritt einer Workflow-Instanz"""
    __tablename__ = "workflow_step_instances"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_instances.id"))

    # Step-Info
    step_name: Mapped[str] = mapped_column(String(100))
    order: Mapped[int]

    # Zuständigkeit
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    assigned_role: Mapped[str | None] = mapped_column(String(100))

    # Status
    status: Mapped[str] = mapped_column(String(50), default="pending")
    # pending, in_progress, completed, skipped, rejected

    # Ergebnis
    completed_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    completed_at: Mapped[datetime | None]
    comment: Mapped[str | None] = mapped_column(Text)

    # Frist
    due_date: Mapped[datetime | None]

    # Relationships
    workflow: Mapped["WorkflowInstance"] = relationship(back_populates="steps")
```

### 7.3 Workflow-Service

```python
# workflows/service.py
class WorkflowService:
    """Service für Workflow-Operationen"""

    def __init__(self, tenant: Tenant, user: User):
        self.tenant = tenant
        self.user = user

    async def start_workflow(
        self,
        workflow_name: str,
        entity_type: str,
        entity_id: uuid.UUID,
    ) -> WorkflowInstance:
        """Startet einen neuen Workflow"""

        definition = WORKFLOWS.get(workflow_name)
        if not definition:
            raise ValueError(f"Workflow {workflow_name} nicht gefunden")

        # Erstelle Instanz
        instance = WorkflowInstance(
            tenant_id=self.tenant.id,
            workflow_name=workflow_name,
            entity_type=entity_type,
            entity_id=entity_id,
            current_step=definition.steps[0].name,
        )

        # Erstelle Step-Instanzen
        entity = await self._get_entity(entity_type, entity_id)

        for order, step_def in enumerate(definition.steps):
            # Prüfe Bedingung
            if step_def.condition and not step_def.condition(entity):
                continue

            # Bestimme zuständigen User
            assigned_user_id = None
            if step_def.user_field:
                assigned_user_id = self._resolve_user_field(entity, step_def.user_field)

            step = WorkflowStepInstance(
                step_name=step_def.name,
                order=order,
                assigned_user_id=assigned_user_id,
                assigned_role=step_def.role,
                status="pending" if order > 0 else "in_progress",
                due_date=self._calculate_due_date(step_def.timeout_days),
            )
            instance.steps.append(step)

        await db.add(instance)
        await db.commit()

        # Benachrichtigung senden
        await self._notify_step_assigned(instance.steps[0])

        return instance

    async def complete_step(
        self,
        instance: WorkflowInstance,
        comment: str | None = None,
        action: str = "approve",  # approve, reject
    ) -> WorkflowInstance:
        """Schließt aktuellen Schritt ab"""

        current_step = self._get_current_step(instance)

        # Berechtigung prüfen
        if not self._can_complete_step(current_step):
            raise PermissionError("Keine Berechtigung für diesen Schritt")

        # Step abschließen
        current_step.status = "completed" if action == "approve" else "rejected"
        current_step.completed_by_id = self.user.id
        current_step.completed_at = datetime.utcnow()
        current_step.comment = comment

        if action == "reject":
            instance.status = "rejected"
            await self._on_workflow_rejected(instance, current_step)
        else:
            # Nächsten Schritt aktivieren
            next_step = self._get_next_step(instance)

            if next_step:
                next_step.status = "in_progress"
                instance.current_step = next_step.step_name
                await self._notify_step_assigned(next_step)
            else:
                # Workflow abgeschlossen
                instance.status = "completed"
                instance.completed_at = datetime.utcnow()
                await self._on_workflow_completed(instance)

        await db.commit()
        return instance

    async def _notify_step_assigned(self, step: WorkflowStepInstance):
        """Benachrichtigt über neuen Workflow-Schritt"""

        recipients = []

        if step.assigned_user_id:
            recipients.append(step.assigned_user_id)
        elif step.assigned_role:
            # Alle User mit dieser Rolle im Tenant
            recipients = await self._get_users_with_role(step.assigned_role)

        for user_id in recipients:
            await notification_service.send(
                user_id=user_id,
                type="workflow_step_assigned",
                title="Neue Aufgabe",
                message=f"Sie haben eine neue Aufgabe im Workflow.",
                link=self._get_entity_url(step.workflow),
            )
```

### 7.4 Workflow-UI

```html
<!-- Vereinfachte Workflow-Ansicht für Sachbearbeiter -->
<div class="workflow-progress bg-white rounded-lg shadow p-6">
    <h3 class="font-semibold mb-4">Bearbeitungsstand</h3>

    <!-- Fortschrittsbalken -->
    <div class="relative mb-6">
        <div class="h-2 bg-gray-200 rounded-full">
            <div class="h-2 bg-primary-500 rounded-full transition-all"
                 style="width: {{ workflow.progress_percent }}%"></div>
        </div>

        <!-- Step-Marker -->
        <div class="flex justify-between mt-2">
            {% for step in workflow.steps %}
            <div class="flex flex-col items-center">
                <div class="w-8 h-8 rounded-full flex items-center justify-center text-sm
                            {% if step.status == 'completed' %}bg-green-500 text-white
                            {% elif step.status == 'in_progress' %}bg-primary-500 text-white
                            {% elif step.status == 'rejected' %}bg-red-500 text-white
                            {% else %}bg-gray-200 text-gray-500{% endif %}">
                    {% if step.status == 'completed' %}
                        <i data-lucide="check" class="w-4 h-4"></i>
                    {% elif step.status == 'rejected' %}
                        <i data-lucide="x" class="w-4 h-4"></i>
                    {% else %}
                        {{ forloop.counter }}
                    {% endif %}
                </div>
                <span class="text-xs mt-1 text-center max-w-[80px]">
                    {{ step.display_name }}
                </span>
            </div>
            {% endfor %}
        </div>
    </div>

    <!-- Aktueller Schritt Details -->
    {% with current=workflow.current_step_instance %}
    {% if current and current.status == 'in_progress' %}
    <div class="bg-blue-50 rounded-lg p-4">
        <div class="flex items-center gap-3 mb-2">
            <i data-lucide="clock" class="w-5 h-5 text-blue-500"></i>
            <span class="font-medium">Warten auf: {{ current.display_name }}</span>
        </div>

        {% if current.assigned_user %}
        <p class="text-sm text-gray-600 mb-2">
            Zuständig: {{ current.assigned_user.display_name }}
        </p>
        {% endif %}

        {% if current.due_date %}
        <p class="text-sm {% if current.is_overdue %}text-red-600{% else %}text-gray-500{% endif %}">
            Frist: {{ current.due_date|date:"d.m.Y" }}
            {% if current.is_overdue %}(überfällig){% endif %}
        </p>
        {% endif %}

        <!-- Aktion wenn User zuständig -->
        {% if current.can_complete %}
        <div class="flex gap-2 mt-4">
            <button hx-post="{% url 'session:workflow_approve' workflow.id %}"
                    hx-target="#workflow-{{ workflow.id }}"
                    class="btn btn-success btn-sm">
                Freigeben
            </button>
            <button hx-get="{% url 'session:workflow_reject_modal' workflow.id %}"
                    hx-target="#modal-container"
                    class="btn btn-outline btn-error btn-sm">
                Ablehnen
            </button>
        </div>
        {% endif %}
    </div>
    {% endif %}
    {% endwith %}
</div>
```

---

## 8. OParl 1.1 Vollintegration

### 8.1 OParl-Schema vollständig nutzen

```python
# oparl/schema.py
"""
OParl 1.1 Vollständige Implementierung

Wir nutzen ALLE Felder des OParl-Schemas, nicht nur die Basis-Felder.
"""

from pydantic import BaseModel, Field, HttpUrl
from datetime import datetime, date
from typing import Optional

class OParlSystem(BaseModel):
    """OParl System-Objekt (Einstiegspunkt)"""
    id: HttpUrl
    type: str = "https://schema.oparl.org/1.1/System"
    oparlVersion: str = "https://schema.oparl.org/1.1/"
    name: str
    contactEmail: Optional[str] = None
    contactName: Optional[str] = None
    website: Optional[HttpUrl] = None
    vendor: Optional[HttpUrl] = None
    product: Optional[HttpUrl] = None
    body: HttpUrl  # Link zur Body-Liste
    otherOparlVersions: Optional[list[HttpUrl]] = None
    created: Optional[datetime] = None
    modified: Optional[datetime] = None


class OParlBody(BaseModel):
    """OParl Body (Kommune) - ALLE Felder"""
    id: HttpUrl
    type: str = "https://schema.oparl.org/1.1/Body"
    name: str
    shortName: Optional[str] = None

    # Kontakt
    contactEmail: Optional[str] = None
    contactName: Optional[str] = None

    # Links
    website: Optional[HttpUrl] = None
    license: Optional[HttpUrl] = None
    licenseValidSince: Optional[date] = None

    # Klassifikation
    classification: Optional[str] = None  # z.B. "Stadt", "Gemeinde", "Kreis"
    equivalent: Optional[list[HttpUrl]] = None  # Gleiche Entität anderswo

    # Geo
    location: Optional["OParlLocation"] = None
    ags: Optional[str] = None  # Amtlicher Gemeindeschlüssel
    rgs: Optional[str] = None  # Regionalschlüssel

    # Listen (externe Links)
    organization: HttpUrl  # /bodies/{id}/organizations
    person: HttpUrl        # /bodies/{id}/persons
    meeting: HttpUrl       # /bodies/{id}/meetings
    paper: HttpUrl         # /bodies/{id}/papers

    # Wahlperioden (eingebettet)
    legislativeTerm: list["OParlLegislativeTerm"] = []

    # Timestamps
    created: Optional[datetime] = None
    modified: Optional[datetime] = None


class OParlOrganization(BaseModel):
    """OParl Organization (Gremium/Fraktion) - ALLE Felder"""
    id: HttpUrl
    type: str = "https://schema.oparl.org/1.1/Organization"
    body: HttpUrl
    name: str
    shortName: Optional[str] = None

    # Typ
    organizationType: Optional[str] = None  # "Rat", "Ausschuss", "Fraktion"
    classification: Optional[str] = None

    # Zeitraum
    startDate: Optional[date] = None
    endDate: Optional[date] = None

    # Hierarchie
    subOrganizationOf: Optional[HttpUrl] = None

    # Kontakt
    website: Optional[HttpUrl] = None
    location: Optional["OParlLocation"] = None
    post: Optional[list[str]] = None  # Postanschrift

    # Verknüpfungen
    meeting: Optional[list[HttpUrl]] = None  # Sitzungen dieses Gremiums
    membership: list["OParlMembership"] = []  # Eingebettet

    # Timestamps
    created: Optional[datetime] = None
    modified: Optional[datetime] = None


class OParlPerson(BaseModel):
    """OParl Person - ALLE Felder"""
    id: HttpUrl
    type: str = "https://schema.oparl.org/1.1/Person"
    body: HttpUrl
    name: str  # Vollständiger Name

    # Namensbestandteile
    familyName: Optional[str] = None
    givenName: Optional[str] = None
    formOfAddress: Optional[str] = None  # "Herr", "Frau"
    affix: Optional[str] = None  # "von", "zu"
    title: Optional[list[str]] = None  # ["Dr.", "Prof."]

    # Demografie
    gender: Optional[str] = None  # "male", "female", "other"
    status: Optional[list[str]] = None  # ["Mitglied", "Vorsitzender"]

    # Kontakt
    email: Optional[list[str]] = None
    phone: Optional[list[str]] = None
    location: Optional["OParlLocation"] = None

    # Links
    website: Optional[list[HttpUrl]] = None

    # Mitgliedschaften (eingebettet)
    membership: list["OParlMembership"] = []

    # Leben
    life: Optional[str] = None  # Kurzvita
    lifeSource: Optional[str] = None

    # Timestamps
    created: Optional[datetime] = None
    modified: Optional[datetime] = None


class OParlMembership(BaseModel):
    """OParl Membership - ALLE Felder"""
    id: HttpUrl
    type: str = "https://schema.oparl.org/1.1/Membership"
    person: HttpUrl
    organization: HttpUrl
    role: Optional[str] = None  # "Vorsitzender", "Mitglied"
    votingRight: Optional[bool] = None  # Stimmrecht
    startDate: Optional[date] = None
    endDate: Optional[date] = None
    onBehalfOf: Optional[HttpUrl] = None  # Im Auftrag von (Fraktion)
    created: Optional[datetime] = None
    modified: Optional[datetime] = None


class OParlMeeting(BaseModel):
    """OParl Meeting (Sitzung) - ALLE Felder"""
    id: HttpUrl
    type: str = "https://schema.oparl.org/1.1/Meeting"
    name: Optional[str] = None
    meetingState: Optional[str] = None  # "eingeladen", "durchgeführt"

    # Wichtig: cancelled Flag
    cancelled: Optional[bool] = None

    # Zeit
    start: Optional[datetime] = None
    end: Optional[datetime] = None

    # Ort
    location: Optional["OParlLocation"] = None
    room: Optional[str] = None  # Raumbezeichnung
    streetAddress: Optional[str] = None
    postalCode: Optional[str] = None
    locality: Optional[str] = None

    # Verknüpfungen
    organization: Optional[list[HttpUrl]] = None  # Gremien
    participant: Optional[list[HttpUrl]] = None  # Teilnehmer (Personen)

    # Tagesordnung (eingebettet oder extern)
    agendaItem: list["OParlAgendaItem"] = []

    # Protokoll und Einladung
    invitation: Optional["OParlFile"] = None
    resultsProtocol: Optional["OParlFile"] = None
    verbatimProtocol: Optional["OParlFile"] = None
    auxiliaryFile: Optional[list["OParlFile"]] = None

    # Timestamps
    created: Optional[datetime] = None
    modified: Optional[datetime] = None


class OParlAgendaItem(BaseModel):
    """OParl AgendaItem (TOP) - ALLE Felder"""
    id: HttpUrl
    type: str = "https://schema.oparl.org/1.1/AgendaItem"
    meeting: HttpUrl
    number: Optional[str] = None  # "1", "2a", "TOP 3"
    order: Optional[int] = None  # Sortierreihenfolge
    name: Optional[str] = None  # Titel des TOP

    # WICHTIG: Öffentlichkeit
    public: bool = True  # false = nichtöffentlich

    # Beratung
    consultation: Optional[list["OParlConsultation"]] = None

    # Ergebnis
    result: Optional[str] = None  # "angenommen", "abgelehnt"
    resolutionText: Optional[str] = None  # Beschlusstext
    resolutionFile: Optional["OParlFile"] = None  # Beschlussdokument

    # Dateien
    auxiliaryFile: Optional[list["OParlFile"]] = None

    # Timestamps
    start: Optional[datetime] = None  # Beginn der Beratung
    end: Optional[datetime] = None  # Ende der Beratung
    created: Optional[datetime] = None
    modified: Optional[datetime] = None


class OParlPaper(BaseModel):
    """OParl Paper (Vorlage) - ALLE Felder"""
    id: HttpUrl
    type: str = "https://schema.oparl.org/1.1/Paper"
    body: HttpUrl
    name: Optional[str] = None
    reference: Optional[str] = None  # Aktenzeichen
    date: Optional[date] = None  # Datum der Vorlage
    paperType: Optional[str] = None  # "Beschlussvorlage", "Antrag"

    # Verknüpfungen
    relatedPaper: Optional[list[HttpUrl]] = None
    superordinatedPaper: Optional[list[HttpUrl]] = None
    subordinatedPaper: Optional[list[HttpUrl]] = None

    # Beratungen
    consultation: list["OParlConsultation"] = []

    # Urheber
    originatorPerson: Optional[list[HttpUrl]] = None
    underDirectionOf: Optional[list[HttpUrl]] = None
    originatorOrganization: Optional[list[HttpUrl]] = None

    # Dateien
    mainFile: Optional["OParlFile"] = None
    auxiliaryFile: Optional[list["OParlFile"]] = None

    # Ort
    location: Optional[list["OParlLocation"]] = None

    # Timestamps
    created: Optional[datetime] = None
    modified: Optional[datetime] = None


class OParlConsultation(BaseModel):
    """OParl Consultation (Beratung) - ALLE Felder"""
    id: HttpUrl
    type: str = "https://schema.oparl.org/1.1/Consultation"
    paper: Optional[HttpUrl] = None
    agendaItem: Optional[HttpUrl] = None
    meeting: Optional[HttpUrl] = None
    organization: Optional[list[HttpUrl]] = None
    authoritative: Optional[bool] = None  # Federführend?
    role: Optional[str] = None  # "Vorberatung", "Beschluss"
    created: Optional[datetime] = None
    modified: Optional[datetime] = None


class OParlFile(BaseModel):
    """OParl File (Dokument) - ALLE Felder"""
    id: HttpUrl
    type: str = "https://schema.oparl.org/1.1/File"
    name: Optional[str] = None
    fileName: Optional[str] = None
    mimeType: Optional[str] = None
    date: Optional[date] = None
    size: Optional[int] = None  # Bytes
    sha1Checksum: Optional[str] = None
    sha512Checksum: Optional[str] = None
    text: Optional[str] = None  # Volltext (OCR)
    accessUrl: HttpUrl  # URL zum Abrufen
    downloadUrl: Optional[HttpUrl] = None
    externalServiceUrl: Optional[HttpUrl] = None
    masterFile: Optional[HttpUrl] = None
    derivativeFile: Optional[list[HttpUrl]] = None
    fileLicense: Optional[HttpUrl] = None
    created: Optional[datetime] = None
    modified: Optional[datetime] = None


class OParlLocation(BaseModel):
    """OParl Location - ALLE Felder"""
    id: Optional[HttpUrl] = None
    type: str = "https://schema.oparl.org/1.1/Location"
    description: Optional[str] = None
    geojson: Optional[dict] = None  # GeoJSON Geometry
    streetAddress: Optional[str] = None
    room: Optional[str] = None
    postalCode: Optional[str] = None
    subLocality: Optional[str] = None
    locality: Optional[str] = None
    bodies: Optional[list[HttpUrl]] = None
    organizations: Optional[list[HttpUrl]] = None
    meetings: Optional[list[HttpUrl]] = None
    papers: Optional[list[HttpUrl]] = None
    created: Optional[datetime] = None
    modified: Optional[datetime] = None


class OParlLegislativeTerm(BaseModel):
    """OParl LegislativeTerm (Wahlperiode) - ALLE Felder"""
    id: HttpUrl
    type: str = "https://schema.oparl.org/1.1/LegislativeTerm"
    body: Optional[HttpUrl] = None
    name: Optional[str] = None
    startDate: Optional[date] = None
    endDate: Optional[date] = None
    created: Optional[datetime] = None
    modified: Optional[datetime] = None
```

### 8.2 OParl-API Implementation

```python
# oparl/router.py
from fastapi import APIRouter, Request, Query
from typing import Optional

router = APIRouter(prefix="/oparl/v1", tags=["oparl"])


@router.get("")
async def get_system(request: Request) -> dict:
    """OParl System-Objekt (Einstiegspunkt)"""
    base_url = str(request.base_url).rstrip("/")

    return {
        "id": f"{base_url}/oparl/v1",
        "type": "https://schema.oparl.org/1.1/System",
        "oparlVersion": "https://schema.oparl.org/1.1/",
        "name": "Mandari Session",
        "contactEmail": "kontakt@mandari.de",
        "website": "https://mandari.de",
        "vendor": "https://mandari.de",
        "product": "https://mandari.de/session",
        "body": f"{base_url}/oparl/v1/bodies",
        "created": "2026-01-01T00:00:00+01:00",
        "modified": datetime.utcnow().isoformat(),
    }


@router.get("/bodies")
async def list_bodies(
    request: Request,
    limit: int = Query(100, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    """Liste aller Bodies (Kommunen)"""

    base_url = str(request.base_url).rstrip("/")

    bodies = await db.execute(
        select(Tenant)
        .where(Tenant.is_active == True)
        .order_by(Tenant.name)
        .limit(limit)
        .offset(offset)
    )

    return {
        "data": [
            await _body_to_oparl(body, base_url)
            for body in bodies.scalars()
        ],
        "pagination": {
            "totalElements": await _count_bodies(),
            "elementsPerPage": limit,
            "currentPage": offset // limit + 1,
        },
        "links": {
            "first": f"{base_url}/oparl/v1/bodies?limit={limit}&offset=0",
            "next": f"{base_url}/oparl/v1/bodies?limit={limit}&offset={offset + limit}",
        }
    }


@router.get("/bodies/{body_id}")
async def get_body(body_id: uuid.UUID, request: Request) -> dict:
    """Einzelne Body abrufen"""

    tenant = await db.get(Tenant, body_id)
    if not tenant:
        raise HTTPException(404, "Body nicht gefunden")

    return await _body_to_oparl(tenant, str(request.base_url).rstrip("/"))


async def _body_to_oparl(tenant: Tenant, base_url: str) -> dict:
    """Konvertiert Tenant zu OParl Body mit ALLEN Feldern"""

    body_url = f"{base_url}/oparl/v1/bodies/{tenant.id}"

    return {
        "id": body_url,
        "type": "https://schema.oparl.org/1.1/Body",
        "name": tenant.name,
        "shortName": tenant.short_name,
        "contactEmail": tenant.settings.get("contact_email"),
        "website": tenant.settings.get("website"),
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "classification": tenant.settings.get("classification", "Stadt"),
        "ags": tenant.settings.get("ags"),
        "rgs": tenant.settings.get("rgs"),

        # Listen-URLs
        "organization": f"{body_url}/organizations",
        "person": f"{body_url}/persons",
        "meeting": f"{body_url}/meetings",
        "paper": f"{body_url}/papers",

        # Wahlperioden eingebettet
        "legislativeTerm": await _get_legislative_terms(tenant.id, base_url),

        # Location wenn vorhanden
        "location": await _get_body_location(tenant.id, base_url),

        "created": tenant.created_at.isoformat() if tenant.created_at else None,
        "modified": tenant.updated_at.isoformat() if tenant.updated_at else None,
    }


@router.get("/bodies/{body_id}/meetings")
async def list_meetings(
    body_id: uuid.UUID,
    request: Request,
    limit: int = Query(100, le=1000),
    offset: int = Query(0, ge=0),
    modified_since: Optional[datetime] = None,
    created_since: Optional[datetime] = None,
) -> dict:
    """
    Liste aller Meetings eines Bodies.

    Unterstützt inkrementellen Sync via modified_since/created_since.
    """

    base_url = str(request.base_url).rstrip("/")

    query = (
        select(OParlMeeting)
        .where(OParlMeeting.tenant_id == body_id)
        .order_by(OParlMeeting.modified_at.desc())
    )

    # Inkrementeller Sync
    if modified_since:
        query = query.where(OParlMeeting.modified_at >= modified_since)
    if created_since:
        query = query.where(OParlMeeting.created_at >= created_since)

    meetings = await db.execute(query.limit(limit).offset(offset))

    return {
        "data": [
            await _meeting_to_oparl(m, base_url)
            for m in meetings.scalars()
        ],
        "pagination": {...},
        "links": {...}
    }


async def _meeting_to_oparl(meeting: OParlMeeting, base_url: str) -> dict:
    """Konvertiert Meeting zu OParl mit ALLEN Feldern"""

    meeting_url = f"{base_url}/oparl/v1/meetings/{meeting.id}"

    # AgendaItems laden
    agenda_items = await db.execute(
        select(OParlAgendaItem)
        .where(OParlAgendaItem.meeting_id == meeting.id)
        .order_by(OParlAgendaItem.order)
    )

    return {
        "id": meeting_url,
        "type": "https://schema.oparl.org/1.1/Meeting",
        "name": meeting.name,
        "meetingState": meeting.status,
        "cancelled": meeting.cancelled,

        "start": meeting.start.isoformat() if meeting.start else None,
        "end": meeting.end.isoformat() if meeting.end else None,

        "location": await _location_to_oparl(meeting.location) if meeting.location else None,
        "room": meeting.room,
        "streetAddress": meeting.street_address,
        "postalCode": meeting.postal_code,
        "locality": meeting.locality,

        "organization": [
            f"{base_url}/oparl/v1/organizations/{org_id}"
            for org_id in meeting.organization_ids or []
        ],

        "participant": [
            f"{base_url}/oparl/v1/persons/{p_id}"
            for p_id in await _get_participants(meeting.id)
        ],

        # TOPs eingebettet - NUR öffentliche für anonyme Requests
        "agendaItem": [
            await _agenda_item_to_oparl(item, base_url)
            for item in agenda_items.scalars()
            if item.public  # WICHTIG: Nur öffentliche!
        ],

        # Dateien
        "invitation": await _file_to_oparl(meeting.invitation_file) if meeting.invitation_file else None,
        "resultsProtocol": await _file_to_oparl(meeting.protocol_file) if meeting.protocol_file else None,

        "created": meeting.created_at.isoformat(),
        "modified": meeting.modified_at.isoformat(),
    }
```

### 8.3 OParl für nichtöffentliche Inhalte (Session-API)

```python
# session/router.py
"""
Session-API erweitert OParl um nichtöffentliche Inhalte.

Authentifizierte Requests erhalten auch nichtöffentliche TOPs und Dokumente.
"""

@router.get("/meetings/{meeting_id}/full")
async def get_meeting_full(
    meeting_id: uuid.UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict:
    """
    Meeting mit ALLEN Inhalten (inkl. nichtöffentlich).

    Erfordert Authentifizierung und entsprechende Berechtigung.
    """

    meeting = await db.get(OParlMeeting, meeting_id)
    checker = PermissionChecker(current_user, request.tenant)

    # Basis OParl-Daten
    result = await _meeting_to_oparl(meeting, str(request.base_url))

    # Nichtöffentliche TOPs hinzufügen wenn berechtigt
    if checker.has_permission(Permission.NON_PUBLIC_VIEW):
        non_public_items = await db.execute(
            select(OParlAgendaItem)
            .where(
                OParlAgendaItem.meeting_id == meeting_id,
                OParlAgendaItem.public == False
            )
        )

        result["agendaItem"].extend([
            await _agenda_item_to_oparl_full(item, str(request.base_url))
            for item in non_public_items.scalars()
        ])

        # Sortieren nach order
        result["agendaItem"].sort(key=lambda x: x.get("order", 0))

    # Session-spezifische Erweiterungen
    result["_session"] = {
        "attendance": await _get_attendance(meeting_id),
        "votes": await _get_votes(meeting_id),
        "workflow": await _get_workflow_status(meeting),
    }

    return result
```

---

## 9. 3-Säulen-Kommunikation

### 9.1 Architektur-Übersicht

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         3-SÄULEN-KOMMUNIKATION                               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   INSIGHT                    WORK                      SESSION               │
│   (Bürger)                (Fraktionen)              (Verwaltung)            │
│   ┌─────────┐            ┌─────────┐              ┌─────────┐              │
│   │ Django  │            │ Django  │              │ Django  │              │
│   │ HTMX    │            │ HTMX    │              │ HTMX    │              │
│   └────┬────┘            └────┬────┘              └────┬────┘              │
│        │                      │                        │                    │
│        │ OParl                │ Session-API            │ Intern             │
│        │ (nur public)        │ (public + non-public)  │                    │
│        │                      │                        │                    │
│        └──────────────────────┼────────────────────────┘                    │
│                               │                                              │
│                               ▼                                              │
│                    ┌─────────────────────┐                                  │
│                    │   MANDARI CORE API  │                                  │
│                    │                     │                                  │
│                    │  ┌───────────────┐  │                                  │
│                    │  │ OParl Router  │  │ ← Öffentliche Daten             │
│                    │  └───────────────┘  │                                  │
│                    │  ┌───────────────┐  │                                  │
│                    │  │Session Router │  │ ← + Nichtöffentlich (Auth)      │
│                    │  └───────────────┘  │                                  │
│                    │  ┌───────────────┐  │                                  │
│                    │  │ Work Router   │  │ ← Fraktionsfunktionen           │
│                    │  └───────────────┘  │                                  │
│                    │  ┌───────────────┐  │                                  │
│                    │  │ Search Router │  │ ← Volltextsuche                 │
│                    │  └───────────────┘  │                                  │
│                    │  ┌───────────────┐  │                                  │
│                    │  │   AI Router   │  │ ← KI-Funktionen                 │
│                    │  └───────────────┘  │                                  │
│                    │                     │                                  │
│                    └──────────┬──────────┘                                  │
│                               │                                              │
│                    ┌──────────┴──────────┐                                  │
│                    ▼                     ▼                                  │
│             ┌─────────────┐       ┌─────────────┐                          │
│             │ PostgreSQL  │       │ Meilisearch │                          │
│             │ (RLS)       │       │ (Suche)     │                          │
│             └─────────────┘       └─────────────┘                          │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 9.2 Kommunikationsmatrix

| Von → Nach | Insight | Work | Session |
|------------|---------|------|---------|
| **Insight** | - | Link zu Work | - |
| **Work** | OParl lesen | - | Session-API |
| **Session** | OParl Export | Session-API | - |

### 9.3 Datenfluss-Szenarien

#### Szenario 1: Bürger sucht Beschluss

```
Bürger → Insight
         │
         │ Suche nach "Haushalt 2026"
         ▼
       ┌─────────────────┐
       │ Meilisearch API │
       │ (nur public)    │
       └────────┬────────┘
                │
                ▼
       ┌─────────────────┐
       │ Ergebnis:       │
       │ - Paper: ...    │
       │ - Meeting: ...  │
       │ - AgendaItem:..│
       └────────┬────────┘
                │
                │ Klick auf Ergebnis
                ▼
       ┌─────────────────┐
       │ OParl API       │
       │ GET /papers/123 │
       └────────┬────────┘
                │
                ▼
       ┌─────────────────┐
       │ Insight zeigt:  │
       │ - Details       │
       │ - KI-Summary    │
       │ - Dokumente     │
       └─────────────────┘
```

#### Szenario 2: Fraktion bereitet Sitzung vor

```
Fraktionsmitglied → Work
                    │
                    │ Authentifizierung
                    ▼
               ┌─────────────────┐
               │ Session-API     │
               │ (mit Auth-Token)│
               └────────┬────────┘
                        │
                        │ GET /session/meetings/456/full
                        ▼
               ┌─────────────────┐
               │ Antwort enthält:│
               │ - Öffentl. TOPs │
               │ - Nichtöff. TOPs│ ← Nur für Mandatsträger
               │ - Vorlagen      │
               │ - Dokumente     │
               └────────┬────────┘
                        │
                        │ Fraktion erstellt Antrag
                        ▼
               ┌─────────────────┐
               │ Work-API        │
               │ POST /motions   │
               └────────┬────────┘
                        │
                        │ Antrag wird in Session
                        │ als Paper angelegt
                        ▼
               ┌─────────────────┐
               │ Session erhält  │
               │ neuen Antrag    │
               │ im Workflow     │
               └─────────────────┘
```

#### Szenario 3: Verwaltung publiziert Beschluss

```
Sachbearbeiter → Session
                 │
                 │ Protokoll genehmigt
                 ▼
            ┌─────────────────┐
            │ Workflow:       │
            │ Status →        │
            │ VERÖFFENTLICHT  │
            └────────┬────────┘
                     │
                     │ Trigger: on_publish
                     ▼
       ┌─────────────┴─────────────┐
       │                           │
       ▼                           ▼
┌─────────────────┐      ┌─────────────────┐
│ Meilisearch     │      │ OParl-Cache     │
│ Index Update    │      │ Invalidierung   │
└─────────────────┘      └─────────────────┘
       │                           │
       │                           │
       ▼                           ▼
┌─────────────────┐      ┌─────────────────┐
│ Insight zeigt   │      │ Externe Systeme │
│ neue Beschlüsse │      │ via OParl       │
└─────────────────┘      └─────────────────┘
```

### 9.4 API-Client für Säulen

```python
# shared/api_client.py
class MandariAPIClient:
    """
    Einheitlicher API-Client für die 3-Säulen-Kommunikation.

    Wird in Insight, Work und Session verwendet.
    """

    def __init__(
        self,
        base_url: str,
        api_type: Literal["oparl", "session", "work"] = "oparl",
        auth_token: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_type = api_type
        self.auth_token = auth_token
        self._session = httpx.AsyncClient()

    @property
    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    # ─────────────────────────────────────────────────────
    # OParl-Methoden (für Insight und externe Konsumenten)
    # ─────────────────────────────────────────────────────

    async def get_system(self) -> OParlSystem:
        """OParl System abrufen"""
        resp = await self._get("/oparl/v1")
        return OParlSystem(**resp)

    async def list_bodies(self, limit: int = 100) -> list[OParlBody]:
        """Alle Bodies (Kommunen) abrufen"""
        resp = await self._get(f"/oparl/v1/bodies?limit={limit}")
        return [OParlBody(**b) for b in resp["data"]]

    async def get_body(self, body_id: str) -> OParlBody:
        """Einzelne Body abrufen"""
        resp = await self._get(f"/oparl/v1/bodies/{body_id}")
        return OParlBody(**resp)

    async def list_meetings(
        self,
        body_id: str,
        modified_since: datetime | None = None,
    ) -> list[OParlMeeting]:
        """Meetings einer Body abrufen (mit optionalem Sync)"""
        url = f"/oparl/v1/bodies/{body_id}/meetings"
        if modified_since:
            url += f"?modified_since={modified_since.isoformat()}"
        resp = await self._get(url)
        return [OParlMeeting(**m) for m in resp["data"]]

    # ─────────────────────────────────────────────────────
    # Session-API-Methoden (für Work und interne Nutzung)
    # ─────────────────────────────────────────────────────

    async def get_meeting_full(self, meeting_id: str) -> dict:
        """Meeting mit allen Inhalten (inkl. nichtöffentlich)"""
        if not self.auth_token:
            raise AuthError("Authentifizierung erforderlich")
        return await self._get(f"/api/v1/session/meetings/{meeting_id}/full")

    async def get_non_public_agenda_items(self, meeting_id: str) -> list[dict]:
        """Nur nichtöffentliche TOPs"""
        if not self.auth_token:
            raise AuthError("Authentifizierung erforderlich")
        return await self._get(f"/api/v1/session/meetings/{meeting_id}/non-public")

    async def submit_motion(self, org_id: str, motion: dict) -> dict:
        """Antrag von Work an Session übermitteln"""
        if not self.auth_token:
            raise AuthError("Authentifizierung erforderlich")
        return await self._post(f"/api/v1/work/org/{org_id}/motions", motion)

    # ─────────────────────────────────────────────────────
    # Interne Methoden
    # ─────────────────────────────────────────────────────

    async def _get(self, path: str) -> dict:
        resp = await self._session.get(
            f"{self.base_url}{path}",
            headers=self._headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, data: dict) -> dict:
        resp = await self._session.post(
            f"{self.base_url}{path}",
            headers=self._headers,
            json=data,
        )
        resp.raise_for_status()
        return resp.json()
```

### 9.5 Work-Session-Verbindung konfigurieren

```python
# work/services/session_connector.py
class SessionConnector:
    """
    Verbindet Work mit Session oder externem RIS.

    Unterstützt zwei Modi:
    1. Mandari Session (volle Funktionalität)
    2. Externes RIS via OParl (nur öffentliche Daten)
    """

    def __init__(self, organization: Organization):
        self.organization = organization
        self.config = organization.session_config

    @property
    def connection_type(self) -> Literal["session", "oparl"]:
        return self.config.get("type", "oparl")

    async def get_client(self) -> MandariAPIClient:
        """Erstellt passenden API-Client"""

        if self.connection_type == "session":
            # Volle Session-Integration
            return MandariAPIClient(
                base_url=self.config["session_url"],
                api_type="session",
                auth_token=await self._get_org_token(),
            )
        else:
            # Nur OParl (externes RIS)
            return MandariAPIClient(
                base_url=self.config["oparl_url"],
                api_type="oparl",
            )

    async def get_meetings(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[Meeting]:
        """Holt Sitzungen - mit vollen Details bei Session-Verbindung"""

        client = await self.get_client()

        if self.connection_type == "session":
            # Session-API: Alle Details inkl. nichtöffentlich
            meetings_data = await client._get(
                f"/api/v1/session/meetings?"
                f"org_id={self.organization.oparl_organization_id}"
                f"&start_date={start_date}&end_date={end_date}"
            )
            return [
                self._session_meeting_to_model(m)
                for m in meetings_data
            ]
        else:
            # OParl: Nur öffentliche Daten
            meetings = await client.list_meetings(
                self.config["body_id"]
            )
            return [
                self._oparl_meeting_to_model(m)
                for m in meetings
                if self._is_relevant_meeting(m)
            ]

    def _session_meeting_to_model(self, data: dict) -> Meeting:
        """Konvertiert Session-Meeting mit allen Details"""
        return Meeting(
            id=data["id"],
            name=data["name"],
            start=datetime.fromisoformat(data["start"]),
            agenda_items=[
                AgendaItem(
                    id=item["id"],
                    name=item["name"],
                    public=item["public"],
                    papers=item.get("papers", []),
                    # Nichtöffentliche Inhalte verfügbar!
                    non_public_reason=item.get("non_public_reason"),
                )
                for item in data["agendaItem"]
            ],
            # Session-spezifische Daten
            attendance=data.get("_session", {}).get("attendance"),
            votes=data.get("_session", {}).get("votes"),
        )
```

---

## 10. Schnittstellen-Framework

### 10.1 Standard-Schnittstellen

```python
# integrations/registry.py
from abc import ABC, abstractmethod

class IntegrationBase(ABC):
    """Basisklasse für alle Integrationen"""

    name: str
    display_name: str
    category: str  # "finance", "document", "calendar", "custom"

    @abstractmethod
    async def configure(self, config: dict) -> bool:
        """Konfiguriert die Integration"""
        pass

    @abstractmethod
    async def test_connection(self) -> bool:
        """Testet die Verbindung"""
        pass

    @abstractmethod
    async def execute(self, action: str, data: dict) -> dict:
        """Führt eine Aktion aus"""
        pass


# Registry für alle verfügbaren Integrationen
INTEGRATIONS: dict[str, type[IntegrationBase]] = {}

def register_integration(cls: type[IntegrationBase]):
    """Decorator zum Registrieren von Integrationen"""
    INTEGRATIONS[cls.name] = cls
    return cls
```

### 10.2 HKR-Integration (Sitzungsgelder)

```python
# integrations/hkr.py
@register_integration
class HKRIntegration(IntegrationBase):
    """Integration für Haushalts-, Kassen-, Rechnungswesen"""

    name = "hkr"
    display_name = "HKR-Schnittstelle"
    category = "finance"

    # Unterstützte Formate
    FORMATS = {
        "xml_standard": "XML Standard",
        "csv_simple": "CSV Einfach",
        "datev": "DATEV",
        "sap_idoc": "SAP IDoc",
    }

    async def configure(self, config: dict) -> bool:
        self.export_format = config.get("format", "xml_standard")
        self.export_path = config.get("export_path")
        self.auto_export = config.get("auto_export", False)
        return True

    async def test_connection(self) -> bool:
        # Prüfe Schreibrechte auf Export-Pfad
        return os.access(self.export_path, os.W_OK)

    async def execute(self, action: str, data: dict) -> dict:
        if action == "export_allowances":
            return await self._export_allowances(data)
        raise ValueError(f"Unbekannte Aktion: {action}")

    async def _export_allowances(self, data: dict) -> dict:
        """Exportiert Sitzungsgelder im konfigurierten Format"""

        period = data.get("period")  # z.B. "2026-01"
        allowances = await self._get_allowances(period)

        if self.export_format == "xml_standard":
            content = self._generate_xml(allowances)
            filename = f"sitzungsgelder_{period}.xml"
        elif self.export_format == "csv_simple":
            content = self._generate_csv(allowances)
            filename = f"sitzungsgelder_{period}.csv"
        elif self.export_format == "sap_idoc":
            content = self._generate_sap_idoc(allowances)
            filename = f"sitzungsgelder_{period}.idoc"

        # Speichern
        filepath = os.path.join(self.export_path, filename)
        with open(filepath, "w") as f:
            f.write(content)

        return {
            "success": True,
            "filepath": filepath,
            "records": len(allowances),
            "total_amount": sum(a.amount for a in allowances),
        }

    def _generate_xml(self, allowances: list) -> str:
        """Generiert XML im HKR-Standard-Format"""
        root = ET.Element("HKR_Export")
        root.set("version", "1.0")
        root.set("datum", datetime.now().isoformat())

        for a in allowances:
            buchung = ET.SubElement(root, "Buchung")
            ET.SubElement(buchung, "PersonalNr").text = a.person.personnel_number
            ET.SubElement(buchung, "Name").text = a.person.name
            ET.SubElement(buchung, "Betrag").text = str(a.amount)
            ET.SubElement(buchung, "Konto").text = a.account_number
            ET.SubElement(buchung, "Kostenstelle").text = a.cost_center
            ET.SubElement(buchung, "Verwendungszweck").text = a.description

        return ET.tostring(root, encoding="unicode", xml_declaration=True)
```

### 10.3 Kalender-Integration

```python
# integrations/calendar.py
@register_integration
class CalendarIntegration(IntegrationBase):
    """Integration für Kalendersysteme"""

    name = "calendar"
    display_name = "Kalender-Synchronisation"
    category = "calendar"

    PROVIDERS = {
        "exchange": "Microsoft Exchange",
        "google": "Google Workspace",
        "caldav": "CalDAV",
        "ical": "iCal Feed",
    }

    async def configure(self, config: dict) -> bool:
        self.provider = config.get("provider", "ical")
        self.credentials = config.get("credentials", {})
        return True

    async def execute(self, action: str, data: dict) -> dict:
        if action == "sync_meetings":
            return await self._sync_meetings(data)
        elif action == "create_event":
            return await self._create_event(data)
        elif action == "get_ical_feed":
            return await self._generate_ical_feed(data)
        raise ValueError(f"Unbekannte Aktion: {action}")

    async def _generate_ical_feed(self, data: dict) -> dict:
        """Generiert iCal-Feed für Sitzungen"""

        org_id = data.get("organization_id")
        meetings = await db.execute(
            select(OParlMeeting)
            .where(OParlMeeting.organization_ids.contains([org_id]))
            .where(OParlMeeting.start >= datetime.now())
            .order_by(OParlMeeting.start)
        )

        cal = Calendar()
        cal.add("prodid", "-//Mandari Session//mandari.de//")
        cal.add("version", "2.0")
        cal.add("x-wr-calname", f"Sitzungen")

        for meeting in meetings.scalars():
            event = Event()
            event.add("uid", f"{meeting.id}@mandari.de")
            event.add("summary", meeting.name)
            event.add("dtstart", meeting.start)
            event.add("dtend", meeting.end or meeting.start + timedelta(hours=2))
            event.add("location", meeting.location_name or "")
            event.add("description", self._meeting_description(meeting))
            cal.add_component(event)

        return {
            "content_type": "text/calendar",
            "content": cal.to_ical().decode("utf-8"),
        }
```

### 10.4 Custom-Schnittstellen-Framework

```python
# integrations/custom.py
@register_integration
class CustomWebhookIntegration(IntegrationBase):
    """Generische Webhook-Integration für Fachanwendungen"""

    name = "custom_webhook"
    display_name = "Custom Webhook"
    category = "custom"

    async def configure(self, config: dict) -> bool:
        self.webhook_url = config["webhook_url"]
        self.auth_type = config.get("auth_type", "none")  # none, basic, bearer, api_key
        self.auth_credentials = config.get("auth_credentials", {})
        self.events = config.get("events", [])  # z.B. ["meeting.created", "paper.approved"]
        self.payload_template = config.get("payload_template")
        return True

    async def test_connection(self) -> bool:
        """Sendet Test-Webhook"""
        try:
            resp = await self._send_webhook({"type": "test", "timestamp": datetime.now().isoformat()})
            return resp.status_code == 200
        except Exception:
            return False

    async def execute(self, action: str, data: dict) -> dict:
        if action == "send_event":
            return await self._send_event(data)
        raise ValueError(f"Unbekannte Aktion: {action}")

    async def _send_event(self, data: dict) -> dict:
        """Sendet Event an Webhook"""

        event_type = data["event_type"]
        if event_type not in self.events:
            return {"skipped": True, "reason": "Event nicht konfiguriert"}

        # Payload aus Template erstellen
        if self.payload_template:
            payload = self._render_template(self.payload_template, data)
        else:
            payload = data

        resp = await self._send_webhook(payload)

        return {
            "success": resp.status_code < 400,
            "status_code": resp.status_code,
            "response": resp.text[:500],
        }

    async def _send_webhook(self, payload: dict):
        """Sendet Webhook mit Authentifizierung"""

        headers = {"Content-Type": "application/json"}

        if self.auth_type == "bearer":
            headers["Authorization"] = f"Bearer {self.auth_credentials['token']}"
        elif self.auth_type == "api_key":
            headers[self.auth_credentials["header"]] = self.auth_credentials["key"]

        auth = None
        if self.auth_type == "basic":
            auth = (self.auth_credentials["username"], self.auth_credentials["password"])

        async with httpx.AsyncClient() as client:
            return await client.post(
                self.webhook_url,
                json=payload,
                headers=headers,
                auth=auth,
                timeout=30.0,
            )


# Event-Dispatcher für Custom Webhooks
class WebhookDispatcher:
    """Dispatcht Events an alle konfigurierten Webhooks"""

    async def dispatch(self, tenant_id: uuid.UUID, event_type: str, data: dict):
        """Sendet Event an alle relevanten Webhooks"""

        # Alle aktiven Webhook-Integrationen des Tenants
        integrations = await db.execute(
            select(TenantIntegration)
            .where(
                TenantIntegration.tenant_id == tenant_id,
                TenantIntegration.integration_name == "custom_webhook",
                TenantIntegration.is_active == True,
            )
        )

        for integration in integrations.scalars():
            webhook = CustomWebhookIntegration()
            await webhook.configure(integration.config)

            if event_type in webhook.events:
                # Asynchron senden (Fire and Forget)
                asyncio.create_task(
                    webhook.execute("send_event", {
                        "event_type": event_type,
                        "tenant_id": str(tenant_id),
                        "timestamp": datetime.now().isoformat(),
                        "data": data,
                    })
                )
```

### 10.5 Integration-UI

```html
<!-- pages/settings/integrations.html -->
<div class="max-w-4xl mx-auto py-8">
    <h1 class="text-2xl font-bold mb-6">Schnittstellen</h1>

    <!-- Kategorien -->
    <div class="space-y-8">
        {% for category, integrations in integrations_by_category.items %}
        <div>
            <h2 class="text-lg font-semibold mb-4 text-gray-700">
                {{ category.display_name }}
            </h2>

            <div class="grid gap-4">
                {% for integration in integrations %}
                <div class="bg-white rounded-lg shadow p-4">
                    <div class="flex items-center justify-between">
                        <div class="flex items-center gap-4">
                            <div class="w-12 h-12 rounded-lg bg-gray-100 flex items-center justify-center">
                                <i data-lucide="{{ integration.icon }}" class="w-6 h-6 text-gray-600"></i>
                            </div>
                            <div>
                                <h3 class="font-medium">{{ integration.display_name }}</h3>
                                <p class="text-sm text-gray-500">{{ integration.description }}</p>
                            </div>
                        </div>

                        <div class="flex items-center gap-3">
                            {% if integration.is_configured %}
                            <span class="badge badge-success">Aktiv</span>
                            {% endif %}

                            <button hx-get="{% url 'session:integration_configure' integration.name %}"
                                    hx-target="#modal-container"
                                    class="btn btn-outline btn-sm">
                                Konfigurieren
                            </button>
                        </div>
                    </div>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endfor %}
    </div>

    <!-- Custom Webhook hinzufügen -->
    <div class="mt-8">
        <button hx-get="{% url 'session:integration_add_webhook' %}"
                hx-target="#modal-container"
                class="btn btn-primary">
            <i data-lucide="plus" class="w-4 h-4 mr-2"></i>
            Custom Webhook hinzufügen
        </button>
    </div>
</div>
```

---

## 11. Performance-Optimierung

### 11.1 Caching-Strategie

```python
# cache/strategy.py
from functools import wraps
import redis.asyncio as redis

class CacheStrategy:
    """Caching-Strategien für verschiedene Datentypen"""

    # TTL in Sekunden
    TTL = {
        "oparl_system": 3600,       # 1 Stunde
        "oparl_body": 1800,         # 30 Minuten
        "meeting_list": 300,        # 5 Minuten
        "meeting_detail": 60,       # 1 Minute
        "paper_list": 300,          # 5 Minuten
        "search_results": 60,       # 1 Minute
        "user_permissions": 300,    # 5 Minuten
    }

    @classmethod
    def get_key(cls, prefix: str, *args) -> str:
        """Generiert Cache-Key"""
        return f"mandari:{prefix}:{':'.join(str(a) for a in args)}"


def cached(prefix: str, ttl: int | None = None):
    """Decorator für gecachte Funktionen"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Cache-Key generieren
            key = CacheStrategy.get_key(prefix, *args, *kwargs.values())

            # Aus Cache versuchen
            cached_value = await redis_client.get(key)
            if cached_value:
                return json.loads(cached_value)

            # Funktion ausführen
            result = await func(*args, **kwargs)

            # In Cache speichern
            cache_ttl = ttl or CacheStrategy.TTL.get(prefix, 300)
            await redis_client.setex(key, cache_ttl, json.dumps(result, default=str))

            return result
        return wrapper
    return decorator


# Cache-Invalidierung
class CacheInvalidator:
    """Invalidiert Cache bei Datenänderungen"""

    @staticmethod
    async def invalidate_meeting(meeting_id: uuid.UUID, tenant_id: uuid.UUID):
        """Invalidiert alle Meeting-bezogenen Caches"""
        patterns = [
            f"mandari:meeting_detail:{meeting_id}*",
            f"mandari:meeting_list:{tenant_id}*",
            f"mandari:oparl_meetings:{tenant_id}*",
        ]
        for pattern in patterns:
            keys = await redis_client.keys(pattern)
            if keys:
                await redis_client.delete(*keys)

    @staticmethod
    async def invalidate_tenant(tenant_id: uuid.UUID):
        """Invalidiert alle Tenant-bezogenen Caches"""
        keys = await redis_client.keys(f"mandari:*:{tenant_id}*")
        if keys:
            await redis_client.delete(*keys)
```

### 11.2 Datenbankoptimierung

```python
# db/optimization.py

# Wichtige Indizes
INDEXES = [
    # Tenant-Isolation (für RLS)
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_meetings_tenant ON oparl_meetings(tenant_id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_papers_tenant ON oparl_papers(tenant_id)",

    # Häufige Abfragen
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_meetings_start ON oparl_meetings(start DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_meetings_org ON oparl_meetings USING GIN(organization_ids)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_papers_reference ON oparl_papers(reference)",

    # Volltextsuche (für einfache Suchen ohne Meilisearch)
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_meetings_name_fts ON oparl_meetings USING GIN(to_tsvector('german', name))",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_papers_name_fts ON oparl_papers USING GIN(to_tsvector('german', name))",

    # Workflow-Abfragen
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_workflow_status ON workflow_instances(tenant_id, status, current_step)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_workflow_entity ON workflow_instances(entity_type, entity_id)",
]


# Eager Loading für häufige Abfragen
def meetings_with_relations():
    """Lädt Meetings mit allen benötigten Relationen"""
    return (
        select(OParlMeeting)
        .options(
            selectinload(OParlMeeting.agenda_items),
            selectinload(OParlMeeting.protocol_file),
            selectinload(OParlMeeting.organizations),
        )
    )
```

### 11.3 HTMX-Optimierungen

```python
# views/mixins.py
class OptimizedHTMXMixin:
    """Optimierungen für HTMX-Responses"""

    def render_to_response(self, context, **response_kwargs):
        response = super().render_to_response(context, **response_kwargs)

        # Keine vollständigen Seiten bei HTMX-Requests
        if self.request.headers.get("HX-Request"):
            # OOB-Swaps für Seiteneffekte
            if hasattr(self, 'oob_updates'):
                oob_html = self._render_oob_updates()
                response.content += oob_html.encode()

        # Browser-Caching für statische Partials
        if hasattr(self, 'cache_control'):
            response['Cache-Control'] = self.cache_control

        return response

    def _render_oob_updates(self) -> str:
        """Rendert Out-of-Band Updates"""
        oob_html = ""
        for target, template in self.oob_updates.items():
            oob_html += f'<div id="{target}" hx-swap-oob="true">'
            oob_html += render_to_string(template, self.get_context_data())
            oob_html += '</div>'
        return oob_html
```

---

## 12. Deployment und Betrieb

### 12.1 Docker-Compose (Produktion)

```yaml
# docker-compose.prod.yml
version: '3.8'

services:
  # ─────────────────────────────────────────────
  # Core Services
  # ─────────────────────────────────────────────

  api:
    build:
      context: .
      dockerfile: apps/api/Dockerfile
    environment:
      - DATABASE_URL=postgresql://mandari:${DB_PASSWORD}@db:5432/mandari
      - REDIS_URL=redis://redis:6379
      - MEILISEARCH_URL=http://meilisearch:7700
      - ENCRYPTION_KEY_PATH=/secrets/encryption.key
    volumes:
      - /secrets:/secrets:ro
    deploy:
      replicas: 3
      resources:
        limits:
          cpus: '2'
          memory: 2G

  session:
    build:
      context: .
      dockerfile: mandari/Dockerfile
    environment:
      - API_URL=http://api:8000
      - DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY}
    depends_on:
      - api

  # ─────────────────────────────────────────────
  # Data Services
  # ─────────────────────────────────────────────

  db:
    image: postgres:16
    environment:
      - POSTGRES_DB=mandari
      - POSTGRES_USER=mandari
      - POSTGRES_PASSWORD=${DB_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./init-db.sql:/docker-entrypoint-initdb.d/init.sql
    deploy:
      resources:
        limits:
          cpus: '4'
          memory: 8G

  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes
    volumes:
      - redis_data:/data

  meilisearch:
    image: getmeili/meilisearch:v1.6
    environment:
      - MEILI_MASTER_KEY=${MEILISEARCH_KEY}
    volumes:
      - meilisearch_data:/meili_data

  # ─────────────────────────────────────────────
  # Storage
  # ─────────────────────────────────────────────

  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    environment:
      - MINIO_ROOT_USER=${MINIO_USER}
      - MINIO_ROOT_PASSWORD=${MINIO_PASSWORD}
    volumes:
      - minio_data:/data

  # ─────────────────────────────────────────────
  # Workers
  # ─────────────────────────────────────────────

  celery:
    build:
      context: .
      dockerfile: apps/api/Dockerfile
    command: celery -A src.worker worker -l info
    environment:
      - DATABASE_URL=postgresql://mandari:${DB_PASSWORD}@db:5432/mandari
      - REDIS_URL=redis://redis:6379
    deploy:
      replicas: 2

  celery-beat:
    build:
      context: .
      dockerfile: apps/api/Dockerfile
    command: celery -A src.worker beat -l info
    environment:
      - DATABASE_URL=postgresql://mandari:${DB_PASSWORD}@db:5432/mandari
      - REDIS_URL=redis://redis:6379

volumes:
  postgres_data:
  redis_data:
  meilisearch_data:
  minio_data:
```

### 12.2 Monitoring

```python
# monitoring/health.py
from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["health"])

@router.get("")
async def health_check():
    """Basis Health-Check"""
    return {"status": "healthy"}

@router.get("/ready")
async def readiness_check():
    """Kubernetes Readiness Probe"""
    checks = {
        "database": await check_database(),
        "redis": await check_redis(),
        "meilisearch": await check_meilisearch(),
    }

    all_healthy = all(c["healthy"] for c in checks.values())

    return {
        "status": "ready" if all_healthy else "not_ready",
        "checks": checks,
    }

@router.get("/metrics")
async def metrics():
    """Prometheus Metrics"""
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    return Response(
        generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )
```

---

## Zusammenfassung

Diese technische Spezifikation definiert Mandari Session als:

1. **Multi-Tenant-fähig**: PostgreSQL RLS, Tenant-Kontext in jedem Request
2. **Vollverschlüsselt**: Transport, At-Rest, Field-Level, E2E für Work
3. **HTMX-First**: Server-Side Rendering, Instant Response, Progressive Enhancement
4. **Klare Rollen**: RBAC mit granularen Permissions
5. **Einfache Workflows**: Konfigurierbar, verständlich, benachrichtigend
6. **OParl 1.1 vollständig**: Alle Felder genutzt, Standard first
7. **3-Säulen-Kommunikation**: OParl für Insight, Session-API für Work
8. **Flexible Schnittstellen**: Standard + Custom Webhooks
9. **Performance-optimiert**: Caching, Indizes, HTMX-Partials
10. **Digital First, Human Friendly**: Modern aber verwaltungsgerecht

---

*Dokument erstellt: Januar 2026*
*Letzte Aktualisierung: Januar 2026*
*Autor: Mandari Development Team*
