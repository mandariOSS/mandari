# Mandari Master Feature List

**Erstellt**: 2026-02-06
**Status**: Konsolidiert aus 5 Einzelplänen

---

## Übersicht nach Kategorie

| Kategorie | Tasks | Priorität |
|-----------|-------|-----------|
| A. Performance & Bugs | 14 | Kritisch |
| B. Text-Extraktion & OCR | 12 | Hoch |
| C. Suche & Indexierung | 10 | Hoch |
| D. SEO & Sitemaps | 11 | Hoch |
| E. CI/CD & Deployment | 10 | Mittel |
| F. Architektur (Shared Library) | 8 | Niedrig |
| G. SPDX & Copyright | 6 | Niedrig |

**Gesamt: 71 Tasks**

---

# A. PERFORMANCE & BUGS (Kritisch)

## A.1 Kritische Bugs

### A.1.1 Fix: OParlFile.size_human mutiert self.size
**Datei**: `insight_core/models.py:474-483`
**Problem**: Property mutiert das Feld permanent
**Aufwand**: 5 Min
```python
# Vorher (BUG):
self.size /= 1024

# Nachher (FIX):
size = self.size  # Lokale Variable
```

### A.1.2 Fix: get_display_name() ignoriert Prefetch
**Datei**: `insight_core/models.py:299-309`
**Problem**: `.all()[:2]` ignoriert prefetch_related
**Aufwand**: 10 Min
```python
# Fix: list() nutzt den Cache
orgs = list(self.organizations.all())[:2]
```

### A.1.3 Fix: O(n²) List-Check in prefetch_papers
**Datei**: `apps/work/meetings/views.py:71-84`
**Problem**: `if x not in list` ist O(n)
**Aufwand**: 10 Min
```python
# Fix: Set statt List
papers_by_ext_id = defaultdict(set)
```

### A.1.4 Fix: LocationMapping lädt alle Records
**Datei**: `insight_core/models.py:748-776`
**Problem**: `for m in cls.objects.filter(body=body)` ohne LIMIT
**Aufwand**: 30 Min
```python
# Fix: Eine Query mit OR-Bedingungen
mapping = cls.objects.filter(body=body).filter(
    Q(location_name=location_name) |
    Q(location_name__iexact=location_name)
).first()
```

---

## A.2 Query-Optimierung

### A.2.1 COUNT → Aggregate in FactionMeetingListView
**Datei**: `apps/work/faction/views.py:91-99`
**Problem**: 4 separate COUNT-Queries
**Aufwand**: 15 Min
```python
stats = FactionMeeting.objects.aggregate(
    total=Count('id'),
    upcoming=Count('id', filter=Q(start__gte=now)),
    ...
)
```

### A.2.2 COUNT → Aggregate in MotionListView
**Datei**: `apps/work/motions/views.py:96-104`
**Aufwand**: 15 Min

### A.2.3 Admin body_count() annotate
**Datei**: `insight_core/admin.py:85-87`
**Problem**: Query pro Admin-Zeile
**Aufwand**: 20 Min
```python
def get_queryset(self, request):
    return super().get_queryset(request).annotate(_body_count=Count('bodies'))
```

### A.2.4 Support Ticket Badge cachen
**Datei**: `apps/work/admin.py:32-42`
**Problem**: COUNT bei jedem Request
**Aufwand**: 15 Min
```python
from django.core.cache import cache
open_count = cache.get_or_set('admin_ticket_count', lambda: ..., 60)
```

### A.2.5 fix_permissions: Prefetch Roles
**Datei**: `apps/tenants/management/commands/fix_permissions.py:163-164`
**Problem**: `.count()` in Loop
**Aufwand**: 15 Min

### A.2.6 link_meeting_orgs: Nur IDs laden
**Datei**: `insight_core/management/commands/link_meeting_orgs.py:21-36`
**Problem**: Alle Orgs in RAM
**Aufwand**: 20 Min
```python
org_lookup = dict(OParlOrganization.objects.values_list('external_id', 'id'))
```

