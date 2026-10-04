# Session-OParl-API (je Mandant)

Jeder aktive Session-Mandant (Kommune im Verwaltungs-RIS) stellt unter

```
https://<host>/session/<slug>/api/oparl/
```

einen eigenen, **OParl-1.1-konformen System-Endpoint** bereit (Issue #35),
sobald die Verwaltung die Schnittstelle freigeschaltet hat (Issue #319, nächster Abschnitt).
Die API folgt dem Muster des mandari-Aggregators (`docs/OPARL_API.md`,
Issue #17): rein lesend, anonym, JSON, CORS offen, Rate-Limit
(`OPARL_API_RATE_LIMIT`, Standard 120 Anfragen/Minute je IP).

Für Datenportale wie GovData beschreibt ein Katalog nach DCAT-AP.de unter `…/api/dcat/catalog` die
offenen Daten des Mandanten und verweist auf diese Schnittstelle ([DCAT_KATALOG.md](DCAT_KATALOG.md)).

## Freischaltung der Schnittstelle (Issue #319)

Offene Daten ab Werk sind gewollt – den Zeitpunkt bestimmt aber die Verwaltung. Ein neu angelegter
Mandant (Einführung, Testdaten, Schulung, Umstieg aus einem Altsystem) ist zunächst **nicht**
abrufbar:

- Alle Endpunkte unter `…/api/oparl/` antworten mit **404** („Mandant nicht gefunden“) – nach außen
  wie ein unbekannter Mandant, ohne Hinweis auf seine Existenz.
- Die anonymen Lesezugriffe der Session-API (Einstieg, Sitzungen und Vorlagen unter
  `/api/v1/session/<slug>/…` und den alten Pfaden unter `…/api/session/…`) antworten ebenfalls mit
  404; angemeldete Nutzer und API-Token des Mandanten lesen weiter. Die Existenz des Mandanten
  verbirgt nur die OParl-Schnittstelle: Die Anmeldung zum Sitzungsdienst, die Prüfung von
  API-Token (401 bei unbekanntem oder fremdem Token) und die Anträge-Endpunkte (401 bzw. 403 ohne
  Anmeldung) verhalten sich wie bei jedem anderen Mandanten. Die Sperre schützt die Daten, nicht den
  Namen des Mandanten.
- Das Bürgerportal registriert keine Quelle. „Im Bürgerportal veröffentlichen“ setzt die
  Freischaltung voraus, weil das Bürgerportal genau diese Schnittstelle liest.

**Freischalten:** Session → Einstellungen → Karte **OParl-Schnittstelle** → „OParl-Schnittstelle
freischalten“ (Berechtigung `manage_settings`). Das Datum steht in
`SessionTenant.oparl_public_since` und in den Einstellungen („Öffentlich seit …“), jede Änderung im
Audit-Log des Mandanten (`publish`/`unpublish`, Objekt „OParl-Schnittstelle“). Die Freischaltung
lässt sich zurücknehmen, solange der Mandant nicht im Bürgerportal veröffentlicht; wer dort
veröffentlicht, beendet das zuerst (Abschnitt „Veröffentlichung beenden“). Im Django-Admin ist das
Datum nur lesbar.

**Bestand:** Mit dem Update wurden alle aktiven Mandanten und alle, die im Bürgerportal
veröffentlichen, mit dem Datum ihrer Anlage freigeschaltet (Migration `session.0047`) – ihre
Schnittstelle bleibt ohne Unterbrechung erreichbar.

- **Spezifikation**: https://oparl.org/spezifikation/
- **Implementierung**: Endpunkte und gelöschte Objekte in `mandari/apps/session/api/oparl.py`,
  Sichtbarkeit/Tombstones in `mandari/apps/session/oparl_publication.py`. Zeitfilter, Blättern,
  Listen-Hülle, `ETag`, Fehler und Rate-Limit kommen aus `mandari/hub/api/` – dieselbe Serialisierung
  wie beim Aggregator (Tabelle in `OPARL_API.md`, Abschnitt „Betrieb“). Wie ein Session-Objekt als
  OParl-Objekt aussieht, legt allein `mandari/hub/ris/mapping/session.py` fest – die eine Abbildung der
  Session-Objekte auf das kanonische Modell (ADR `docs/adr/20260929-kanonisches-modell.md`). Wer ein
  Feld ergänzt oder ändert, tut das dort; die Schnittstelle gibt die Abbildung unverändert aus.
  Welche Objekte öffentlich sind, wählt weiterhin `oparl_publication.py` aus. Die Abbildung verlässt
  sich darauf nicht allein: Für eine nichtöffentliche Sitzung (samt Ort), einen nichtöffentlichen
  Tagesordnungspunkt, eine nicht veröffentlichte Vorlage oder Beratung und eine Datei ohne
  öffentliches Bezugsobjekt bricht sie mit `NotPublicError` ab, statt Inhalte auszugeben.

## Sicherheitsgarantie: NUR öffentliche Daten

Die API liefert ausschließlich Daten, die im Session-RIS als öffentlich
markiert sind:

| Objekttyp | Sichtbarkeitsregel |
|-----------|--------------------|
| Meeting | `is_public=True`; eine nichtöffentliche Sitzung mit veröffentlichtem Termin (`date_public`, Issue #757) nur als Termin: Name, Beginn, Ende, Status, Gremien und `mandari:nonPublic: true` – ohne Ort, Tagesordnung, Anlagen, Niederschrift und Sitzungsformat |
| AgendaItem | `is_public=True` **und** Sitzung öffentlich (NÖ-Teil niemals; `resolutionText` nur der öffentliche Beschlusstext) |
| Paper | `is_public=True` |
| File | `is_public=True` **und** übergeordnetes Objekt (Vorlage/Sitzung/TOP) öffentlich; die öffentliche Niederschrift nur, solange sie veröffentlicht ist |
| Consultation | Vorlage öffentlich; Referenzen auf NÖ-Sitzungen/-TOPs werden ausgelassen |
| Person | ohne geschützte Daten — verschlüsselte Felder (Telefon, Adresse, Bankdaten) werden nie gelesen; `email` nur mit Einwilligung (Kennzeichen „Kontaktdaten veröffentlichen“, siehe unten) |
| Organization, Membership, LegislativeTerm | vollständig (keine Ö/NÖ-Unterteilung) |

Nicht-öffentliche Objekte existieren nach außen nicht: Ihre
Objekt-Endpunkte liefern 404 (sofern sie nie veröffentlicht waren).
Beweis-Suite: `python scripts/smoke_session_oparl.py` (Ö/NÖ-Beweis über
die gesamte API-Oberfläche) sowie die pytest-Suite
`mandari/apps/session/tests/test_security_matrix.py`.

## Kontaktdaten nur mit Einwilligung (Issue #319)

`Person.email` erscheint nur, wenn an der Person das Kennzeichen **„Kontaktdaten veröffentlichen“**
gesetzt ist (Session → Personen → Bearbeiten → Kontakt → Veröffentlichung). Dazu gehören das
**Datum** und ein **Nachweis der Einwilligung** (z. B. „Schriftliche Erklärung, abgelegt in der
Mandatsakte“); ohne beides lässt sich das Kennzeichen nicht setzen. Datum und Nachweis bleiben
intern und erscheinen nie in der Schnittstelle. Wer das Kennzeichen entfernt, entfernt auch Datum und
Nachweis; die Änderung steht im Audit-Log. Telefon und Adresse werden nie veröffentlicht.

Standard ist **nicht veröffentlichen**. Bei der Umstellung (Migration `session.0047`) wurden
Ratsmitglieder mit E-Mail-Adresse übernommen – aktive Personen mit laufender Mitgliedschaft im Rat
als Mitglied, Vorsitz oder Stellvertretung; Nachweis „Übernahme aus dem Bestand (Ratsmitglied,
Adresse war bereits veröffentlicht)“. Bei allen anderen Personen (etwa sachkundige Bürgerinnen und
Bürger, Beratende, Gäste, ehemalige Mitglieder) entfällt die Adresse. Ihr Änderungszeitpunkt wurde
gesetzt, damit inkrementelle Abnehmer (`modified_since`) und der Bürgerportal-Spiegel die Person neu
abrufen und die Adresse entfernen. Die Verwaltung sollte die übernommenen Einträge prüfen – etwa
private Adressen von Ratsmitgliedern.

### Baustein für das Verzeichnis der Verarbeitungstätigkeiten

Verantwortlich ist die Kommune; mandari verarbeitet im Auftrag. Für ihr Verzeichnis nach Art. 30
DSGVO kann sie die Veröffentlichung so beschreiben:

| Angabe | Inhalt |
|---|---|
| Bezeichnung | Veröffentlichung von Ratsinformationen über die OParl-Schnittstelle und das Bürgerportal |
| Zweck | Transparenz der Ratsarbeit; offene Daten nach dem OParl-Standard |
| Rechtsgrundlage | Art. 6 Abs. 1 lit. e DSGVO i. V. m. dem Kommunalrecht des Landes (Öffentlichkeit der Sitzungen, Bekanntmachung); für die E-Mail-Adresse Art. 6 Abs. 1 lit. a DSGVO (Einwilligung), sofern keine eigene Grundlage für eine dienstliche Adresse besteht |
| Betroffene | Mitglieder der Gremien, sachkundige Bürgerinnen und Bürger, Beratende, Antragstellende |
| Daten | Name, Titel, Anrede, Mitgliedschaften und Funktionen; E-Mail-Adresse nur mit Kennzeichen „Kontaktdaten veröffentlichen“; öffentliche Sitzungen, Vorlagen, Beschlüsse und Anlagen |
| Empfänger | Öffentlichkeit (anonymer Abruf), Bürgerportal, weitere OParl-Abnehmer |
| Löschung | Die Schnittstelle zeigt den jeweils aktuellen Stand; entfernte Kontaktdaten verschwinden mit dem nächsten Abgleich aus dem Bürgerportal; im Übrigen nach dem Löschkonzept (`DSGVO_LOESCHKONZEPT.md`) |
| Nachweis | Datum und Nachweis der Einwilligung je Person; Freischaltung der Schnittstelle mit Datum und Audit-Eintrag |

## Endpunkte

| Endpunkt | Inhalt |
|----------|--------|
| `GET …/api/oparl/` | System-Objekt (Einstiegspunkt) |
| `GET …/api/oparl/bodies/` | Externe Liste mit der einen Kommune |
| `GET …/api/oparl/body/` | Body-Objekt (inkl. eingebetteter `legislativeTerm`) |
| `GET …/api/oparl/organizations/` | Gremien (paginiert, filterbar) |
| `GET …/api/oparl/people/` | Personen (paginiert, Memberships eingebettet) |
| `GET …/api/oparl/meetings/` | Sitzungen (nur Ö; TOPs des Ö-Teils eingebettet) |
| `GET …/api/oparl/papers/` | Vorlagen (nur Ö; `mainFile`/`auxiliaryFile`/`consultation` eingebettet) |
| `GET …/api/oparl/memberships/`, `…/agendaitems/`, `…/consultations/`, `…/files/`, `…/legislativeterms/` | weitere externe Listen (OParl 1.1 Body-Listen) |
| `GET …/api/oparl/<typ>/<uuid>/` | Objekt-Endpunkte aller Typen |
| `GET …/api/oparl/file/<uuid>/download/` | Anonymer Datei-Abruf (nur öffentlich sichtbare Anlagen; `?download=1` für Attachment) |
| `GET …/api/oparl/body/changes/` | Änderungsfeed (kompatible Erweiterung, nur wenn eingeschaltet; Abschnitt „Änderungsfeed“) |
| `GET …/api/oparl/body/snapshot/` | Snapshot mit Cursor-Übergabe (NDJSON, Einstieg in den Änderungsfeed) |

Objekttypen für `<typ>`: `organization`, `person`, `membership`, `meeting`,
`agendaitem`, `paper`, `consultation`, `file`, `legislativeterm`, `location`.

Die Adressen enden mit einem Schrägstrich; sie sind die Kennungen der Objekte und ändern sich nicht.
Wer eine Adresse ohne Schrägstrich abruft (`…/api/oparl/body`), wird dauerhaft auf die gültige
weitergeleitet (`301`, Parameter der Anfrage bleiben erhalten).

`Paper.mainFile` ist die älteste öffentliche Anlage der Vorlage, alle
weiteren erscheinen unter `auxiliaryFile`.

`File.fileName` und der Dateiname beim Download sind der Anzeigename der Anlage,
nie der Speichername: Seit der Deduplizierung (Issue #226) teilen sich Anlagen mit
gleichem Inhalt eine Datei, deren Speichername aus einem anderen, womöglich
nichtöffentlichen Upload stammen kann.

Fassungen (Issue #226) werden nicht ausgeliefert: Die API zeigt eine freigegebene
Vorlage mit Betreff, Metadaten und öffentlichen Anlagen im aktuellen Stand; ältere
Fassungen von Vorlagen und Anlagen erscheinen weder hier noch im Bürgerportal.
Sachverhalt und Beschlussvorschlag der Vorlage sind nicht Teil der OParl-Ausgabe.
Offen: Änderungen an Betreff oder öffentlichen Anlagen nach der Freigabe erscheinen
sofort – eine Ausgabe, die bis zu einer erneuten Freigabe den freigegebenen Stand
zeigt, ist nicht umgesetzt.

## Konformität zu OParl 1.1

Die Ausgabe hält sich an die Feldtypen und Wertelisten der Spezifikation. Ein externer Validator
prüft das in der CI gegen eine Instanz mit Demo-Daten (Abschnitt „Prüfung“ in `OPARL_API.md`).

| Eigenschaft | Ausgabe |
|---|---|
| `Organization.organizationType` | einer der sieben Werte der Spezifikation (Tabelle unten); die feinere Art steht in `classification` |
| `File.date` | Datum `yyyy-mm-dd` (Tag der Ablage in der Zeitzone der Installation); der Zeitpunkt steht in `created` |
| `System.license`, `Body.license` | URL der Lizenz, die die Verwaltung festgelegt hat; `Body.licenseValidSince` nennt, seit wann sie gilt. Ohne Festlegung entfallen die Felder |
| `Meeting.location` | eingebettetes Location-Objekt (Abschnitt „Sitzungsort“) |
| `Body.legislativeTerm` | immer vorhanden (Pflichtfeld), ohne Wahlperiode als leere Liste |
| IDs und Links | aus `SITE_URL`, unabhängig vom Host der Anfrage; Kennungen aus der festgeschriebenen Basis (Abschnitt „Betrieb“) |
| Bedingte Anfragen | `ETag` an jeder Antwort, `If-None-Match` ergibt `304` (Abschnitt „Bedingte Anfragen“) |

| Art des Gremiums in Session | `organizationType` | `classification` |
|---|---|---|
| Ausschuss | `Gremium` | `Ausschuss` |
| Rat | `Gremium` | `Rat` |
| Beirat | `Gremium` | `Beirat` |
| Kommission | `Gremium` | `Kommission` |
| Fraktion | `Fraktion` | `Fraktion` |
| Amt/Fachbereich | `Verwaltungsbereich` | `Amt/Fachbereich` |
| Sonstiges | `Sonstiges` | `Sonstiges` |

### Lizenz der offenen Daten

**Session → Einstellungen → Karte „OParl-Schnittstelle“ → „Lizenz der offenen Daten“** (Berechtigung
`manage_settings`). Zur Auswahl stehen die Datenlizenz Deutschland (Zero 2.0 und Namensnennung 2.0)
sowie Creative Commons (CC0 1.0 und CC BY 4.0); „Keine Angabe“ entfernt die Lizenz aus der Ausgabe.
Die Wahl steht in `SessionTenant.oparl_license`, der Zeitpunkt in `oparl_license_valid_since`, jede
Änderung im Audit-Log. Welche Lizenz passt, entscheidet die Kommune; mandari gibt keine vor.

### Sitzungsort (`Meeting.location`)

Der Ort einer Sitzung ist ein eingebettetes Location-Objekt mit `description` (Ort, z. B. „Rathaus“),
`room`, `streetAddress`, `postalCode` und `locality`, dazu `bodies` und `meetings`. Session führt den
Ort an der Sitzung; das Objekt trägt deshalb die Kennung der Sitzung und ist unter
`…/api/oparl/location/<Kennung der Sitzung>/` abrufbar – nur für öffentliche Sitzungen. Entfällt die
Ortsangabe oder ist die Sitzung gelöscht bzw. nicht mehr öffentlich, liefert die Adresse ein gekürztes
Objekt mit `"deleted": true`; Sitzungen, die nie öffentlich waren, ergeben 404.

Die bisherigen Felder `mandari:locationName`, `mandari:locationRoom` und `mandari:locationAddress`
bleiben zusätzlich erhalten. Sie sind **abgekündigt** und entfallen frühestens am 01.10.2027
(`RELEASE_POLITIK.md`, zwölf Monate). Das Bürgerportal liest sie bis dahin weiter; seine Ortsangabe
bleibt unverändert.

Im RIS-Bestand des Bürgerportals steht der Ort als Text an der Sitzung (Gebäude vor Raum, Anschrift
mit Postleitzahl und Ort) – Spiegel und Ingestor legen dafür kein eigenes Location-Objekt an, weil der
Ort nur zu dieser einen Sitzung gehört. Er verschwindet deshalb mit ihr: Wird die Sitzung gelöscht
oder nichtöffentlich, ist auch der Ort im Bürgerportal und im Aggregator (`/oparl/v1/`) sofort nicht
mehr abrufbar. Ein Location-Objekt, das ein älterer Stand des Ingestors unter der Kennung des Ortes
angelegt hat, wird dabei mit zurückgenommen; der Ingestor markiert es außerdem beim nächsten Abgleich
der Sitzung. Der Aggregator bildet den Ort aus dem Text (`…/v1/location/<Kennung der Sitzung>`).

### Bedingte Anfragen (ETag, 304)

Jede erfolgreiche JSON-Antwort trägt einen `ETag` über ihren Inhalt und `Cache-Control: no-cache`.
Wer die Antwort aufbewahrt, fragt mit `If-None-Match: <ETag>` nach und erhält `304 Not Modified`
ohne Inhalt, solange sich nichts geändert hat. Geprüft wird bei jeder Anfrage neu: Eine Rücknahme
(Sitzung nicht mehr öffentlich, Schnittstelle gesperrt) wirkt sofort, auch bei passendem `ETag`.
Der `ETag` hängt am Inhalt, nicht an einem Zeitstempel; er ändert sich genau dann, wenn sich die
Antwort ändert.

### Hinweise für bestehende Abnehmer

Adressen und IDs bleiben unverändert. Geändert haben sich Werte zweier Felder:

- `organizationType` nannte bisher den internen Schlüssel (`committee`, `council`, `faction`,
  `advisory`, `commission`, `department`, `other`). Wer danach filtert, stellt auf die Werte der
  Tabelle oben um oder nutzt `classification`.
- `File.date` war ein Zeitpunkt (`2026-09-30T08:15:00+00:00`) und ist jetzt ein Datum (`2026-09-30`).
  Wer den Zeitpunkt braucht, liest `created`.

Neu hinzugekommen sind `Meeting.location`, `license`, `licenseValidSince` und der Objekttyp
`location`; Abnehmer, die unbekannte Felder ignorieren, brauchen nichts zu tun.

## Öffentliche Niederschrift (`resultsProtocol`, Issue #318)

Mit dem Veröffentlichen einer Niederschrift entsteht eine Datei mit ihrem öffentlichen Teil
(PDF, dazu der Text im OParl-Feld `File.text`). Sie hängt an der Sitzung und erscheint als
`Meeting.resultsProtocol` (OParl 1.1: Ergebnisprotokoll), nicht zusätzlich unter
`auxiliaryFile`; abrufbar über den üblichen Datei-Endpunkt. Name und Dateiname sind fest
(„Niederschrift (öffentlicher Teil)“), der Speichername trägt nur das Sitzungsdatum.

- Grundlage sind ausschließlich unverschlüsselte Felder der öffentlichen Sitzung und ihrer
  öffentlichen TOPs (ohne nichtöffentliche Unterpunkte), der öffentliche allgemeine Teil und
  Berichtigungen des öffentlichen Teils. Verschlüsselte Felder werden dafür nie entschlüsselt;
  eine nichtöffentliche Sitzung hat keine öffentliche Fassung.
- Erzeugt wird beim Veröffentlichen, nicht beim Abruf. Ändert sich danach der öffentliche Inhalt
  (Berichtigung, TOP oder Sitzung wird nichtöffentlich, Anwesenheit korrigiert), entsteht eine
  neue Datei mit neuer Kennung; die alte wird gelöscht, hinterlässt einen Tombstone und
  verschwindet sofort aus dem Bürgerportal. Dasselbe gilt für „Veröffentlichung zurücknehmen“.
- Niederschriften, die vor diesem Stand veröffentlicht wurden, erhalten ihre Datei einmalig über
  `python manage.py session_publish_protocols` (siehe `DEPLOYMENT.md`).

Das Bürgerportal zeigt die Datei auf der Sitzungsseite (Karte „Niederschrift“), gespeist aus dem
gespiegelten `resultsProtocol` bzw. `verbatimProtocol` – auch bei fremden OParl-Quellen.

## Abstimmungsergebnisse (Erweiterung, Issue #41)

Öffentliche TOPs mit Ergebnis tragen zusätzlich zu `result`/`resolutionText` die Vendor-Felder
`mandari:vote` (Summen: `method`, `methodLabel`, `result`, `resultLabel`, `yes`, `no`, `abstain`) und —
**nur bei namentlicher Abstimmung** (`voting_method = roll_call`) — `mandari:rollCall`
(Liste aus `name`, `vote`, `voteLabel`, inklusive Befangenheit als `excluded`). Offen erfasste, geheime
oder nur summierte Abstimmungen liefern nie Einzelstimmen. Die Insight-OParl-API reicht beide Felder
durch, die Insight-Sitzungsseite zeigt Summen und (aufklappbar) die namentlichen Stimmen.

## Umsetzungsstand und Genehmigung der Niederschrift (Erweiterung, Issue #525)

- `AgendaItem` `mandari:implementation`: Umsetzungsstand eines Beschlusses (`status` aus `open`,
  `in_progress`, `done`, `deferred`, dazu `statusLabel`, `deadline`, `note`, `modified`). Nur nach derselben
  Regel wie die Beschlussseiten im Bürgerportal: Freigabe der Verwaltung am Mandanten und am Beschluss,
  angenommen, nicht abgesetzt, öffentlicher TOP einer öffentlichen Sitzung, Mandant veröffentlicht. `note` ist
  die öffentliche Statusmeldung; Erledigungsvermerk, Zuständigkeit und Bearbeitung bleiben intern. Ändert sich
  die Freigabe am Mandanten (Schalter „Umsetzungsstand veröffentlichen“, Veröffentlichung im Bürgerportal beendet
  bzw. wieder aufgenommen), gelten die betroffenen Beschlüsse als geändert (`modified`): Abgleiche mit
  `modified_since` lesen sie sofort neu, nicht erst beim nächsten Vollabgleich.
- `Meeting` `mandari:protocolApproval`: Genehmigung der veröffentlichten Niederschrift, nur solange
  `resultsProtocol` ausgeliefert wird. `mode` ist `follow_up` (genehmigt, Tag der genehmigenden Sitzung bzw. der
  Genehmigung in `date`, die Sitzung in `meeting` nur, wenn sie öffentlich ist) oder `direct` (ohne
  Genehmigungsschritt veröffentlicht). Den Weg hält die Niederschrift beim Genehmigen bzw. beim Veröffentlichen
  ohne Genehmigungsschritt fest; Rücknahme und erneute Veröffentlichung (etwa nach einer Berichtigung) ändern ihn
  nicht.

Ingestor und Spiegel übernehmen beide Erweiterungen wie Beschlussnummer und Abstimmung in Spalten des RIS-Bestands
(`mandari_oparl/extensions.py`); Bürgerportal und Work lesen sie über die Lese-Fassade (`hub/ris/selectors.py`).

## Sitzungsformat und Übertragung (Erweiterung, Issue #138)

Hybride und digitale Sitzungen sowie Sitzungen mit Übertragung tragen am `Meeting` zusätzlich
`mandari:meetingFormat` (`presence`, `hybrid`, `digital`), `mandari:meetingFormatLabel` und
`mandari:publicAccess` mit `url` (Livestream bzw. Anmeldeseite), `note`, `hint` (fertiger Hinweis
für die Öffentlichkeit) sowie – bei digitalen Sitzungen mit geschütztem Zugang nach Landesrecht –
`registrationRequired` und `registrationDays`. Präsenzsitzungen ohne Übertragung bleiben unverändert.
Der Zugangsweg für zugeschaltete Mitglieder wird **nie** ausgeliefert (verschlüsselt, nur in der Ladung
an die Mitglieder). Die Insight-Sitzungsseite zeigt daraus die Karte „Teilnahme der Öffentlichkeit“;
Links nur mit `http(s)`. Rechtsgrundlagen je Land: `SESSION_SITZUNGSFORMAT_LANDESRECHT.md`.

## Termine nichtöffentlicher Sitzungen (Erweiterung, Issue #757)

Ein Gremium kann die Termine seiner nichtöffentlichen Sitzungen veröffentlichen (Einstellung „Termine
nichtöffentlicher Sitzungen veröffentlichen“, je Sitzung „Termin veröffentlichen“), z. B. der stets
nichtöffentliche Hauptausschuss. Das `Meeting` erscheint dann mit Name, Beginn, Ende, Status, Gremien und
`mandari:nonPublic: true`; Ort (`location`, auch unter `…/location/<Kennung>/`), Tagesordnung, Anlagen,
Niederschrift und Sitzungsformat fehlen. Wird eine öffentliche Sitzung nichtöffentlich und bleibt nur ihr
Termin veröffentlicht, nimmt die Schnittstelle TOPs, Anlagen und Ort zurück (Tombstones), die Sitzung bleibt.

## Beratungsfolge (Consultation)

Die Beratungsfolge aus Issue #34 (`SessionConsultation`) wird spec-konform
als `Consultation` ausgeliefert: `paper`, `organization`, `meeting`/
`agendaItem` (sobald terminiert und öffentlich), `role`
(Vorberatung/Anhörung/Entscheidung/Kenntnisnahme) und `authoritative`
für die entscheidende Station. Eine Station in einer nichtöffentlichen
Sitzung bzw. auf einem nichtöffentlichen TOP nennt nur `paper` – ohne
`organization`, `role`, `authoritative`, `meeting` und `agendaItem`.

## Pagination und Zeitfilter

Wie beim Aggregator: OParl-Listen-Envelope (`data`/`pagination`/`links`)
mit echten `links.next`-URLs und HTTP-`Link`-Headern; Seitengröße über
`OPARL_API_PAGE_SIZE` (Standard 100). Sortierung nach `modified`
aufsteigend — stabil für inkrementelle Clients. Mit `modified_since`
werden Objekte und Tombstones seitenweise zusammengeführt; geladen
werden nur die Objekte der angefragten Seite.

Alle Listen unterstützen `created_since`, `created_until`,
`modified_since`, `modified_until`. **Zeitstempel MÜSSEN eine explizite
Zeitzone enthalten** (`+00:00`, `Z`, …); naive Zeitstempel werden mit
HTTP 400 abgelehnt (`+` in URLs als `%2B` kodieren).

## Gelöschte/entöffentlichte Objekte (Tombstones)

OParl 1.1 §2.8, Muster wie beim Aggregator: Objekte, die einmal
öffentlich ausgeliefert wurden und danach **gelöscht** oder auf
**nicht öffentlich** gestellt werden, hinterlassen einen Tombstone
(`SessionOParlTombstone`):

- Objekt-Endpunkte liefern weiterhin **HTTP 200** mit dem gekürzten
  Objekt (`id`, `type`, `created`, `modified`, `deleted: true`);
  `modified` ist der Löschzeitpunkt. Inhalte entfallen vollständig.
- Listen **ohne** Filter enthalten keine Tombstones.
- Listen **mit `modified_since`** enthalten passende Tombstones —
  inkrementelle Clients bekommen Löschungen zuverlässig mit.
- Wird ein Objekt wieder veröffentlicht (NÖ → Ö), verschwindet der
  Tombstone und das Vollobjekt ist wieder abrufbar.
- Ö→NÖ-Wechsel kaskadieren: Eine entöffentlichte Sitzung hinterlässt
  auch Tombstones für ihre öffentlichen TOPs und Anlagen; eine
  entöffentlichte Vorlage für ihre Anlagen und Beratungsstationen.

## Änderungsfeed (kompatible Erweiterung von OParl 1.1)

Ist der Änderungsfeed in der Installation eingeschaltet (`OPARL_CHANGES_ENABLED`, siehe
`OPARL_API.md`, Abschnitte „Änderungsfeed“ und „Betrieb“), bietet jeder freigeschaltete Mandant ihn
unter `…/api/oparl/body/changes/` an; der Body nennt die Adresse in `mandari:changes`. Format, Cursor,
Aufbewahrung und `410` sind dieselben wie beim Aggregator – beide Ausgaben nutzen dieselbe
Serialisierung (`mandari/hub/api/changes.py`).

- **Kommune im Journal:** die kanonische Kennung des Body, `uuid5` über `…/api/oparl/body/`.
  Ereignisse nennen Objekte ebenfalls mit ihrer kanonischen Kennung (`uuid5` über die Adresse des
  Objekts, ADR `docs/adr/20260929-kanonisches-modell.md`).
- **Adressen nur für Öffentliches:** Der Feed nennt die Adresse eines Objekts (`id`) nur, wenn es
  öffentlich ist (Tabelle „Sicherheitsgarantie“) oder es war und einen Eintrag für Gelöschtes
  hinterlassen hat (`SessionOParlTombstone`). Was nie öffentlich war, erscheint nicht – auch nicht,
  wenn ein Ereignis es fälschlich als öffentlich meldet –, und die Antwort des Feeds bleibt dann
  dieselbe.
- **Rücknahmen:** Wird ein Objekt gelöscht oder nichtöffentlich, erscheint es als `delete` mit Grund;
  unter seiner Adresse steht das gekürzte Objekt. Dafür muss der Eintrag für Gelöschtes bestehen
  bleiben; er enthält nur Typ, Kennung und Zeitpunkte.
- **Freischaltung:** Feed und Snapshot folgen der Freischaltung der Schnittstelle wie alle übrigen
  Endpunkte: Ohne sie (oder bei deaktiviertem Mandanten) antworten sie mit `404`, gleich, was im Journal
  geschieht. Nach erneuter Freischaltung gilt ein Cursor weiter (innerhalb der Aufbewahrung), denn die
  Freischaltung ändert weder Daten noch ihre Sichtbarkeit. Das Ende der Veröffentlichung im
  Bürgerportal betrifft nur dessen Spiegel (`OPARL_API.md`, Abschnitt „Welche Kommunen“), nicht die
  eigene Schnittstelle des Mandanten.
- **Snapshot:** `…/api/oparl/body/snapshot/` (am Body in `mandari:snapshot`) liefert den öffentlichen
  Gesamtstand als NDJSON mit dem Cursor, ab dem der Feed fortsetzt: den Body und die Objekte der Listen
  `organizations`, `people`, `meetings` und `papers` mit ihren Einbettungen (Mitgliedschaften,
  Tagesordnungspunkte des öffentlichen Teils, öffentliche Anlagen, Beratungen). Form und Übergabe wie
  beim Aggregator (`OPARL_API.md`, Abschnitt „Snapshot“).

## Konsumenten

Ein generischer OParl-Client kann einen Session-Mandanten vollständig und
inkrementell spiegeln — insbesondere der mandari-Ingestor bzw. der lokale
Sync-Befehl für den Insight-Durchstich (Issue #36, nächster Abschnitt).

# Session-Kommunen im Bürgerportal (Insight-Durchstich)

Der eigentliche USP: Eine Session-Kommune erscheint **automatisch** im
offenen, kommunenübergreifenden Insight-Bürgerportal (Issue #36) — der
Ingestor konsumiert die Session-OParl-API als ganz normale OParl-Quelle.

## Veröffentlichungs-Schalter

Der Mandant entscheidet, ab wann seine öffentlichen Daten ins Portal
fließen: **Session → Einstellungen → Bürgerportal** (Berechtigung
`manage_settings`; Feld `SessionTenant.insight_publish`, Umschalten wird
im Audit-Log protokolliert). Voraussetzung ist die freigeschaltete
OParl-Schnittstelle (Issue #319, oben): Vorher bietet die Karte das Veröffentlichen
nicht an, und kein Weg (Oberfläche, Befehl, Signal) registriert eine Quelle.

Beim Aktivieren wird die OParl-API des Mandanten automatisch als
`OParlSource` registriert (Signal-Hook, `sync_config.session_tenant` =
Mandanten-Slug). Beendet wird die Veröffentlichung nur mit einer Entscheidung,
was mit dem bereits gespiegelten Bestand geschieht (nächster Abschnitt).

## Veröffentlichung beenden (Issue #618)

**Session → Einstellungen → Bürgerportal → „Veröffentlichung beenden …“** führt auf
eine eigene Seite mit drei Möglichkeiten. Nach der Auswahl zeigt mandari eine
Zusammenfassung der Folgen (wie viele Sitzungen, Vorlagen, Dokumente, Gremien und
Personen betroffen sind); erst die Bestätigung wirkt. Jede Änderung steht im
Audit-Log (`unpublish` bzw. `publish`, mit altem und neuem Stand und der Wirkung).
In allen drei Fällen ruht der Abgleich (Quelle inaktiv); die Daten in Session und
die eigene OParl-Schnittstelle des Mandanten bleiben unberührt.

| | Vorübergehend abschalten | Als Archiv behalten | Dauerhaft zurücknehmen |
|---|---|---|---|
| Wofür | Wartung, Prüfung der veröffentlichten Daten | Wechsel zu einem anderen System | Daten sollen nicht mehr über das Bürgerportal abrufbar sein |
| Seiten der Kommune | Hinweis statt Inhalt, HTTP 503 mit `Retry-After` | lesbar, Hinweis „Archiv – nicht mehr aktuell“ | „nicht mehr verfügbar“, HTTP 410 |
| Einstieg `/insight/k/<slug>/` | Hinweis (503) | lesbar mit Hinweis | 410 |
| Kommunenauswahl | bleibt gelistet | bleibt gelistet | nicht mehr gelistet |
| Suche, Merkliste | Einträge ausgeblendet | Einträge bleiben | Einträge entfernt |
| Sitemap der Kommune | 503 mit `Retry-After` | unverändert | 410, nicht mehr im Sitemap-Index |
| OParl-API des Bürgerportals (`/oparl/v1/`) | 503 mit `Retry-After` für Listen und Objekte der Kommune | unverändert | Einträge als gelöscht (`"deleted": true`, in `modified_since`-Listen) |
| Beschlussseiten „Was wurde aus …?“ | Hinweis (503) | lesbar, keine neuen Abos, keine E-Mails mehr | 410 |
| Bestand in der Datenbank | unverändert | unverändert | als zurückgenommen markiert (nicht gelöscht) |

**Umkehrbar:** Die Möglichkeiten lassen sich untereinander wechseln („Möglichkeit
ändern …“); aus einer dauerhaften Rücknahme heraus kommt der Bestand dabei zurück.
„Wieder veröffentlichen“ hebt jede Möglichkeit auf und nimmt den Abgleich wieder auf.
Nach einer dauerhaften Rücknahme kommen genau die Einträge zurück, die in Session
weiterhin öffentlich sind (wie beim Reaktivieren eines Mandanten, Issue #317).

**Früher beendet (alter Stand):** Mandanten, die vor dieser Auswahl beendet haben,
haben keine Möglichkeit gewählt; ihr Bestand steht weiter ohne Hinweis im Bürgerportal.
Die Einstellungen weisen darauf hin und bieten „Umgang mit dem bisherigen Bestand
festlegen …“ an. Betrieb: `session_insight_source --tenant <slug> --deactivate --mode <…>`.

**Technik:** Die Entscheidung steht in `SessionTenant.insight_end_mode`
(`paused`, `archived`, `withdrawn`; leer = alter Stand ohne Hinweis). Den Stand, nach dem
das Bürgerportal Seiten, Suche, Sitemaps und OParl richtet, trägt die Quelle in
`OParlSource.sync_config["portal_state"]` (`insight_core/publication.py`,
Middleware `PublicationStateMiddleware`). Maßgeblich ist die Kommune der Seite: bei
Detailseiten die des Eintrags, bei Kalender, Kalender-Abo (ICS) und Sitzungsplan eine
per `?kommune=<uuid>` angegebene, sonst die gewählte – auch wenn erst die Seite selbst
sie wählt (erster Aufruf ohne gewählte Kommune). Speichern, Wirkung und Audit laufen in einer
Transaktion; bricht etwa eine große Rücknahme ab, bleibt alles beim alten Stand.
Eine endgültige Löschung des Bestands bleibt ein eigener Auftrag der Kommune
(`manage.py purge_deleted`, siehe `docs/OPARL_API.md`).

Anders beim **Deaktivieren des Mandanten** (Issue #317): Dann nimmt mandari die
Quelle vollständig zurück – Quelle inaktiv, Kommune nicht mehr gelistet, alle
gespiegelten Einträge zurückgenommen –, und das Reaktivieren stellt sie wieder
her. Dazu, zum eigenen Einstieg `/insight/k/<slug>/` je Körperschaft und zum
Anlegen neuer Mandanten siehe `docs/SESSION_MANDANT_ANLEGEN.md`. Der Body der
API nennt Körperschaftstyp und AGS des Mandanten als `classification` und `ags`,
sofern gepflegt (sonst wie bisher `classification: "Kommune"`).

## Quelle per CLI registrieren

```bash
# Registrieren (setzt insight_publish und legt die OParlSource an); setzt die freigeschaltete
# OParl-Schnittstelle voraus – mit --oparl-freischalten wird sie zugleich freigeschaltet
python manage.py session_insight_source --tenant musterstadt
python manage.py session_insight_source --tenant musterstadt --oparl-freischalten

# Alle veröffentlichten Mandanten (nach)registrieren, z. B. nach Umzug
python manage.py session_insight_source --all

# Deaktivieren (alter Weg: Quelle aus, Bestand bleibt ohne Hinweis sichtbar)
python manage.py session_insight_source --tenant musterstadt --deactivate

# Veröffentlichung beenden mit Auswahl (paused, archived oder withdrawn)
python manage.py session_insight_source --tenant musterstadt --deactivate --mode archived

# Abweichende Basis-URL (z. B. lokale Instanz)
python manage.py session_insight_source --tenant musterstadt --base-url http://localhost:8000
```

Die Schnittstelle vergibt ihre IDs und Links immer aus `SITE_URL`. Eine abweichende Basis-URL
taugt deshalb nur, wenn sie dieselbe Adresse wie `SITE_URL` hat (etwa eine lokale Instanz mit
`SITE_URL=http://localhost:8000`). Nach einem Domainwechsel nicht neu registrieren, sondern umziehen
(Abschnitt „Domainwechsel“).

## Sync-Wege

1. **Produktion: Ingestor-Daemon** (`ingestor/`): Die registrierte Quelle
   ist eine normale OParl-Quelle und wird vom Daemon im regulären Zyklus
   mitsynchronisiert (inkl. `modified_since` und Tombstones). Es ist
   keine weitere Konfiguration nötig — hier wird bewusst **kein**
   automatischer Prod-Sync eingerichtet; der Daemon-Zyklus übernimmt.
2. **Lokal/Einzel-Sync: `sync_session_insight`** — synchroner,
   leichtgewichtiger Spiegel ohne Daemon-Abhängigkeiten (urllib,
   funktioniert auch mit SQLite):

   ```bash
   # inkrementell (modified_since = letzter Sync, inkl. Tombstones)
   python manage.py sync_session_insight --tenant musterstadt

   # Voll-Sync bzw. gezielt nach Quell-URL
   python manage.py sync_session_insight --source-url http://localhost:8000/session/musterstadt/api/oparl/ --full

   # alle registrierten Session-Quellen
   python manage.py sync_session_insight --all
   ```

   Implementierung: `mandari/insight_sync/session_mirror.py`.

Nach dem Sync ist die Kommune im Bürgerportal sichtbar
(`/insight/vorgaenge/…`, `/insight/termine/…`, Suche/Karte je nach
aktivierten Diensten); Änderungen erscheinen mit dem nächsten Zyklus,
Ö→NÖ-Wechsel und Löschungen werden über Tombstones nachgezogen und im
Portal ausgeblendet.

## Anleitung: Musterstadt-Mandant an Insight anbinden

1. In Session als Admin des Mandanten anmelden:
   `/session/musterstadt/settings/` → Karte **OParl-Schnittstelle** →
   „OParl-Schnittstelle freischalten“, dann Karte **Bürgerportal** →
   „Im Bürgerportal veröffentlichen“. (Alternativ:
   `python manage.py session_insight_source --tenant musterstadt --oparl-freischalten`.)
2. Prüfen: Im Django-Admin unter *OParl-Quellen* existiert jetzt
   `Sitzungsdienst Stadt Musterstadt` mit der URL
   `<SITE_URL>/session/musterstadt/api/oparl/` (aktiv).
3. Sync anstoßen (falls nicht auf den Daemon-Zyklus gewartet werden soll):
   `python manage.py sync_session_insight --tenant musterstadt --full`
4. Ergebnis: Die öffentlichen Gremien, Personen, Sitzungen (nur Ö-Teile),
   Vorlagen, Anlagen und Beratungsfolgen der Musterstadt erscheinen im
   Bürgerportal unter `/insight/`.

Ende-zu-Ende-Beweis (inkl. NÖ-Ausschluss über den kompletten
Insight-Datenbestand): `python scripts/smoke_insight_durchstich.py`.

## Betrieb

| Umgebungsvariable | Standard | Bedeutung |
|-------------------|----------|-----------|
| `OPARL_API_PAGE_SIZE` | `100` | Objekte pro Listen-Seite |
| `OPARL_API_RATE_LIMIT` | `120` | Anfragen/Minute je IP (`0` = deaktiviert) |

IDs, Listen- und Blätter-Links der API bauen auf der öffentlichen Adresse der Installation auf
(`SITE_URL`), nicht auf dem Host der Anfrage. Die API antwortet unter jedem konfigurierten Host
(`ALLOWED_HOSTS`), liefert aber überall dieselben IDs.

**Adressen und Kennungen.** Die IDs der API sind die Adressen der Session-Objekte; sie folgen
`SITE_URL`. Die kanonischen Kennungen, unter denen RIS-Bestand, Bürgerportal, Änderungsfeed und
Ereignisse die Objekte führen (`uuid5`, ADR `docs/adr/20260929-kanonisches-modell.md`), bilden sich
aus denselben Adressen auf der **Basisadresse der Kennungen**. Diese Basis wird einmal je Installation
festgelegt: beim Update auf diese Version aus dem damaligen `SITE_URL` (Migration `common/0009`), auf
einer neuen Installation beim ersten Bedarf. Danach ändert sie sich nicht mehr, auch nicht mit
`SITE_URL`. Solange beide gleich sind, ist die kanonische URI eines Objekts seine Adresse. Jede
Bürgerportal-Quelle eines Mandanten trägt ihre Basis zusätzlich in `sync_config["id_base"]`; daraus
rechnen Ingestor und Spiegel (`shared/mandari_oparl/ids.py`).

`python manage.py check_ris_ids --dry-run` zeigt die festgeschriebene Basis neben `SITE_URL` und meldet,
wenn beide voneinander abweichen oder eine Quelle ihre Kennungen auf einer anderen Basis bildet.

### Domainwechsel

Zieht die Installation auf eine neue Domain um, bleiben alle Kennungen erhalten; nur die Adressen
ändern sich. Ablauf:

1. Ingestor bzw. Worker anhalten, damit kein Abgleich zwischen den Schritten läuft.
2. `SITE_URL` (bzw. `DOMAIN`) auf die neue Adresse setzen und die Anwendung neu starten. Die API gibt
   ab jetzt die neuen Adressen aus, Änderungsfeed und Snapshot nennen dieselben Kennungen wie vorher.
3. Die Adressen im Bestand des Bürgerportals umziehen – erst die Vorschau, dann mit `--yes`:

   ```bash
   python manage.py move_session_sources          # Vorschau, ändert nichts
   python manage.py move_session_sources --yes    # umziehen
   ```

   Der Befehl setzt die URL jeder Session-Quelle auf ihre Adresse unter `SITE_URL` und schreibt
   Adressen, Verweise, Links und Rohdaten ihrer Objekte um. Die Kennungen bleiben; die bisherige Basis
   steht danach in `sync_config["id_base"]` der Quelle. Eine nach dem Wechsel automatisch angelegte,
   leere Quelle unter der neuen Adresse geht dabei auf. Stehen unter der neuen Adresse schon Objekte,
   bricht der Befehl ohne Änderung ab.
4. Ingestor bzw. Worker wieder starten. Der nächste Abgleich aktualisiert die Objekte, statt sie neu
   anzulegen.
5. `python manage.py check_ris_ids --dry-run`: keine abweichenden Kennungen und URIs. Der Hinweis, dass
   die Basis der Kennungen nicht `SITE_URL` ist, ist nach einem Domainwechsel gewollt.

Smoke-Tests: `python scripts/smoke_session_oparl.py` (Spec-Struktur,
Pagination, Filter, Tombstones, Ö/NÖ-Beweis) und
`pytest apps/session/tests/test_security_matrix.py` (aus `mandari/`;
Tenant-Isolation, Ö/NÖ-Matrix, Permission-Matrix).
