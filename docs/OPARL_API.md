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
| `mandari:locationList` | Body | URL der Orte-Liste (Vendor-Erweiterung) |
| `mandari:summary` | Paper | KI-generierte Zusammenfassung (falls vorhanden) |
| `mandari:originalAccessUrl` | File | Original-Datei-URL beim Quellserver |
| `mandari:sha256`, `mandari:pageCount` | File | SHA-256-Hash / Seitenzahl |
| `mandari:locationName`, `mandari:locationAddress` | Meeting | Ortsangabe als Text, falls kein Location-Objekt auflösbar |

## Einschränkungen (v1)

- **Keine Tombstones**: Die mandari-Datenmodelle führen kein Lösch-Flag; gelöschte
  Objekte können daher nicht als `{"id": …, "type": …, "deleted": true}` in Listen
  erscheinen, sondern verschwinden einfach. Für einen vollständigen Abgleich ist
  periodisch ein Voll-Sync nötig.
- **Nicht abgebildete Referenzen**: Felder ohne Fremdschlüssel im Datenmodell
  (`Meeting.participant`, `Paper.originatorPerson`/`originatorOrganization`/
  `underDirectionOf`/`relatedPaper`, `Organization.subOrganizationOf`,
  `AgendaItem.resolutionFile`) werden ausgelassen, statt Original-URLs
  durchzureichen.
- **Lizenz**: Die Lizenz der Quelldaten wird — soweit von der Kommune angegeben —
  am Body-Objekt (`license`) durchgereicht; eine übergreifende Lizenzangabe am
  System-Objekt ist noch offen (siehe Issue #17).
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

Smoke-Test: `python scripts/smoke_oparl_api.py` (SQLite, synthetische Daten aller
zwölf Objekttypen, 95 Checks).
