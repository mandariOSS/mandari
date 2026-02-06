# Implementierungsplan: Text-Extraktion, SEO & Suche

## Übersicht

Dieser Plan adressiert 6 zusammenhängende Features für die Mandari-Plattform:

| Feature | Beschreibung | Priorität |
|---------|-------------|-----------|
| A | Text-Extraktion während Ingestion | Hoch |
| B | Rohtext-Anzeige auf Seiten | Mittel |
| C | Mistral OCR Integration | Hoch |
| D | SEO-Indexierung (nur Insight) | Hoch |
| E | Hierarchische Sitemap-Struktur | Hoch |
| F | Volltextsuche-Optimierung | Mittel |

---

## A) Text-Extraktion während Ingestion

### Aktueller Zustand
- Text-Extraktion ist **separat** vom Sync (`python manage.py extract_texts`)
- `OParlFile.text_content` bleibt nach Sync leer
- Manueller Eingriff erforderlich

### Lösung: Async Queue-basierte Extraktion

```
Sync → upsert_file() → Queue Extraction Task → Background Worker
                                                      ↓
                                              text_content gefüllt
                                                      ↓
                                              Meilisearch Index Update
```

### Zu erstellende/ändernde Dateien

**1. Neue Migration** (`insight_core/migrations/0009_text_extraction_tracking.py`)
```python
# Neue Felder für OParlFile:
text_extraction_status = CharField(choices=["pending", "processing", "completed", "failed"])
text_extraction_method = CharField()  # pypdf, tesseract, mistral
text_extraction_error = TextField()
```

**2. Neuer Service** (`insight_core/services/text_extraction_queue.py`)
- `TextExtractionQueue.queue_extraction(file: OParlFile)`
- `TextExtractionQueue.process_pending(batch_size=50)`

**3. Neue Tasks** (`insight_sync/tasks.py` erweitern)
```python
@task
def extract_text_for_file(file_id: UUID) -> dict

@task
def extract_texts_batch(body_id: UUID | None, limit: int = 100) -> dict
```

**4. Sync-Integration** (`ingestor/src/storage/database.py`)
- Nach `upsert_file()`: Task enqueuen wenn Datei neu/geändert

### Konfiguration
```python
# settings.py
TEXT_EXTRACTION_ENABLED = True
TEXT_EXTRACTION_ASYNC = True
TEXT_EXTRACTION_MAX_SIZE_MB = 50
```

---

## B) Rohtext-Anzeige auf Seiten

### Aktueller Zustand
- Paper-Detailseite hat bereits "Rohtext"-Tab
- Zeigt `file.text_content` in `<pre>`-Tag

### Verbesserungen

**1. Komponente** (`templates/components/text_viewer.html`)
```html
<div x-data="textViewer()" class="...">
    <!-- Zeichenanzahl -->
    <span>{{ char_count|intcomma }} Zeichen</span>

    <!-- Kopieren-Button -->
    <button @click="copyToClipboard()">Kopieren</button>

    <!-- Suche im Text -->
    <input x-model="searchQuery" placeholder="Im Text suchen...">

    <!-- Text mit Highlighting -->
    <pre x-html="highlightedText" class="whitespace-pre-wrap"></pre>
</div>
```

**2. Lazy Loading für große Texte** (`insight_core/views.py`)
- HTMX-Endpoint: `/insight/vorgaenge/<uuid>/text/<file_id>/`
- Paginierung für Texte > 100.000 Zeichen

**3. Zu ändernde Dateien**
- `templates/pages/papers/detail.html` - Text-Viewer einbinden
- `insight_core/views.py` - Lazy-Loading Endpoint
- `insight_core/urls.py` - Neue Route

---

## C) Mistral OCR Integration

### Aktueller Zustand
- Nur Tesseract OCR (lokal, langsam, mittelmäßige Qualität)
- Fallback-Kette: pypdf → Tesseract

### Neue Fallback-Kette
```
pypdf (schnell, nur Text-PDFs)
    ↓ falls leer
Mistral OCR (API, hohe Qualität)
    ↓ falls Rate-Limit oder Fehler
Tesseract (lokal, Fallback)
```

### Zu erstellende Dateien

**1. Mistral Service** (`insight_core/services/mistral_ocr.py`)
```python
class MistralOCRService:
    BASE_URL = "https://api.mistral.ai/v1/ocr"

    async def extract_text(self, pdf_bytes: bytes) -> str
    async def is_available(self) -> bool

    # Rate Limiting
    rate_limiter = RateLimiter(requests_per_minute=60)
```

**2. Document Extraction erweitern** (`insight_core/services/document_extraction.py`)
```python
async def _extract_text_from_pdf(data: bytes) -> tuple[str, int, str]:
    # 1. pypdf versuchen
    text, pages = _pypdf_extract(data)
    if text.strip():
        return text, pages, "pypdf"

    # 2. Mistral OCR (wenn konfiguriert)
    if settings.MISTRAL_API_KEY:
        try:
            text = await mistral_service.extract_text(data)
            return text, pages, "mistral"
        except RateLimitError:
            pass

    # 3. Tesseract Fallback
    text, _ = _tesseract_extract(data)
    return text, pages, "tesseract"
```

