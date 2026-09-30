# OParl-Aggregations-API

mandari stellt die gespiegelten Ratsinformationen aller angebundenen Kommunen als
**eigene, OParl-1.1-konforme Datenquelle** bereit (Issue #17). Statt 50+ kommunale
OParl-Endpunkte einzeln abzufragen, genügt ein einziger Endpunkt — inklusive
mandari-Anreicherungen (KI-Zusammenfassungen, Volltexte, stabile Datei-Proxies).

- **Lesend, anonym, JSON** — keine Authentifizierung nötig
- **CORS offen** (`Access-Control-Allow-Origin: *`)
- **Rate-Limit**: standardmäßig 120 Anfragen/Minute je IP (HTTP 429 bei Überschreitung)
- **Spezifikation**: https://oparl.org/spezifikation/

## Basis-URL

Die Basis-URL ist über die Umgebungsvariable `OPARL_BASE_URL` konfigurierbar
(Standard: `SITE_URL` + `/oparl`). Alle Objekt-IDs und Listen-Links werden aus
dieser Basis gebaut — die API ist damit host-unabhängig und kann auch unter einer
eigenen Subdomain (z. B. `oparl.mandari.de`) ausgeliefert werden.

Einstiegspunkt ist das System-Objekt:

```bash
curl https://mandari.de/oparl/v1/system
```

## Endpunkte

| Endpunkt | Inhalt |
|----------|--------|
| `GET /oparl/v1/` | JSON-Übersicht über die API |
| `GET /oparl/v1/system` | OParl-System-Objekt (Einstiegspunkt) |
| `GET /oparl/v1/bodies` | Externe Liste aller Kommunen (paginiert) |
| `GET /oparl/v1/body/<uuid>` | Einzelne Kommune |
| `GET /oparl/v1/body/<uuid>/organizations` | Gremien der Kommune (paginiert, filterbar) |
| `GET /oparl/v1/body/<uuid>/people` | Personen der Kommune (paginiert, filterbar) |
| `GET /oparl/v1/body/<uuid>/meetings` | Sitzungen der Kommune (paginiert, filterbar) |
| `GET /oparl/v1/body/<uuid>/papers` | Vorlagen/Drucksachen der Kommune (paginiert, filterbar) |
| `GET /oparl/v1/body/<uuid>/locations` | Orte der Kommune (Vendor-Erweiterung, paginiert) |
| `GET /oparl/v1/<typ>/<uuid>` | Objekt-Endpunkte aller Typen (siehe unten) |

Objekttypen für `<typ>`: `body`, `organization`, `person`, `membership`, `meeting`,
`agendaitem`, `paper`, `consultation`, `file`, `location`, `legislativeterm`.

Einbettungen gemäß OParl 1.1: `Body.legislativeTerm`, `Person.membership`,
`Meeting.agendaItem`, `Meeting.location`, `Meeting.invitation`/`auxiliaryFile`,
`Paper.consultation`, `Paper.mainFile`/`auxiliaryFile` werden als vollständige
Objekte eingebettet; alle übrigen Referenzen sind URLs auf diese API.

## Konformität zu OParl 1.1

| Eigenschaft | Ausgabe |
|---|---|
| `Organization.organizationType` | einer der sieben Werte der Spezifikation (`Gremium`, `Partei`, `Fraktion`, `Verwaltungsbereich`, `externes Gremium`, `Institution`, `Sonstiges`). Angaben der Quelle werden zugeordnet (Tabelle unten); Unbekanntes gilt als `Sonstiges`. Weicht die Angabe der Quelle ab, steht sie zusätzlich in `mandari:originalOrganizationType` |
| `File.date` | Datum `yyyy-mm-dd`: der Tag, den die Quelle nennt. Nennt sie einen Zeitpunkt, gilt dessen Tag in der Zeitzone der Installation; der Zeitpunkt der Quelle steht in `created` |
| `Meeting.location` | immer ein Location-Objekt: das der Quelle oder, wenn der Ort im Bestand nur als Text an der Sitzung steht, eines mit der Kennung der Sitzung (`…/v1/location/<Kennung der Sitzung>`, `description` aus Ort und Anschrift). Das gilt für Quellen, die den Ort nur als Text nennen, und für Session-Mandanten (siehe „Orte aus Text“) |
| `Body.locationList` | URL der Orte-Liste (Standardfeld; zuvor nur `mandari:locationList`) |
| `Body.legislativeTerm` | immer vorhanden (Pflichtfeld), ohne Wahlperiode als leere Liste |
| `System.license` | nur, wenn der Betreiber `OPARL_LICENSE_URL` setzt; sonst gilt die Lizenz der Kommune am Body |
| Bedingte Anfragen | `ETag` an jeder erfolgreichen Antwort, `If-None-Match` ergibt `304` |

Zuordnung von `organizationType` (Groß- und Kleinschreibung spielt keine Rolle):

| Angabe der Quelle | `organizationType` |
|---|---|
| einer der sieben Werte der Spezifikation | bleibt (Schreibweise vereinheitlicht) |
| Ausschuss, Rat, Beirat, Kommission, Hauptorgan, Hilfsorgan (auch Mehrzahl, z. B. „Ausschüsse“, „Gremien“) | `Gremium` |
| Fraktionen, Parteien, Institutionen | `Fraktion`, `Partei`, `Institution` |
| Amt, Fachbereich, Dezernat, Dienststelle, Organisationseinheit, Verwaltung (auch Mehrzahl) | `Verwaltungsbereich` |
| Schlüssel eines Session-Mandanten (`committee`, `council`, `advisory`, `commission`, `faction`, `department`, `other`) | wie in `SESSION_OPARL_API.md` |
| alles andere | `Sonstiges` |

Einige RIS ordnen ihre Gremien nach dem Kommunalrecht ein: „Hauptorgan“ (Rat, Kreistag) und
„Hilfsorgan“ (Ausschüsse, Beiräte) sind Gremien, „Amt“, „Dienststellen“ und „Organisationseinheit“
gehören zur Verwaltung. Die Zuordnung steht in `mandari/hub/ris/mapping/bestand.py`
(`_ORGANIZATION_TYPE_SYNONYMS`).

### Bedingte Anfragen (ETag, 304)

Jede erfolgreiche JSON-Antwort trägt einen `ETag` über ihren Inhalt und `Cache-Control: no-cache`.
Eine Anfrage mit `If-None-Match: <ETag>` erhält `304 Not Modified` ohne Inhalt, solange sich die
Antwort nicht geändert hat:

```bash
curl -i https://mandari.de/oparl/v1/system                       # ETag: "5f2c…"
curl -i -H 'If-None-Match: "5f2c…"' https://mandari.de/oparl/v1/system   # 304
```

Der `ETag` hängt am Inhalt, nicht an einem Zeitstempel. Anfragen mit `If-None-Match` zählen zum
Rate-Limit; für den laufenden Abgleich bleibt `modified_since` der richtige Weg.

### Hinweise für bestehende Abnehmer

Adressen und IDs bleiben unverändert. Geändert haben sich Werte zweier Felder: `organizationType`
reichte bisher die Angabe der Quelle unverändert durch (bei Session-Mandanten deren interne Schlüssel
wie `committee`) und `File.date` war ein Zeitpunkt. Wer die Angabe der Quelle braucht, liest
`mandari:originalOrganizationType`; wer den Zeitpunkt braucht, liest `created`. Die Felder
`mandari:locationName`, `mandari:locationAddress` und `mandari:locationList` sind **abgekündigt** und
entfallen frühestens am 01.10.2027; Ersatz sind `Meeting.location` und `Body.locationList`.

### Prüfung

`python scripts/oparl_validator.py --validator <Pfad zu oparl-validator-rs>` baut eine Instanz mit
Demo-Daten, startet sie auf dem eigenen Rechner und prüft Aggregator und Session-Schnittstelle:
mit dem externen Validator [oparl-validator-rs](https://github.com/konstin/oparl-validator-rs)
(Pflichtfelder, Feldtypen, externe Listen, Abrufbarkeit verlinkter Objekte) und mit einer eigenen
Typprüfung (Datums- und Zeitformate, `organizationType`, unbekannte Eigenschaften, gelöschte
Objekte; `mandari/hub/api/tests/konformitaet.py`). Ohne `--validator` läuft nur die eigene
Prüfung. In der CI läuft beides im Job „OParl-Validator“ bei Änderungen an den Schnittstellen und
bei jedem Push auf `dev` und `main`. Den Hinweis des Validators auf unverschlüsseltes HTTP wertet
das Skript nicht, weil die Testinstanz lokal läuft.

## Adressen und Weiterleitungen

Die Adresse eines Objekts ist seine Kennung (`id`) und ändert sich nicht. Die Adressen des Aggregators
haben keinen Schrägstrich am Ende (`/oparl/v1/system`, `/oparl/v1/paper/<uuid>`). Wer die Schreibweise
mit Schrägstrich abruft (`/oparl/v1/system/`), wird dauerhaft auf die gültige Adresse weitergeleitet
(`301`, Parameter der Anfrage bleiben erhalten) statt auf eine Fehlerseite zu laufen. Die
Weiterleitung nennt einen Pfad ohne Host; der Abnehmer bleibt auf dem Host, über den er die
Schnittstelle erreicht. Kennungen in Antworten nennen immer die gültige Schreibweise.

## Pagination

Externe Listen liefern 100 Objekte pro Seite (`?page=N`), sortiert nach `modified`
aufsteigend (stabil für inkrementelle Clients), im OParl-Listen-Envelope:

```json
{
  "data": ["..."],
  "pagination": {
    "totalElements": 150,
    "elementsPerPage": 100,
    "currentPage": 1,
    "totalPages": 2
  },
  "links": {
    "first": ".../meetings",
    "self": ".../meetings",
    "next": ".../meetings?page=2",
    "last": ".../meetings?page=2"
  }
}
```

Zusätzlich werden die Links als HTTP-`Link`-Header (`rel="first|prev|next|last"`)
ausgeliefert.

## Zeitfilter (Zeitzonen-Pflicht!)

Alle externen Listen unterstützen `created_since`, `created_until`,
`modified_since`, `modified_until` (jeweils inklusiv, kombinierbar):

```bash
# Alle Sitzungen, die seit dem 1. 6. 2026 (UTC) geändert wurden
curl "https://mandari.de/oparl/v1/body/<uuid>/meetings?modified_since=2026-06-01T00%3A00%3A00%2B00%3A00"

# Auch das Z-Suffix ist gültig
curl "https://mandari.de/oparl/v1/body/<uuid>/meetings?modified_since=2026-06-01T00:00:00Z"
```

**Zeitstempel MÜSSEN eine explizite Zeitzone enthalten** (`+01:00`, `+00:00` oder
`Z`). Naive Zeitstempel sind mehrdeutig und werden mit HTTP 400 und einer klaren
Fehlermeldung abgelehnt — wir wiederholen den verbreiteten Zeitzonenfehler vieler
kommunaler OParl-Server bewusst nicht. Achtung: `+` in URLs als `%2B` kodieren.

Die Filter arbeiten auf denselben Werten, die als `created`/`modified`
ausgeliefert werden (OParl-Zeitstempel der Quelle, Fallback: Zeitpunkt der
Spiegelung in mandari).

## Gelöschte Objekte (Tombstones)

Objekte, die im Quellsystem gelöscht wurden (OParl `deleted: true`), werden in
mandari **nie physisch gelöscht**, sondern nur markiert. Die API verhält sich
dabei spec-konform (OParl 1.1, „Umgang mit gelöschten Objekten"):

- **Objekt-Endpunkte** liefern gelöschte Objekte weiterhin mit **HTTP 200** aus —
  als gekürztes Objekt mit ausschließlich den Pflichtfeldern:

  ```json
  {
    "id": "https://mandari.de/oparl/v1/paper/<uuid>",
    "type": "https://schema.oparl.org/1.1/Paper",
    "created": "2024-01-01T00:00:00+00:00",
    "modified": "2026-07-01T12:00:00+00:00",
    "deleted": true
  }
  ```

  `modified` entspricht dem Löschzeitpunkt; alle weiteren Attribute entfallen.
- **Externe Listen ohne Filter** (und mit rein `created_*`-/`modified_until`-Filtern)
  enthalten gelöschte Objekte **nicht**.
- **Externe Listen mit `modified_since`** enthalten die passenden gelöschten
  Objekte als Tombstones — inkrementelle Clients bekommen Löschungen so
  zuverlässig mit, ohne Voll-Sync.
- **Eingebettete Referenzen** (z. B. `Meeting.agendaItem`, `Paper.consultation`,
  `Person.membership`) lassen gelöschte Objekte aus; Tombstones erscheinen nur
  als Top-Level-Objekte.

Physische Löschung erfolgt ausschließlich manuell auf explizite Aufforderung
einer Kommune (`manage.py purge_deleted`, siehe Betrieb).

## Dateien (File)

`accessUrl` und `downloadUrl` zeigen auf den mandari-Datei-Proxy
(`/insight/dokumente/<uuid>/preview/` bzw. `…?download=1`). Vorteile:

- stabil erreichbar, auch wenn der kommunale Quellserver offline ist
- DSGVO-freundlich (Client verbindet sich nicht mit dem RIS-Server)

Die Original-URL bleibt als `mandari:originalAccessUrl` erhalten. Der extrahierte
Volltext (`text`) wird nur am Objekt-Endpunkt `/oparl/v1/file/<uuid>` ausgeliefert,
nicht in eingebetteten Datei-Objekten (Payload-Größe).

## Vendor-Attribute (Namespace `mandari:`)

| Attribut | Objekt | Inhalt |
|----------|--------|--------|
| `mandari:originalId` | alle | Original-URL des Objekts im kommunalen Quellsystem |
| `mandari:slug`, `mandari:displayName` | Body | URL-Slug / Anzeigename der Kommune |
| `mandari:locationList` | Body | abgekündigt: URL der Orte-Liste, jetzt im Standardfeld `locationList` |
| `mandari:originalOrganizationType` | Organization | Angabe der Quelle, wenn sie keiner der Werte der Spezifikation ist |
| `mandari:summary` | Paper | KI-generierte Zusammenfassung (falls vorhanden) |
| `mandari:originalAccessUrl` | File | Original-Datei-URL beim Quellserver |
| `mandari:sha256`, `mandari:pageCount` | File | SHA-256-Hash / Seitenzahl |
| `mandari:locationName`, `mandari:locationAddress` | Meeting | abgekündigt: Ortsangabe als Text, wenn die Quelle kein Location-Objekt liefert; steht jetzt in `Meeting.location` |

## Einschränkungen (v1)

- **Nicht abgebildete Referenzen**: Felder ohne Fremdschlüssel im Datenmodell
  (`Meeting.participant`, `Paper.originatorPerson`/`originatorOrganization`/
  `underDirectionOf`/`relatedPaper`, `Organization.subOrganizationOf`,
  `AgendaItem.resolutionFile`) werden ausgelassen, statt Original-URLs
  durchzureichen.
- **Lizenz**: Die Lizenz der Quelldaten wird — soweit von der Kommune angegeben —
  am Body-Objekt (`license`) durchgereicht. Eine übergreifende Angabe am System-Objekt gibt es
  nur, wenn der Betreiber `OPARL_LICENSE_URL` setzt; sie gilt laut Spezifikation für alle Objekte
  ohne eigene Angabe und setzt voraus, dass die Lizenzen der Quellen das zulassen.
- **Orte aus Text**: Location-Objekte, die aus der Textangabe einer Sitzung entstehen, stehen nicht
  in der Orte-Liste der Kommune; sie sind eingebettet und unter ihrer ID abrufbar. Dazu gehört der
  Sitzungsort eines Session-Mandanten: Er gehört zur Sitzung und steht im Bestand als Text an ihr,
  nicht als eigenes Objekt. Wird die Sitzung zurückgenommen, liefert seine Adresse nur noch ein
  gekürztes Objekt mit `"deleted": true`.
- Meetings-Protokolle (`invitation`, `resultsProtocol`, `verbatimProtocol`) und
  `Paper.mainFile` werden über die Original-Rohdaten zugeordnet; fehlt diese
  Zuordnung, erscheinen die Dateien unter `auxiliaryFile`.

## Betrieb

| Umgebungsvariable | Standard | Bedeutung |
|-------------------|----------|-----------|
| `OPARL_BASE_URL` | `SITE_URL` + `/oparl` | Basis-URL aller Objekt-IDs |
| `OPARL_API_PAGE_SIZE` | `100` | Objekte pro Listen-Seite |
| `OPARL_API_RATE_LIMIT` | `120` | Anfragen/Minute je IP (`0` = deaktiviert) |
| `OPARL_API_CACHE_SECONDS` | `60` | Cache-Dauer ungefilterter Listen-Seiten |
| `OPARL_LICENSE_URL` | leer | URL der Lizenz am System-Objekt (`license`); leer = keine übergreifende Angabe |

**Eine Serialisierung für beide Ausgaben:** Aggregator und Session-Schnittstelle
(`SESSION_OPARL_API.md`) gehen denselben Weg – Abbildung auf das kanonische Modell, dann Ausgabe
(ADR `docs/adr/20260929-kanonisches-modell.md`):

| Schritt | Aggregator | Session-Schnittstelle |
|---|---|---|
| Was ist sichtbar, unter welcher Adresse? | `mandari/hub/api/aggregator.py`, Routen in `mandari/hub/api/urls.py` | `mandari/apps/session/api/oparl.py` |
| Abbildung auf das kanonische Modell | `mandari/hub/ris/mapping/bestand.py` | `mandari/hub/ris/mapping/session.py` |
| Bausteine des Modells (Typ-URLs, Datum und Zeit, gekürzte Objekte, `organizationType`) | `mandari/hub/ris/canonical.py` | dieselben |
| Serialisierung: Zeitfilter, Blättern, Listen-Hülle, Gelöschtes in inkrementellen Listen | `mandari/hub/api/serialization.py` | dieselbe |
| HTTP-Hülle: JSON, `ETag`/`304`, Fehler, CORS, Rate-Limit | `mandari/hub/api/http.py` | dieselbe |

Wer ein Feld ergänzt oder ändert, tut das in der Abbildung der Quelle; wer das Verhalten von Listen,
Filtern oder Fehlern ändert, in `hub/api` – es gilt dann für beide Ausgaben. Das frühere Paket
`oparl_api` ist darin aufgegangen.

**Kanonische Kennungen** (ADR `docs/adr/20260929-kanonisches-modell.md`): Jedes Objekt des
RIS-Bestands trägt die Kennung `uuid5(NS_MANDARI_RIS, URI)` aus `shared/mandari_oparl/ids.py`.
Die URI ist bei Fremd-RIS die `id` der Quelle, bei Session-Mandanten die öffentliche OParl-URL auf
Basis von `SITE_URL`. Der Namensraum ist der URL-Namensraum aus RFC 9562 und ändert sich nie.
Ingestor und Django vergeben neue Kennungen mit derselben Funktion; beide Testsuiten prüfen die
gemeinsamen Testvektoren (`shared/mandari_oparl/ids_testvektoren.json`). Bestehende Kennungen bleiben
unverändert. Abweichungen im Bestand zählt ein lesender Befehl:

```bash
# je Quelle und Entität: abweichende Kennung, abweichende URI (Session), ohne URI, Kollisionen
python manage.py check_ris_ids --dry-run
python manage.py check_ris_ids --dry-run --source <UUID oder URL der Quelle> --examples 5
```

**Physische Löschung auf Aufforderung einer Kommune** (`purge_deleted`):

```bash
# Dry-Run: zeigt markierte Objekte einer Kommune
python manage.py purge_deleted --body <slug>

# Nur Markierungen älter als 30 Tage / nur bestimmte Objekte
python manage.py purge_deleted --body <slug> --older-than 30
python manage.py purge_deleted --ids <uuid> <uuid>

# Endgültig löschen (inkl. Elasticsearch-Dokumente + lokale Dateikopien)
python manage.py purge_deleted --body <slug> --yes
```

Smoke-Tests: `python scripts/smoke_oparl_api.py` (SQLite, synthetische Daten aller
zwölf Objekttypen, 95 Checks) und `python scripts/smoke_tombstones.py`
(Tombstone-Verhalten: Sync-Markierung, Portal-Ausblendung, API-Tombstones,
purge_deleted).
