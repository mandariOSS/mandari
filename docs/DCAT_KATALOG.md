# Datenkatalog nach DCAT-AP.de

Implementierungsnotiz zu Issue #104. Der Katalog beschreibt die offenen Ratsinformationen je Kommune in der
Sprache der Open-Data-Portale ([DCAT-AP.de 3.0](https://www.dcat-ap.de/def/dcatde/3.0/spec/)). GovData, die
Landesportale und jedes CKAN mit [ckanext-dcat](https://github.com/ckan/ckanext-dcat) sammeln ihn unter einer
festen Adresse ein. Die Daten selbst bleiben in der OParl-Schnittstelle ([OPARL_API.md](OPARL_API.md)); der
Katalog verweist auf sie.

Er ist der erste Adapter der Datendrehscheibe
([ADR Adapter-Rahmen](adr/20260929-adapter-rahmen.md)): Er übersetzt ohne eigene Regeln. Was öffentlich ist,
unter welcher Lizenz und seit wann, entscheidet der Eigentümer der Daten. Portale holen den Katalog ab; es gibt
keine Zustellung und deshalb kein Abonnement auf Ereignisse.

## Adressen

Der Katalog ist je Installation einzuschalten (`DCAT_ENABLED=true`); ausgeschaltet antworten alle Adressen mit
`404`.

| Adresse | Inhalt |
|---|---|
| `/data/dcat/catalog` | alle gelisteten Kommunen mit Lizenz in einem Katalog – ein Einstieg für ein Datenportal |
| `/data/dcat/body/<uuid>/catalog` | Katalog einer Kommune; die Kennung ist die des Body in der OParl-Schnittstelle |
| `/session/<slug>/api/dcat/catalog` | Katalog eines Session-Mandanten (Herausgeber ist die Kommune selbst) |

Jede Adresse gibt es in drei Formen:

| Endung | Form | Medientyp |
|---|---|---|
| `.ttl` | Turtle | `text/turtle` |
| `.rdf` | RDF/XML | `application/rdf+xml` |
| `.jsonld` | JSON-LD | `application/ld+json` |

Ohne Endung entscheidet der `Accept`-Kopf (Vorgabe Turtle); die Antwort nennt die gewählte Adresse in
`Content-Location` und trägt `Vary: Accept`. Jede Antwort verweist im `Link`-Kopf (`rel="alternate"`) auf alle
drei Formen. Unbekannte Endungen ergeben `404`.

Die Adressen bauen auf `SITE_URL` auf, nicht auf dem Host der Anfrage. Sie sind zugleich die Kennungen der
Kataloge; die Kennungen der Datensätze sind Hash-Adressen im Katalog der Kommune
(`…/body/<uuid>/catalog#sitzungen`). Ein Datensatz hat im Gesamtkatalog und im Katalog seiner Kommune dieselbe
Kennung; Portale erkennen ihn so wieder.

## Inhalt je Kommune

Drei Datensätze, so wie man Ratsinformationen sucht:

| Datensatz | Kennung | Distributionen |
|---|---|---|
| Sitzungen und Tagesordnungen | `#sitzungen` | OParl-Liste `meetings`, Sitzungskalender (iCalendar) |
| Vorlagen und Beschlüsse | `#vorlagen` | OParl-Liste `papers` |
| Gremien und Mandate | `#gremien` | OParl-Listen `organizations` und `people` |

Ist der Änderungsfeed eingeschaltet (`OPARL_CHANGES_ENABLED`), hat jeder Datensatz zusätzlich Feed und Snapshot
der Kommune als Distribution; beide gelten für alle Objekte der Kommune. Den Sitzungskalender gibt es nur im
Katalog des Aggregators (er ist ein Export des Bürgerportals).

Die OParl-Schnittstelle erscheint als `dcat:DataService` (Endpunkt: System-Objekt des Aggregators, Beschreibung:
die OParl-Spezifikation). Die Distributionen der Schnittstelle verweisen mit `dcat:accessService` darauf und
nennen OParl 1.1 als Standard (`dct:conformsTo`).

| Angabe | Quelle |
|---|---|
| Titel, Beschreibung, Schlagworte | feste Texte mit dem Namen der Kommune |
| Thema | `GOVE` (Regierung und öffentlicher Sektor, EU-Vokabular „Data theme“) |
| Raumbezug | `dct:spatial` und `dcatde:politicalGeocodingLevelURI` aus dem Gemeindeschlüssel (8 Stellen Gemeinde, 5 Kreis, 3 Regierungsbezirk, 2 Land), ersatzweise dem Regionalschlüssel |
| Zeitraum | früheste und späteste Sitzung, Vorlage bzw. Beginn des ältesten Gremiums (ohne Gelöschtes) |
| Letzte Änderung | jüngstes `modified` der Objekte des Datensatzes (Zeitstempel der Quelle, ersatzweise der eigene) |
| Veröffentlicht | erstmals im Bestand |
| Aktualisierung | `UPDATE_CONT` (laufend); als Archiv behaltene Kommune `NEVER` |
| Zugang, Sprache, Verfügbarkeit | `PUBLIC`, `DEU`, `STABLE` |
| Webseite | Einstieg der Kommune im Bürgerportal |

### Wer ist wer – keine Personendaten

**Aggregator:**

- **Herausgeber** (`dct:publisher`) und **Kontakt** (`dcat:contactPoint`) ist der Betreiber der Installation: Er
  macht die Daten über seine Schnittstelle zugänglich (`DCAT_PUBLISHER_NAME`, `DCAT_PUBLISHER_URL`,
  `DCAT_CONTACT_EMAIL`).
- **Urheber** (`dct:creator`) ist die Kommune, aus deren Ratsinformationssystem die Daten stammen. Ihr Name ist
  auch der Text der Namensnennung.

**Session-Mandant:** Herausgeber und Kontakt ist die Kommune selbst – Name, Webseite und Kontakt-E-Mail des
Mandanten, dieselben Angaben, die seine OParl-Schnittstelle an System und Body nennt. Die Kontakt-E-Mail sollte
deshalb ein Funktionspostfach sein.

Herausgeber, Urheber und Kontakt sind Stellen, nie Personen. Kontaktname und -adresse aus einer fremden
OParl-Quelle (`contactName`, `contactEmail`) übernimmt der Katalog bewusst nicht; die Kontaktadresse des Betreibers
sollte ein Funktionspostfach sein. Personen der Kommune erscheinen nur über die verlinkte OParl-Liste, nicht im
Katalog.

### Lizenz

DCAT-AP.de verlangt eine Lizenz an jeder Distribution, und zwar aus der
[Lizenzliste von DCAT-AP.de](https://www.dcat-ap.de/def/licenses/). Maßgeblich ist die Angabe der Kommune in
ihrer OParl-Schnittstelle (`Body.license`). Die Lizenz der Installation (`OPARL_LICENSE_URL`) gilt nur, wenn die
Kommune keine angibt – wie in OParl 1.1, wo `System.license` nur für Objekte ohne eigene Angabe gilt. Eine
unbekannte oder einschränkende Angabe der Kommune ersetzt sie nie. Gängige Schreibweisen der Lizenzadressen werden
zugeordnet (`http`/`https`, `www.`, Schrägstrich am Ende, Sprachfassung), etwa:

| Angabe | Lizenzliste |
|---|---|
| `https://www.govdata.de/dl-de/zero-2-0` | `dl-zero-de/2.0` |
| `https://www.govdata.de/dl-de/by-2-0` | `dl-by-de/2.0` |
| `https://creativecommons.org/publicdomain/zero/1.0/` | `cc-zero` |
| `https://creativecommons.org/licenses/by/4.0/` | `cc-by/4.0` |

Die vollständige Zuordnung steht in `hub/adapters/dcat/vokabular.py`. Lizenzen mit Namensnennung bekommen
`dcatde:licenseAttributionByText` mit dem Namen der Kommune. Session-Mandanten wählen ihre Lizenz in den
Einstellungen (Karte „OParl-Schnittstelle“); dieselbe Karte zeigt die Adresse des Katalogs bzw. den Hinweis, dass
es ohne Lizenz keinen gibt.

**Ohne Lizenz kein Katalog:** Ohne Angabe (auch keine der Installation), bei einer unbekannten Angabe oder bei
einer Lizenz mit Einschränkungen (nicht kommerziell, keine Bearbeitung) antwortet der Katalog der Kommune mit `404`
und einem Problem nach RFC 9457 (Typ `keine-lizenz`, Hinweis auf `Body.license`); im Gesamtkatalog fehlt die
Kommune.

### Fremde Angaben

Webseiten und Kontaktadressen (`Body.website` aus dem Ratsinformationssystem, Einstellungen der Installation)
werden im Graphen zu IRIs. Der Katalog übernimmt sie nur, wenn sie als IRI taugen: Webadressen mit `http`/`https`
und Host, ohne Leer- und Steuerzeichen, spitze oder geschweifte Klammern, Anführungszeichen und ähnliches;
E-Mail-Adressen nur in gültiger Form. Alles andere gilt als „keine Angabe“. Steuerzeichen in Namen und Texten
fallen weg, weil RDF/XML sie nicht darstellen kann. So macht eine fehlerhafte Angabe einer einzelnen Kommune den
Gesamtkatalog nicht unlesbar (`hub/adapters/dcat/adressen.py`).

### Session-Mandanten

- Abrufbar wie die OParl-Schnittstelle erst nach der Freischaltung (Issue #319), sonst `404` ohne Auskunft über den
  Mandanten. Das Ende der Veröffentlichung im Bürgerportal berührt den Katalog nicht.
- Zeitraum und letzte Änderung nur aus öffentlichen Objekten (`apps/session/oparl_publication.py`).
- Webseite (`dcat:landingPage`): der Einstieg im Bürgerportal, solange der Mandant dort veröffentlicht, sonst die
  Webseite der Kommune.
- Kennung bei GovData: Feld „Kennung bei GovData“ am Mandanten (`SessionTenant.dcat_contributor_id`, im Admin unter
  „OParl-Verknüpfung“); nur in der Form `http://dcat-ap.de/def/contributors/…`.
- Für eine gelistete Kommune, die einen Session-Mandanten dieser Installation spiegelt, leitet der Katalog des
  Aggregators auf den Katalog des Mandanten weiter, in derselben Form; im Gesamtkatalog fehlt sie. So stehen
  dieselben Daten nicht unter zwei Herausgebern in den Portalen. Die Weiterleitung ist vorübergehend (`302`,
  `Cache-Control: max-age=3600`), weil sich die Spiegelung ändern lässt; eine dauerhafte (`301`) dürften Portale
  unbegrenzt behalten. Eine nicht gelistete oder gelöschte Kommune antwortet mit `404` ohne Weiterleitung. Ist der
  Mandant nicht (mehr) freigeschaltet, antwortet das Ziel mit `404`, und die Kommune fehlt in beiden Katalogen –
  lieber keine Angabe als dieselben Daten unter dem Betreiber als Herausgeber.

### Veröffentlichungsstand

Für den Aggregator wie bei Feed und Snapshot (`insight_core/publication.py`):

| Stand | Katalog der Kommune | Gesamtkatalog |
|---|---|---|
| nicht gelistet, gelöscht, unbekannt | `404` | fehlt |
| vorübergehend abgeschaltet | `503` mit `Retry-After` | bleibt stehen |
| als Archiv behalten | wie sonst, Aktualisierung `NEVER` | wie sonst |
| dauerhaft zurückgenommen | `410` (Problemtyp `kommune-zurueckgenommen`) | fehlt |

Eine vorübergehend abgeschaltete Kommune bleibt im Gesamtkatalog: Nicht erreichbar ist nicht gelöscht. Ein Portal,
das sie dort verlöre, würde ihre Datensätze löschen.

## HTTP

- `ETag` über den Inhalt, `Cache-Control: no-cache`, `If-None-Match` ergibt `304`.
- Gleiche Daten ergeben in jedem Prozess dieselben Bytes: Turtle sortiert rdflib, JSON-LD wird nach der
  Serialisierung geordnet, RDF/XML wird flach und geordnet geschrieben (`hub/adapters/dcat/rdf.py`).
- Ein fertiger Katalog liegt `DCAT_CACHE_SECONDS` lang im Cache (Vorgabe 300). Der Schlüssel enthält den
  Veröffentlichungsstand; eine Rücknahme gilt sofort.
- Nur `GET`/`HEAD`, CORS offen, Ratenbegrenzung wie die OParl-Schnittstelle (`OPARL_API_RATE_LIMIT`).
- rdflib (rund 15 MB) wird erst beim ersten Abruf eines Katalogs geladen.

## Einstellungen

| Variable | Vorgabe | Bedeutung |
|---|---|---|
| `DCAT_ENABLED` | `false` | Katalog einschalten |
| `DCAT_PUBLISHER_NAME` | `mandari` | Herausgeber der Datensätze des Aggregators (der Betreiber) |
| `DCAT_PUBLISHER_URL` | `SITE_URL` | Webseite des Herausgebers |
| `DCAT_CONTACT_EMAIL` | leer | Kontaktadresse (Funktionspostfach); leer: nur die Webseite |
| `DCAT_CONTRIBUTOR_ID` | leer | Kennung des Betreibers bei GovData (`http://dcat-ap.de/def/contributors/…`); andere Werte werden nicht ausgegeben. Session-Mandanten tragen ihre eigene Kennung (siehe oben). |
| `DCAT_CACHE_SECONDS` | `300` | Zwischenspeicher der fertigen Kataloge (0 = aus) |

Ein vorgeschalteter Reverse-Proxy muss `/data/dcat/*` an die Anwendung weiterreichen; das `Caddyfile` der
Community Edition tut das. Installationen mit eigener Proxy-Konfiguration tragen den Pfad dort nach.

## So sammelt ein Datenportal den Katalog ein

1. **Vorher prüfen:** Katalog unter `/data/dcat/catalog.ttl` abrufen und im
   [DCAT-AP.de-Validator](https://www.itb.ec.europa.eu/shacl/dcat-ap.de/upload) (Profil „DCAT-AP.de 3.0 –
   Spezifikation“) prüfen. Herausgeber, Kontakt und Lizenzen sollten stimmen.
2. **CKAN mit ckanext-dcat** (auch Landesportale auf CKAN-Basis): Eine Harvest-Quelle vom Typ „DCAT RDF Harvester“
   mit der Adresse des Katalogs anlegen, Format Turtle (oder die Adresse ohne Endung, dann per
   Inhaltsaushandlung). Für den Aggregator genügt der Gesamtkatalog; ein Portal einer einzelnen Kommune nimmt den
   Katalog dieser Kommune. Ein täglicher Abgleich reicht; der Katalog ändert sich mit den Daten nur in den
   Zeitstempeln.
3. **GovData:** Der Betreiber meldet sich als Datenbereitsteller an und nennt die Adresse des Katalogs als
   Harvesting-Quelle. GovData vergibt dabei eine Kennung; sie gehört nach `DCAT_CONTRIBUTOR_ID` und erscheint
   danach als `dcatde:contributorID` an jedem Datensatz. Landesportale (etwa Open.NRW) leiten ihre Datensätze
   meist selbst an GovData weiter; dann nur bei einem der beiden anmelden, sonst entstehen Dubletten.
   Eine Kommune mit Session-Mandant meldet sich selbst an und nennt den Katalog ihres Mandanten; ihre Kennung von
   GovData trägt der Betreiber am Mandanten ein.
4. Abgleich: Portale erkennen Datensätze an `dct:identifier` (gleich der Kennung) und Änderungen an
   `dct:modified`. Verschwindet eine Kommune aus dem Katalog (Rücknahme, keine Lizenz mehr), löschen Portale
   ihre Datensätze beim nächsten Abgleich.

## Prüfung

- Tests: `hub/adapters/dcat/tests/` (Modell, Formen, Aggregator) und `apps/session/tests/test_dcat_katalog.py`
  (Session-Mandant, Weiterleitung, Einstellungen).
- **SHACL in der CI** (`scripts/dcat_shacl.py`, Job „OParl-Validator“): Gesamtkatalog, Katalog einer fremden
  Kommune und Katalog des Demo-Mandanten, je in allen drei Formen, gegen die Regeln von DCAT-AP 3.0.0 (SEMIC,
  CC BY 4.0) und DCAT-AP.de 3.0 (GovData, CC0) – dieselbe Zusammenstellung wie im Profil „DCAT-AP.de 3.0 –
  Spezifikation“ des DCAT-AP.de-Validators. Die Regeln werden in festgelegter Fassung (Commit, SHA-256) geladen,
  nicht mitgeliefert; die kontrollierten Vokabulare von ihren offiziellen Adressen, ersatzweise aus dem
  Zwischenspeicher. Ein Verstoß (`sh:Violation`) lässt den Lauf scheitern, Warnungen werden gemeldet.
- Lokal: `pip install pyshacl==0.40.1` (Extra `dev`), dann `python scripts/dcat_shacl.py`.

## Nicht enthalten

- **HVD-Kennzeichnung** (`dcatap:applicableLegislation`, `dcatap:hvdCategory`): Ratsinformationen gehören zu
  keiner Kategorie der Durchführungsverordnung (EU) 2023/138 über hochwertige Datensätze; der Katalog kennzeichnet
  deshalb nichts.
- CKAN-kompatible Lese-API, schema.org-Angaben in den Seiten des Bürgerportals und eine Fehlerliste der
  SHACL-Prüfung in der Oberfläche (weitere Teile von #104).
- Blättern (Hydra): Ein Katalog einer Kommune hat drei Datensätze, der Gesamtkatalog drei je Kommune.

## Code

| Modul | Inhalt |
|---|---|
| `hub/adapters/dcat/vokabular.py` | kontrollierte Vokabulare, Zuordnung der Lizenzen, Raumbezug |
| `hub/adapters/dcat/katalog.py` | Katalogmodell und die drei Datensätze einer Kommune (ohne Django und RDF) |
| `hub/adapters/dcat/adressen.py` | Basis der Adressen, Prüfung fremder Webadressen und E-Mail-Adressen |
| `hub/adapters/dcat/rdf.py` | Serialisierung als Turtle, RDF/XML und JSON-LD |
| `hub/adapters/dcat/http.py` | Formen, Inhaltsaushandlung, Zwischenspeicher, Absagen |
| `hub/adapters/dcat/aggregator.py` | Kataloge des Aggregators, Routen in `urls.py` |
| `apps/session/api/dcat.py` | Katalog je Session-Mandant (das Fachmodul legt fest, was öffentlich ist) |
| `scripts/dcat_shacl.py` | SHACL-Prüfung in der CI |