---

## A.3 Datenbank-Indizes

### A.3.1 Index auf OParlConsultation.agenda_item_external_id
**Datei**: Neue Migration
**Aufwand**: 15 Min

### A.3.2 Index auf OParlConsultation.meeting_external_id
**Datei**: Neue Migration
**Aufwand**: 10 Min

### A.3.3 GIN-Index auf OParlPaper.name (für ICONTAINS)
**Datei**: Neue Migration mit RunSQL
**Aufwand**: 30 Min
```sql
CREATE INDEX paper_name_trgm ON insight_core_oparlpaper
USING gin (name gin_trgm_ops);
```

---

## A.4 Caching

### A.4.1 LocationMapping Cache
**Datei**: `insight_core/models.py`
**Aufwand**: 30 Min

### A.4.2 View-Level Cache für öffentliche Seiten
**Dateien**: `insight_core/views.py`
**Aufwand**: 30 Min
```python
@cache_page(60 * 5)  # 5 Minuten
```

---

# B. TEXT-EXTRAKTION & OCR (Hoch)

## B.1 Datenbank-Erweiterungen

### B.1.1 Migration: text_extraction_status Feld
**Datei**: Neue Migration
**Felder**:
- `text_extraction_status` (pending/processing/completed/failed)
- `text_extraction_method` (pypdf/tesseract/mistral)
- `text_extraction_error` (TextField)
**Aufwand**: 20 Min

### B.1.2 Migration: OParlBody.slug Feld
**Datei**: Neue Migration
**Aufwand**: 15 Min

---

## B.2 Mistral OCR Integration

### B.2.1 MistralOCRService erstellen
**Datei**: `insight_core/services/mistral_ocr.py` (NEU)
**Aufwand**: 2 Std
```python
class MistralOCRService:
    async def extract_text(self, pdf_bytes: bytes) -> str
    async def is_available(self) -> bool
```

### B.2.2 Rate Limiter für Mistral
**Datei**: `insight_core/services/mistral_ocr.py`
**Aufwand**: 30 Min

### B.2.3 document_extraction.py: Mistral-Fallback einbauen
**Datei**: `insight_core/services/document_extraction.py`
**Aufwand**: 1 Std
```
Kette: pypdf → Mistral → Tesseract
```

---

## B.3 Automatische Extraktion

### B.3.1 TextExtractionQueue Service
**Datei**: `insight_core/services/text_extraction_queue.py` (NEU)
**Aufwand**: 1 Std

### B.3.2 extract_text_for_file Task
**Datei**: `insight_sync/tasks.py`
**Aufwand**: 30 Min

### B.3.3 Sync-Integration: Task nach upsert_file()
**Datei**: `ingestor/src/storage/database.py`
**Aufwand**: 1 Std

---

## B.4 Rohtext-Anzeige

### B.4.1 text_viewer.html Komponente
**Datei**: `templates/components/text_viewer.html` (NEU)
**Features**: Kopieren, Suchen, Highlighting
**Aufwand**: 1 Std

### B.4.2 Paper-Detail: Text-Viewer einbinden
**Datei**: `templates/pages/papers/detail.html`
**Aufwand**: 30 Min

### B.4.3 HTMX Lazy-Loading für große Texte
**Datei**: `insight_core/views.py`
**Aufwand**: 1 Std

---

# C. SUCHE & INDEXIERUNG (Hoch)

## C.1 Automatische Indexierung

### C.1.1 Django Signals für Indexierung
**Datei**: `insight_core/signals.py` (NEU)
**Aufwand**: 1 Std
```python
@receiver(post_save, sender=OParlPaper)
def index_paper(sender, instance, **kwargs):
    index_document.enqueue(...)
```

### C.1.2 signals.py in apps.py laden
**Datei**: `insight_core/apps.py`
**Aufwand**: 5 Min

