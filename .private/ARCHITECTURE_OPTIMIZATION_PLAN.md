# Architektur-Optimierung: Option A - Monorepo mit Shared Library

**Erstellt**: 2026-02-05
**Status**: Planung
**Ziel**: Model-Duplizierung eliminieren, Wartbarkeit verbessern, Performance beibehalten

---

## 1. Problemanalyse

### Aktuelle Situation
```
beta/
├── mandari/
│   └── insight_core/
│       └── models.py          # Django ORM (987 Zeilen, 16+ Models)
└── ingestor/
    └── src/storage/
        └── models.py          # SQLAlchemy ORM (Duplizierung!)
```

### Probleme
1. **Model-Duplizierung**: Jede Änderung an OParl-Strukturen muss zweimal gemacht werden
2. **Divergenz-Risiko**: Models können auseinanderlaufen ohne es zu bemerken
3. **Keine gemeinsame Validierung**: Jede Seite validiert anders
4. **Import-Hack**: Django importiert Ingestor via `sys.path.insert()`

---

## 2. Zielarchitektur

```
beta/
├── shared/                           # NEU: Gemeinsame Library
│   ├── pyproject.toml               # Eigenständiges Package
│   ├── src/
│   │   └── mandari_shared/
│   │       ├── __init__.py
│   │       ├── oparl/
│   │       │   ├── __init__.py
│   │       │   ├── schemas.py       # Pydantic Models (Single Source of Truth)
│   │       │   ├── constants.py     # OParl-Konstanten, Enums
│   │       │   └── validators.py    # Gemeinsame Validierungslogik
│   │       ├── events/
│   │       │   ├── __init__.py
│   │       │   ├── types.py         # Event-Typen (Pydantic)
│   │       │   └── channels.py      # Redis Channel Namen
│   │       └── config/
│   │           ├── __init__.py
│   │           └── settings.py      # Gemeinsame Settings (DB-URL, Redis, etc.)
│   └── tests/
│
├── mandari/                          # Django App (angepasst)
│   └── insight_core/
│       ├── models.py                # Django ORM, nutzt shared.oparl.schemas
│       └── adapters.py              # NEU: Pydantic → Django Model Konvertierung
│
└── ingestor/                         # Async Ingestor (angepasst)
    └── src/storage/
        ├── models.py                # SQLAlchemy, nutzt shared.oparl.schemas
        └── adapters.py              # NEU: Pydantic → SQLAlchemy Konvertierung
```

---

## 3. Shared Library Design

### 3.1 Pydantic Schemas (Single Source of Truth)

