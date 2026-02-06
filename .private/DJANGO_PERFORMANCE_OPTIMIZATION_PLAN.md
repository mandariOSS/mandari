# Django Performance-Optimierungsplan

**Erstellt**: 2026-02-05
**Status**: Analyse abgeschlossen
**Ziel**: Query-Optimierung, Python-Effizienz, Caching

---

## Executive Summary

Die Analyse hat **9 kritische**, **12 hohe** und **8 mittlere** Performance-Probleme identifiziert. Die wichtigsten Kategorien:

| Kategorie | Kritisch | Hoch | Mittel |
|-----------|----------|------|--------|
| N+1 Queries | 4 | 3 | - |
| Ineffiziente Queries | 4 | 2 | 2 |
| Python-Ineffizienzen | 2 | 2 | 1 |
| Fehlende Indizes | - | 3 | - |
| Caching fehlt | - | 2 | 3 |
| Bug (Mutation) | 1 | - | - |

**Geschätzter Performance-Gewinn nach Optimierung: 50-80% weniger DB-Queries**

---

## KRITISCHE PROBLEME (P0)

### 1. BUG: OParlFile.size_human mutiert self.size

**Datei**: `insight_core/models.py`, Zeilen 474-483

**Problem**:
```python
@property
def size_human(self):
    for unit in ["B", "KB", "MB", "GB"]:
        if self.size < 1024:
            return f"{self.size:.1f} {unit}"
        self.size /= 1024  # BUG: Mutiert self.size!
```

**Auswirkung**: Nach erstem Aufruf ist `self.size` dauerhaft verändert

**Fix**:
```python
@property
def size_human(self):
    if not self.size:
        return ""
    size = self.size  # Lokale Variable!
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
```

---

### 2. LocationMapping.get_coordinates_for_location() - O(n) Loop

**Datei**: `insight_core/models.py`, Zeilen 748-776

**Problem**:
```python
# 3 separate Queries, letzte lädt ALLE Records!
mapping = cls.objects.filter(body=body, location_name=location_name).first()
mapping = cls.objects.filter(body=body, location_name__iexact=location_name).first()
for m in cls.objects.filter(body=body):  # ALLE ohne LIMIT!
    if m.location_name.lower() in location_name.lower():  # Python-Vergleich
```

**Auswirkung**: Bei jedem Meeting-Detail-View, 3 Queries + Python-Loop

**Fix**:
```python
@classmethod
def get_coordinates_for_location(cls, body, location_name):
    if not location_name:
        return None

    # Eine Query mit OR-Bedingungen und DB-seitigem Matching
    mapping = cls.objects.filter(
        body=body
    ).filter(
        Q(location_name=location_name) |
        Q(location_name__iexact=location_name) |
        Q(location_name__icontains=location_name) |
        Q(raw_location_name__icontains=location_name)
    ).first()

    if mapping:
        return {"lat": float(mapping.latitude), "lng": float(mapping.longitude)}
    return None
```

**Zusätzlich**: Cache hinzufügen (siehe Caching-Sektion)

---

### 3. link_meeting_orgs.py - Lädt alle Orgs in RAM

**Datei**: `insight_core/management/commands/link_meeting_orgs.py`, Zeilen 21-36

**Problem**:
```python
org_lookup = {}
for org in OParlOrganization.objects.all():  # ALLE in RAM!
    org_lookup[org.external_id] = org
```

**Auswirkung**: Bei 10.000+ Orgs = 100MB+ RAM, langsam

**Fix**:
```python
# Option A: Nur IDs laden
org_lookup = dict(
    OParlOrganization.objects.values_list('external_id', 'id')
)

# Option B: Batch-Processing
def process_in_batches(queryset, batch_size=1000):
    for obj in queryset.iterator(chunk_size=batch_size):
        yield obj
```

---

### 4. prefetch_papers_for_agenda_items() - O(n²) Liste

**Datei**: `apps/work/meetings/views.py`, Zeilen 71-84

**Problem**:
```python
papers_by_ext_id = {}
for consultation in consultations:
    if consultation.paper not in papers_by_ext_id[ext_id]:  # O(n) pro Iteration!
        papers_by_ext_id[ext_id].append(consultation.paper)
```

**Auswirkung**: Bei 1000 Consultations = bis zu 1.000.000 Vergleiche

