# Nicht-OParl-Quellen: Scraper & Bridges

Mandari kann neben echten OParl-APIs auch Ratsinformationssysteme ohne
OParl-Schnittstelle anbinden. Zwei Wege:

1. **Scraper-Adapter im Ingestor** (`ingestor/src/scrapers/`) — z. B.
   **SessionNet** (Somacos). Der Adapter erzeugt synthetisches
   OParl-1.1-JSON und speist es in die unveränderte Pipeline ein
   (OParlProcessor → Upserts → OCR-Queue → Elasticsearch → Tombstones).
2. **Externer OParl-Proxy** — z. B. **ALLRIS** via
   [Aeroid/oparl-bridge](https://github.com/Aeroid/oparl-bridge) (MIT).
   Der Proxy läuft als eigener Container; Mandari konsumiert seine
   OParl-Ausgabe als ganz normale OParl-Quelle. Der Ingestor-Kern bleibt
   unberührt.

Die Art einer Quelle steht in `OParlSource.sync_config["source_type"]`
(JSONB, additiv — bewusst keine eigene Spalte, um Schema-Drift zwischen
Django und Ingestor zu vermeiden):

| `source_type` | Bedeutung |
|---|---|
| `oparl` (Default, Schlüssel fehlt) | echte OParl-API |
| `bridge:allris` | oparl-bridge-Proxy vor ALLRIS (normaler OParl-Sync, nur Provenienz-Label) |
| `scraper:sessionnet` | SessionNet-HTML-Scraper im Ingestor |

Im Django-Admin (OParl-Quellen) sind Art, Parse-Quote und robots.txt-Status
je Quelle sichtbar; der Filter "Quellen-Art" trennt OParl/Bridge/Scraper.

---

## 0. Zensus vor dem Anbinden: `probe-ris`

Bevor ein Adapter entsteht, klärt der Zensus je Kommune vier Fragen mit **höchstens fünf
Anfragen** (robots.txt, Startseite, bei 403 eine Vergleichsanfrage mit neutralem Client,
danach OParl-Kandidaten) und ohne jede Umgehung: Welcher Hersteller (SessionNet, ALLRIS 3/4,
Sternberg RIM, more! rubin, regisafe, komuna)? Erlaubt die robots.txt unseren User-Agent
(RFC 9309, Produkt-Token)? Liegt ein Bot-Gate oder eine WAF davor
(Browser-Verifikation, Proof-of-Work) oder sperrt die Quelle nur unseren User-Agent? Gibt es
längst einen OParl-Endpunkt (herstellertypische Pfade zuerst, Fehlerobjekte werden erkannt)?
Die robots.txt gilt schon beim Zensus: Gesperrte Pfade (Startseite, OParl-Kandidaten) fragt er
nicht an. Ist die robots.txt nicht erreichbar (5xx, 408, 429, Netzfehler), endet die Prüfung
nach dieser einen Anfrage mit dem Befund `nicht_erreichbar`.

Auch der Takt gilt wie im Abgleich (#1027): Vor jeder Anfrage, auch vor jeder Weiterleitung, reserviert
der Zensus einen Zeitpunkt in der Drossel je Host (`src/client/host_pacing.py`, Redis über alle Prozesse),
also höchstens eine Anfrage je `INGESTOR_REQUEST_INTERVAL` (Standard eine Sekunde) an denselben Host; ist
für den Host eine Quelle mit größerem `request_interval` registriert, gilt deren Abstand. Jede
Weiterleitung zählt als eigene Anfrage gegen die fünf. Einer Weiterleitung folgt der Zensus nur, wenn die
robots.txt des Ziel-Hosts das Ziel erlaubt; die robots.txt eines neuen Hosts kostet dabei eine Anfrage. Die
Vergleichsanfrage trägt dieselbe Kennung wie im Abgleich (`python-httpx/<Version>`, siehe
`docs/MONITORING.md`), geht an die Adresse, die 403 geliefert hat, folgt keiner Weiterleitung und entfällt mit
`OPARL_UA_PROBE_ENABLED=false`; nur eine Antwort 2xx darauf ergibt „User-Agent gesperrt“, eine Weiterleitung
steht als unklar in den Hinweisen. `manage.py add_oparl_source` hält den Takt ebenso ein, bei einer schon
registrierten Quelle mit deren `request_interval`, bei einer anderen registrierten Quelle auf demselben Host
mit deren größerem Abstand, und folgt Weiterleitungen ebenfalls nur nach der robots.txt des Ziels.

```bash
mandari-ingestor probe-ris https://buergerinfo.example.org/bi/            # eine Kommune, JSON
mandari-ingestor probe-ris --batch kommunen.txt --format csv --out zensus.csv
mandari-ingestor probe-ris --batch kommunen.txt --store                     # Ergebnis an registrierte Quellen
```

Das Ergebnis enthält einen Vorschlag für `sync_config` (OParl-Systemadresse mit Version oder
SessionNet-Basis-URL) beziehungsweise die Begründung, warum kein Adapter sinnvoll ist (robots,
Gate, Hersteller ohne Scraping-Pfad, siehe ADR 2026-09-17). Der Batch-Lauf liefert zwei
Listen: **ohne OParl, robots-frei** (Scraper-Kandidaten) und **OParl vorhanden, nicht
registriert** (Aktivierung und Registrierung anstoßen). Mit `--store` landet das Ergebnis in
`sync_config["probe"]` der passenden Quelle und ist im Django-Admin sichtbar. Fixtures und
Tests: `ingestor/tests/fixtures/ris/`, `ingestor/tests/test_probe_ris.py`.

## 1. SessionNet-Kommune anbinden (Schritt für Schritt)

SessionNet-Bürgerinfo-Frontends sind mandantenfähige Standard-Templates.
Der Adapter unterstützt die klassische `*.asp`-Variante und die neuere
`*.php`-Variante (identisches Markup, verifiziert an
buergerinfo.luedenscheid.de und rat.eschweiler.de/bi).

### Schritt 1: Basis-URL ermitteln

Die Basis-URL ist das Verzeichnis, in dem die SessionNet-Seiten liegen —
erkennbar an Seiten wie `si0040.asp` (Sitzungskalender), `vo0050.asp`
(Vorlage), `gr0040.asp` (Gremien). Beispiele:

- `https://buergerinfo.luedenscheid.de/` (Kürzel direkt im Root, `.asp`)
- `https://rat.eschweiler.de/bi/` (Unterverzeichnis `bi/`, `.php`)
- `https://sessionnet.owl-it.de/unna/bi/` (Hoster mit Mandanten-Pfad)

Kurz prüfen: `<basis-url>si0040.asp` (oder `.php`) muss den
Sitzungskalender liefern und der Seitentitel „SessionNet | …" sein.

### Schritt 2: robots.txt prüfen

Der Ingestor prüft robots.txt automatisch vor jedem Crawl und überspringt
die Quelle bei einem Disallow. Seit #116 ist das kein stiller Parse-Fehler mehr:
Die Quelle bekommt die Fehlerklasse `robots_blocked` („robots.txt sperrt“), der
Betriebsmonitor und die Alarmmail zeigen Grund und Handlungsempfehlung, und die
Quellen-Schonung fragt nur noch täglich nach (`docs/MONITORING.md`). Manche
Hoster sperren alle Mandanten bis auf einen — dann hilft nur das Gespräch mit
dem Betreiber, keine Umgehung. Vorab manuell prüfen schadet nicht:
`https://<host>/robots.txt`.

### Schritt 3: Quelle im Django-Admin anlegen

Admin → Insight → OParl-Quellen → Hinzufügen:

- **Name**: z. B. „Stadt Eschweiler (SessionNet)"
- **URL**: die Basis-URL (dient als eindeutiger Quellen-Schlüssel), z. B.
  `https://rat.eschweiler.de/bi/`
- **Sync config**:

```json
{
  "source_type": "scraper:sessionnet",
  "scraper": {
    "base_url": "https://rat.eschweiler.de/bi/",
    "body_name": "Stadt Eschweiler",
    "variant": "php",
    "rate_limit_seconds": 2.0,
    "calendar_window_days": [-60, 210],
    "full_window_days": [-365, 210]
  }
}
```

Alle `scraper`-Schlüssel außer `base_url` sind optional:

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `body_name` | "Unbekannte Kommune" | Anzeigename des Bodies |
| `variant` | Auto-Detect | `"asp"` oder `"php"` |
| `rate_limit_seconds` | `2.0` | Mindestabstand zwischen Requests je Host, nie unter 1 s (kleinere Werte werden angehoben, Warnung im Log) |
| `max_concurrent` | `1` | immer 1 (Serialisierung, RIS-Server schonen) |
| `calendar_window_days` | `[-60, 210]` | Kalenderfenster inkrementeller Läufe |
| `full_window_days` | `[-365, 210]` | Fenster des Full-Crawls (Historie) |
| `max_detail_pages` | unbegrenzt | Obergrenze Detailseiten je Lauf (Onboarding/Pilot) |
| `members_on_full_only` | `true` | Gremien-Mitglieder nur im Full-Crawl crawlen |
| `bodies` | – (eine Körperschaft) | Mehrere Körperschaften je Instanz: `"auto"` oder Liste `[{"cpanr": 1, "name": "…", "short_name": "…"}]`, siehe Schritt 3a |

Außerhalb von `scraper` (auf oberster Ebene der Sync config, auch für OParl-Quellen):

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `download_headers` | keine | Zusätzliche HTTP-Header für **Datei-Downloads** dieser Quelle (Dateicache, Textextraktion im Ingestor und in Django), z. B. `{"Referer": "https://rat.example.de/bi/", "Cookie": "consent=1"}`. Für RIS, die Anlagen nur mit Referer oder Consent-Cookie ausliefern (#116). Werte sind Klartext im Admin — keine persönlichen Sitzungs-Cookies hinterlegen. |
| `file_downloads` | `true` | `false`: Dateien dieser Quelle **nicht automatisch abrufen** (Textextraktion im Ingestor und in Django, Dateicache, Vorschau). Für RIS, die Dokumente nur hinter einer Zugangsprüfung für Menschen ausliefern (z. B. ALTCHA); die Vorschau verweist dann auf das Original. Die Dateien bleiben offen und werden nachgeholt, sobald der Schalter fällt. |
| `request_interval` | `INGESTOR_REQUEST_INTERVAL` (1 s) | Mindestabstand in Sekunden zwischen dem Beginn zweier Anfragen an den Host dieser Quelle (0–30). Er gilt über alle Abrufplätze, alle Quellen auf demselben Host und alle Prozesse (Daemon, Einzelabgleich, Dokument-Cache und Vorschau in Django), weil alle denselben Zeitstempel je Host in Redis reservieren. Ohne den Schlüssel gilt der Standard (`INGESTOR_REQUEST_INTERVAL` bzw. `RIS_REQUEST_INTERVAL` in Django, je eine Anfrage je Sekunde). Der Wert darf nur verlangsamen: Werte unter 1 s, auch `0`, gelten als 1 s (Warnung im Log, `mandari_oparl.pacing.MIN_REQUEST_INTERVAL`, #1025); schneller als eine Anfrage je Sekunde und Server rufen wir nie ab. Ohne erreichbares Redis drosselt jeder Prozess für sich. Je Prozess und Host laufen höchstens `INGESTOR_HOST_MAX_CONCURRENT` Anfragen (Standard 2) gleichzeitig, damit der reservierte Takt nicht so viele Sekunden vorausreicht, wie es Abrufplätze gibt; das gilt auch für den Ziel-Host einer Weiterleitung (der Platz des Abrufs zieht dorthin um). Django (Dokument-Cache, Vorschau) kennt diese Grenze nicht, hält aber denselben Takt ein. |
| `list_params` | keine | Zusätzliche Parameter für die erste Seite jeder OParl-Liste, z. B. `{"size": 100}` bei ALLRIS (100 statt 10 Einträge je Seite). Folgeseiten kommen aus `links.next`. |
| `modified_since_margin_minutes` | `INGESTOR_MODIFIED_SINCE_MARGIN_MINUTES` (120) | Sicherheitsabstand des Änderungsfensters in Minuten (0–10080). Inkrementelle Läufe fragen je Kommune nach allem, was seit dem Beginn ihres letzten vollständig erfolgreichen Abgleichs abzüglich dieses Abstands geändert wurde; ohne einen solchen Abgleich, oder liegt er weiter zurück, wie früher nach den letzten sieben Tagen (#1025). Quellen, deren Objekte erst Stunden nach ihrem `modified` sichtbar werden, brauchen einen größeren Abstand. Listen, deren `modified` keine Uhrzeit trägt, erkennt der Ingestor selbst und fragt sie ab 00:00 Uhr an (`timed_lists`). |
| `body_sync_started` | – (pflegt der Ingestor) | Beginn des letzten vollständig erfolgreichen Abgleichs je Kommune (Kennung → Zeitpunkt UTC), Anfang des Änderungsfensters. Ein Lauf mit Lücke schreibt ihn nicht fort; konnte ein Lauf Objekte nicht speichern (Fehler, Mitgliedschaft ohne Person, TOP ohne Sitzung), rückt er höchstens bis zu ihrem ältesten `modified` vor. Objekte, die die Quelle trotz Filter liefert (`modified` mehr als einen Tag vor dem Fenster der Liste), halten ihn nicht; sie kommen ohnehin in jedem Lauf wieder. Löschen setzt die Kommune auf das Fenster von sieben Tagen zurück. |
| `missing_lists` | – (pflegt der Ingestor) | Optionale Listen (Mitgliedschaften, Tagesordnungspunkte, Beratungen, Dateien, Orte, Wahlperioden), die die Quelle nicht anbietet, mit dem Zeitpunkt der letzten Prüfung (Adresse → Zeitpunkt UTC): Die erste Seite antwortete ohne Filter mit HTTP 404 oder einem OParl-Fehlerobjekt, während andere Listen der Kommune lesbar waren. Inkrementelle Läufe fragen sie einmal am Tag an, der Vollabgleich jedes Mal (in Zeitscheiben nicht, #1033); gibt es sie wieder, wird der Vermerk gelöscht. Pflichtlisten (Gremien, Personen, Sitzungen, Vorlagen) werden nie vermerkt, dort bleibt ein 404 eine Lücke; ein Fehlerobjekt gilt dort wie bisher als leere Liste, solange andere Listen der Kommune lesbar sind (#1025). Beantwortet SD.NET RIM eine gefilterte Liste ohne Treffer mit HTTP 404 und Code 802, gilt sie als leer und vollständig, nicht als fehlend (#1034). |
| `timed_lists` | – (pflegt der Ingestor) | Listen, deren `modified` nachweislich die Uhrzeit trägt, mit dem Zeitpunkt des Nachweises (Adresse → Zeitpunkt UTC). Nur sie fragen inkrementelle Läufe mit dem kurzen Fenster ab „Beginn minus Abstand“ an. Alle übrigen Listen beginnen um 00:00 Uhr (dem früheren Zeitpunkt von 00:00 Uhr Ortszeit und 00:00 Uhr UTC): Trägt `modified` nur das Datum, fände das kurze Fenster ab etwa 02:00 Uhr nichts mehr vom selben Tag, auch bei einer Liste, die noch kein Lauf prüfen konnte. Vermerkt, sobald ein Lauf dort Änderungen der letzten sieben Tage sieht und alle eine Uhrzeit tragen; gelöscht, sobald er dort eine solche Änderung nur mit Datum sieht, auch 00:00 Uhr Ortszeit in UTC angegeben (`22:00:00Z`) (#1025). |
| `full_sync_slices` | keine | Vollabgleich nur in Zeitscheiben, etwa `{"days": 30, "since": "2010-01-01"}`: Der Vollabgleich fragt jede Liste nur mit `modified_since`/`modified_until` an, nie ohne Filter, von alt nach neu, und setzt nach einem Abbruch an der letzten vollständigen Scheibe fort. Für Quellen, deren ungefilterte Listen abbrechen (Stadt Hamm). `days` 1–366 (Standard 30), `since` Pflicht, optional `lists`. Er endet immer mit dem Zeitfenster (`full_sync_window`, sonst Standardfenster des Daemons). Einzelheiten in [`ABRUFREGELN.md`](ABRUFREGELN.md#vollabgleich-in-zeitscheiben) (#1033). |
| `full_sync_progress`, `full_sync_filter_ignored` | – (pflegt der Ingestor) | Fortschritt des Vollabgleichs in Zeitscheiben je Liste und Befund „Server ignoriert den Filter“ (#1033). |
| `slow_lists` | – (pflegt der Ingestor) | Optionale Listen, die nach Zeitüberschreitungen ausgesetzt sind (1, 2, 4, 8, 16, dann höchstens 24 Stunden), mit eigenem Fensteranfang; danach holt der inkrementelle Lauf sie in Scheiben zu einem Tag nach, bei Servern ohne Filter wie bisher ohne Scheiben (#1033, [`ABRUFREGELN.md`](ABRUFREGELN.md#langsame-listen)). Nicht löschen: Die Änderungen seit dem Fensteranfang holte dann erst der nächste Vollabgleich; stattdessen `until` in die Vergangenheit setzen. |
| `carry_modified_since` | `false` | `true`: Die Quelle filtert mit `modified_since`, lässt den Parameter aber in `links.next` weg (ALLRIS). Der Ingestor hängt ihn an jede Folgeseite an. Ohne den Schalter gilt die Quelle als „ohne Filter“, und der inkrementelle Abgleich erreicht neue Einträge erst im nächtlichen Vollabgleich, weil die Listen aufsteigend sortiert sind. |

Zieht eine Quelle um und ändert dabei die Form ihrer Adressen, behalten die Objekte im Bestand ihre Kennungen
über `id_address`, `id_base` und `id_rules` (Abbildungsregeln vom Rest der neuen Adresse auf den der alten,
`shared/mandari_oparl/ids.py`, ADR `docs/adr/20260929-kanonisches-modell.md`). Ungültige Regeln halten den Abgleich
der Quelle an, statt Objekte mit anderen Kennungen anzulegen.

Eigenheiten der OParl-Schnittstelle von ALLRIS (`…/oparl/system`, gesehen Oktober 2026) gleicht der Ingestor
selbst aus: Organisationen mit `"type": "gr"`/`"at"` und der Typ-URL in `Type`, Beratungen mit `agendaitem` statt
`agendaItem`, in Vorlagen eingebettete Beratungen ohne Sitzung und Tagesordnungspunkt (vorhandene Bezüge bleiben
erhalten).

### Schritt 3a: Instanzen mit mehreren Körperschaften

Eine SessionNet-Instanz kann mehrere Körperschaften führen, typisch die Samtgemeinde mit ihren
Mitgliedsgemeinden, manchmal auch eine kommunale Gesellschaft. Erkennbar ist das an der Auswahl
„Mandant“ in der Filterleiste der Gremienliste (`gr0040`) und an der Spalte „Mandant“ im
Sitzungskalender; die Links tragen den Parameter `__cpanr`. Ohne weitere Angabe liest der Adapter
nur die vorausgewählte Körperschaft und meldet im Log, wie viele die Instanz führt.

Mit `scraper.bodies` wird je Körperschaft ein eigener Body angelegt, mit eigenen Gremien, Personen,
Sitzungen, Vorlagen und Wahlperioden:

```json
{
  "source_type": "scraper:sessionnet",
  "scraper": {
    "base_url": "https://ratsinfo.example.de/bi/",
    "body_name": "Samtgemeinde Musterheide",
    "bodies": [
      {"cpanr": 1, "name": "Samtgemeinde Musterheide"},
      {"cpanr": 2, "name": "Gemeinde Musterdorf", "short_name": "Musterdorf"}
    ]
  }
}
```

- `"bodies": "auto"` übernimmt alle Mandanten der Auswahl mit ihren Namen (eine zusätzliche Anfrage,
  um die Nummer des vorausgewählten Mandanten zu ermitteln). Mit einer Liste wählt man aus, etwa
  ohne die kommunale Gesellschaft; fehlt `name`, gilt der Name aus der Auswahl.
- Kennung eines Bodies: Basis-URL mit `?__cpanr=<n>`. Gremien, Sitzungen, Vorlagen und Dateien
  behalten ihre instanzweiten Kennungen (`__kgrnr`, `__ksinr`, `__kvonr`); Personen und Orte gibt es
  je Körperschaft (`pe0051.asp?__cpanr=<n>&__kpenr=<k>`), damit wer im Samtgemeinderat und im Rat
  einer Mitgliedsgemeinde sitzt, in beiden Körperschaften vollständig erscheint.
- Gremienliste und Kalender werden je Körperschaft mit `__cpanr` abgerufen, Detailseiten einmal je
  Lauf. Sitzungen, die laut Spalte „Mandant“ zu einer anderen Körperschaft gehören, ordnet der
  Adapter nicht zu (Gegenprobe, falls die Instanz den Parameter nicht beachtet).
- Eine Vorlage, die in Gremien mehrerer Körperschaften beraten wird, gehört zur ersten Körperschaft
  in der Reihenfolge von `bodies`, in der sie im Lauf vorkommt.
- `bodies` vor dem ersten Lauf setzen. Der Upsert ändert die Körperschaft eines vorhandenen Objekts
  nicht: Wird eine schon gecrawlte Quelle umgestellt, bleiben ihre Gremien, Sitzungen und Vorlagen
  beim bisherigen Body (Basis-URL). Dann den Bestand der Quelle vorher entfernen.
- Jeder neue Body startet ungelistet (`is_listed` aus, Issue #555). Erst nach der Freigabe die
  gewünschten Bodies im Admin listen – bei mehreren Körperschaften jeden einzeln. Vorhandene Bodies
  behalten ihre Einstellung.

### Schritt 4: Probe-Crawl mit Limit

Beim Onboarding zunächst mit strengem Limit fahren
(`"max_detail_pages": 20`), Ergebnis im Admin prüfen (Scraper-Status:
Parse-Quote, gespeicherte Entitäten), Stichproben gegen die Live-Seiten
vergleichen. Danach das Limit entfernen und einen Full-Sync auslösen
(Admin-Aktion „Vollständiger Sync").

### Was der Adapter extrahiert

| SessionNet-Seite | OParl-Entität | external_id (kanonische URL) |
|---|---|---|
| Basis-URL | Body | `https://<host>/<prefix>/` (mehrere Körperschaften: `…/?__cpanr=N`) |
| `gr0040` (Auswahl Wahlperiode, `__cwpnr`) | LegislativeTerm (am Body) | `…/gr0040.asp?__cwpnr=N` |
| `si0057?__ksinr=` | Meeting inkl. TOPs (Ö/NÖ, Beschlüsse) | `…/si0057.asp?__ksinr=N` |
| TOP in `si0057` | AgendaItem | `…/si0057.asp?__ksinr=N#agendaitem/kvonr-K` (mit Vorlage), sonst `…#agendaitem/nr-3.2` bzw. `nr-N1` (nichtöffentlich), ohne Nummer `…#agendaitem/pos-7` |
| `si0050?__ksinr=` | Sitzungsdokumente (Einladung/Niederschrift) | `…/getfile.asp?id=N&type=do` |
| `vo0050?__kvonr=` | Paper (Betreff, Nummer, Art) + Anlagen-PDFs | `…/vo0050.asp?__kvonr=N` |
| `gr0040` | Organizations (Gremien) | `…/kp0040.asp?__kgrnr=N` |
| `kp0040?__kgrnr=` | Persons + Memberships (inkl. Stimmrecht) | `…/pe0051.asp?__kpenr=N` |
| TOP ↔ Vorlage | Consultation | `…/vo0050.asp?__kvonr=N#consultation/<ksinr>` |

PDF-Anlagen laufen automatisch durch die bestehende OCR-Queue
(`text_extraction_status='pending'` → pypdf → Tesseract → Mistral) und
werden in Elasticsearch indexiert.

### Kennung der Tagesordnungspunkte

Die Sitzungsseite (`si0057`) nennt je Punkt weder eine eigene Kennung noch einen Link auf eine Detailseite
(geprüft an den Golden Files aus Lüdenscheid und Eschweiler). Stabil ist nur die Vorlage (Issue #863):

- **Punkt mit Vorlage:** `…#agendaitem/kvonr-<__kvonr>`, gleich unter welcher Nummer und in welchem Teil der Sitzung
  er steht. Fügt die Verwaltung davor einen Punkt ein oder nummeriert um, bleibt die Kennung. Berät die Sitzung
  dieselbe Vorlage an mehreren Punkten, zählen die späteren weiter (`kvonr-<__kvonr>-2`).
- **Punkt ohne Vorlage** (Eröffnung, Mitteilungen, Anfragen, Gliederungspunkte wie „Bauordnung“): keine stabile
  Quelle der Kennung. Er heißt nach seiner Nummer (`nr-3.2`, im nichtöffentlichen Teil `nr-N1` wie „N 1“ auf der
  Seite), ohne Nummer nach seiner Position (`pos-7`). Nach einer Einfügung davor trägt die Kennung einen anderen
  Punkt; Work hängt seine Daten über den fachlichen Anker um (#547). Bekommt ein Punkt nachträglich eine Vorlage,
  erhält er eine neue Kennung.
- **Punkte, die die Tagesordnung nicht mehr liefert** (abgesetzt, nachträglich mit Vorlage, umnummeriert ohne
  Vorlage), markiert der Abruf nach derselben Regel wie Sitzungen und Gremien als gelöscht (`quelle_geloescht`,
  Ereignis `ris.object.depublished`, nichtöffentliche Punkte nur nichtöffentlich): nur in Vollabgleichen ohne
  Fehler, nur für Sitzungen, deren Tagesordnung der Lauf gelesen hat (Seite lesbar, mindestens ein Punkt), und erst
  nach `SCRAPER_TOMBSTONE_FULL_CRAWLS` solchen Läufen in Folge. Liest ein Vollabgleich die Sitzung nicht (oder gar
  keine Tagesordnung), beginnt die Zählung ihrer Punkte von vorn. Nur Punkte in der Form seit #863; Zähler in
  `scraper_state.missing.agendaitem`. Taucht ein Punkt wieder auf, hebt der Upsert die Markierung auf.
- **Bremse je Sitzung:** Für Tagesordnungspunkte gilt nicht die Bremse je Quelle (`SCRAPER_TOMBSTONE_MAX_MISSING`),
  sondern je Sitzung: Fehlt mehr als die Hälfte ihrer Punkte, etwa weil ein ganzer Teil der Seite fehlt, zählt für
  diese Sitzung nichts, und ihre Zähler bleiben stehen. Was der Abruf vor #863 nie gelöscht hat, hält so den Abgleich
  der übrigen Sitzungen nicht an. `last_run.agenda_braked` nennt gebremste Sitzungen und fehlende Punkte; die Quelle
  bekommt dafür keine Warnung im Betriebsmonitor.

**Umstellung des Bestands.** Bis Issue #863 war die Nummer die Kennung (`…#agendaitem/3.2`, `N1`). Der erste Abruf
einer Sitzung mit der neuen Form schreibt ihre Punkte in place um: `external_id` neu, `id` bleibt (wie beim Umzug
einer Quelle, ADR `docs/adr/20260929-kanonisches-modell.md`). Mit um ziehen die Kennung in `raw_json` des Punkts, die
Verweise der Beratungen (Spalte `agenda_item_external_id` und `raw_json.agendaItem`) und die Tagesordnung in
`raw_json` der Sitzung; der Änderungsfeed meldet dabei nichts, und Ereignisse verweisen auf die Zeile im Bestand.
Notizen, Positionen und Suchindex hängen an der Zeile und bleiben gültig; `check_ris_ids` zählt die Punkte danach
als „Kennung abweichend“, das ist gewollt. Zuordnung:

1. Punkt mit Vorlage: die Zeile mit derselben Beratung, auch wenn die Nummer sich seit dem letzten Abruf geändert hat.
2. Punkt ohne Vorlage: die Zeile ohne Vorlage mit demselben Namen und gleich öffentlich, wenn der Name auf beiden
   Seiten nur einmal vorkommt; sonst die Zeile unter seiner bisherigen Nummer (ohne Vorlage, gleich öffentlich),
   die der Abruf bisher überschrieben hätte.
3. Übrige Zeilen (der Punkt steht nicht mehr auf der Tagesordnung) bekommen die Kennung nach ihren eigenen Angaben
   (`kvonr-…`, `nr-…`, bei Bedarf mit Zählung) und fallen dann unter den Löschabgleich.

Die abgelöste Kennung bleibt als `mandari:previousId` an Punkt und Tagesordnung stehen; der Abruf schreibt sie bei
jedem Speichern wieder dazu. Sitzungen, die kein Abruf mehr liest (außerhalb des Kalenderfensters), behalten ihre
Kennungen. Eine Kennung der alten Form vergibt der Adapter nie wieder; die behaltene `id` stößt deshalb mit keiner
neuen Zeile zusammen.

**Nicht ohne Rückweg zurückfallen.** Ein Ingestor von vor #863 vergibt wieder die Nummer als Kennung und will mit der
`id` einer umgeschriebenen Zeile eine neue anlegen: Der Primärschlüssel ist vergeben, und der ganze Abruf der Quelle
bricht ab. Vor einem Rückfall je SessionNet-Quelle den Daemon für die Quelle anhalten (`is_active = false`) und

```bash
docker compose exec ingestor python -m src.main top-kennungen-zurueck --source <URL der Quelle>              # zählt
docker compose exec ingestor python -m src.main top-kennungen-zurueck --source <URL der Quelle> --ausfuehren # schreibt
```

ausführen. Der Befehl gibt umgeschriebenen Zeilen genau ihre abgelöste Kennung zurück (`mandari:previousId`); danach
passt jede `id` wieder zu ihrer Kennung. Er endet mit Fehler, wenn Zeilen ohne Rückweg bleiben; dann nicht
zurückfallen. Punkte, die seit der Umstellung neu angelegt wurden, bleiben in der neuen Form, und der ältere Abruf
legt sie ein zweites Mal an (Dublette, kein Abbruch).

`--neue-punkte` schreibt auch diese Punkte auf die Kennung um, die der ältere Abruf vergäbe, sofern frei. Ihre `id`
passt danach nicht mehr zur Kennung. Nur für einen dauerhaften Rückfall verwenden: **Wird nach einem Rückweg mit
`--neue-punkte` wieder auf den neuen Abruf umgestellt, kann er solchen Zeilen eine andere als ihre ursprüngliche
Kennung geben; liefert die Seite später die ursprüngliche, scheitert der Abruf am Primärschlüssel.** Ohne
`--neue-punkte` ist ein späteres erneutes Umstellen sicher: Es läuft wie die erste Umstellung.

### Änderungserkennung & Löschungen

- **Listen-Diffing**: Je Kalendermonat wird ein Hash der Sitzungsliste in
  `sync_config["scraper_state"]["list_snapshots"]` gehalten; unveränderte
  Monate werden im inkrementellen Lauf übersprungen.
- **Content-Hash je Entität**: `mandari:contentHash` im `raw_json`;
  Upsert nur bei Differenz. `modified` ist die Crawl-Zeit des letzten
  echten Updates — unsere eigene OParl-Ausgabe bleibt damit inkrementell
  konsumierbar.
- **Verschwinden ≠ Löschen**: Objekte, die in **3 aufeinanderfolgenden
  Full-Crawls** (konfigurierbar: `SCRAPER_TOMBSTONE_FULL_CRAWLS`) nicht
  mehr gesehen wurden, werden per `mark_entity_deleted` tombstoned
  (`deleted=true`, ES-Dokument entfernt) — nie physisch gelöscht.
  Taucht ein Objekt wieder auf, hebt der Upsert-Pfad den Tombstone
  automatisch auf. Erfasst werden derzeit Sitzungen im Crawl-Fenster und
  Gremien (konservativ, um Historie außerhalb des Fensters nie fälschlich
  zu tombstonen).
- **Nur vollständige Full-Crawls zählen** (Issue #556): Ist eine Seite
  nicht lesbar (Netzfehler, Zeitüberschreitung, 4xx, 5xx) oder keine
  SessionNet-Seite, endet der Crawl am Detailseiten-Budget oder fehlt ein
  Kalendermonat, bleibt der betroffene Typ in diesem Lauf unvollständig:
  Seine Zähler bleiben stehen. Eine Sitzung, die der Kalender nennt, deren
  Seite aber nicht lesbar ist, gilt als gesehen. `scraper_state.last_run`
  nennt `complete`, `failed_pages` (davon tote Verweise: `gone_pages`) und
  je Typ den Grund (`incomplete`).
- **Keine SessionNet-Seite** ist jede Seite ohne das SessionNet-Layout
  (Klassen `smc…`), also Sperr-, Prüf- und Hinweisseiten mit Status 200 –
  auch wenn sie den Produktnamen nennen: Prüfseiten übernehmen die
  angefragte Adresse (üblicher Installationspfad `/sessionnet/…`) in
  Formular, Weiterleitung oder Titel. Eine Kalenderseite muss zudem die
  Tageszeilen des Monats tragen (`td.silink`, auch an Tagen ohne Sitzung);
  fehlen sie, ist der Monat nicht lesbar und ergibt keine leere Liste.
- **Bremse**: Fehlen in einem Lauf mehr als 10 Objekte eines Typs
  (`SCRAPER_TOMBSTONE_MAX_MISSING`), zählt der Lauf keines davon
  (`last_run.tombstone_braked`, Konsole „gebremst“) – Quelle prüfen. Der
  Betriebsmonitor zeigt die Quelle dann als „Warnung“ („Löschabgleich
  gebremst“), der Admin nennt unter „Scraper-Status“ gebremste und
  unvollständige Typen.
- **Aktualität**: Der Admin zeigt je Quelle den letzten vollständigen
  Abgleich („Vollständig abgeglichen“). Vollständig heißt: jede Liste ganz
  gelesen und keine Seite gestört (Netzfehler, Zeitüberschreitung, 4xx außer
  404/410, 5xx, Sperr- oder fremde Seite). Ein toter Verweis auf eine
  Detailseite (die Quelle antwortet selbst mit 404/410) und eine einzelne
  nicht auswertbare Detailseite (etwa eine Vorlage ohne Betreff) zählen
  nicht, sonst rückte die Aktualität bei einem dauerhaft toten Verweis nie
  vor; gehäuft fallen sie über die Parse-Quote auf (unter 80 % gilt der
  Lauf als fehlerhaft und damit nicht als vollständig). Ein abgebrochener
  Crawl setzt `last_sync` und den Fehlerstatus der Quelle nicht mehr
  zurück; nach mehreren Fehlschlägen greift die Schonung wie bei
  OParl-Quellen.

### Monitoring

- Prometheus: `mandari_ingestor_scraper_pages_fetched_total`,
  `mandari_ingestor_scraper_parse_failures_total{page_type=…}`,
  `mandari_ingestor_scraper_parse_quota` je Quelle.
- Fällt die Parse-Quote eines Laufs unter 80 %
  (`SCRAPER_PARSE_QUOTA_WARN`), wird gewarnt und der Lauf als fehlerhaft
  im SyncLog vermerkt — typisches Symptom eines Frontend-Redesigns der
  Instanz (Parser-Bruch).
- Golden-File-Tests (`ingestor/tests/test_sessionnet_parser.py`) mit
  eingefrorenen HTML-Fixtures zweier realer Instanzen und einer pseudonymisierten Instanz mit
  mehreren Körperschaften (`tests/test_sessionnet_mehrere_koerperschaften.py`) sichern die Parser
  in CI ab.
- robots-Sperren erscheinen als Fehlerklasse `robots_blocked` im
  Betriebsmonitor (Admin → Monitoring) und in der Alarmmail, mit Grund und
  Empfehlung; die Quelle wird täglich erneut geprüft (#116).

---

## 2. ALLRIS via oparl-bridge (externer Container)

**ALLRIS 4/net** (CC e-gov) rendert sein Frontend per Apache-Wicket/Ajax —
klassisches HTML-Scraping reicht dort nicht.
[Aeroid/oparl-bridge](https://github.com/Aeroid/oparl-bridge) (MIT,
FastAPI + Playwright + SQLite) scrapt eine ALLRIS-Instanz (Wicket-UI plus
die XML-API der ALLRIS-Windows-App, `/app01`) und publiziert die Daten als
OParl-1.1-API. Mandari konsumiert diese API als **ganz normale
OParl-Quelle** — null Änderungen am Ingestor-Kern.

**Gepinnte Version: `v0.2.0`**
(Commit `930d0a6a9005c79758dfaaa6ea52b044158e17a0`, 2026-06-14).
Kein Fork im Mandari-Repo — die Bridge ist eine externe Abhängigkeit;
Updates bewusst durch Neu-Pinnen übernehmen.

Bewertungs-Kurzfassung (Code-Review 2026-07-20):

- Funktionsfähig und aktiv gepflegt; eigene Tests + OParl-Validierungs-
  Skript (`scripts/validate_oparl.py`); saubere Struktur
  (scraper/normalizer/db/api getrennt).
- Ein Deployment je ALLRIS-Instanz (keine Mandantenfähigkeit).
- OParl-Ausgabe: Listen ohne Pagination (`links: {}`) und ohne
  `modified_since`-Filter — für unseren Ingestor unkritisch: er folgt
  `links.next` nur wenn vorhanden und vergleicht Objekte ohnehin
  client-seitig (`batch_check_entities_exist`); überzählige
  Query-Parameter ignoriert die Bridge.
- Initial-Sync historischer Daten dauert je nach Kommune 10–40 Minuten
  (Playwright); PDFs werden on-demand geproxiet, nicht gespeichert.
- Kein offizielles Docker-Image → eigenes Dockerfile nötig (Beispiel
  unten). Playwright/Chromium bleiben damit aus dem Ingestor-Container
  heraus.

### Deployment (Beispiel)

Beispieldateien: [`docs/examples/oparl-bridge.Dockerfile`](examples/oparl-bridge.Dockerfile)
und [`docs/examples/docker-compose.oparl-bridge.yml`](examples/docker-compose.oparl-bridge.yml).

```bash
# Image einmalig aus der gepinnten Upstream-Version bauen
docker build -f docs/examples/oparl-bridge.Dockerfile \
  -t mandari/oparl-bridge:v0.2.0 \
  https://github.com/Aeroid/oparl-bridge.git#v0.2.0
```

```yaml
# Auszug: ein Bridge-Container je ALLRIS-Kommune
services:
  oparl-bridge-musterstadt:
    image: mandari/oparl-bridge:v0.2.0
    environment:
      OPARL_ALLRIS_BASE_URL: "https://ratsinfo.musterstadt.de/allris"
      OPARL_API_BASE_URL: "https://oparl-musterstadt.intern.mandari.de"
      OPARL_BODY_NAME: "Stadt Musterstadt"
      OPARL_SCRAPER_DELAY_MS: "2000"
    volumes:
      - oparl_bridge_musterstadt:/data
    restart: unless-stopped
```

### Anbindung in Mandari

1. Bridge-Container starten, initialen Sync abwarten
   (`oparl-bridge-sync`-CLI bzw. UI der Bridge), OParl-Ausgabe prüfen:
   `curl https://<bridge-host>/` (System-Objekt) und `/bodies`.
2. Quelle im Django-Admin anlegen:
   - **URL**: System-Endpoint der Bridge (z. B. `https://<bridge-host>/`)
   - **Sync config**: `{"source_type": "bridge:allris"}`
3. Sync auslösen — die Quelle verhält sich wie jede OParl-API. Da die
   Bridge kein `modified_since` filtert, greift automatisch der
   client-seitige Abgleich des Ingestors (vorhandene Mechanik, Issue #22).

---

## 3. Politeness-Defaults (alle Scraper-Quellen)

- **User-Agent**: `mandari-ingestor/<Version> (+https://mandari.de/ingestor/; support@mandari.de)`
  (Env `INGESTOR_USER_AGENT`, ältere Schreibweise `SCRAPER_USER_AGENT`). Er gilt für
  OParl-Client, Scraper und Textextraktion. Django (Dokument-Cache, Textextraktion,
  Vorschau, Personenfotos, `add_oparl_source`) meldet sich mit demselben Produkt-Token,
  nie mit einem Browser-User-Agent. Der User-Agent ist **je Quelle** im
  Admin überschreibbar (Feld *User-Agent*, leer = Standard). Er nennt Produkt-Token,
  Version, die Infoseite für Betreiber und die Kontaktadresse. Mindestens eine Quelle
  filtert das Wort der Infoseite im User-Agent und antwortet mit HTTP 403 (Issue #123).
  Für solche Quellen setzen wir im Admin einen User-Agent ohne den Pfad der Infoseite.
  Wie der Ingestor eine Sperre erkennt und was dann zu tun ist: `docs/MONITORING.md`,
  Abschnitt „Sperren und 5xx-Serien“.
- **Rate-Limit**: max. 1 Request / 2 s je Host (konfigurierbar je Quelle),
  `max_concurrent=1` — RIS-Server kleiner Kommunen sind schwachbrüstig. Der Abstand
  gilt über alle Quellen und Prozesse hinweg (Drossel je Host, gemeinsamer Zeitstempel
  in Redis; siehe `request_interval` oben).
- **robots.txt**: nach RFC 9309 mit Platzhaltern (`*`, `$`), 24-h-Cache je Host,
  getrennt für Seiten bzw. Schnittstelle und Dateien. Disallow → kein Abruf; ist die
  Basis-URL gesperrt, wird die Quelle nicht gecrawlt und im Admin markiert. Fehlt die
  robots.txt (4xx), ist alles erlaubt. Ist sie nicht erreichbar (5xx, 408, 429, Netzfehler),
  werden Abrufe zurückgestellt, bis ein Abruf gelingt (Störung, keine Sperre). Für Dateien gilt
  der User-Agent aus `download_headers`, auch beim Abruf der robots.txt. Ausnahmen nur mit Freigabe und Vermerk
  (`manage.py robots_override`), Überblick mit `manage.py robots_report`
  (`docs/MONITORING.md`).
- **Keine Umgehung** von Logins, CAPTCHAs oder Session-Schranken — nur
  öffentliche Bürgerinfo-Bereiche.
- **Full-Crawls** sind selten (Scheduler-Nachtfenster) und gestaffelt;
  beim Onboarding einer Kommune wächst der OCR-Backlog sprunghaft →
  Kommunen einzeln aufschalten.

## 4. Ausgeschlossene Systeme

Für gehostetes **Sternberg RIM** (JavaScript-verifizierende WAF, opake Kennungen),
**regisafe** und **komuna** (`Disallow: /`, Single-Page-Apps) bauen wir keinen
Adapter und umgehen nichts; robots.txt ist nach § 44b UrhG ein Nutzungsvorbehalt.
Stattdessen bieten wir der Kommune den Kooperationspfad an (OParl aktivieren oder
unseren User-Agent per robots.txt erlauben). Begründung je System, Wiedervorlage und
Textvorlage: [ADR 2026-09-17](adr/20260917-kein-scraping-sternberg-regisafe-komuna.md).

## 5. Opt-out- / Takedown-Prozess

Kommunen (oder Betroffene) können sich jederzeit über die auf der
Infoseite für Betreiber (<https://mandari.de/ingestor/>) genannten Kontakte melden. Ablauf:

1. **Drosseln/Pausieren**: Quelle im Admin deaktivieren (`is_active`)
   oder `rate_limit_seconds` erhöhen — wirkt ab dem nächsten Lauf der Quelle.
   Alternativ setzt die Kommune ein robots.txt-Disallow für unseren
   User-Agent (`mandari-ingestor`); der Ingestor respektiert das
   automatisch.
2. **Inhalte entfernen (Takedown)**: Objekte werden zunächst tombstoned
   (`deleted=true` — aus Portalen und Suche verschwunden, Daten bleiben
   für Prüfzwecke). Endgültiges physisches Löschen ausschließlich über
   das bestehende Django-Kommando:

   ```bash
   python manage.py purge_deleted --dry-run   # Vorschau
   python manage.py purge_deleted --yes       # endgültig löschen
   ```

3. **Abwägung**: Opt-out-Wünsche werden geprüft, nicht blind ausgeführt
   (Transparenzinteresse an amtlichen Informationen); mindestens
   Drosselung/Nachtfenster wird immer angeboten. Reaktionszeit: 5 Werktage.

## 6. OParl 1.0 anbinden (more! rubin auf gremien.info)

Streng genommen keine Nicht-OParl-Quelle, aber der gleiche Anbindungsweg:
more! rubin liefert auf `*.gremien.info` standardmäßig **OParl 1.0** — auch
ohne Registrierung bei der Kommune. Der Ingestor toleriert die 1.0-Eigenheiten
seit Issue #122; die Kommune muss nichts freischalten.

### Erkennungsmerkmale

| Merkmal | Beobachtung (drei geprüfte Mandanten, September 2026) |
|---|---|
| System-URL | `https://<mandant>.gremien.info/oparl/system` (auch `/oparl` liefert das System) |
| Namespace | `oparlVersion` und alle `type`-URLs unter `https://schema.oparl.org/1.0/` |
| 1.1-Pfade | `/oparl/v1.1/...` antwortet mit **HTTP 200** und einem Fehlerobjekt `{"type": ".../1.0/Error", "message": "Requested class doesn't exist."}` |
| Body-Liste | `system.body` zeigt auf `/oparl/Body` (Standard-Liste mit `data[]`, `pagination`, `links`) |
| Body-Felder | nur `organization`, `person`, `meeting`, `paper`; **keine** Listen für Membership, AgendaItem, File, Location; `legislativeTerm` und `location` eingebettet |
| Eingebettete Objekte | Membership im Person-Objekt, AgendaItem/Dateien in Meeting, `mainFile`/`auxiliaryFile`/`consultation` in Paper (inkl. Volltext im Feld `text`) |
| Zeitstempel | `created` = `modified` = Abrufdatum um Mitternacht bei **allen** Objekten; Organisationen ganz ohne Stempel |
| `modified_since` | wird stillschweigend ignoriert (Parameter fehlt in `links.self`/`links.next`, `totalElements` unverändert) |
| Sortierung | Paper aufsteigend (neueste auf der letzten Seite), Meetings absteigend |
| Vendor-Felder | `more-it-solutions:internal`, `more-software-gmbh:faction_id`, … werden ignoriert, bleiben im `raw_json` |
| robots.txt | `Disallow: /config/` und `/documents.php` — die OParl-Pfade sind erlaubt |

### Was der Ingestor daraus macht

- **Autodiscovery**: Fehlerobjekt oder 404 auf einem 1.1-Pfad → Rückfall auf
  die 1.0-Pfadvarianten (`/oparl/system`, dann `/oparl`). Die erkannte Version
  landet in `OParlSource.oparl_version` (Admin, schreibgeschützt). Nur bei
  "Pfad existiert nicht"-Signalen, nie bei Timeouts oder 5xx.
- **Fehlerobjekte** werden nie als Datensatz verarbeitet.
- **Ignoriertes `modified_since`** wird an den Listen-Links erkannt; der Host
  steht dann als „filtert nicht“ in `sync_config["unfiltered_hosts"]` dieser
  Quelle, ihre Folgelisten senden den Parameter nicht mehr. Andere Quellen,
  auch auf demselben Server, betrifft der Vermerk nicht (Issue #1041).
- **Änderungserkennung ohne Zeitstempel**: Objekte mit synthetischem oder
  fehlendem `modified` bekommen wie Scraper-Quellen einen Content-Hash
  (`mandari:contentHash` im `raw_json`). Der inkrementelle Lauf upsertet nur
  bei Inhaltsänderung; die Early-Stop-Regel (fünf unveränderte Seiten) greift
  damit wieder.

### Grenzen

- Aufsteigend sortierte Listen (Paper): neue Vorlagen stehen am Ende, der
  inkrementelle Lauf bricht vorher ab. Sie kommen spätestens mit dem
  täglichen Voll-Sync (`FULL_SYNC_INTERVAL_HOURS`, Standard 24 h).
- Ein Voll-Sync zieht die komplette Liste (Beispiel: 34 Paper-Seiten à
  100 Objekte mit Volltext, ca. 0,6 MB je Seite). Kommunen einzeln
  aufschalten, nicht Dutzende an einem Tag.
- Volltexte kommen bereits im JSON (`text`); PDFs müssen dafür nicht geladen
  werden. Die OCR-Queue behandelt sie wie sonst auch.
- Löschungen werden über `deleted: true` geliefert; ob more! rubin gelöschte
  Objekte in den Listen behält, ist nicht belegt.

### Quelle anlegen

1. System-URL prüfen (eine Anfrage, unser User-Agent):

   ```bash
   curl -sS -A "Mandari-Ingestor/2.0 (https://github.com/mandariOSS/mandari)" \
     https://<mandant>.gremien.info/oparl/system | head -c 400
   ```

   Erwartet: `"oparlVersion":"https://schema.oparl.org/1.0/"` und `"body":".../oparl/Body"`.

2. Quelle und Bodies anlegen — das Kommando folgt der Body-Liste inklusive
   Paginierung und speichert `oparl_version`:

   ```bash
   python manage.py add_oparl_source "https://<mandant>.gremien.info/oparl/system"
   ```

   Die Quelle ist danach inaktiv, alle neuen Bodies sind ungelistet (Issue #555).
   Verbandsgemeinden liefern mehrere Bodies (Ortsgemeinden, Zweckverbände);
   die gewünschten Bodies nach der Prüfung im Admin listen (`is_listed`), die
   übrigen ungelistet lassen oder löschen. Ist die Quelle schon registriert,
   bleibt ihr Status (aktiv oder abgeschaltet) unverändert.

3. Erstlauf als Voll-Sync anstoßen (Admin-Aktion „Voll-Sync" oder
   `mandari-ingestor sync --body <System-URL> --full`); regelmäßig übernimmt
   der Daemon erst, wenn die Quelle im Admin aktiviert ist. Nach dem
   Vollabgleich prüft der Ingestor den Filter; im Log steht
   „modified_since-Prüfung für <mandant>.gremien.info: ignored".

4. Nach dem ersten inkrementellen Lauf im Admin prüfen: `oparl_version = 1.0`,
   `sync_config.unfiltered_hosts` enthält den Host (Vermerk „filtert nicht“,
   gilt nur für diese Quelle, Issue #1041), im SyncLog stehen nur noch
   wenige neue/aktualisierte Objekte.

5. Geo-Daten zuordnen (OSM-Grenze, Gemeindeschlüssel, Kartenausschnitt,
   Straßen, Adressen; Zweckverbände und Gesellschaften als „keine
   Gebietskörperschaft“), erst als Probelauf:

   ```bash
   python manage.py resolve_body_geodata --source <mandant>.gremien.info --dry-run
   python manage.py resolve_body_geodata --source <mandant>.gremien.info
   ```

   more! rubin liefert keinen Gemeindeschlüssel; die Ortsgemeinden werden per
   Name im Gebiet der Verbandsgemeinde gefunden. Mehrdeutiges steht danach im
   Admin unter „Geo-Vorschläge“. Einzelheiten: `docs/INSIGHT_GEO.md`.