### C.1.3 index_document Task
**Datei**: `insight_search/tasks.py` (NEU)
**Aufwand**: 30 Min

### C.1.4 batch_index Task
**Datei**: `insight_search/tasks.py`
**Aufwand**: 30 Min

---

## C.2 Fuzzy Search & Synonyme

### C.2.1 Deutsche Kommunal-Synonyme
**Datei**: `insight_search/synonyms.py` (NEU)
**Aufwand**: 1 Std
```python
GERMAN_MUNICIPAL_SYNONYMS = {
    "Stadtrat": ["Rat", "Gemeinderat"],
    "Bürgermeister": ["OB", "Oberbürgermeister"],
    ...
}
```

### C.2.2 setup_meilisearch Management Command
**Datei**: `insight_search/management/commands/setup_meilisearch.py` (NEU)
**Aufwand**: 1 Std
- Fuzzy Search konfigurieren
- Synonyme laden
- Searchable Attributes setzen

---

## C.3 Such-Verbesserungen

### C.3.1 Highlighting in search_service.py
**Datei**: `insight_core/services/search_service.py`
**Aufwand**: 30 Min
```python
"attributesToHighlight": ["name", "text_content"],
"highlightPreTag": "<mark>",
```

### C.3.2 Such-Template: Highlights anzeigen
**Datei**: `templates/pages/search/results.html`
**Aufwand**: 30 Min

### C.3.3 Suche in Datei-Texten aktivieren
**Datei**: `insight_core/services/search_service.py`
**Aufwand**: 30 Min

---

# D. SEO & SITEMAPS (Hoch)

## D.1 robots.txt

### D.1.1 robots_txt View
**Datei**: `insight_core/views.py`
**Aufwand**: 15 Min
```
Disallow: /work/
Disallow: /session/
Disallow: /admin/
```

### D.1.2 robots.txt URL-Route
**Datei**: `insight_core/urls.py`
**Aufwand**: 5 Min

---

## D.2 Meta-Tags & Open Graph

### D.2.1 SEOContext Dataclass
**Datei**: `insight_core/seo.py` (NEU)
**Aufwand**: 30 Min

### D.2.2 get_paper_seo() Helper
**Datei**: `insight_core/seo.py`
**Aufwand**: 20 Min

### D.2.3 get_meeting_seo() Helper
**Datei**: `insight_core/seo.py`
**Aufwand**: 15 Min

### D.2.4 base.html: SEO Meta-Block
**Datei**: `templates/base.html`
**Aufwand**: 30 Min
```html
<meta property="og:title" content="{{ seo.title }}">
```

### D.2.5 PaperDetailView: SEO-Context
**Datei**: `insight_core/views.py`
**Aufwand**: 15 Min

### D.2.6 MeetingDetailView: SEO-Context
**Datei**: `insight_core/views.py`
**Aufwand**: 15 Min

---

## D.3 Sitemaps

### D.3.1 StaticPagesSitemap Klasse
**Datei**: `insight_core/sitemaps.py` (NEU)
**Aufwand**: 30 Min

### D.3.2 PaperSitemap Klasse (pro Kommune)
**Datei**: `insight_core/sitemaps.py`
**Aufwand**: 30 Min

### D.3.3 MeetingSitemap, OrganizationSitemap, PersonSitemap
**Datei**: `insight_core/sitemaps.py`
**Aufwand**: 45 Min

### D.3.4 Sitemap-Index View
**Datei**: `insight_core/views.py`
**Aufwand**: 30 Min

### D.3.5 Sitemap URL-Routen
**Datei**: `insight_core/urls.py`
**Aufwand**: 15 Min

---

# E. CI/CD & DEPLOYMENT (Mittel)

## E.1 GitHub Actions

### E.1.1 build-and-publish.yml Workflow
**Datei**: `.github/workflows/build-and-publish.yml` (NEU)
**Aufwand**: 2 Std