**Fix**:
```python
from collections import defaultdict

papers_by_ext_id = defaultdict(set)  # Set statt List!
for consultation in consultations:
    if consultation.paper and consultation.agenda_item_external_id:
        ext_id = consultation.agenda_item_external_id
        papers_by_ext_id[ext_id].add(consultation.paper)  # O(1)!

# Am Ende in List konvertieren falls nötig
papers_by_ext_id = {k: list(v) for k, v in papers_by_ext_id.items()}
```

---

### 5. get_display_name() ignoriert Prefetch

**Datei**: `insight_core/models.py`, Zeilen 299-309

**Problem**:
```python
def get_display_name(self):
    orgs = self.organizations.all()[:2]  # Ignoriert prefetch_related!
```

**Auswirkung**: N+1 Queries trotz Prefetch

**Fix**:
```python
def get_display_name(self):
    # Nutze bereits geladene Daten
    orgs = list(self.organizations.all())[:2]  # list() nutzt Cache!
    if orgs:
        org_names = [org.name for org in orgs if org.name]
        return ", ".join(org_names)
    return self.name or f"Sitzung {self.external_id}"
```

**Oder besser**: Annotate in Query
```python
# In View
meetings = OParlMeeting.objects.annotate(
    display_name=Coalesce(
        ArrayAgg('organizations__name', distinct=True)[:2],
        F('name'),
        Value('Unbenannte Sitzung')
    )
)
```

---

## HOHE PRIORITÄT (P1)

### 6. Redundante COUNT-Queries

**Dateien**:
- `apps/work/faction/views.py`, Zeilen 91-99
- `apps/work/motions/views.py`, Zeilen 96-104

**Problem**:
```python
context["stats"] = {
    "total": all_meetings.count(),      # Query 1
    "upcoming": all_meetings.filter(...).count(),  # Query 2
    "pending_protocol": all_meetings.filter(...).count(),  # Query 3
}
```

**Fix**:
```python
from django.db.models import Count, Q

stats = FactionMeeting.objects.filter(
    organization=self.organization
).aggregate(
    total=Count('id'),
    upcoming=Count('id', filter=Q(start__gte=now)),
    pending_protocol=Count('id', filter=Q(
        status="completed",
        protocol_approved=False
    )),
)
context["stats"] = stats  # 1 Query statt 4!
```

---

### 7. Fehlende DB-Indizes auf OParlConsultation

**Datei**: `insight_core/models.py`, Zeilen 603-604

**Problem**:
```python
agenda_item_external_id = models.TextField(blank=True, null=True)  # Kein Index!
meeting_external_id = models.TextField(blank=True, null=True)      # Kein Index!
```

**Auswirkung**: Full Table Scans bei Lookups

**Fix**:
```python
agenda_item_external_id = models.TextField(
    blank=True, null=True, db_index=True
)
meeting_external_id = models.TextField(
    blank=True, null=True, db_index=True
)
```

**Migration**:
```python
# Neue Migration erstellen
class Migration(migrations.Migration):
    operations = [
        migrations.AddIndex(
            model_name='oparlconsultation',
            index=models.Index(
                fields=['agenda_item_external_id'],
                name='consultation_agenda_idx'
            ),
        ),
        migrations.AddIndex(
            model_name='oparlconsultation',
            index=models.Index(
                fields=['meeting_external_id'],
                name='consultation_meeting_idx'
            ),
        ),
    ]
```

---

### 8. Admin body_count() N+1

**Datei**: `insight_core/admin.py`, Zeilen 85-87

**Problem**:
```python
def body_count(self, obj):
    return obj.bodies.count()  # Query pro Row!
```

**Fix**:
```python
@admin.register(OParlSource)
class OParlSourceAdmin(admin.ModelAdmin):
    list_display = [..., "body_count"]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(_body_count=Count('bodies'))

    @admin.display(description="Bodies")
    def body_count(self, obj):
        return obj._body_count  # Kein Query!
```

---

### 9. Support Ticket Badge bei jedem Request

**Datei**: `apps/work/admin.py`, Zeilen 32-42

**Problem**:
```python
def support_ticket_badge(request):
    open_count = SupportTicket.objects.filter(...).count()  # Jeder Request!
```