```python
# shared/src/mandari_shared/oparl/schemas.py

from pydantic import BaseModel, HttpUrl, field_validator
from datetime import datetime
from typing import Optional

class OParlBaseSchema(BaseModel):
    """Basis für alle OParl-Objekte"""
    external_id: HttpUrl              # OParl URL (Primary Identifier)
    oparl_type: str                   # z.B. "https://schema.oparl.org/1.1/Body"
    oparl_created: Optional[datetime] = None
    oparl_modified: Optional[datetime] = None
    raw_json: dict                    # Original API Response

    model_config = {
        "from_attributes": True,      # Ermöglicht ORM-Konvertierung
        "extra": "ignore"
    }

class OParlBodySchema(OParlBaseSchema):
    """Kommune/Körperschaft"""
    name: str
    short_name: Optional[str] = None
    website: Optional[HttpUrl] = None
    license: Optional[str] = None
    license_valid_since: Optional[datetime] = None
    oparl_since: Optional[datetime] = None
    ags: Optional[str] = None         # Amtlicher Gemeindeschlüssel
    equivalent_body: list[HttpUrl] = []
    contact_email: Optional[str] = None
    contact_name: Optional[str] = None
    classification: Optional[str] = None
    location_id: Optional[str] = None

    @field_validator('ags')
    @classmethod
    def validate_ags(cls, v):
        if v and len(v) != 8:
            raise ValueError('AGS muss 8 Zeichen haben')
        return v

class OParlOrganizationSchema(OParlBaseSchema):
    """Gremium (Ausschuss, Fraktion, etc.)"""
    name: str
    short_name: Optional[str] = None
    body_id: str                      # Referenz zu Body
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    organization_type: Optional[str] = None
    classification: Optional[str] = None
    website: Optional[HttpUrl] = None
    location_id: Optional[str] = None

class OParlPersonSchema(OParlBaseSchema):
    """Ratsmitglied"""
    name: str
    family_name: Optional[str] = None
    given_name: Optional[str] = None
    form_of_address: Optional[str] = None
    affix: Optional[str] = None
    title: list[str] = []
    gender: Optional[str] = None
    email: list[str] = []
    phone: list[str] = []
    status: list[str] = []
    life: Optional[str] = None
    body_id: str

class OParlMeetingSchema(OParlBaseSchema):
    """Sitzung"""
    name: Optional[str] = None
    meeting_state: Optional[str] = None
    cancelled: bool = False
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    location_id: Optional[str] = None
    organization_ids: list[str] = []
    body_id: str
    invitation_id: Optional[str] = None
    results_protocol_id: Optional[str] = None
    verbatim_protocol_id: Optional[str] = None

class OParlAgendaItemSchema(OParlBaseSchema):
    """Tagesordnungspunkt"""
    meeting_id: str
    number: Optional[str] = None
    order: Optional[int] = None
    name: Optional[str] = None
    public: bool = True
    consultation_id: Optional[str] = None
    result: Optional[str] = None
    resolution_text: Optional[str] = None
    resolution_file_id: Optional[str] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None

class OParlPaperSchema(OParlBaseSchema):
    """Vorlage/Drucksache"""
    body_id: str
    name: Optional[str] = None
    reference: Optional[str] = None
    date: Optional[datetime] = None
    paper_type: Optional[str] = None
    main_file_id: Optional[str] = None
    originator_person_ids: list[str] = []
    under_direction_of_ids: list[str] = []
    originator_organization_ids: list[str] = []

class OParlFileSchema(OParlBaseSchema):
    """Dokument-Datei"""
    name: Optional[str] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    size: Optional[int] = None
    sha1_checksum: Optional[str] = None
    sha512_checksum: Optional[str] = None
    text: Optional[str] = None
    access_url: Optional[HttpUrl] = None
    download_url: Optional[HttpUrl] = None
    external_service_url: Optional[HttpUrl] = None
    master_file_id: Optional[str] = None
    derivative_file_ids: list[str] = []
    file_license: Optional[str] = None

# ... weitere Schemas für Consultation, Membership, Location, etc.
```

### 3.2 Event-Typen

```python
# shared/src/mandari_shared/events/types.py

from pydantic import BaseModel
from datetime import datetime
from typing import Literal, Optional
from enum import Enum

class EventType(str, Enum):
    SYNC_STARTED = "sync:started"
    SYNC_COMPLETED = "sync:completed"
    SYNC_FAILED = "sync:failed"
    ENTITY_CREATED = "entity:created"
    ENTITY_UPDATED = "entity:updated"
    ENTITY_BATCH = "entity:batch"

class SyncEvent(BaseModel):
    event_type: EventType
    timestamp: datetime
    source_url: str
    source_name: Optional[str] = None

class EntityEvent(BaseModel):
    event_type: EventType
    timestamp: datetime
    entity_type: str                  # z.B. "OParlBody", "OParlMeeting"
    entity_id: str                    # external_id
    body_name: Optional[str] = None
    changes: Optional[dict] = None    # Für Updates: was hat sich geändert

class BatchEvent(BaseModel):
    event_type: Literal[EventType.ENTITY_BATCH]
    timestamp: datetime
    entity_type: str
    count: int
    body_name: Optional[str] = None
```

### 3.3 Redis Channels

```python
# shared/src/mandari_shared/events/channels.py

class Channels:
    """Zentrale Definition aller Redis Pub/Sub Channels"""

    # Sync-Lifecycle Events
    SYNC = "mandari:sync"

    # Entity CRUD Events
    ENTITIES = "mandari:entities"

    # Spezifische Entity-Channels (für selektives Subscriben)
    BODIES = "mandari:entities:body"
    ORGANIZATIONS = "mandari:entities:organization"
    PERSONS = "mandari:entities:person"
    MEETINGS = "mandari:entities:meeting"
    PAPERS = "mandari:entities:paper"

    # Health & Metrics
    HEALTH = "mandari:health"
    METRICS = "mandari:metrics"
```

---

## 4. Adapter-Pattern für ORM-Integration

### 4.1 Django Adapter