### E.1.2 Multi-Arch Docker Build (amd64/arm64)
**Datei**: `.github/workflows/build-and-publish.yml`
**Aufwand**: 30 Min

### E.1.3 GitHub Release mit Changelog
**Datei**: `.github/workflows/build-and-publish.yml`
**Aufwand**: 30 Min

---

## E.2 Docker Images

### E.2.1 Dockerfile.django optimieren
**Datei**: `mandari/Dockerfile`
**Aufwand**: 1 Std

### E.2.2 Dockerfile.ingestor optimieren
**Datei**: `ingestor/Dockerfile`
**Aufwand**: 30 Min

### E.2.3 Vereinfachte docker-compose.yml für Self-Hosting
**Datei**: `docker-compose.yml`
**Aufwand**: 1 Std

---

## E.3 Install-Skript

### E.3.1 install.sh Hauptskript
**Datei**: `install.sh` (NEU)
**Aufwand**: 3 Std

### E.3.2 update.sh Skript
**Datei**: `update.sh` (NEU)
**Aufwand**: 30 Min

### E.3.3 backup.sh Skript
**Datei**: `backup.sh` (NEU)
**Aufwand**: 30 Min

### E.3.4 INSTALL.md Dokumentation
**Datei**: `INSTALL.md` (NEU)
**Aufwand**: 1 Std

---

# F. ARCHITEKTUR - SHARED LIBRARY (Niedrig)

## F.1 Package-Struktur

### F.1.1 shared/ Verzeichnis mit pyproject.toml
**Datei**: `shared/pyproject.toml` (NEU)
**Aufwand**: 30 Min

### F.1.2 OParlBaseSchema Pydantic Model
**Datei**: `shared/src/mandari_shared/oparl/schemas.py` (NEU)
**Aufwand**: 30 Min

### F.1.3 Alle 16 OParl-Schemas
**Datei**: `shared/src/mandari_shared/oparl/schemas.py`
**Aufwand**: 3 Std

### F.1.4 Event-Typen in shared
**Datei**: `shared/src/mandari_shared/events/types.py` (NEU)
**Aufwand**: 1 Std

---

## F.2 Adapter

### F.2.1 Django Adapter
**Datei**: `mandari/insight_core/adapters.py` (NEU)
**Aufwand**: 2 Std

### F.2.2 SQLAlchemy Adapter
**Datei**: `ingestor/src/storage/adapters.py` (NEU)
**Aufwand**: 2 Std

### F.2.3 Ingestor auf shared umstellen
**Datei**: `ingestor/src/sync/processor.py`
**Aufwand**: 3 Std

### F.2.4 sys.path.insert() Hack entfernen
**Datei**: `mandari/insight_sync/tasks.py`
**Aufwand**: 30 Min

---

# G. SPDX & COPYRIGHT (Niedrig)

### G.1 LICENSE-Datei erstellen
**Datei**: `LICENSE` (NEU mit AGPL-3.0-or-later Volltext)
**Aufwand**: 15 Min

### G.2 CONTRIBUTING.md: Copyright-Abschnitt
**Datei**: `CONTRIBUTING.md`
**Aufwand**: 30 Min

### G.3 DCO-Abschnitt hinzufügen
**Datei**: `CONTRIBUTING.md`
**Aufwand**: 20 Min

### G.4 add-spdx-headers.py Skript
**Datei**: `scripts/add-spdx-headers.py` (NEU)
**Aufwand**: 30 Min

### G.5 SPDX-Header zu allen Dateien hinzufügen
**Aktion**: Skript ausführen
**Aufwand**: 30 Min (automatisiert)

### G.6 DCO-Check GitHub Action
**Datei**: `.github/workflows/dco-check.yml` (NEU)
**Aufwand**: 15 Min

---

# PRIORISIERTE UMSETZUNGSREIHENFOLGE

