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

## Neue Kommune: Geo-Daten automatisch zuordnen (`resolve_body_geodata`, #351)

Beim Freischalten einer Quelle genügt ein Befehl, sobald der Ingestor die Körperschaften
angelegt hat. Erst den Probelauf ansehen, dann ausführen:

```bash
python manage.py resolve_body_geodata --source <UUID oder Teil der URL> --dry-run
python manage.py resolve_body_geodata --source <UUID oder Teil der URL>
python manage.py resolve_body_geodata --body <UUID|Slug>          # einzelne Kommune
python manage.py resolve_body_geodata --all --no-import           # nur zuordnen, nichts laden
```

Ablauf je Körperschaft (übergeordnete zuerst, z. B. die Verbandsgemeinde vor ihren Ortsgemeinden):

1. **Ohne eigenes Gebiet:** Namen mit Zweckverband, Schulverband, GmbH, AG, AöR, Anstalt,
   Eigenbetrieb, Gesellschaft, Stadtwerke, Waldgemark(ung), Stiftung, Sparkasse oder Beirat werden
   als „Keine Gebietskörperschaft“ gekennzeichnet. „Gebiet von“ zeigt auf die engste übergeordnete
   Körperschaft derselben Quelle (Verbandsgemeinde, Kreis; sonst die einzige Gebietskörperschaft der
   Quelle). Sie bekommen keine OSM-Grenze und kein Straßenverzeichnis, übernehmen aber Zentrum und
   Kartenausschnitt der übergeordneten Körperschaft und erscheinen nicht als Lücke. Verbandsgemeinden
   sind Gebietskörperschaften. Wer „Keine Gebietskörperschaft“ oder „Gebiet von“ im Admin ändert,
   setzt damit „Gebietsangabe von Hand gesetzt“; der Befehl ändert beides danach nicht mehr.
2. **Schlüssel aus OParl:** `ags` bzw. `rgs` aus dem Body-Objekt der Quelle. Aus dem 12-stelligen
   Regionalschlüssel wird der Gemeindeschlüssel (Stellen 1–5 und 10–12). Ein gepflegter AGS wird
   nie überschrieben.
3. **Grenze per AGS:** Overpass `relation["boundary"="administrative"]["de:amtlicher_gemeindeschluessel"="<AGS>"]`.
   Nur ein einziger Treffer wird übernommen.
4. **Namenssuche:** Präfixe wie „Ortsgemeinde“, „Stadt“, „Verbandsgemeinde“, „Landkreis“ werden
   abgelöst und bestimmen die erwartete Verwaltungsebene (`admin_level` 8, 6/8, 7, 6). Gesucht wird
   im Gebiet der übergeordneten Körperschaft derselben Quelle (eine Overpass-Abfrage je Gebiet für
   alle ihre Gemeinden), sonst bundesweit nach dem genauen Namen. Der Regionalschlüssel der
   übergeordneten Körperschaft filtert Nachbarn heraus; amtliche Zusätze wie „Herxheim bei
   Landau/Pfalz“ und Klammerzusätze gelten als gleicher Name. Ein eindeutiger Treffer wird samt AGS
   und Regionalschlüssel aus den OSM-Tags übernommen.
5. **Nicht raten:** Mehrere Treffer oder nur ungefähre (gleiches erstes Wort, etwa
   „Stahlhofen a.W.“ gegen „Stahlhofen am Wiesensee“) landen als **Geo-Vorschlag** im Admin
   (Admin → Geo-Vorschläge oder unten auf der Kommunen-Seite). „Übernehmen“ setzt Relation und
   fehlende Schlüssel und verwirft die übrigen Vorschläge; danach `resolve_body_geodata --body <ID>`
   für Kartenausschnitt, Straßen und Adressen.
6. **Nachlauf:** Für neu zugeordnete oder unvollständige Gemeinden `fetch_osm_geodata` (Zentrum,
   Bounding-Box) und `import_streets --with-addresses`; Regionalebenen (AGS kürzer als acht Stellen)
   überspringt `import_streets`. Verbandsgemeinden bekommen ein eigenes Straßenverzeichnis über ihr
   ganzes Gebiet.

Neue Felder an `OParlBody`: `rgs` (Regionalschlüssel; Verbandsgemeinden, Ämter und Samtgemeinden
haben nur diesen, 9 Stellen), `is_non_territorial`, `territory_parent`, `territory_set_manually`.
Vorschläge liegen in `OParlBodyGeoSuggestion` (`insight_body_geo_suggestions`). Die Spalten an
`oparl_bodies` sind nullable oder haben einen DB-Default; der Ingestor kennt sie nicht und führt
sie in `ENRICHMENT_FIELDS`.

Rücksicht auf OSM: eigener User-Agent mit Kontaktadresse, Pause zwischen zwei Abfragen (`--pause`,
Standard 3 s), Retry mit Wartezeit bei 429/504 und Ersatz-Endpoint (`services/overpass.py`, auch von
`import_streets` genutzt). Nach drei Abfragen in Folge ohne Antwort bricht der Lauf ab. Gespeichert
wird je Körperschaft sofort; ein erneuter Aufruf macht dort weiter und fragt für vollständige
Kommunen nichts ab. `--dry-run` fragt Overpass ab, speichert aber nichts und lädt nichts nach.

## Kommunen ohne OSM-Zuordnung

- `python manage.py check_body_geodata [--all]` listet je Kommune: Ebene (Gemeinde, Verband,
  Region, ohne Gebiet), OSM-Relation-ID, AGS bzw. Regionalschlüssel, Bounding-Box, Anzahl Straßen
  und Adressen sowie die fehlenden Punkte. Ändert nichts. Körperschaften ohne eigenes Gebiet sind
  keine Lücke; bei Verbandsgemeinden genügt der Regionalschlüssel.
- Admin → Kommunen: Spalten „OSM“ und „AGS“, Filter „Geo-Zuordnung“ (ohne OSM-Relation oder AGS,
  ohne Straßenverzeichnis, vollständig, mit Geo-Vorschlag, keine Gebietskörperschaft).
- Pflegeweg: `resolve_body_geodata` (siehe oben), offene Vorschläge im Admin übernehmen, danach
  `extract_locations` bzw. automatischer Lauf. Von Hand bleibt möglich: Relation-ID/AGS im Admin
  eintragen und `resolve_body_geodata --body <ID>` aufrufen (lädt Kartenausschnitt, Straßen und
  Adressen nach und ergänzt fehlende Schlüssel aus OSM).

## Smoke-Test

```bash
cd mandari
pytest insight_core/tests/test_paper_locations.py insight_core/tests/test_geo_import_addresses.py \
       insight_core/tests/test_georef_addresses.py insight_core/tests/test_neighborhood_autocomplete.py \
       insight_core/tests/test_admin_paper_locations.py insight_core/tests/test_geo_coverage.py \
       insight_core/tests/test_body_geo_resolver.py
```

## Offen

- `generate_alerts` (Nachbarschafts-Abos) rechnet weiterhin per JSONB-Haversine; Umstellung auf
  `PaperLocation` ist ein Folgeschritt.
- Alternative Geo-Feld im Elasticsearch-Index wurde nicht umgesetzt (Tabelle mit Index reicht für
  das Ziel < 300 ms).
