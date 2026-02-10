# Mandari Insight - Feature Status & Roadmap

> **Zuletzt aktualisiert**: 2026-02-10 (Gremien: Tabellarische Liste, Detail-Tabs, Rat-Sonderfall)
> **Aktualisiert durch**: Claude Code (automatisch nach jeder Implementierung)
>
> **Anweisung an AI-Agenten**: Nach Abschluss jeder Aufgabe, die ein Feature in dieser Liste betrifft,
> MUSS diese Datei aktualisiert werden: Status, Fortschritt, ggf. neue Einträge.

---

## Status-Legende

| Symbol | Bedeutung |
|--------|-----------|
| :white_check_mark: | Vollständig implementiert & funktional |
| :construction: | In Umsetzung / teilweise implementiert |
| :clipboard: | Geplant, noch nicht begonnen |
| :no_entry: | Blockiert / benötigt Vorarbeit |

---

## Gesamtübersicht

| # | Feature | Status | Fortschritt | Priorität |
|---|---------|--------|-------------|-----------|
| 1 | [RIS-Datenspiegelung (OParl)](#1-ris-datenspiegelung-oparl) | :white_check_mark: | 100% | - |
| 2 | [Volltextsuche & Fuzzy Search](#2-volltextsuche--fuzzy-search) | :white_check_mark: | 100% | - |
| 3 | [Text-Extraktion & OCR](#3-text-extraktion--ocr) | :white_check_mark: | 100% | - |
| 4 | [SEO & Sitemaps](#4-seo--sitemaps) | :white_check_mark: | 100% | - |
| 5 | [KI-Zusammenfassungen](#5-ki-zusammenfassungen) | :white_check_mark: | 100% | - |
| 6 | [KI-Chatbot (RAG)](#6-ki-chatbot-rag) | :construction: | 10% | HOCH |
| 7 | [Statistiken & Dashboard](#7-statistiken--dashboard) | :construction: | 40% | MITTEL |
| 8 | [Georeferenzierung & Karte](#8-georeferenzierung--karte) | :construction: | 50% | HOCH |
| 9 | [Semantische Suche (Vektor)](#9-semantische-suche-vektor) | :white_check_mark: | 100% | MITTEL |
| 10 | [Straßensuche (frankfurt-gestalten)](#10-straßensuche-frankfurt-gestalten-stil) | :clipboard: | 0% | HOCH |
| 11 | [Ratsfragen (Abgeordnetenwatch)](#11-ratsfragen-abgeordnetenwatch-stil) | :clipboard: | 0% | HOCH |
| 12 | [Newsletter (Nachbarschafts-Updates)](#12-newsletter-nachbarschafts-updates) | :clipboard: | 0% | HOCH |
| 13 | [Benutzerkonten & Abonnements](#13-benutzerkonten--abonnements) | :construction: | 50% | HOCH |
| 14 | [Sidebar-Layout (Insight Portal)](#14-sidebar-layout-insight-portal) | :white_check_mark: | 100% | - |

---

## Detailbeschreibung

### 1. RIS-Datenspiegelung (OParl)

**Status**: :white_check_mark: Vollständig | **Fortschritt**: 100%

Mandari Insight spiegelt OParl-Ratsinformationssysteme (1.0 + 1.1) und stellt sie als öffentliches Portal bereit.

| Komponente | Status | Details |
|------------|--------|---------|
| Ingestor (Metadata-Sync) | :white_check_mark: | Async Python, 20 concurrent requests, inkrementell + full |
| Body (Kommune) | :white_check_mark: | Portal-Startseite mit Body-Auswahl |
| Organizations (Gremien) | :white_check_mark: | Tabellarische Liste (Aktiv/Alle), Detail mit Tabs (Mitglieder, Sitzungskalender, Infos), Rat-Sonderfall (Ratsmitglieder/Weitere getrennt, OB+BM+Fraktionsvorsitz bei Ratsmitgliedern), ehemalige Mitglieder collapsible |
| Persons (Personen) | :white_check_mark: | Liste + Detail, Mitgliedschaften, Suche |
| Meetings (Sitzungen) | :white_check_mark: | Liste + Detail, Kalender (FullCalendar), TOPs |
| Papers (Vorgänge) | :white_check_mark: | Liste + Detail, Filter nach Typ, Beratungsfolge |
| Files (Dokumente) | :white_check_mark: | In Paper-Detail eingebettet, Text-Viewer, Dokument-Viewer (iframe Modal) |
| Dokument-Kontext | :white_check_mark: | Gremium, Sitzungsdatum, TOP bei Dateien (Dateiliste, Paper-Detail, Suche, Viewer-Modal) |
| Dokument-Viewer | :white_check_mark: | iframe-Modal für PDF-Vorschau, überall nutzbar (Dateiliste, Paper-Detail, Suche) |
| AgendaItems (TOPs) | :white_check_mark: | In Meeting-Detail eingebettet |
| Consultations (Beratungen) | :white_check_mark: | Paper ↔ Meeting-Verknüpfung aufgelöst |
| Memberships | :white_check_mark: | In Person- und Organization-Detail |
| Multi-Body-Support | :white_check_mark: | Session-basierte Body-Auswahl |
| HTMX-Partials | :white_check_mark: | Dynamisches Nachladen aller Listen |

**Dateien**: `insight_core/views.py`, `insight_core/models.py`, `insight_core/urls.py`, `templates/pages/`

**Getestete OParl-Server**: Münster (1.1), Bonn, Aachen (ITK Rheinland), Berlin Pankow (1.0), München Transparent (1.0)

---

### 2. Volltextsuche & Fuzzy Search

**Status**: :white_check_mark: Vollständig | **Fortschritt**: 100%

| Komponente | Status | Details |
|------------|--------|---------|
| Meilisearch-Integration | :white_check_mark: | 5 Indexe: papers, meetings, persons, organizations, files |
| Fuzzy Search (Typo-Toleranz) | :white_check_mark: | 1 Typo ab 4 Zeichen, 2 Typos ab 8 Zeichen |
| Deutsche Synonyme | :white_check_mark: | 110+ Synonymgruppen (Stadtrat↔Rat, ÖPNV↔Nahverkehr, etc.) |
| Dateiinhalt-Suche | :white_check_mark: | `text_content` in Files-Index, Highlighting |
| Auto-Indexierung (Django) | :white_check_mark: | Signals bei Model-Save → Meilisearch |
| Auto-Indexierung (Ingestor) | :white_check_mark: | Post-Sync Phase 3 → Meilisearch Bulk |
| Auto-Index-Setup (Ingestor) | :white_check_mark: | `ensure_index_settings()` vor Phase 3 (idempotent) |
| Auto-Index-Setup (Docker) | :white_check_mark: | `setup_meilisearch` in docker-entrypoint.sh |
| Auto-Index-Setup (Install) | :white_check_mark: | `setup_meilisearch` in install.sh |
| Reindex-Command | :white_check_mark: | `manage.py reindex_meilisearch [--index] [--body] [--clear]` |
| Such-UI | :white_check_mark: | `/insight/suche/`, Filter nach Typ, HTMX-Ergebnisse |
| Highlighting | :white_check_mark: | HTML-Markierung in Treffern |
| Fallback (ORM) | :white_check_mark: | Django-ORM-Suche wenn Meilisearch offline |
| Semantische Suche (Vektor) | :white_check_mark: | Hybrid Search via Meilisearch v1.12 + BAAI/bge-m3 → [#9](#9-semantische-suche-vektor) |

**Dateien**: `insight_search/`, `insight_core/signals.py`, `insight_core/services/search_service.py`, `insight_core/services/search_documents.py`

---

### 3. Text-Extraktion & OCR

**Status**: :white_check_mark: Vollständig | **Fortschritt**: 100%

| Komponente | Status | Details |
|------------|--------|---------|
| pypdf (Text-PDFs) | :white_check_mark: | Schnell, erste Wahl |
| Mistral OCR (Cloud AI) | :white_check_mark: | Pixtral Vision, Rate-Limiting, `MISTRAL_API_KEY` |
| Tesseract OCR (lokal) | :white_check_mark: | Deutsch (`deu`), Fallback |
| Fallback-Kette | :white_check_mark: | pypdf → Mistral → Tesseract, automatisch |
| Django-Command | :white_check_mark: | `manage.py extract_texts` |
| Ingestor-Extraktion | :white_check_mark: | Post-Sync Phase 2, async, concurrent |
| Status-Tracking | :white_check_mark: | pending/processing/completed/failed/skipped |
| Text-Viewer UI | :white_check_mark: | Suche im Text, Highlighting |
| SHA256-Checksummen | :white_check_mark: | Deduplizierung |

**Dateien**: `insight_core/services/document_extraction.py`, `insight_core/services/mistral_ocr.py`, `ingestor/src/extraction/extractor.py`

---

### 4. SEO & Sitemaps

**Status**: :white_check_mark: Vollständig | **Fortschritt**: 100%

| Komponente | Status | Details |
|------------|--------|---------|
| robots.txt | :white_check_mark: | Dynamisch generiert |
| Sitemap-Index | :white_check_mark: | `/sitemap.xml` → pro Kommune |
| Per-Body Sitemaps | :white_check_mark: | `/sitemap-insight-<slug>.xml` |
| Open Graph Tags | :white_check_mark: | In `base.html` |
| Twitter Cards | :white_check_mark: | In `base.html` |
| JSON-LD (Schema.org) | :white_check_mark: | Strukturierte Daten |
| SEO-Context-Generatoren | :white_check_mark: | Pro Entitätstyp |

**Dateien**: `insight_core/seo.py`, `insight_core/sitemaps.py`, `templates/base.html`

---

### 5. KI-Zusammenfassungen

**Status**: :white_check_mark: Vollständig | **Fortschritt**: 100%

| Komponente | Status | Details |
|------------|--------|---------|
| Summary-Service | :white_check_mark: | `insight_ai/services/summarizer.py` |
| Nebius/Kimi K2 Provider | :white_check_mark: | `insight_ai/providers/nebius.py`, 32k Token |
| Multi-Perspektiven-Prompt | :white_check_mark: | `insight_ai/services/prompts.py` |
| Caching (DB) | :white_check_mark: | `OParlPaper.summary` Feld |
| HTMX-Endpoint | :white_check_mark: | `/insight/vorgaenge/<pk>/zusammenfassung/` |
| Auto-Textextraktion | :white_check_mark: | Extrahiert PDFs on-demand falls nötig |
| Fehlerbehandlung | :white_check_mark: | NoTextContent, APINotConfigured, SummaryError |

**Dateien**: `insight_ai/`

---

### 6. KI-Chatbot (RAG)

**Status**: :construction: Stub | **Fortschritt**: 10%

Ein KI-Chatbot, der Bürger:innen Fragen zu kommunalpolitischen Daten beantwortet. Nutzt RAG (Retrieval Augmented Generation) um Antworten mit echten Quellen aus dem RIS zu belegen.

| Komponente | Status | Details |
|------------|--------|---------|
| Chat-View | :white_check_mark: | `/insight/chat/`, Template existiert |
| Chat-API-Endpoint | :white_check_mark: | `/insight/chat/api/message/`, JSON-basiert |
| DSGVO-Consent | :white_check_mark: | Session-basierte Zustimmung |
| LLM-Integration | :clipboard: | TODO: Groq/Nebius API anbinden |
| RAG-Pipeline | :clipboard: | Meilisearch-Kontext → LLM-Prompt |
| Streaming-Responses | :clipboard: | SSE für Echtzeit-Ausgabe |
| Quellennachweise | :clipboard: | Verlinkte RIS-Dokumente in Antworten |
| Konversations-Kontext | :clipboard: | Session-basierte Chat-Historie |
| Konto-Pflicht / Abo | :no_entry: | Benötigt [#13 Benutzerkonten](#13-benutzerkonten--abonnements) |

**Abhängigkeiten**: Semantische Suche (#9) würde Qualität stark verbessern. Konto/Abo (#13) für Zugangssteuerung.

**Dateien**: `insight_core/views.py:1167-1205` (Stub), `templates/pages/chat.html`

---

### 7. Statistiken & Dashboard

**Status**: :construction: Basis | **Fortschritt**: 40%

| Komponente | Status | Details |
|------------|--------|---------|
| Entity-Counts pro Body | :white_check_mark: | Startseite: Gremien, Personen, Sitzungen, Vorgänge |
| Kommende Sitzungen (Top 5) | :white_check_mark: | Startseite |
| Aktuelle Vorgänge (Top 5) | :white_check_mark: | Startseite |
| Vorgänge nach Typ (Chart) | :clipboard: | Kuchendiagramm / Balkendiagramm |
| Sitzungen nach Monat (Chart) | :clipboard: | Zeitverlauf |
| Aktivitäts-Timeline | :clipboard: | Chronologische Übersicht aller Änderungen |
| Top-Gremien nach Aktivität | :clipboard: | Ranking nach Sitzungshäufigkeit |
| Abstimmungsstatistiken | :clipboard: | Benötigt Abstimmungsdaten (selten in OParl) |

**Dateien**: `insight_core/views.py:63-111` (PortalHomeView)

---

### 8. Georeferenzierung & Karte

**Status**: :construction: Basis-Karte | **Fortschritt**: 50%

| Komponente | Status | Details |
|------------|--------|---------|
| Karten-View | :white_check_mark: | `/insight/karte/`, Leaflet.js |
| GeoJSON-Endpoint | :white_check_mark: | `/insight/karte/partials/markers/` |
| DSGVO-Tile-Proxy | :white_check_mark: | OSM-Kacheln über eigenen Server, DB-Cache |
| Body-Geokoordinaten | :white_check_mark: | lat/lon, Bounding Box, OSM-Relation |
| LocationMapping-Model | :white_check_mark: | Ortsname → Koordinaten (manuell) |
| Papers auf Karte | :white_check_mark: | Vorgänge der letzten 4 Wochen mit Geodaten |
| KI-Georeferenzierung | :clipboard: | Automatische Ortsextraktion aus Dokumenten per LLM |
| Geocoding-Service | :clipboard: | Nominatim/OSM-Integration für Adress→Koordinaten |
| Nachbarschafts-Zuordnung | :clipboard: | Stadtteile/Quartiere als Polygone, Zuordnung |
| Reverse Geocoding | :clipboard: | Koordinaten → Stadtteil/Straße |

**Kernidee**: Dokumente enthalten oft Straßennamen und Ortsbezeichnungen ("Spielplatz Hansaring", "Bebauungsplan Wolbecker Straße"). Per KI-Scan (LLM oder NER) sollen diese Orte automatisch extrahiert und geocodiert werden, um Vorgänge einer Nachbarschaft zuzuordnen.

**Abhängigkeiten**: KI-Georeferenzierung benötigt Text-Extraktion (:white_check_mark: erledigt) + LLM-Pipeline.

**Dateien**: `insight_core/views.py` (MapView, map_markers), `insight_core/models.py` (LocationMapping, OParlBody Geo-Felder), `templates/pages/map.html`

---

### 9. Semantische Suche (Vektor)

**Status**: :white_check_mark: Vollständig | **Fortschritt**: 100%

Hybrid Search (Keyword + Vektor) über Meilisearch v1.12 Built-in HuggingFace Embedder (BAAI/bge-m3). Paper-Boosting: Papers werden gefunden, wenn der Suchbegriff nur in ihren Dateien (PDFs) vorkommt.

| Komponente | Status | Details |
|------------|--------|---------|
| Embedding-Modell | :white_check_mark: | BAAI/bge-m3 via Meilisearch HuggingFace Embedder |
| Meilisearch Upgrade | :white_check_mark: | v1.6 → v1.12 (stabile Embedder-API) |
| Hybrid Search | :white_check_mark: | Keyword + Vektor kombiniert, `semanticRatio=0.5` (konfigurierbar) |
| Paper-Boosting | :white_check_mark: | `file_contents_preview` in Paper-Dokumenten (max 25KB aus PDF-Text) |
| Embedder pro Index | :white_check_mark: | papers, files, meetings, persons, organizations |
| Graceful Degradation | :white_check_mark: | Fallback auf Keyword-Only wenn Embedder nicht konfiguriert |
| Paper Re-Index bei File-Update | :white_check_mark: | Signal: File-Save → Parent-Paper neu indexiert |
| Django Settings | :white_check_mark: | `MEILISEARCH_EMBEDDING_MODEL`, `MEILISEARCH_SEMANTIC_RATIO` |
| Ingestor Settings | :white_check_mark: | `meilisearch_embedding_model`, `meilisearch_semantic_ratio` |

**Dateien**: `setup_meilisearch.py`, `search_service.py`, `search_documents.py`, `signals.py`, `reindex_meilisearch.py`, `ingestor/src/indexing/meilisearch.py`, `ingestor/src/indexing/document_builders.py`

**Abhängigkeiten**: Verbessert #6 (Chatbot RAG) und #10 (Straßensuche) erheblich.

---

### 10. Straßensuche (frankfurt-gestalten-Stil)

**Status**: :clipboard: Geplant | **Fortschritt**: 0%

Bürger:innen geben ihre Straße ein und sehen alle relevanten Vorgänge in ihrer Nachbarschaft — wie bei [frankfurt-gestalten.de](https://frankfurt-gestalten.de).

| Komponente | Status | Details |
|------------|--------|---------|
| Straßen-Autovervollständigung | :clipboard: | Input mit Vorschlägen (Nominatim/eigene DB) |
| Straße → Koordinaten | :clipboard: | Geocoding-Service |
| Radius-/Stadtteil-Suche | :clipboard: | "Alle Vorgänge im Umkreis von 500m" |
| Ergebnis-Karte | :clipboard: | Gefilterte Kartenansicht |
| Ergebnis-Liste | :clipboard: | Chronologische Liste mit Relevanz |
| Stadtteil-Auswahl | :clipboard: | Alternativ: Stadtteil aus Dropdown wählen |
| Benachrichtigung | :clipboard: | "Benachrichtige mich bei neuen Vorgängen hier" → #12 |

**Abhängigkeiten**: Benötigt #8 (Georeferenzierung) für automatische Orts-Zuordnung von Dokumenten.

**Referenz**: [frankfurt-gestalten.de](https://frankfurt-gestalten.de) — zeigt kommunale Vorgänge nach Straße/Stadtteil.

---

### 11. Ratsfragen (Abgeordnetenwatch-Stil)

**Status**: :clipboard: Geplant | **Fortschritt**: 0%

Bürger:innen können öffentliche Fragen an **Ratsmitglieder** (nicht alle RIS-Personen) stellen. Antworten sind öffentlich sichtbar — wie bei [abgeordnetenwatch.de](https://abgeordnetenwatch.de).

| Komponente | Status | Details |
|------------|--------|---------|
| Ratsmitglied-Flag | :clipboard: | Unterscheidung: Ratsmitglied vs. Sachkundige/r vs. Verwaltung |
| Frage-Model | :clipboard: | `PublicQuestion`: Autor, Empfänger, Text, Status |
| Antwort-Model | :clipboard: | `PublicAnswer`: Text, Datum, öffentlich |
| Frage stellen (UI) | :clipboard: | Formular auf Person-Detail, mit DSGVO-Hinweis |
| Moderation | :clipboard: | Freischaltung vor Veröffentlichung (Spam, Beleidigung) |
| Öffentliche Anzeige | :clipboard: | Fragen + Antworten auf Personen-Profil |
| E-Mail-Benachrichtigung | :clipboard: | Ratsmitglied wird per E-Mail über neue Frage informiert |
| Antwort-Erinnerung | :clipboard: | Automatische Erinnerung nach X Tagen ohne Antwort |
| Statistik | :clipboard: | Antwortquote pro Ratsmitglied |
| Konto-Pflicht (Fragesteller) | :no_entry: | Benötigt [#13 Benutzerkonten](#13-benutzerkonten--abonnements) |

**Abgrenzung**: Nur Ratsmitglieder (Mitglied im Gremium "Rat") können adressiert werden. Verwaltungsmitarbeiter und reine OParl-Personen (z.B. Sachbearbeiter) sind ausgeschlossen.

**Abhängigkeiten**: Benutzerkonten (#13) für Fragesteller, E-Mail-Infrastruktur.

---

### 12. Newsletter (Nachbarschafts-Updates)

**Status**: :clipboard: Geplant | **Fortschritt**: 0%

Regelmäßige E-Mails an Insight-Nutzer:innen über neue kommunale Vorgänge in ihrer Nachbarschaft.

| Komponente | Status | Details |
|------------|--------|---------|
| Newsletter-Abo-Model | :clipboard: | Nutzer, Body, Stadtteil/Straße, Frequenz |
| E-Mail-Template | :clipboard: | HTML-Mail mit neuen Vorgängen, Sitzungen |
| Frequenz-Optionen | :clipboard: | Täglich, wöchentlich, monatlich |
| Stadtteil-Filter | :clipboard: | Nur Vorgänge aus der eigenen Nachbarschaft |
| Themen-Filter | :clipboard: | z.B. nur Bildung, nur Verkehr, nur Umwelt |
| KI-Zusammenfassung | :clipboard: | Kurze Zusammenfassung der wichtigsten Vorgänge |
| Abmelde-Link | :clipboard: | DSGVO-konforme Abmeldung |
| Versand-Service | :clipboard: | Celery/Django-Q Task oder externer Dienst |
| Konto-Pflicht | :no_entry: | Benötigt [#13 Benutzerkonten](#13-benutzerkonten--abonnements) |

**Abhängigkeiten**: Georeferenzierung (#8) für Nachbarschafts-Zuordnung, Benutzerkonten (#13), E-Mail-Infrastruktur.

---

### 13. Benutzerkonten & Abonnements

**Status**: :construction: Konten vorhanden, Abos fehlen | **Fortschritt**: 50%

| Komponente | Status | Details |
|------------|--------|---------|
| User-Model (Custom) | :white_check_mark: | E-Mail-Auth, UUID, Avatar |
| Login / Registrierung | :white_check_mark: | Nur per Einladung (aktuell) |
| 2FA (TOTP) | :white_check_mark: | Authenticator-App |
| Trusted Devices | :white_check_mark: | "Gerät merken" (30 Tage) |
| Session Management | :white_check_mark: | Aktive Sitzungen, IP/Standort |
| Rate Limiting | :white_check_mark: | 5 Versuche / 15 Min |
| Password Reset | :white_check_mark: | Token-basiert, 24h gültig |
| Öffentliche Registrierung | :clipboard: | Aktuell nur Einladung — Self-Service nötig |
| Insight-Profil | :clipboard: | Öffentliches Profil für Insight-Nutzer:innen |
| Abo-Modell | :clipboard: | Free / Premium Tiers |
| Payment-Integration | :clipboard: | Stripe oder Mollie |
| Feature-Gates | :clipboard: | KI-Chatbot, KI-Zusammenfassungen hinter Abo |
| Usage-Tracking | :clipboard: | API-Calls, Zusammenfassungen pro Monat |

**Offene Frage**: Welche Features sind kostenlos, welche im Abo?

| Feature | Free | Premium (Vorschlag) |
|---------|------|---------------------|
| RIS-Daten durchsuchen | :white_check_mark: | :white_check_mark: |
| Volltextsuche | :white_check_mark: | :white_check_mark: |
| Karte | :white_check_mark: | :white_check_mark: |
| Ratsfragen stellen | :white_check_mark: | :white_check_mark: |
| KI-Zusammenfassungen | 3/Tag | Unbegrenzt |
| KI-Chatbot | :no_entry: | :white_check_mark: |
| Newsletter | :white_check_mark: (wöchentlich) | Täglich + Themenfilter |
| Straßensuche + Alerts | :white_check_mark: | :white_check_mark: |

---

### 14. Sidebar-Layout (Insight Portal)

**Status**: :white_check_mark: Vollständig | **Fortschritt**: 100%

Alle Insight-Seiten wurden vom Top-Navbar-Layout (base.html → navbar.html + footer.html) auf ein modernes Sidebar-Layout umgestellt. Marketing-Seiten (Impressum, Datenschutz) bleiben unverändert auf base.html.

| Komponente | Status | Details |
|------------|--------|---------|
| base_insight.html | :white_check_mark: | Standalone Base-Template mit Sidebar-Layout, Dark Mode, Alpine.js |
| insight_sidebar.html | :white_check_mark: | 260px Sidebar: Navigation, Kommune-Switcher, User, OParl-Info |
| city_search_modal.html | :white_check_mark: | Command-Palette Kommunen-Suche mit Client-Filterung |
| insight_footer.html | :white_check_mark: | Minimaler Footer (Mandari + Links) |
| Responsive (Mobile) | :white_check_mark: | Sidebar hidden auf Mobile, Hamburger-Menü, Overlay |
| Dark Mode | :white_check_mark: | Toggle in Top-Bar, localStorage persistent |
| Active-State Navigation | :white_check_mark: | Sidebar-Link aktiv per request.resolver_match.url_name |
| Meeting-Badge | :white_check_mark: | upcoming_meeting_count in context_processors.py |
| portal/home.html redesign | :white_check_mark: | Hero+Search, Meeting-Grid, Papers-Liste (variant_2 Design) |
| 15 Templates umgestellt | :white_check_mark: | extends base_ris.html/base.html → base_insight.html |
| Full-Width Layout | :white_check_mark: | Alle Insight-Seiten nutzen volle Breite (kein max-w-7xl) |
| Dokument-Viewer | :white_check_mark: | document_viewer.html in base_insight.html integriert |
| SEO erhalten | :white_check_mark: | Meta-Tags, JSON-LD, OG-Tags im head |

**Dateien**: `templates/base_insight.html`, `templates/components/insight_sidebar.html`, `templates/components/city_search_modal.html`, `templates/components/insight_footer.html`, `insight_core/context_processors.py`

---

## Abhängigkeitsgraph

```
#3 Text-Extraktion (erledigt)
 └─► #2 Dateiinhalt-Suche (erledigt)
 └─► #5 KI-Zusammenfassungen (erledigt)
 └─► #8 KI-Georeferenzierung
      └─► #10 Straßensuche
           └─► #12 Newsletter (Nachbarschaft)

#9 Semantische Suche
 └─► #6 KI-Chatbot (RAG-Qualität)

#13 Benutzerkonten & Abos
 └─► #6 KI-Chatbot (Zugang)
 └─► #11 Ratsfragen (Fragesteller)
 └─► #12 Newsletter (Empfänger)
```

---

## Empfohlene Umsetzungsreihenfolge

| Phase | Features | Begründung |
|-------|----------|------------|
| **Phase 1** | #13 Öffentliche Registrierung | Basis für alle nutzerbezogenen Features |
| **Phase 2** | #8 KI-Georeferenzierung | Voraussetzung für Straßensuche + Newsletter |
| **Phase 3** | #10 Straßensuche | Killer-Feature für Bürger:innen-Engagement |
| **Phase 4** | #6 KI-Chatbot (RAG) | Aufwändig, aber hoher Wow-Faktor |
| **Phase 5** | #11 Ratsfragen | Community-Feature, braucht Moderation |
| **Phase 6** | #12 Newsletter | Bindung & regelmäßige Nutzung |
| **Phase 7** | #9 Semantische Suche | Qualitätsverbesserung für Suche + Chatbot |
| **Phase 8** | #7 Statistiken erweitern | Nice-to-have, Charts & Trends |

---

## Technische Infrastruktur-Status

| Komponente | Status | Details |
|------------|--------|---------|
| Django 6.0 | :white_check_mark: | Backend |
| PostgreSQL 16 | :white_check_mark: | Primäre Datenbank |
| Meilisearch | :white_check_mark: | Volltextsuche |
| Redis | :white_check_mark: | Cache, Events, Sessions |
| HTMX + Alpine.js | :white_check_mark: | Frontend-Interaktivität |
| Tailwind CSS | :white_check_mark: | Styling |
| Ingestor (Python async) | :white_check_mark: | OParl-Sync |
| insight_ai (Nebius) | :white_check_mark: | KI-Zusammenfassungen |
| Tesseract OCR | :white_check_mark: | Lokale Text-Extraktion |
| Celery / Task Queue | :clipboard: | Für Newsletter, Bulk-Extraktion |
| Stripe / Mollie | :clipboard: | Für Abo-Zahlungen |
| Vektor-DB (Meilisearch Embedder) | :white_check_mark: | Hybrid Search via BAAI/bge-m3, Meilisearch v1.12 |