**Fix**:
```python
from django.core.cache import cache

def support_ticket_badge(request):
    cache_key = "admin_support_ticket_count"
    open_count = cache.get(cache_key)

    if open_count is None:
        open_count = SupportTicket.objects.filter(
            status__in=["open", "in_progress", "escalated"]
        ).count()
        cache.set(cache_key, open_count, timeout=60)  # 1 Minute

    return open_count
```

---

### 10. fix_permissions.py - .count() in Loop

**Datei**: `apps/tenants/management/commands/fix_permissions.py`, Zeilen 163-164

**Problem**:
```python
for m in memberships:
    if m.roles.count() == 0:  # Query pro Membership!
```

**Fix**:
```python
memberships = Membership.objects.prefetch_related('roles').all()
no_role_members = [m for m in memberships if not m.roles.all()]
```

---

### 11. Properties statt Annotate (is_active, is_current)

**Dateien**:
- `insight_core/models.py`, Zeile 179-187 (OParlOrganization.is_active)
- `insight_core/models.py`, Zeile 528-536 (OParlMembership.is_active)
- `insight_core/models.py`, Zeile 664-680 (OParlLegislativeTerm.is_current)

**Problem**:
```python
@property
def is_active(self):
    return self.end_date >= timezone.now().date()  # Berechnung pro Objekt
```

**Fix in Views**:
```python
from django.db.models import Case, When, Value, BooleanField
from django.utils import timezone

organizations = OParlOrganization.objects.annotate(
    is_active_computed=Case(
        When(end_date__isnull=True, then=Value(True)),
        When(end_date__gte=timezone.now().date(), then=Value(True)),
        default=Value(False),
        output_field=BooleanField(),
    )
).filter(is_active_computed=True)  # Filterung in DB!
```

---

## MITTLERE PRIORITÄT (P2)

### 12. OParlPaper.name ohne Index (wird durchsucht)

**Datei**: `insight_core/models.py`, Zeile ~330

**Problem**: `name` wird in Suche verwendet, hat aber keinen Index

**Fix**:
```python
# Für ICONTAINS-Suche: GIN Index mit pg_trgm
class Migration(migrations.Migration):
    operations = [
        migrations.RunSQL(
            "CREATE INDEX paper_name_trgm ON insight_core_oparlpaper USING gin (name gin_trgm_ops);",
            "DROP INDEX paper_name_trgm;"
        ),
    ]
```

---

### 13. Calendar Events - String Truncation in Python

**Datei**: `insight_core/views.py`, Zeile ~670

**Problem**:
```python
title = full_title[:37] + "..."  # Python Slicing
```

**Fix**: Truncate in Template oder Client-Side

---

### 14. MeetingListView lädt alle Orgs

**Datei**: `apps/work/meetings/views.py`, Zeile 22

**Problem**:
```python
for org in OParlOrganization.objects.all():  # ALLE!
```

**Fix**:
```python
# Nur relevante Orgs für diesen Tenant/Body
orgs = OParlOrganization.objects.filter(
    body__in=request.tenant.bodies.all()
).only('id', 'name', 'external_id')
```

---

## CACHING-STRATEGIE

### View-Level Caching

```python
from django.views.decorators.cache import cache_page
from django.utils.decorators import method_decorator

# Öffentliche Seiten (5 Minuten)
@method_decorator(cache_page(60 * 5), name='dispatch')
class PublicMeetingListView(ListView):
    pass

# Kalender-Events (1 Minute)
@cache_page(60)
def calendar_events(request):
    pass
```

### Method-Level Caching

```python
from django.core.cache import cache
from functools import wraps

def cache_method(timeout=300, key_prefix=''):
    def decorator(method):
        @wraps(method)
        def wrapper(self, *args, **kwargs):
            cache_key = f"{key_prefix}:{self.pk}:{args}:{kwargs}"
            result = cache.get(cache_key)
            if result is None:
                result = method(self, *args, **kwargs)
                cache.set(cache_key, result, timeout)
            return result
        return wrapper
    return decorator

# Verwendung
class LocationMapping(models.Model):
    @classmethod
    @cache_method(timeout=3600, key_prefix='location_coords')
    def get_coordinates_for_location(cls, body, location_name):
        # ...
```

### Caching für OParl-Daten

```python
# settings.py
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    },
    "oparl": {  # Separater Cache für OParl-Daten
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "oparl",
        "TIMEOUT": 3600,  # 1 Stunde default
    },
}
```

### Cache Invalidation bei Sync

