# Geo-Verortung im Insight-Portal (Straßenverzeichnis, Umkreissuche, Korrektur, Kartenanalyse)

Issue #54 (Ausbaustufe) auf Basis der produktiven Geo-Pipeline (Gazetteer, OParl-Orte,
automatischer Lauf). Diese Notiz beschreibt Module, Tabellen, Commands und Smoke-Tests, dazu die Messung von
Karten und Plänen in Anlagen (`analyze_maps`, #599).

## Datenmodell (`insight_core/models.py`)

| Modell | Tabelle | Zweck |
|--------|---------|-------|
| `Street` | `insight_streets` | Straßenverzeichnis aus OSM (highway-Ways mit Namen), Zentroid je Way. Wahrheitsquelle für Pass 1 der Georeferenzierung. |
| `Address` | `insight_addresses` | Hausnummern-Punkte aus OSM (`addr:street` + `addr:housenumber`), Index `(body, normalized_street, normalized_house_number)`. |
| `PaperLocation` | `insight_paper_locations` | Eine Zeile je Verortung eines Vorgangs mit `source` (Herkunft), `status` (auto/confirmed/removed/hidden) und Index `(body, latitude, longitude)`. |
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
4. Gelesen werden Titel und Hauptteil des Hauptdokuments (OParl `mainFile`, Text bis zur Zeile „Anlagen“). Orte, die
   nur in Anlagen stehen, werden nicht verortet; ohne Angabe zum Hauptdokument zählen alle Dokumente. Sammellisten
   nichtöffentlicher Punkte, Wirtschaftspläne, Jahresabschlüsse und Aufsichtsratsvorlagen bekommen keinen Ort
   (`titel_ohne_ort`). Straßennamen, die Gattungsnamen sind („Markt“, „Schulstraße“), zählen nur mit Hausnummer.
5. Jeder Eintrag trägt seine Fundstelle (`fundstelle`: `titel`, `dokument`, `ki`, #1269).

## Datenschutz bei Orten (`services/ortsschutz.py`, #1269)

Eingaben aus der Einwohnerschaft (Bürgerantrag, Anregungen und Beschwerden, Einwohnerfrage und -antrag, Bürgerbrief,
Eingabe, Petition) und Bauanträge bzw. Vorgänge der Bauordnung können eine Privatanschrift nennen. Erkannt werden sie an
Art und Titel (`grund_nach_art`, ohne Abfrage) und zusätzlich am federführenden Amt (`underDirectionOf`: Bauordnung,
Bauaufsicht, Untere Naturschutzbehörde) und an über- und untergeordneten Vorgängen (`grund`).

- **Verortung:** nie ein Adresspunkt, höchstens der Punkt der Straße – in der Pipeline, bei Orten der Kommune
  (`oparl_locations`; unbekannte Straße: kein Punkt) und bei der KI-Prüfung. Eingaben aus der Einwohnerschaft werden
  nur aus dem Titel verortet, weil im Volltext meist die Anschrift der Einreichenden steht.
- **Anzeige:** Titel und Ortsnamen ohne Hausnummer („[Hausnummer ausgelassen]“) auf der Vorgangsseite, im Seitentitel,
  in der KI-Zusammenfassung samt Description, in Vorgangsliste, Startseite, „Beschlüsse“, Merkliste, Tagesordnung,
  Karte, Umkreissuche, Suchtreffern und Suchvorschau der Kopfzeile, Benachrichtigungen, Umsetzungsstand von Beschlüssen
  (Seite und Abo-Mails) und in den Werkzeugen des KI-Assistenten (auch Dokumentabschnitte). Datenbank,
  OParl-Schnittstelle und Work behalten den vollen Titel.
- **Bestand:** `python manage.py adresspunkte_zurueckstufen [--body <slug>] [--ausfuehren] [--rueckgaengig]` stuft
  vorhandene Adresspunkte dieser Vorgänge auf die Straße zurück (`services/ortsschutz_bestand.py`). Ist die Straße
  unbekannt oder schon ein Ort des Vorgangs, wird die Zeile ausgeblendet (`status=hidden`). Gelöscht wird nichts, die
  alten Angaben stehen in `PaperLocation.vorher`; `--rueckgaengig` stellt sie wieder her. Ohne `--ausfuehren` nur
  Probelauf. Findet ein späterer Verortungslauf eine zurückgestufte Straße nicht mehr (etwa weil Eingaben aus der
  Einwohnerschaft jetzt nur aus dem Titel verortet werden), blendet `sync_paper_locations` die Zeile aus, statt sie zu
  löschen; `vorher` bleibt, die Rückstufung bleibt umkehrbar.

## Orte auf der Vorgangsseite (`services/orte_anzeige.py`, #1269)

Jeder Ort trägt eine Sicherheit und seine Fundstelle: **gesichert** (Angabe der Kommune, amtlicher Umring, von Hand
eingetragen oder bestätigt), **erschlossen** (Straße im Titel oder Dokument) und **unsicher** (von einer KI erkannt
oder ergänzt, KI-Prüfung „unsicher“). Die Randspalte nennt höchstens fünf Orte, weitere aufklappbar in einer Zeile.
Beraten mindestens drei Bezirksgremien (Bezirksvertretung, Ortsrat, Bezirksausschuss …) den Vorgang oder liegen
mindestens sechs Orte über die Hälfte der Ausdehnung der Kommune verteilt, steht „Stadtweit“ mit Begründung statt einer
Straßenliste.

## KI-Prüfung der Verortung (`services/verortung_pruefung.py`, #1222)

Ist für die Zusammenfassungen die Claude API gewählt, prüft dieselbe Anfrage im Tagesbatch die automatisch
gefundenen Orte eines Vorgangs (Herkunft Adresse, Straße, KI-Extraktion, Status „automatisch“). „passt nicht“
blendet den Ort aus (Status „entfernt“, ohne Prüfdatum, im Admin über den Filter „KI-Prüfung“ zu finden und mit
„Bestätigen“ zurückzuholen); „anderer Ort“ ersetzt ihn durch den genannten Ort; fehlende Orte geokodiert die
bestehende Pipeline, nie die KI, und sie tragen die Herkunft `ai_check`. Orte aus OParl, manuelle, amtliche
Umringe und bestätigte bleiben unangetastet. Kennzeichnung und Anbieter: [KI.md](KI.md).

## Verortung nach neuem Text (`services/georef_abonnement.py`, #919)

Der automatische Lauf (Zeitplan `verortung_automatisch`, alle `GEOREF_AUTO_INTERVAL_MINUTES`) verortet Vorgänge im
Stand `pending` mit erkanntem Text. Mit `GEOREF_SUBSCRIPTION=aktiv` stößt zusätzlich jeder neue Text
(`ris.file.text_extracted`) die Verortung seines Vorgangs an (Abonnement `insight.verortung`):

- Ein schon automatisch verorteter Vorgang (`completed`, `ai_needed`, `skipped`, `failed`) geht zurück auf `pending`,
  weil der neue Text weitere Orte nennen kann. Vorgänge mit Ergebnis der KI (`georef_method` mit `ai`,
  `no_locations`) und laufende bleiben unberührt; offizielle, manuelle und Umring-Orte bleiben bei jedem Lauf
  erhalten, entfernte gesperrt.
- Mit `TASKS_BACKEND=journal` reiht das Abonnement je Vorgang den Auftrag `verortung_vorgang` ein, der genau diesen
  Vorgang wie der Zeitplan verortet (Regex/Gazetteer, nie KI, nie externe Geocoder). Ohne Journal verortet der
  Zeitplan beim nächsten Lauf.
- Nur in Kommunen mit Straßenverzeichnis und mit `GEOREF_ENABLED`/`GEOREF_AUTO_ENABLED`. Zeitplan und Auftrag
  beanspruchen einen Vorgang nur aus `pending`, keiner verortet ihn doppelt.

## Verortungs-Tabelle und Umkreissuche (`services/paper_locations.py`)

- `sync_paper_locations(paper)`: JSON → Tabelle, idempotent. Regeln:
  - Einträge im Umkreis von 50 m einer **entfernten** Zeile werden verworfen und aus dem JSON
    gestrichen (Sperrmerkmal; der automatische Lauf legt sie nicht wieder an).
  - **Ausgeblendete** Zeilen (`hidden`, Datenschutz #1269) bleiben unverändert und sperren genau ihren Punkt; Karte,
    Umkreissuche und Vorgangsseite zeigen sie nicht.
  - **Bestätigte** Zeilen bleiben erhalten und werden ins JSON zurückgeschrieben, auch wenn ein
    neuer Lauf sie nicht mehr liefert.
  - Automatische Zeilen ohne JSON-Gegenstück werden gelöscht; zurückgestufte Zeilen (`vorher` gesetzt, #1269) werden
    nur ausgeblendet, damit `adresspunkte_zurueckstufen --rueckgaengig` sie wiederherstellen kann.
- `nearby_papers(body, lat, lon, radius_m, limit)`: Bounding-Box-Vorfilter auf dem Index,
  Haversine-Feinfilter als SQL-Ausdruck (`ACos/Cos/Sin/Radians`, läuft auf PostgreSQL und SQLite),
  nächster Punkt je Vorgang, sortiert nach Entfernung. Ersetzt den JSONB-Vollscan in
  `views/neighborhood.py`.
- Backfill nach der Migration: `python manage.py backfill_paper_locations [--body <slug>] [--dry-run]`.

Leistungsnachweis: `insight_core/tests/test_paper_locations.py::test_nearby_papers_scales_with_index`
legt 20 000 Verortungen an und prüft, dass die Umkreissuche mit zwei Abfragen (Verortungen und
amtliche Umringe) und deutlich unter einer Sekunde antwortet (SQLite; auf PostgreSQL mit Index deutlich
schneller).

## Amtliche Umringe von Bebauungsplänen (`services/plan_boundaries.py`, #598)

Viele Kommunen und das Land NRW veröffentlichen die Umringe (Geltungsbereiche) ihrer Bebauungspläne
als Geodienst. mandari verlinkt sie, statt sie aus PDF-Anlagen zu rekonstruieren.

| Modell | Tabelle | Zweck |
|--------|---------|-------|
| `PlanBoundarySource` | `insight_plan_boundary_sources` | Quelle je Kommune: OGC API – Features oder WFS 2.0 (GeoJSON), Eigenschaften für Plannummer, Titel und Planseite, Planstand, Quellenangabe, Rang |
| `PlanBoundary` | `insight_plan_boundaries` | Zwischenspeicher der Umringe (GeoJSON in WGS84, Box, Punkt im Umring, Fläche) |
| `PaperPlanReference` | `insight_paper_plan_references` | Plannummer aus dem Vorlagentitel und die zugeordneten Umringe; ohne Umring (`match = none`) ist das eine protokollierte Lücke |

**Ablauf (täglich, `sync_plan_boundaries`):**

1. Jede aktive Quelle wird einmal abgerufen (Kennung und Takt je Server wie alle Abrufe bei Diensten,
   [ABRUFREGELN.md](ABRUFREGELN.md#andere-abrufe), eine Sekunde Pause zwischen Seiten,
   zwei Sekunden zwischen Quellen, Obergrenzen für Seiten, Objekte und 24 MB je Antwort). Liefert ein
   Dienst mit Bestand plötzlich nichts oder weniger Objekte, als er selbst meldet (`numberMatched`,
   bei GeoServer `totalFeatures` – etwa wegen einer Obergrenze des Dienstes), bleibt der Bestand
   erhalten und der Fehler steht an der Quelle. Ein WFS wird mit einer einzigen Anfrage abgerufen
   (ohne `STARTINDEX`, weil seitenweises Abrufen ohne feste Sortierung Objekte doppelt liefern oder
   auslassen kann); seine Obergrenze (`maxFeatures`/`wfs_maxfeatures`) muss also über der Zahl der
   Pläne liegen. MapServer meldet keine Gesamtzahl – dort die Zahl der Umringe nach dem ersten Lauf
   mit dem Planverzeichnis der Kommune vergleichen.
   Objekte außerhalb der Kommune (Rand 0,05°) werden übersprungen; liefert ein Dienst Breite vor Länge,
   werden die Achsen anhand des Gemeindezentrums getauscht.
2. Vorlagen mit „Bebauungsplan Nr. …“ bzw. „B-Plan Nr. …“ im Titel werden zugeordnet. Nennt der Titel
   eine Änderung („1. Änderung des Bebauungsplans Nr. 579“, „Bebauungsplan Nr. 388 – 3. Änderung“),
   gilt: Umring der Änderung, sonst Umring des laufenden Verfahrens, sonst Umring des Ursprungsplans
   (mit Hinweis). Ohne Änderung: der rechtskräftige Plan, sonst das Verfahren. Von mehreren Quellen
   gewinnt die mit dem kleinsten Rang.
3. Ein Punkt im Umring kommt als Verortung (`source = plan_boundary`, „amtlich“) an den Vorgang. Er
   wirkt damit in Karte, Umkreissuche und Abos. Ein neuer Georef-Lauf behält ihn; wer ihn im Admin
   entfernt, sperrt auch Umring und Umkreistreffer des Vorgangs.
4. Hat eine Kommune keine aktive Quelle mehr (im Admin deaktiviert oder gelöscht, etwa weil die Lizenz
   nicht passt), räumt der Lauf auf: Bezüge und automatische Umring-Verortungen verschwinden, im Admin
   bestätigte Verortungen bleiben. Die Fläche ist sofort weg; wer den Punkt nicht bis zum nächsten
   Lauf stehen lassen will, ruft `sync_plan_boundaries --body <slug> --no-fetch` auf.

**Umkreissuche:** `nearby_papers` findet zusätzlich Vorgänge, deren Umring den Suchkreis berührt – auch
wenn der Punkt im Umring weiter weg liegt; die Entfernung ist dann die zum Umring (0 = darin).
`generate_alerts` nutzt dieselbe Prüfung. Geometrie lädt die Suche nur für Umringe mit Vorgang und
davon höchstens für die 200 nächsten (Abstand zur Box als Vorauswahl). Gemessen mit allen 686 Umringen
Münsters aus der Landes-API (SQLite, lokal): Radius 2 km 36 ms, 5 km und 20 km rund 60 ms statt 115
bzw. 170 ms.

**Vorgangsseite:** Karte mit Umring (Fläche) und Orten (`frontend/js/paper-map.ts`), darunter je Plan
Planstand, Link zur Planseite der Kommune und die Quellenangabe der Quelle, auch in der Kartenleiste.

**Einrichten:**

```bash
# Landesquelle NRW (rechtskräftige Pläne, OGC API) für eine Kommune mit AGS anlegen
python manage.py sync_plan_boundaries --add-nrw-source --body muenster
# Probelauf: abrufen und zählen, nichts speichern
python manage.py sync_plan_boundaries --body muenster --dry-run
# Abrufen, zuordnen, Abdeckung der letzten 12 Monate und Lücken ausgeben
python manage.py sync_plan_boundaries --body muenster
```

Kommunale Dienste legt der Admin unter „Umring-Quellen (Bebauungspläne)“ an. Beispiel MapServer-WFS
(Stadt Münster, Ebene „Im Verfahren“): Art `WFS`, Adresse `https://geo.stadt-muenster.de/mapserv/bplan_serv`,
Ebene `ms:bplan1`, Abfrageparameter `{"OUTPUTFORMAT": "GEOJSON"}`, Plannummer `plannr`, Titel `name`,
Planseite `scanurl`, Planstand „Im Verfahren“, Rang kleiner als der der Landesquelle. Die Ebene
`ms:bplan2` enthält die rechtskräftigen Pläne; dort trägt `planid` (INSPIRE-Kennung) auch die Nummer der
Änderung und ist als Plannummer besser geeignet als `plannr`. Für „im Verfahren“ gibt Münster keine Lizenz an; vor dem
Einschalten klären (siehe Machbarkeitsbericht, Abschnitt Urheberrecht). Lücken zeigt der Admin unter
„Bebauungspläne der Vorgänge“, Filter „Kein Umring gefunden“.

Der Abruf läuft täglich um 04:50 als Zeitplan `befehl:sync_plan_boundaries` im Worker (siehe
`DEPLOYMENT.md`, „Geplante Aufgaben“); von Hand:
`docker compose exec worker python manage.py sync_plan_boundaries --trotz-zeitplan`.

## Nachbarschafts-Autocomplete (`services/neighborhood.py`)

`GET /insight/nachbarschaft/autocomplete/?q=…` sucht im eigenen Verzeichnis der aktiven Kommune:

- „Straße Hausnummer“ → Treffer aus `Address` (Präfix auf Straße und Hausnummer),
- sonst `Street` (eine Zeile je Straßenname, Zentroid gemittelt): Präfix-Treffer zuerst, dann
  Teilstring-Treffer, höchstens 10 Ergebnisse.
- Photon (`PHOTON_API_URL`) nur noch als Fallback, wenn die Kommune keine Straßen hat; im Takt je Server
  (höchstens eine Anfrage je Sekunde). Ist binnen 2 Sekunden kein Zeitpunkt frei, gibt es keine Vorschläge.

Antwortformat unverändert: `[{"name", "lat", "lon"}]`.

## Admin-Korrektur-Workflow (`insight_core/admin.py`)

- **Verortungen** (`PaperLocationAdmin`): Liste mit Herkunft (`OParl-Ort`, `Adresse`, `Straße`,
  `KI-Extraktion`, `Manuell`, `Amtlicher Umring`, `KI-Prüfung`), Prüfstatus (automatisch, bestätigt, entfernt,
  ausgeblendet), Urteil der KI-Prüfung, Vorgang, Koordinaten. Zeilenaktionen „Bestätigen“ / „Entfernen“
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

Rücksicht auf OSM: Kennung mit Infoseite und Kontaktadresse und Takt je Server wie alle Abrufe bei Diensten
([ABRUFREGELN.md](ABRUFREGELN.md#andere-abrufe), Nominatim und Overpass höchstens eine Anfrage je Sekunde), Pause
zwischen zwei Abfragen (`--pause`,
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

## Karten und Pläne in Anlagen messen (`analyze_maps`, #599)

Viele Anlagen zu Vorlagen enthalten Karten und Pläne: Lagepläne, Bebauungspläne, FNP-Ausschnitte,
Straßenplanungen. Bevor ein Dokument-Worker sie dauerhaft erkennt, rendert und verortet, misst
`analyze_maps` den Bestand – nur aus dem lokalen Dateicache, ohne einen einzigen Abruf beim
Ratsinformationssystem und ohne etwas zu speichern.

### Bausteine der Kartenanalyse

| Baustein | Ort | Aufgabe |
|----------|-----|---------|
| Seitenanalyse | `apps/common/documents/page_analysis.py` | fachfrei (Plattform-Schicht): Messwerte je Seite, Einordnung als Karte |
| Auswertung | `insight_core/services/map_survey.py` | Dateien der Kommune aus dem Cache, Straßenverzeichnis, Kennzahlen, Stichprobe |
| Befehl | `insight_core/management/commands/analyze_maps.py` | Aufruf, Ausgabe, JSON, Stichprobe, Auswertung der Prüfung von Hand |

Bibliotheken: pypdfium2 (PDFium) und pypdf, nicht PyMuPDF – Begründung in
[`docs/adr/20260930-pdf-seitenanalyse-bibliothek.md`](adr/20260930-pdf-seitenanalyse-bibliothek.md).

### Aufruf von `analyze_maps`

```bash
# Gesamter Cache der Kommune (neueste Dateien zuerst); nur Auswertung, --dry-run ist Pflicht
python manage.py analyze_maps --body muenster --dry-run
# Teilmenge, Kennzahlen als JSON
python manage.py analyze_maps --body muenster --dry-run --limit 2000 --json /tmp/karten.json
# Stichprobe für die Prüfung von Hand: je zur Hälfte vorhergesagte Karten und andere Seiten
python manage.py analyze_maps --body muenster --dry-run --sample-csv /tmp/stichprobe.csv --sample-size 200
# Spalte „label“ mit ja/nein füllen, dann Präzision und Trefferquote berechnen
python manage.py analyze_maps --evaluate /tmp/stichprobe.csv
```

Der Befehl hält eine Singleton-Sperre (`--ohne-sperre` erzwingt) und liest Datei für Datei;
`--max-mb` (Vorgabe `FILE_CACHE_MAX_MB`) und `--max-pages` (Vorgabe 300) begrenzen große Dokumente.
Nur öffentliche Dateien: gelöschte Dateien, Dateien gelöschter Vorgänge und Sitzungen sowie von
mandari Session zurückgenommene Dateien bleiben außen vor. Eine Seite, die PDFium nicht lesen kann,
wird übersprungen und gezählt (`seiten_nicht_lesbar`); scheitert eine ganze Datei unerwartet, zählt
sie unter „Analysefehler“ (im Log nur Datei-ID und Fehlerart), und der Lauf geht weiter.

#### Lauf auf dem Server: eigener Container, nicht im Web-Dienst

Ein Lauf über den ganzen Cache einer großen Kommune dauert Stunden und braucht viel Rechenzeit.
Deshalb nicht per `docker exec` im laufenden Web-Container starten, sondern in einem kurzlebigen
Container des Anwendungsdienstes (`docker compose run`, wie bei `backup.sh`): gleiches Image, gleiche
Umgebung und dieselben Volumes bzw. Bind-Mounts für den Dateicache, aber ein eigener Prozessraum mit
der Speichergrenze des Dienstes (`mem_limit: 1g` in `docker-compose.yml`). Läuft der Speicher über,
beendet der Kernel nur diesen Container, nicht die Web-Worker.

```bash
# im Verzeichnis mit docker-compose.yml und .env
docker compose run -d --no-deps --label mandari.autoheal=false --name mandari-kartenanalyse \
  mandari python manage.py analyze_maps --body muenster --dry-run \
  --json /tmp/karten.json --sample-csv /tmp/stichprobe.csv --sample-size 200
docker inspect -f '{{.HostConfig.Memory}}' mandari-kartenanalyse   # 1073741824 = 1 GB
docker logs -f mandari-kartenanalyse                                # Fortschritt alle 500 Dateien
docker wait mandari-kartenanalyse                                   # 0 = fertig
docker inspect -f '{{.State.OOMKilled}}' mandari-kartenanalyse      # true = Speichergrenze gerissen
docker cp mandari-kartenanalyse:/tmp/karten.json .
docker cp mandari-kartenanalyse:/tmp/stichprobe.csv .
docker rm mandari-kartenanalyse
```

Der Befehl öffnet die Dateien nur lesend und schreibt nichts in die Datenbank. „Speicher höchstens“
in der Ausgabe (bzw. `speicher_max_mb` im JSON) ist der Spitzenwert dieses Containers über alle
Dateien, also auch über den größten Plan – das ist die Messung für die Obergrenze von 1 GB.

### Kennzahlen der Kartenanalyse

- Dateien und Seiten, davon Karten bzw. Pläne (Anteil), Dateien mit mindestens einer Kartenseite
- Kartenseiten mit maschinenlesbarem Maßstab (`/VP` mit `/Measure /RL`, AutoCAD-Plots), als GeoPDF,
  mit Koordinatenbeschriftung (Rechts-/Hochwert-Paare im Textlayer), mit mindestens vier Straßennamen
  aus dem Straßenverzeichnis der Kommune, als Scan ohne Textlayer, mit 1-bit-Rastergrundkarte
- Median der effektiven Rasterauflösung, Papierformate, häufigste Erzeuger (Creator/Producer)
- Übersprungene Dateien (nicht im Cache, kein PDF, zu groß, nicht lesbar, Analysefehler), nicht
  lesbare Seiten, Laufzeit, höchster Speicherbedarf des Prozesses (Linux)

### Messwerte je Seite und Einordnung

`analyze_pdf` misst Format, Pfadobjekte und -segmente (auch in Form-XObjects), Vektordichte (Segmente
je cm²), Rasterbilder (Pixel, Bittiefe, Filter, effektive dpi, Flächenanteil), den Textlayer (auf
Wunsch mit Rechtecken), Kartenstichworte, Koordinatenpaare, Maßstäbe aus `/VP` (der Papierbereich
entfällt) und GeoPDF-Merkmale (`/Measure /GEO`, `/LGIDict`). Bildseiten bekommen eine grobe Vorschau
(40 dpi), um Karten (wenige Farbtöne) von Fotos zu trennen.

`classify_page` vergibt Punkte nach den Regeln aus dem Machbarkeitstest (Stufe C), Karte ab 3 Punkten:

| Merkmal | Punkte |
|---------|--------|
| Vektorzeichnung (≥ 5.000 Segmente und ≥ 10 je cm²) | +2 |
| Maßstab im PDF (`/VP`) | +2 |
| GeoPDF | +3 |
| größer als A4 | +1 |
| ≥ 2 bzw. ≥ 4 Kartenstichworte | +1 bzw. +2 |
| ≥ 2 Straßennamen aus dem Straßenverzeichnis | +1 |
| Textseite (> 1.500 Zeichen, kaum Vektoren) | −3 |
| Bildseite: wie eine Karte (wenige Farben, 1-bit-Grundkarte, Vektoren oder `/VP`) bzw. wie ein Foto | +2 bzw. −2 |

Auf den 44 Seiten der Stichprobe des Machbarkeitstests (18 PDFs, Münster) ergibt das ohne
Straßenverzeichnis Präzision 0,92 und Trefferquote 0,79 (11 von 14 Karten). Verpasst werden wie im
Test der Scan ohne Textlayer, der Nachdruck mit in Pfade umgewandelter Schrift und ein A0-Plan mit
geringer Vektordichte, den im Machbarkeitstest erst die Straßennamen über die Schwelle hoben; Fehlalarm
bleibt der Querschnitt. Die Regeln sind an dieser Stichprobe entstanden – die Prüfung von Hand an 200 Seiten
aus dem Bestand (Akzeptanzkriterium: Präzision ≥ 0,9, Trefferquote ≥ 0,8) steht aus.

### Stichprobe und Prüfung von Hand

Die Stichprobe zieht je zur Hälfte Seiten, die als Karte eingeordnet wurden, und andere Seiten
(Zufallsauswahl über den ganzen Lauf). Die Spalte `weight` gibt an, für wie viele Seiten des Bestands
eine Seite steht. `--evaluate` rechnet Präzision und Trefferquote einmal für die Stichprobe und einmal
nach Schichten gewichtet – die gewichtete Trefferquote ist die Schätzung für den Bestand, weil Karten
in der Stichprobe überrepräsentiert sind.
