# OParl-Aggregations-API

mandari stellt die gespiegelten Ratsinformationen aller angebundenen Kommunen als
**eigene, OParl-1.1-konforme Datenquelle** bereit (Issue #17). Statt 50+ kommunale
OParl-Endpunkte einzeln abzufragen, genügt ein einziger Endpunkt — inklusive
mandari-Anreicherungen (KI-Zusammenfassungen, Volltexte, stabile Datei-Proxies).

- **Lesend, anonym, JSON** — keine Authentifizierung nötig
- **CORS offen** (`Access-Control-Allow-Origin: *`)
- **Rate-Limit**: standardmäßig 120 Anfragen/Minute je IP (HTTP 429 bei Überschreitung)
- **Spezifikation**: https://oparl.org/spezifikation/
- **Für Datenportale**: ein Katalog je Kommune nach DCAT-AP.de, der auf diese Schnittstelle verweist
  ([DCAT_KATALOG.md](DCAT_KATALOG.md))

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
| `GET /oparl/v1/body/<uuid>/changes` | Änderungsfeed der Kommune (kompatible Erweiterung, nur wenn eingeschaltet; Abschnitt „Änderungsfeed“) |
| `GET /oparl/v1/body/<uuid>/snapshot` | Snapshot der Kommune mit Cursor-Übergabe (NDJSON, Einstieg in den Änderungsfeed) |
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

## Änderungsfeed (kompatible Erweiterung von OParl 1.1)

Wer eine Kommune laufend nachzieht, muss nicht alle Listen erneut abrufen: Der Änderungsfeed nennt,
was sich seit dem letzten Abruf geändert hat – einschließlich Löschungen und Rücknahmen
(ADR `docs/adr/20260929-aenderungsfeed-format.md`). OParl-1.1-Clients sind nicht betroffen;
`modified_since` bleibt erhalten.

**Einschalten:** Der Feed ist je Installation abgeschaltet, bis die Erzeuger der Ereignisse laufen
(`OPARL_CHANGES_ENABLED`, Abschnitt „Betrieb“). Eingeschaltet nennt jeder Body einer gelisteten
Kommune die Adresse seines Feeds in `mandari:changes` und die seines Snapshots in `mandari:snapshot`.

**Welche Kommunen:** Feed und Snapshot gibt es nur für Kommunen, die das Bürgerportal veröffentlicht
und in `/oparl/v1/bodies` listet. Es gelten dieselben Stände wie für die Seiten des Bürgerportals:

| Stand der Kommune | Feed und Snapshot | Cursor danach |
|---|---|---|
| veröffentlicht, gelistet (auch „Archiv“) | werden geliefert | – |
| nicht gelistet (etwa die Demo-Kommune) | `404` wie bei ausgeschaltetem Feed; der Body nennt die Adressen nicht | bleibt gültig, wenn sie wieder gelistet wird |
| vorübergehend abgeschaltet | `503` mit `Retry-After` | bleibt gültig: Es wird nichts gelöscht, Änderungen aus der Zwischenzeit folgen hinter dem Cursor |
| dauerhaft zurückgenommen | `410` (`application/problem+json`, Typ `…/kommune-zurueckgenommen`, ohne Snapshot): ihre Einträge gelten als gelöscht | gilt nicht mehr: nach der Wiederveröffentlichung `410` mit Verweis auf den Snapshot |

Rücknahme und Wiederherstellung einer ganzen Kommune ändern den Bestand, ohne dass der Feed sie
nachzeichnen kann. Wer auf das `410` hin seine Kopie gelöscht hat, bekäme die wiederhergestellten
Einträge mit seinem alten Cursor nicht zurück; deshalb beginnt danach jeder Abnehmer über den Snapshot
neu. Das gilt auch, wenn ein Session-Mandant deaktiviert und wieder aktiviert wird. Die Absagen sind
feste Antworten ohne `ETag`: Sie verraten nichts darüber, ob oder wann sich in der Kommune etwas
ändert.

```bash
curl "https://mandari.de/oparl/v1/body/<uuid>/changes?limit=100"
curl "https://mandari.de/oparl/v1/body/<uuid>/changes?after=<cursor>"
```

```json
{
  "data": [
    {
      "cursor": "Zf3n…",
      "operation": "upsert",
      "type": "https://schema.oparl.org/1.1/Paper",
      "id": "https://mandari.de/oparl/v1/paper/<uuid>",
      "modified": "2026-09-30T08:14:03+00:00"
    },
    {
      "cursor": "k9Qa…",
      "operation": "delete",
      "type": "https://schema.oparl.org/1.1/File",
      "id": "https://mandari.de/oparl/v1/file/<uuid>",
      "modified": "2026-09-30T08:15:10+00:00",
      "reason": "nichtoeffentlich"
    }
  ],
  "cursor": "k9Qa…",
  "links": {
    "self": "https://mandari.de/oparl/v1/body/<uuid>/changes?limit=100",
    "next": "https://mandari.de/oparl/v1/body/<uuid>/changes?after=k9Qa…&limit=100",
    "snapshot": "https://mandari.de/oparl/v1/body/<uuid>/snapshot"
  }
}
```

| Feld | Inhalt |
|---|---|
| `data` | höchstens `limit` Einträge (Vorgabe 100, höchstens 1000) in aufsteigender Reihenfolge |
| `cursor` | Stand für die nächste Anfrage (`after`); `links.next` ist die fertige Adresse dazu |
| Eintrag `operation` | `upsert` (neu oder geändert), `delete` (entfernt oder nicht mehr öffentlich), `redact` (Inhalte sind aus Kopien zu entfernen) |
| Eintrag `type`, `id` | Typ und Adresse des Objekts. Inhalte stehen nicht im Feed; der Abnehmer ruft das Objekt unter `id` ab |
| Eintrag `modified` | Zeitpunkt der Änderung |
| Eintrag `reason` | bei `delete` immer: `quelle_geloescht`, `zurueckgenommen` oder `nichtoeffentlich` (nennt die Quelle keinen Grund, `quelle_geloescht`); bei `redact`: `datenschutz` |
| Eintrag `cursor` | Stand unmittelbar nach diesem Eintrag |

**So liest ein Abnehmer:**

1. Einstieg über den Snapshot der Kommune (`links.snapshot`); er nennt den Cursor, ab dem der Feed
   fortsetzt. Solange das Journal einer Installation noch vollständig ist, geht es auch ohne Cursor
   von vorn.
2. `links.next` abrufen, bis `data` leer ist – dann ist der Abnehmer aktuell. (Ausnahme: Hat eine
   Anfrage einen großen Block übersprungener Ereignisse gelesen, kann eine leere Seite einen
   vorgerückten Cursor tragen; der nächste Abruf setzt dort fort, verloren geht nichts.)
3. Den `cursor` **jeder** Antwort speichern, auch den einer leeren, und beim nächsten Abruf verwenden.
4. Einträge idempotent verarbeiten: Ein Objekt kann mehrfach erscheinen. Bei `upsert` das Objekt unter
   `id` neu abrufen, bei `delete` entfernen, bei `redact` auch aus Kopien, Caches und Weitergaben
   entfernen und die Operation weiterreichen.

**Cursor:** ein opakes Token – speichern, nicht deuten. Er gilt für die Kommune und die Installation,
die ihn ausgegeben haben, und zwar `OPARL_CHANGES_RETENTION_DAYS` Tage (mindestens 30) ab seiner
Ausgabe. Jede Antwort gibt einen frischen Cursor aus; wer regelmäßig abruft, bleibt also gültig, auch
wenn sich lange nichts ändert.

**Abgelaufener Cursor:** `410 Gone` mit einer Fehlerbeschreibung nach RFC 9457
(`application/problem+json`) und dem Verweis auf den Snapshot:

```json
{
  "type": "https://docs.mandari.de/api/probleme/cursor-abgelaufen",
  "title": "Nicht mehr verfügbar",
  "status": 410,
  "detail": "Der Cursor ist nicht mehr gültig: …",
  "instance": "/oparl/v1/body/<uuid>/changes",
  "snapshot": "https://mandari.de/oparl/v1/body/<uuid>/snapshot"
}
```

**Nur Öffentliches:** Der Feed enthält ausschließlich öffentliche Änderungen. Wird ein Objekt
nichtöffentlich, erscheint es einmal als `delete` mit dem Grund `nichtoeffentlich`; was danach mit ihm
geschieht, erscheint nicht. Nichtöffentliches ist auch nicht mittelbar erkennbar: Cursor und `ETag`
einer Antwort ändern sich nur durch öffentliche Einträge (und einmal am Tag durch den Ausgabetag des
Cursors), und der Cursor lässt keine Zählung der Ereignisse erkennen.

Einträge gibt es nur für Objekte, die die Ausgabe unter ihrer Adresse ausliefert (auch als gekürztes
Objekt). Ereignisse zu anderen Objekten werden übersprungen, ohne dass sich die Antwort ändert. Eine
Ausnahme hält den Feed in Gang: Liest eine Anfrage mehr als `max(limit × 10, 1000)` Ereignisse, ohne ihre
Seite zu füllen, rückt der Cursor bis zum zuletzt gelesenen vor. Daran ist nur erkennbar, dass viele als
öffentlich gemeldete Ereignisse ohne Adresse geschehen sind.

**HTTP:** wie die übrige Schnittstelle – `ETag` und `304` für unveränderte Seiten, Rate-Limit mit
`429` und `Retry-After`, `503` mit `Retry-After` bei vorübergehend abgeschalteter Kommune, `410` bei
dauerhaft zurückgenommener (Abschnitt „Welche Kommunen“). Die Schreibweise mit Schrägstrich am Ende
leitet weiter.

**Einschränkungen:**

- Einträge gibt es für Objekte mit eigener Adresse (die Objekttypen der Tabelle „Endpunkte“).
  Eine erfasste Abstimmung erscheint als `upsert` ihres Tagesordnungspunkts.
- Die Rücknahme einer Abstimmung nennt derzeit keinen Tagesordnungspunkt und ergibt deshalb keinen
  Eintrag – auch nicht beim Grund `datenschutz`. Wer `mandari:vote` bzw. `mandari:rollCall` speichert,
  erfährt so nicht, dass Einzelstimmen aus seinen Kopien zu entfernen sind; er ruft den
  Tagesordnungspunkt bei seiner nächsten Änderung neu ab.
- Eingebettete Objekte: Ändert sich ein Tagesordnungspunkt, eine Beratung oder eine Datei, nennt der
  Feed dieses Objekt. Wer Sitzungen oder Vorlagen samt Einbettungen speichert, ruft das einbettende
  Objekt (`AgendaItem.meeting`, `Consultation.paper`, `File.paper`/`meeting`) mit ab.
- Gespiegelte Session-Mandanten: Der Aggregator nennt nur Objekte, die sein Bestand kennt. Meldet ein
  Session-Mandant eine Änderung, bevor der Abgleich sie übernommen hat, fehlt ein neues Objekt im Feed,
  und `id` eines bekannten liefert kurz noch den alten Stand. Die Übernahme erscheint verlässlich erst,
  wenn der Abgleich sie mit einem eigenen Ereignis meldet (in Arbeit).

### Snapshot: Einstieg mit Cursor-Übergabe

Der Snapshot liefert den Gesamtstand einer Kommune und den Cursor, ab dem der Feed fortsetzt. Er ist
der Einstieg für neue Abnehmer und der Wiedereinstieg nach einem abgelaufenen Cursor. Der Body nennt
seine Adresse in `mandari:snapshot`.

```bash
curl --compressed "https://mandari.de/oparl/v1/body/<uuid>/snapshot" -o musterstadt.ndjson
```

Die Antwort ist NDJSON (`application/x-ndjson`): je Zeile ein JSON-Objekt.

```
{"snapshot_cursor":"k9Qa…","body":"https://mandari.de/oparl/v1/body/<uuid>","changes":"https://mandari.de/oparl/v1/body/<uuid>/changes?after=k9Qa…","created":"2026-09-30T08:20:00+00:00","objects":1843}
{"id":"https://mandari.de/oparl/v1/body/<uuid>","type":"https://schema.oparl.org/1.1/Body",…}
{"id":"https://mandari.de/oparl/v1/organization/<uuid>","type":"https://schema.oparl.org/1.1/Organization",…}
…
```

- **Erste Zeile:** `snapshot_cursor`, die Adresse des Body, die fertige Adresse für den Feed
  (`changes`), der Zeitpunkt des Cursors und die Zahl der folgenden Zeilen (`objects`). Der Cursor
  steht auch in der Kopfzeile `Snapshot-Cursor` (mit `HEAD` ohne den Inhalt abrufbar).
- **Vollständigkeit prüfen:** Folgen weniger Zeilen, als `objects` nennt, ist der Abruf abgebrochen –
  dann den Snapshot neu laden. Das gilt unabhängig von der Übertragung; `Content-Length` fehlt, wenn ein
  Proxy komprimiert.
- **Weitere Zeilen:** der Body und alle Objekte der externen Listen der Kommune (Gremien, Personen,
  Sitzungen, Vorlagen, Orte) – genau so, wie die Listen sie ausgeben, also mit denselben Einbettungen
  (Mitgliedschaften in Personen, Tagesordnungspunkte und Dateien in Sitzungen, Beratungen und Dateien
  in Vorlagen). Jedes Objekt steht einmal darin; Gelöschtes fehlt.
- **Übergabe ohne Verlust:** Der Cursor wird festgehalten, bevor das erste Objekt gelesen wird. Was
  sich währenddessen oder danach ändert, steht im Feed hinter dem Cursor. Ein Objekt kann deshalb nach
  dem Snapshot noch einmal als `upsert` erscheinen – idempotent verarbeiten.
- **Wiedereinstieg:** Wer nach `410` neu einsteigt, ersetzt seinen Stand durch den Snapshot: Was darin
  nicht mehr vorkommt, ist entfernt.
- **HTTP:** `Cache-Control: no-store`, kein `ETag`; `Content-Length` nur bei unkomprimierter
  Übertragung. Der Snapshot entsteht vollständig, bevor die Übertragung beginnt; bei großen Kommunen
  kann die Antwort deshalb auf sich warten lassen (Zeitlimit des Abnehmers großzügig wählen). Je
  Client-Adresse entsteht höchstens ein Snapshot zugleich (sonst `429` mit `Retry-After`); entstehen
  insgesamt gerade zu viele, antwortet die Schnittstelle mit `503` und `Retry-After`.

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
| `mandari:changes` | Body | Adresse des Änderungsfeeds der Kommune (nur wenn eingeschaltet und die Kommune gelistet ist) |
| `mandari:snapshot` | Body | Adresse des Snapshots der Kommune (nur wenn der Änderungsfeed eingeschaltet und die Kommune gelistet ist) |
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
| `OPARL_CHANGES_ENABLED` | `false` | Änderungsfeed je Kommune einschalten (Aggregator und Session-Schnittstelle) |
| `OPARL_CHANGES_RETENTION_DAYS` | `90` | Gültigkeit eines Cursors des Änderungsfeeds in Tagen (mindestens 30) |
| `OPARL_SNAPSHOT_PARALLEL` | `2` | Snapshots, die gleichzeitig entstehen dürfen; weitere Anfragen erhalten `503` mit `Retry-After`. Je Client-Adresse höchstens einer (`429`) |

**Änderungsfeed einschalten:** Der Feed liest die öffentlichen Ereignisse `ris.*` aus dem Journal der
Ereignistechnik. Er gehört erst eingeschaltet (`OPARL_CHANGES_ENABLED=true`), wenn in der Installation
der Sequenzierer läuft (`manage.py events_sequencer`) und die Erzeuger Ereignisse schreiben (Ingestor
bzw. Session) – sonst bliebe er leer und täuschte Abnehmern vor, es habe sich nichts geändert.
Ausgeschaltet gibt es die Adresse nicht, und kein Body weist auf sie hin. Das Journal muss seine Zeilen
mindestens `OPARL_CHANGES_RETENTION_DAYS` Tage behalten; ein Aufräumen darf nie kürzer greifen und
hält fest, was es gelöscht hat (`apps.events.pruning`) – nur daran erkennt der Feed, dass Abnehmern
Zeilen fehlen können. Der Schlüssel der Cursor ist aus `SECRET_KEY` abgeleitet: Nach einem Wechsel des
Schlüssels sind ausgegebene Cursor ungültig (`410`), und Abnehmer steigen über den Snapshot wieder ein; Schlüssel in
Djangos `SECRET_KEY_FALLBACKS` gelten weiter.

**Snapshot im Betrieb:** Ein Snapshot liest den ganzen öffentlichen Bestand einer Kommune. Er wird in
eine temporäre Datei geschrieben (Platz im temporären Verzeichnis des Containers: bei großen Kommunen
einige hundert Megabyte) und danach in Blöcken übertragen; die Datenbank liest nur die Anfrage selbst,
ein langsamer Abnehmer hält keine Verbindung fest. `OPARL_SNAPSHOT_PARALLEL` begrenzt, wie viele
gleichzeitig entstehen (über den gemeinsamen Cache der Installation), und je Client-Adresse entsteht
höchstens einer: Ein einzelner Abnehmer belegt nicht alle Plätze, auch nicht mit abgebrochenen Abrufen,
deren Aufbau noch läuft. Bis die Datei fertig ist, fließt kein Byte zum Abnehmer. Vor dem Einschalten
Bauzeit und Größe für die größte Kommune der Installation messen und das Leerlauf-Zeitlimit
vorgeschalteter Proxys und Ingress-Komponenten darauf abstimmen.

**Eine Serialisierung für beide Ausgaben:** Aggregator und Session-Schnittstelle
(`SESSION_OPARL_API.md`) gehen denselben Weg – Abbildung auf das kanonische Modell, dann Ausgabe
(ADR `docs/adr/20260929-kanonisches-modell.md`):

| Schritt | Aggregator | Session-Schnittstelle |
|---|---|---|
| Was ist sichtbar, unter welcher Adresse? | `mandari/hub/api/aggregator.py`, Routen in `mandari/hub/api/urls.py` | `mandari/apps/session/api/oparl.py` |
| Abbildung auf das kanonische Modell | `mandari/hub/ris/mapping/bestand.py` | `mandari/hub/ris/mapping/session.py` |
| Bausteine des Modells (Typ-URLs, Datum und Zeit, gekürzte Objekte, `organizationType`) | `mandari/hub/ris/canonical.py` | dieselben |
| Serialisierung: Zeitfilter, Blättern, Listen-Hülle, Gelöschtes in inkrementellen Listen | `mandari/hub/api/serialization.py` | dieselbe |
| Änderungsfeed | `mandari/hub/api/changes.py` | derselbe |
| Snapshot | `mandari/hub/api/snapshot.py` | derselbe |
| HTTP-Hülle: JSON, `ETag`/`304`, Fehler, CORS, Rate-Limit | `mandari/hub/api/http.py` | dieselbe |

Wer ein Feld ergänzt oder ändert, tut das in der Abbildung der Quelle; wer das Verhalten von Listen,
Filtern oder Fehlern ändert, in `hub/api` – es gilt dann für beide Ausgaben. Das frühere Paket
`oparl_api` ist darin aufgegangen.

**Kanonische Kennungen** (ADR `docs/adr/20260929-kanonisches-modell.md`): Jedes Objekt des
RIS-Bestands trägt die Kennung `uuid5(NS_MANDARI_RIS, URI)` aus `shared/mandari_oparl/ids.py`.
Die URI ist bei Fremd-RIS die `id` der Quelle, bei Session-Mandanten die öffentliche OParl-URL auf
der einmal festgeschriebenen Basisadresse der Installation (anfangs gleich `SITE_URL`; ein
Domainwechsel ändert keine Kennung, siehe `docs/SESSION_OPARL_API.md`, „Domainwechsel“). Der
Namensraum ist der URL-Namensraum aus RFC 9562 und ändert sich nie.
Ingestor und Django vergeben neue Kennungen mit derselben Funktion; beide Testsuiten prüfen die
gemeinsamen Testvektoren (`shared/mandari_oparl/ids_testvektoren.json`). Bestehende Kennungen bleiben
unverändert. Abweichungen im Bestand zählt ein lesender Befehl:

```bash
# je Quelle und Entität: abweichende Kennung, abweichende URI (Session), ohne URI, Kollisionen;
# dazu die festgeschriebene Basis der Kennungen im Vergleich zu SITE_URL
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