## Sprint 1: Kritische Bugs & Quick Wins (1-2 Tage)
- [ ] A.1.1 Fix: size_human Mutation
- [ ] A.1.2 Fix: get_display_name() Prefetch
- [ ] A.1.3 Fix: O(n²) List-Check
- [ ] A.1.4 Fix: LocationMapping
- [ ] A.2.1-A.2.2 COUNT → Aggregate

## Sprint 2: Datenbank-Optimierung (1 Tag)
- [ ] A.3.1-A.3.3 Indizes erstellen
- [ ] A.2.3-A.2.6 Weitere Query-Fixes

## Sprint 3: Text-Extraktion Basis (2-3 Tage)
- [ ] B.1.1-B.1.2 Migrationen
- [ ] B.2.1-B.2.3 Mistral OCR
- [ ] B.3.1-B.3.3 Automatische Extraktion

## Sprint 4: Suche (2 Tage)
- [ ] C.1.1-C.1.4 Auto-Indexierung
- [ ] C.2.1-C.2.2 Synonyme & Fuzzy
- [ ] C.3.1-C.3.3 Highlighting

## Sprint 5: SEO (2 Tage)
- [ ] D.1.1-D.1.2 robots.txt
- [ ] D.2.1-D.2.6 Meta-Tags
- [ ] D.3.1-D.3.5 Sitemaps

## Sprint 6: CI/CD (3 Tage)
- [ ] E.1.1-E.1.3 GitHub Actions
- [ ] E.2.1-E.2.3 Docker
- [ ] E.3.1-E.3.4 Install-Skript

## Sprint 7: Cleanup (1-2 Tage)
- [ ] G.1-G.6 SPDX & Copyright
- [ ] B.4.1-B.4.3 UI für Rohtext

## Später / Backlog
- [ ] F.1-F.2 Shared Library (nur wenn nötig)
- [ ] A.4.1-A.4.2 View-Caching

---

# DATEI-ÜBERSICHT

## Neue Dateien (zu erstellen)
```
insight_core/
├── services/
│   ├── mistral_ocr.py              # B.2.1
│   └── text_extraction_queue.py    # B.3.1
├── seo.py                          # D.2.1
├── sitemaps.py                     # D.3.1
├── signals.py                      # C.1.1
└── migrations/
    ├── 0009_text_extraction.py     # B.1.1
    ├── 0010_body_slug.py           # B.1.2
    └── 0011_consultation_indexes.py # A.3.1

insight_search/
├── synonyms.py                     # C.2.1
├── tasks.py                        # C.1.3
└── management/commands/
    └── setup_meilisearch.py        # C.2.2

templates/
└── components/
    └── text_viewer.html            # B.4.1

.github/workflows/
├── build-and-publish.yml           # E.1.1
└── dco-check.yml                   # G.6

Root:
├── install.sh                      # E.3.1
├── update.sh                       # E.3.2
├── backup.sh                       # E.3.3
├── INSTALL.md                      # E.3.4
└── scripts/add-spdx-headers.py     # G.4
```

## Zu ändernde Dateien
```
insight_core/models.py              # A.1.1, A.1.2, A.1.4
insight_core/views.py               # A.2.3, B.4.3, D.1.1, D.2.5-6, D.3.4
insight_core/urls.py                # D.1.2, D.3.5
insight_core/apps.py                # C.1.2
insight_core/admin.py               # A.2.3
insight_core/services/document_extraction.py  # B.2.3
insight_core/services/search_service.py       # C.3.1, C.3.3

apps/work/faction/views.py          # A.2.1
apps/work/motions/views.py          # A.2.2
apps/work/meetings/views.py         # A.1.3
apps/work/admin.py                  # A.2.4
apps/tenants/management/commands/fix_permissions.py  # A.2.5

ingestor/src/storage/database.py    # B.3.3
insight_sync/tasks.py               # B.3.2

templates/base.html                 # D.2.4
templates/pages/papers/detail.html  # B.4.2
templates/pages/search/results.html # C.3.2

CONTRIBUTING.md                     # G.2, G.3
```