### Konfiguration
```python
# settings.py
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_OCR_RATE_LIMIT = 60  # pro Minute
```

---

## D) SEO-Indexierung (nur Insight)

### Aktueller Zustand
- **Kein robots.txt**
- **Keine Open Graph Tags**
- **Kein JSON-LD**
- Nur einfache meta description

### Zu blockierende Bereiche
```
/work/*      → Disallow (privat)
/session/*   → Disallow (privat)
/admin/*     → Disallow
/accounts/*  → Disallow
```

### Zu erstellende Dateien

**1. SEO Utilities** (`insight_core/seo.py`)
```python
@dataclass
class SEOContext:
    title: str
    description: str
    canonical_url: str
    og_type: str = "website"
    og_image: str | None = None
    json_ld: dict | None = None

def get_paper_seo(paper, request) -> SEOContext
def get_meeting_seo(meeting, request) -> SEOContext
def get_organization_seo(org, request) -> SEOContext
def get_person_seo(person, request) -> SEOContext
```

**2. robots.txt View** (`insight_core/views.py`)
```python
def robots_txt(request):
    content = """
User-agent: *
Allow: /
Allow: /insight/
Disallow: /work/
Disallow: /session/
Disallow: /admin/
Disallow: /accounts/

Sitemap: https://mandari.de/sitemap.xml
"""
    return HttpResponse(content, content_type="text/plain")
```

**3. Base Template erweitern** (`templates/base.html`)
```html
{% block seo_meta %}
<!-- Open Graph -->
<meta property="og:title" content="{{ seo.title }}">
<meta property="og:description" content="{{ seo.description }}">
<meta property="og:url" content="{{ seo.canonical_url }}">
<meta property="og:type" content="{{ seo.og_type }}">
<meta property="og:image" content="{{ seo.og_image }}">

<!-- Twitter -->
<meta name="twitter:card" content="summary_large_image">

<!-- Canonical -->
<link rel="canonical" href="{{ seo.canonical_url }}">

<!-- JSON-LD -->
{% if seo.json_ld %}
<script type="application/ld+json">{{ seo.json_ld|json_script }}</script>
{% endif %}
{% endblock %}
```

**4. Detail-Views erweitern**
- `PaperDetailView` → `context["seo"] = get_paper_seo(...)`
- `MeetingDetailView` → `context["seo"] = get_meeting_seo(...)`
- etc.

---

## E) Hierarchische Sitemap-Struktur

### Struktur
```
/sitemap.xml                           (Index)
├── /sitemap-pages.xml                 (Statische Seiten)
└── /sitemap-insight-<body-slug>.xml   (Pro Kommune)
    ├── Vorgänge (Papers)
    ├── Termine (Meetings)
    ├── Gremien (Organizations)
    └── Personen (Persons)
```

### Zu erstellende Dateien

**1. Sitemaps** (`insight_core/sitemaps.py`)
```python
from django.contrib.sitemaps import Sitemap

class StaticPagesSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.5

    def items(self):
        return [
            ("insight_core:home", 1.0),
            ("insight_core:produkt", 0.8),
            # ...
        ]

class PaperSitemap(Sitemap):
    changefreq = "monthly"
    limit = 50000  # Google-Limit

    def __init__(self, body_slug):
        self.body_slug = body_slug

    def items(self):
        return OParlPaper.objects.filter(
            body__slug=self.body_slug
        ).order_by("-date")[:self.limit]

    def location(self, paper):
        return f"/insight/vorgaenge/{paper.id}/"

# Analog: MeetingSitemap, OrganizationSitemap, PersonSitemap
```

**2. Sitemap Views** (`insight_core/views.py`)
```python
def sitemap_index(request):
    """Dynamischer Sitemap-Index mit allen Kommunen."""
    ...

def body_sitemap(request, body_slug):
    """Sitemap für eine Kommune."""
    ...
```

**3. URLs** (`insight_core/urls.py`)
```python
path("sitemap.xml", views.sitemap_index),
path("sitemap-pages.xml", views.static_sitemap),
path("sitemap-insight-<slug:body_slug>.xml", views.body_sitemap),
```

**4. Migration** - `OParlBody.slug` Feld hinzufügen
```python
slug = models.SlugField(max_length=100, unique=True)
```

### Caching
- Sitemaps 24h cachen
- Bei Sync-Abschluss invalidieren

---

## F) Volltextsuche-Optimierung

### Aktueller Zustand
- Meilisearch mit 5 Indizes (papers, meetings, persons, organizations, files)
- **Keine automatische Indexierung**
- Keine Fuzzy-Search-Konfiguration
- Keine deutschen Synonyme