```python
# mandari/insight_core/adapters.py

from mandari_shared.oparl.schemas import (
    OParlBodySchema, OParlOrganizationSchema, OParlMeetingSchema, ...
)
from .models import OParlBody, OParlOrganization, OParlMeeting, ...

class DjangoAdapter:
    """Konvertiert Pydantic Schemas ↔ Django Models"""

    @staticmethod
    def body_to_model(schema: OParlBodySchema) -> OParlBody:
        """Pydantic → Django Model"""
        return OParlBody(
            external_id=str(schema.external_id),
            name=schema.name,
            short_name=schema.short_name,
            website=str(schema.website) if schema.website else None,
            # ... weitere Felder
            raw_json=schema.raw_json,
            oparl_created=schema.oparl_created,
            oparl_modified=schema.oparl_modified,
        )

    @staticmethod
    def body_from_model(model: OParlBody) -> OParlBodySchema:
        """Django Model → Pydantic"""
        return OParlBodySchema.model_validate(model)

    # ... Adapter für alle anderen Entity-Typen
```

### 4.2 SQLAlchemy Adapter (Ingestor)

```python
# ingestor/src/storage/adapters.py

from mandari_shared.oparl.schemas import OParlBodySchema, ...
from .models import OParlBody, ...

class SQLAlchemyAdapter:
    """Konvertiert Pydantic Schemas ↔ SQLAlchemy Models"""

    @staticmethod
    def body_to_model(schema: OParlBodySchema) -> OParlBody:
        """Pydantic → SQLAlchemy Model"""
        return OParlBody(
            external_id=str(schema.external_id),
            name=schema.name,
            # ... Mapping
        )

    @staticmethod
    def body_from_dict(data: dict) -> OParlBodySchema:
        """Raw API Response → Pydantic (mit Validierung!)"""
        return OParlBodySchema(
            external_id=data.get('id'),
            oparl_type=data.get('type'),
            name=data.get('name'),
            # ... Mapping mit Defaults
            raw_json=data,
        )
```

---

## 5. Implementierungsschritte

### Phase 1: Shared Library erstellen (Woche 1)

| Schritt | Beschreibung | Abhängigkeiten |
|---------|--------------|----------------|
| 1.1 | `shared/` Verzeichnis mit pyproject.toml erstellen | - |
| 1.2 | Pydantic Basis-Schemas definieren (OParlBaseSchema) | 1.1 |
| 1.3 | Alle 16+ OParl Schemas implementieren | 1.2 |
| 1.4 | Event-Typen in shared verschieben | 1.1 |
| 1.5 | Unit Tests für Schemas schreiben | 1.3 |
| 1.6 | Package lokal installierbar machen (`pip install -e shared/`) | 1.1 |

### Phase 2: Ingestor anpassen (Woche 2)

| Schritt | Beschreibung | Abhängigkeiten |
|---------|--------------|----------------|
| 2.1 | `mandari-shared` als Dependency hinzufügen | Phase 1 |
| 2.2 | SQLAlchemy Adapter implementieren | 2.1 |
| 2.3 | `processor.py` anpassen: Raw JSON → Pydantic → SQLAlchemy | 2.2 |
| 2.4 | Events auf shared Event-Typen umstellen | 2.1 |
| 2.5 | Alte `storage/models.py` Schemas entfernen (Duplikate) | 2.3 |
| 2.6 | Integration Tests | 2.5 |

### Phase 3: Django anpassen (Woche 3)

| Schritt | Beschreibung | Abhängigkeiten |
|---------|--------------|----------------|
| 3.1 | `mandari-shared` als Dependency hinzufügen | Phase 1 |
| 3.2 | Django Adapter implementieren | 3.1 |
| 3.3 | `insight_core/models.py` aufräumen (Validierung → shared) | 3.2 |
| 3.4 | Event-Handler für Redis Pub/Sub (via Django Signals) | 3.1 |
| 3.5 | `sys.path.insert()` Hack entfernen | 3.2 |
| 3.6 | Integration Tests | 3.5 |

### Phase 4: CI/CD & Dokumentation (Woche 4)

| Schritt | Beschreibung | Abhängigkeiten |
|---------|--------------|----------------|
| 4.1 | Dockerfile für shared Library (Multi-Stage Build) | Phase 1-3 |
| 4.2 | GitHub Actions anpassen (shared testen vor Deploy) | 4.1 |
| 4.3 | CLAUDE.md aktualisieren | - |
| 4.4 | DEPENDENCIES.md aktualisieren | - |
| 4.5 | Smoke Tests im Staging | 4.1-4.4 |

