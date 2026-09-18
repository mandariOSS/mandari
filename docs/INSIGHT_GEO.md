# Geo-Verortung im Insight-Portal (Straßenverzeichnis, Umkreissuche, Korrektur)

Issue #54 (Ausbaustufe) auf Basis der produktiven Geo-Pipeline (Gazetteer, OParl-Orte,
automatischer Lauf). Diese Notiz beschreibt Module, Tabellen, Commands und Smoke-Tests.

## Datenmodell (`insight_core/models.py`)

| Modell | Tabelle | Zweck |
|--------|---------|-------|
| `Street` | `insight_streets` | Straßenverzeichnis aus OSM (highway-Ways mit Namen), Zentroid je Way. Wahrheitsquelle für Pass 1 der Georeferenzierung. |
| `Address` | `insight_addresses` | Hausnummern-Punkte aus OSM (`addr:street` + `addr:housenumber`), Index `(body, normalized_street, normalized_house_number)`. |
| `PaperLocation` | `insight_paper_locations` | Eine Zeile je Verortung eines Vorgangs mit `source` (Herkunft), `status` (auto/confirmed/removed) und Index `(body, latitude, longitude)`. |
| `OParlPaper.locations` | JSON | Bleibt das Anzeigeformat für Karte, Detailseite, Alerts. Wird aus `PaperLocation` heraus konsistent gehalten. |

Der Ingestor kennt die neuen Tabellen nicht (kein SQLAlchemy-Modell nötig; der
Schema-Contract vergleicht nur Tabellen, die der Ingestor beschreibt).

## Import aus OSM (`import_streets`)

```bash
python manage.py import_streets --body muenster                    # Straßen
python manage.py import_streets --body muenster --with-addresses   # Straßen + Hausnummern
python manage.py import_streets --all --with-addresses
```

Voraussetzung ist `osm_relation_id` an der Kommune. Die Adressabfrage nutzt
`nwr["addr:housenumber"]["addr:street"](area)` mit `out tags center`; Nodes liefern
`lat/lon` direkt, Ways/Relations den Mittelpunkt. Upsert per `(osm_type, osm_id)`.
`parse_address_element()` ist die testbare Parser-Funktion.

## Georeferenzierung (`services/georeferencing.py`, `services/gazetteer.py`)

1. Regex-Kandidaten und Volltextsuche gegen das Straßenverzeichnis (unverändert).
2. Neu: Treffer mit Hausnummer werden über `StreetGazetteer.refine()` /
   `resolve_address_point()` auf den OSM-Adresspunkt verfeinert. Gibt es die Adresse nicht,
   bleibt der Straßen-Zentroid (`precision: street`).
3. `update_paper_georef()` und `apply_oparl_locations()` rufen nach dem Speichern
   `sync_paper_locations()` auf, damit Tabelle und JSON übereinstimmen.

## Verortungs-Tabelle und Umkreissuche (`services/paper_locations.py`)

- `sync_paper_locations(paper)`: JSON → Tabelle, idempotent. Regeln:
  - Einträge im Umkreis von 50 m einer **entfernten** Zeile werden verworfen und aus dem JSON
    gestrichen (Sperrmerkmal; der automatische Lauf legt sie nicht wieder an).
  - **Bestätigte** Zeilen bleiben erhalten und werden ins JSON zurückgeschrieben, auch wenn ein
    neuer Lauf sie nicht mehr liefert.
  - Automatische Zeilen ohne JSON-Gegenstück werden gelöscht.
- `nearby_papers(body, lat, lon, radius_m, limit)`: Bounding-Box-Vorfilter auf dem Index,
  Haversine-Feinfilter als SQL-Ausdruck (`ACos/Cos/Sin/Radians`, läuft auf PostgreSQL und SQLite),
  nächster Punkt je Vorgang, sortiert nach Entfernung. Ersetzt den JSONB-Vollscan in
  `views/neighborhood.py`.
- Backfill nach der Migration: `python manage.py backfill_paper_locations [--body <slug>] [--dry-run]`.

Leistungsnachweis: `insight_core/tests/test_paper_locations.py::test_nearby_papers_scales_with_index`
legt 20 000 Verortungen an und prüft, dass die Umkreissuche mit einer Abfrage und deutlich unter
einer Sekunde antwortet (SQLite; auf PostgreSQL mit Index deutlich schneller).

## Nachbarschafts-Autocomplete (`services/neighborhood.py`)

`GET /insight/nachbarschaft/autocomplete/?q=…` sucht im eigenen Verzeichnis der aktiven Kommune:

- „Straße Hausnummer“ → Treffer aus `Address` (Präfix auf Straße und Hausnummer),
- sonst `Street` (eine Zeile je Straßenname, Zentroid gemittelt): Präfix-Treffer zuerst, dann
  Teilstring-Treffer, höchstens 10 Ergebnisse.
- Photon (`PHOTON_API_URL`) nur noch als Fallback, wenn die Kommune keine Straßen hat.

Antwortformat unverändert: `[{"name", "lat", "lon"}]`.

## Admin-Korrektur-Workflow (`insight_core/admin.py`)

- **Verortungen** (`PaperLocationAdmin`): Liste mit Herkunft (`OParl-Ort`, `Adresse`, `Straße`,
  `KI`, `Manuell`), Prüfstatus, Vorgang, Koordinaten. Zeilenaktionen „Bestätigen“ / „Entfernen“
  (ein Klick je Zeile), dieselben Aktionen auf der Detailseite und als Sammelaktion.
- **Vorgang** (`OParlPaperAdmin`): Inline „Verortungen“ (nur lesend, Änderungslink zur Verortung),
  Sammelaktion „Verortungen aus dem JSON neu aufbauen“.
- „Entfernen“ setzt `status=removed`, streicht den Punkt aus `paper.locations` und sperrt ihn für
  spätere Läufe. „Bestätigen“ einer entfernten Verortung hebt die Sperre auf.

## Kommunen ohne OSM-Zuordnung

- `python manage.py check_body_geodata [--all]` listet je Kommune: OSM-Relation-ID, AGS,
  Bounding-Box, Anzahl Straßen und Adressen sowie die fehlenden Punkte. Ändert nichts.
- Admin → Kommunen: Spalten „OSM“ und „AGS“, Filter „Geo-Zuordnung“ (ohne OSM-Relation oder AGS,
  ohne Straßenverzeichnis, vollständig).
- Pflegeweg: Relation-ID/AGS im Admin eintragen → `fetch_osm_geodata --all` (Zentrum, BBox) →
  `import_streets --all --with-addresses` → `extract_locations` bzw. automatischer Lauf.

## Smoke-Test

```bash
cd mandari
pytest insight_core/tests/test_paper_locations.py insight_core/tests/test_geo_import_addresses.py \
       insight_core/tests/test_georef_addresses.py insight_core/tests/test_neighborhood_autocomplete.py \
       insight_core/tests/test_admin_paper_locations.py insight_core/tests/test_geo_coverage.py
```

## Offen

- `generate_alerts` (Nachbarschafts-Abos) rechnet weiterhin per JSONB-Haversine; Umstellung auf
  `PaperLocation` ist ein Folgeschritt.
- Alternative Geo-Feld im Elasticsearch-Index wurde nicht umgesetzt (Tabelle mit Index reicht für
  das Ziel < 300 ms).