### Verbesserungen

**1. Django Signals** (`insight_core/signals.py`)
```python
@receiver(post_save, sender=OParlPaper)
def index_paper(sender, instance, **kwargs):
    from insight_search.tasks import index_document
    index_document.enqueue("papers", instance.id, _paper_to_doc(instance))

@receiver(post_save, sender=OParlFile)
def index_file(sender, instance, **kwargs):
    if instance.text_content:
        index_document.enqueue("files", instance.id, _file_to_doc(instance))
```

**2. Synonyme** (`insight_search/synonyms.py`)
```python
GERMAN_MUNICIPAL_SYNONYMS = {
    "Stadtrat": ["Rat", "Gemeinderat"],
    "Bürgermeister": ["OB", "Oberbürgermeister"],
    "Ausschuss": ["Gremium", "Fachausschuss"],
    "Vorlage": ["Drucksache", "Antrag"],
    "Fraktion": ["Ratsfraktion"],
    # ...
}
```

**3. Meilisearch Setup Command** (`insight_search/management/commands/setup_meilisearch.py`)
```python
class Command(BaseCommand):
    def handle(self, *args, **options):
        client = get_meilisearch_client()

        for index_name in ALL_INDEXES:
            index = client.index(index_name)

            # Fuzzy Search (Typo-Toleranz)
            index.update_typo_tolerance({
                "enabled": True,
                "minWordSizeForTypos": {"oneTypo": 4, "twoTypos": 8}
            })

            # Synonyme
            index.update_synonyms(GERMAN_MUNICIPAL_SYNONYMS)

            # Searchable Attributes
            if index_name == "files":
                index.update_searchable_attributes([
                    "name", "file_name", "text_content"
                ])
```

**4. Highlighting** (`insight_core/services/search_service.py`)
```python
search_params = {
    # ... bestehende Parameter
    "attributesToHighlight": ["name", "text_content"],
    "highlightPreTag": "<mark>",
    "highlightPostTag": "</mark>",
}
```

---

## Implementierungsreihenfolge

```
Phase 1: Grundlagen
├── Migration für OParlFile-Tracking-Felder
├── Migration für OParlBody.slug
└── Mistral OCR Service erstellen

Phase 2: Text-Extraktion
├── Text-Extraction-Queue Service
├── Document-Extraction mit Mistral erweitern
├── Sync-Integration (Tasks nach upsert)
└── extract_texts Command aktualisieren

Phase 3: Suche
├── Django Signals für Auto-Indexierung
├── Synonyme definieren
├── setup_meilisearch Command
└── Highlighting in Search Service

Phase 4: SEO
├── seo.py Utilities
├── robots.txt View
├── base.html Meta-Tags
└── Detail-Views mit SEO-Context

Phase 5: Sitemaps
├── sitemaps.py
├── Sitemap Views
├── URL-Routen
└── Caching

Phase 6: UI
├── Text-Viewer Komponente
├── Lazy-Loading für große Texte
└── Suche im Dokument
```

---

## Kritische Dateien

| Datei | Änderung |
|-------|----------|
| `insight_core/models.py` | Neue Felder für OParlFile, OParlBody.slug |
| `insight_core/services/document_extraction.py` | Mistral OCR Integration |
| `insight_core/services/search_service.py` | Highlighting, Fuzzy Search |
| `ingestor/src/storage/database.py` | Task-Queue nach upsert |
| `templates/base.html` | SEO Meta-Tags Block |
| `insight_core/urls.py` | Sitemap, robots.txt Routes |

## Neue Dateien

| Datei | Zweck |
|-------|-------|
| `insight_core/services/mistral_ocr.py` | Mistral API Client |
| `insight_core/services/text_extraction_queue.py` | Queue-basierte Extraktion |
| `insight_core/seo.py` | SEO Context Utilities |
| `insight_core/sitemaps.py` | Sitemap-Klassen |
| `insight_core/signals.py` | Auto-Indexierung |
| `insight_search/synonyms.py` | Deutsche Kommunal-Synonyme |
| `templates/components/text_viewer.html` | Text-Anzeige-Komponente |

---

## Umgebungsvariablen

```bash
# Neu
MISTRAL_API_KEY=sk-...
TEXT_EXTRACTION_ASYNC=True
MEILISEARCH_AUTO_INDEX=True
```

## Verifikation

1. **Text-Extraktion**: `python manage.py sync_oparl` → Prüfen ob text_content gefüllt
2. **Mistral OCR**: Scanned PDF hochladen → OCR-Qualität prüfen
3. **SEO**: Google Rich Results Test mit Paper-URL
4. **Sitemap**: `curl https://mandari.de/sitemap.xml` → Struktur validieren
5. **Suche**: "Stdtrat" suchen → Fuzzy Match zu "Stadtrat"