---

## 6. Dependency Management

### shared/pyproject.toml
```toml
[project]
name = "mandari-shared"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.10.0",
    "pydantic-settings>=2.6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.24.0",
]
```

### mandari/pyproject.toml (Django)
```toml
dependencies = [
    # ... bestehende Dependencies
    "mandari-shared @ file:///${PROJECT_ROOT}/../shared",
]
```

### ingestor/pyproject.toml
```toml
dependencies = [
    # ... bestehende Dependencies
    "mandari-shared @ file:///${PROJECT_ROOT}/../shared",
]
```

---

## 7. Docker Build Strategy

### docker-compose.yml Anpassung
```yaml
services:
  api:
    build:
      context: .
      dockerfile: Dockerfile.django
      args:
        - SHARED_PATH=./shared
    # shared wird beim Build kopiert

  ingestor:
    build:
      context: .
      dockerfile: Dockerfile.ingestor
      args:
        - SHARED_PATH=./shared
```

### Dockerfile.django (Beispiel)
```dockerfile
FROM python:3.12-slim

# Shared Library zuerst kopieren
COPY shared/ /app/shared/
RUN pip install -e /app/shared/

# Django App
COPY mandari/ /app/mandari/
WORKDIR /app/mandari
RUN pip install -e .

CMD ["gunicorn", "mandari.wsgi:application"]
```

---

## 8. Risiken & Mitigationen

| Risiko | Wahrscheinlichkeit | Impact | Mitigation |
|--------|-------------------|--------|------------|
| Pydantic Breaking Changes | Niedrig | Mittel | Version pinnen, Tests |
| Performance-Regression durch Validierung | Mittel | Mittel | Bulk-Operationen ohne Validierung Option |
| Migration bestehender Daten | Niedrig | Hoch | Keine DB-Änderungen nötig, nur Code |
| CI/CD Komplexität | Mittel | Niedrig | Separate Test-Jobs für shared |

---

## 9. Erfolgskriterien

- [ ] Alle OParl-Schemas in `mandari-shared` definiert
- [ ] Keine duplizierten Model-Definitionen mehr
- [ ] Ingestor nutzt shared Schemas für Validierung
- [ ] Django nutzt shared Schemas für Serialisierung
- [ ] Event-Typen zentral definiert
- [ ] `sys.path.insert()` Hack entfernt
- [ ] Alle Tests grün
- [ ] Dokumentation aktualisiert

---

## 10. Nicht im Scope

- Celery Integration (bleibt bei Django Tasks + APScheduler)
- Microservice API (Ingestor bleibt DB-gekoppelt)
- Django ASGI Migration (nicht notwendig für dieses Ziel)
- Model-Migrationen (Datenbank-Schema bleibt unverändert)

---

## Anhang: Betroffene Dateien

### Zu erstellen (neu)
- `beta/shared/pyproject.toml`
- `beta/shared/src/mandari_shared/__init__.py`
- `beta/shared/src/mandari_shared/oparl/schemas.py`
- `beta/shared/src/mandari_shared/oparl/constants.py`
- `beta/shared/src/mandari_shared/oparl/validators.py`
- `beta/shared/src/mandari_shared/events/types.py`
- `beta/shared/src/mandari_shared/events/channels.py`
- `beta/mandari/insight_core/adapters.py`
- `beta/ingestor/src/storage/adapters.py`

### Zu modifizieren
- `beta/mandari/pyproject.toml` (Dependency hinzufügen)
- `beta/ingestor/pyproject.toml` (Dependency hinzufügen)
- `beta/ingestor/src/sync/processor.py` (Adapter nutzen)
- `beta/ingestor/src/events.py` (shared Events nutzen)
- `beta/mandari/insight_sync/tasks.py` (Import-Hack entfernen)
- `beta/docker-compose.yml` (Build Context anpassen)
- `beta/Dockerfile.django` (shared kopieren)
- `beta/Dockerfile.ingestor` (shared kopieren)
- `beta/CLAUDE.md` (Dokumentation)
- `beta/DEPENDENCIES.md` (neue Dependency)

### Zu bereinigen (Code entfernen)
- `beta/ingestor/src/storage/models.py` (duplizierte Pydantic-ähnliche Klassen)