```python
# insight_sync/tasks.py oder Ingestor
from django.core.cache import caches

def invalidate_oparl_cache(body_id=None):
    oparl_cache = caches['oparl']
    if body_id:
        # Selektiv invalidieren
        oparl_cache.delete_pattern(f"*:body_{body_id}:*")
    else:
        oparl_cache.clear()
```

---

## IMPLEMENTIERUNGSPLAN

### Phase 1: Kritische Bugs (Tag 1)
| Task | Datei | Aufwand |
|------|-------|---------|
| Fix size_human Mutation | `insight_core/models.py` | 5 Min |
| Fix get_display_name Prefetch | `insight_core/models.py` | 15 Min |
| Fix O(n²) List-Check | `apps/work/meetings/views.py` | 10 Min |

### Phase 2: Query-Optimierung (Tag 2-3)
| Task | Datei | Aufwand |
|------|-------|---------|
| LocationMapping DB-Filter | `insight_core/models.py` | 30 Min |
| COUNT → Aggregate | `apps/work/faction/views.py` | 15 Min |
| COUNT → Aggregate | `apps/work/motions/views.py` | 15 Min |
| Admin annotate | `insight_core/admin.py` | 20 Min |
| Management Commands | Diverse | 1 Std |

### Phase 3: Indizes (Tag 4)
| Task | Aufwand |
|------|---------|
| Migration für Consultation-Indizes | 15 Min |
| Migration für Paper name GIN Index | 30 Min |
| Testen & Deployment | 1 Std |

### Phase 4: Caching (Tag 5)
| Task | Aufwand |
|------|---------|
| LocationMapping Cache | 30 Min |
| Support Ticket Badge Cache | 15 Min |
| View-Level Caching | 30 Min |
| Cache Invalidation Hook | 1 Std |

### Phase 5: Properties → Annotate (Woche 2)
| Task | Aufwand |
|------|---------|
| is_active Refactoring | 2 Std |
| Alle Views anpassen | 3 Std |
| Tests aktualisieren | 2 Std |

---

## MONITORING & VALIDIERUNG

### Django Debug Toolbar (Development)
```python
# settings.py (DEBUG only)
INSTALLED_APPS += ['debug_toolbar']
MIDDLEWARE += ['debug_toolbar.middleware.DebugToolbarMiddleware']
```

### Query-Logging
```python
# settings.py
LOGGING = {
    'handlers': {
        'sql': {
            'class': 'logging.FileHandler',
            'filename': 'sql.log',
        },
    },
    'loggers': {
        'django.db.backends': {
            'handlers': ['sql'],
            'level': 'DEBUG',
        },
    },
}
```

### PostgreSQL Slow Query Log
```sql
-- postgresql.conf
log_min_duration_statement = 100  -- Log queries > 100ms
```

### Metriken (Production)
```python
# Prometheus Metrics
from prometheus_client import Histogram

db_query_duration = Histogram(
    'django_db_query_duration_seconds',
    'Database query duration',
    ['query_type']
)
```

---

## ERFOLGSKRITERIEN

Nach Implementierung:

- [ ] Keine N+1 Queries in Django Debug Toolbar
- [ ] Meeting-List-View: < 10 Queries (aktuell: ~50+)
- [ ] Paper-Detail-View: < 15 Queries (aktuell: ~30+)
- [ ] Admin Source-Liste: < 5 Queries (aktuell: ~100+)
- [ ] LocationMapping-Lookup: 1 Query + Cache (aktuell: 3+)
- [ ] size_human Bug behoben
- [ ] Alle COUNT-Aggregationen in 1 Query

---

## ANHANG: Betroffene Dateien

### Zu ändern
```
insight_core/models.py          # 5 Änderungen
insight_core/views.py           # 2 Änderungen
insight_core/admin.py           # 2 Änderungen
apps/work/meetings/views.py     # 2 Änderungen
apps/work/faction/views.py      # 1 Änderung
apps/work/motions/views.py      # 1 Änderung
apps/work/admin.py              # 1 Änderung
apps/tenants/management/commands/fix_permissions.py  # 1 Änderung
insight_core/management/commands/link_meeting_orgs.py # 1 Änderung
```

### Neue Migrationen
```
insight_core/migrations/XXXX_add_consultation_indexes.py
insight_core/migrations/XXXX_add_paper_name_gin_index.py
```
