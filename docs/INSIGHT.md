# Bürgerportal: Kommunenwechsel, Fraktionen, Ratsfragen, Personenfotos

Kleinere Funktionen des Bürgerportals (mandari Insight). Verortung und Karten stehen in [INSIGHT_GEO.md](INSIGHT_GEO.md),
der KI-Assistent in [KI.md](KI.md), Live-Übertragungen in [LIVE_UEBERTRAGUNG.md](LIVE_UEBERTRAGUNG.md).

## 1. Kommunenwechsel und Kommunenverzeichnis

In Deutschland gibt es rund 11.000 Gemeinden, dazu Gemeindeverbände wie Samtgemeinden, Verbandsgemeinden und
Ämter. Das Bürgerportal zeigt deshalb nie eine Liste aller Kommunen. Wer die Kommune wechselt (Seitenleiste,
Kopfzeile am Handy oder Auswahlseite `/insight/`), hat vier Wege:

1. **Suchfeld „Kommune oder Postleitzahl“.** Während der Eingabe kommen höchstens acht Vorschläge vom Server,
   unscharf (Tippfehler) und umlauttolerant („Ubungsheim“ findet „Übungsheim“). Gefunden wird über den Namen, einen
   Ortsteil oder eine Postleitzahl. Kreis und Land stehen in der zweiten Zeile, damit gleichnamige Orte
   unterscheidbar sind; ein zweites Wort filtert danach („Beispielstadt Hessen“).
2. **„In meiner Nähe“.** Nur auf Klick fragt der Browser den Standort. An den Server geht ausschließlich eine
   Zelle von 0,1 Grad (etwa 11 km), die genaue Entfernung rechnet der Browser. Gespeichert wird nichts. Dafür
   muss der Proxy den Standort für die eigene Seite erlauben (siehe „Proxy und Standortfreigabe“ unten).
3. **„Zuletzt besucht“.** Bis zu fünf Kommunen merkt sich der Browser selbst (`localStorage`, kein Cookie).
4. **Stöbern** Land → Kreis → (Gemeindeverband →) Kommune. Kreisfreie Städte sind auf der Ebene des Landes direkt
   wählbar; Kreise mit mehr als 40 Einträgen gliedern sich zuerst nach Gemeindeverbänden.

Wählbar sind nur Kommunen mit Daten, also mit einer gelisteten Körperschaft (`OParlBody.is_listed`) desselben
Regionalschlüssels oder AGS. Alle übrigen erscheinen mit dem Hinweis „noch nicht verfügbar“. Jede Kommune behält
ihre direkte Adresse `/insight/k/<slug>/`.

Ohne JavaScript leistet die Seite `/insight/kommunen/` dasselbe mit Formular und Links. Am Handy öffnet sich der
Wechsel als Blatt über den ganzen Bildschirm mit dem Fokus im Suchfeld; Pfeiltasten wandern zwischen Suchfeld und
Vorschlägen, Escape führt zurück bzw. schließt den Dialog.

### Schnittstellen des Kommunenwechsels

Keine der drei Antworten hängt von der gewählten Kommune ab oder legt eine Sitzung an; der Browser darf sie
zwischenspeichern (`Cache-Control: public`). Weil die Anmeldeprüfung jede Anfrage sieht, tragen sie `Vary: Cookie`:
Ein gemeinsamer Cache teilt sie nur zwischen Anfragen mit demselben Cookie. Stammt das Verzeichnis aus einer Datei,
nennt jede Antwort zusätzlich ihre Quellen (`quellen`: Name, Adresse, Lizenz, Adresse der Lizenz).

| Adresse | Zweck |
|---|---|
| `GET /insight/kommunen/vorschlaege/?q=…` | höchstens acht Vorschläge (`name`, `ort`, `hinweis`, `verfuegbar`, `url`) |
| `GET /insight/kommunen/naehe/?zelle=51.9,7.6` | Kandidaten rund um die Zelle mit Koordinaten, dazu die nächste Kommune mit Daten |
| `GET /insight/kommunen/stoebern/?land=05&kreis=05999&verband=…` | eine Stufe des Stöberns |

Code: `insight_core/services/kommunenverzeichnis.py` (Suche, Nähe, Stöbern), `insight_core/views/kommunen.py`,
`frontend/alpine/kommunen-wahl.ts`, `templates/components/kommunen_wahl.html`.

### Verzeichnis füllen

Das Verzeichnis (`Municipality`, Suchbegriffe in `MunicipalityTerm`) wird per CSV-Datei befüllt. In PostgreSQL
legt die Migration die Erweiterung `pg_trgm` und einen Trigramm-Index auf die Suchbegriffe an; die Suche antwortet
damit auch bei Zehntausenden Begriffen in wenigen Millisekunden.

```bash
python manage.py kommunenverzeichnis_importieren --datei kommunen.csv            # anlegen bzw. aktualisieren
python manage.py kommunenverzeichnis_importieren --datei kommunen.csv --ersetzen # Einträge außerhalb der Datei entfernen
python manage.py kommunenverzeichnis_importieren --aus-koerperschaften           # gelistete Kommunen ergänzen
```

Die gelisteten Kommunen ergänzt der Worker selbst: Der Zeitplan `insight_core.schedules.kommunenverzeichnis_abgleichen`
übernimmt stündlich jede gelistete Kommune mit Regionalschlüssel oder AGS, die im Verzeichnis fehlt (idempotent,
wie `--aus-koerperschaften`). Nach einem Deploy und nach dem Listen einer neuen Kommune ist der Wechsel damit
spätestens nach einer Stunde vollständig, ohne Handgriff. Wer nicht warten will, ruft den Befehl einmal von Hand auf.

Format: UTF-8, Semikolon, Kopfzeile, je Gemeinde, Gemeindeverband oder kreisfreie Stadt eine Zeile.

```
schluessel;name;art;kreis;breite;laenge;plz;ortsteile
059990000000;Beispielstadt;Kreisfreie Stadt;;51.9625;7.6256;48143|48145;Nordviertel|Heidekamp
039995401000;Samtgemeinde Heideland;Samtgemeinde;Landkreis Musterkreis;52.62;10.24;;
039995401014;Moorbach;Gemeinde;Landkreis Musterkreis;52.61;10.24;29331;Unterdorf
```

| Spalte | Inhalt |
|---|---|
| `schluessel` | Regionalschlüssel (12 Stellen) oder, wenn nicht bekannt, Amtlicher Gemeindeschlüssel (8 Stellen); Land und Kreis ergeben sich daraus |
| `name` | Name ohne Zusatz „Stadt“ bzw. „Gemeinde“ |
| `art` | z. B. Stadt, Gemeinde, Kreisfreie Stadt, Samtgemeinde; Samtgemeinde, Verbandsgemeinde, Amt und Verwaltungsgemeinschaft bilden im Stöbern eine eigene Stufe |
| `kreis` | Name des Kreises, bei kreisfreien Städten leer |
| `breite`, `laenge` | Mittelpunkt in Dezimalgrad (für „In meiner Nähe“), optional |
| `plz`, `ortsteile` | mit `|` getrennt, optional |

Der Import ist idempotent und läuft in einer Transaktion; fehlerhafte Zeilen werden mit Zeilennummer gemeldet und
übersprungen. Ohne Import findet der Wechsel weiterhin alle gelisteten Kommunen über ihren Namen; „noch nicht
verfügbar“, Ortsteile, Postleitzahlen und das Stöbern brauchen das Verzeichnis.

**Quellen und Namensnennung.** Schlüssel, Namen, Kreise und Mittelpunkte enthält das Gemeindeverzeichnis des
Statistischen Bundesamts (GV-ISys, Datenlizenz Deutschland – Namensnennung 2.0). Postleitzahlen und Ortsteile lassen
sich aus OpenStreetMap ableiten (ODbL, Namensnennung „© OpenStreetMap-Mitwirkende“). Einträge aus der Datei tragen
das Merkmal `imported`; sobald es einen solchen Eintrag gibt, nennen der Kommunenwechsel, die Seite
`/insight/kommunen/` und die drei Schnittstellen beide Quellen mit Lizenz. Die Antworten der Schnittstellen sind
damit Auszüge einer abgeleiteten Datenbank unter ODbL. Wer andere Quellen nutzt, setzt die Nennung in den
Einstellungen (`INSIGHT_KOMMUNENVERZEICHNIS_QUELLEN`, Liste mit `name`, `url`, `lizenz`, `lizenz_url`). Die
Umwandlung der Quellen in das Format oben übernimmt der Betreiber.

### Proxy und Standortfreigabe

„In meiner Nähe“ braucht die Standortfreigabe des Browsers. Das mitgelieferte `Caddyfile` erlaubt sie nur für die
eigene Seite (`Permissions-Policy: camera=(), microphone=(), geolocation=(self)`). Es setzt die Policy nur als
Vorgabe (`?Permissions-Policy`), wenn die Antwort keine eigene mitbringt: Die Website unter derselben Domain schickt
eine strengere Policy, die der Proxy sonst überschriebe. Wer einen eigenen Proxy betreibt,
darf `geolocation` nicht ganz sperren (`geolocation=()`): Der Browser lehnt die Abfrage dann ohne Rückfrage ab, und
der Wechsel meldet nur, dass der Standort nicht freigegeben wurde.

## 2. Fraktionen ohne OParl-Fraktion

Issue #916. Viele Ratsinformationssysteme liefern über OParl keine Fraktionsmitgliedschaften; die Fraktionen
erscheinen dort nicht als Gremium (Beispiel: eine Kommune mit 160 Gremien, keines als Fraktion gekennzeichnet).
Das Personenverzeichnis konnte dann keine Fraktion zeigen. Insight pflegt die Zuordnung deshalb selbst.

### Datenmodell der Fraktionszuordnung

`insight_core.PersonFraktion` (Tabelle `insight_person_fraktionen`, Migration `insight_core/0053_person_fraktion`),
eine Zeile je Zuordnung einer Person in einer Körperschaft:

| Feld | Bedeutung |
|------|-----------|
| `person`, `body` | Person und Körperschaft (`PROTECT`, ADR [Fremdschlüssel](adr/20260929-fremdschluessel-ris-bestand.md)) |
| `bezeichnung` | Name der Fraktion, z. B. „Fraktion A“ |
| `organisation` | optional: die Fraktion als Gremium im RIS (`SET_NULL`) |
| `partei` | optional, Text |
| `gueltig_ab`, `gueltig_bis` | Zeitraum; leer = offen |
| `quelle` | `einblendung` (Live-Übertragung), `hand` (Admin), `oparl` |
| `status` | `bestaetigt`, `vorschlag`, `abgelehnt` |
| `belege`, `zuletzt_gesehen` | Zahl und Zeitpunkt der Lesungen aus der Einblendung |

Die Datenbank erzwingt höchstens eine laufende bestätigte Zuordnung (ohne Ende) je Person und Körperschaft und
einen offenen Vorschlag je Bezeichnung (ohne Groß-/Kleinschreibung). Eigene Tabelle statt Spalten an `oparl_*`:
Der Ingestor schreibt diese Tabellen und kennt die Zuordnung nicht.

Gespeichert werden nur Bezeichnung, Zeitpunkte und Zahl der Lesungen, nie Bild- oder Tondaten.

### Regeln der automatischen Übernahme

`insight_core/services/fraktionen.py`, Schnittstelle für die Live-Erkennung (#915):

```python
fraktion_aus_einblendung(*, person, body, bezeichnung, zeitpunkt, eindeutig) -> PersonFraktion | None
aktuelle_fraktion(person, body, *, stichtag=None) -> PersonFraktion | None
```

- Gleiche bestätigte Zuordnung: eine Lesung mehr (`belege`), `zuletzt_gesehen` geht nie zurück.
- Keine bestätigte Zuordnung: bei `eindeutig=True` (Person eindeutig zugeordnet, Fraktion mehrfach gleich gelesen)
  entsteht eine bestätigte Zuordnung mit Quelle `einblendung` und Beginn am Tag der Lesung, sonst ein Vorschlag.
  Ein Vorschlag gleicher Bezeichnung zählt hoch bzw. wird bestätigt.
- Abweichende Bezeichnung bei bestehender bestätigter Zuordnung: nur ein Vorschlag, nie ein Überschreiben. Von Hand
  gepflegte Werte ändert der automatische Weg damit nie; bei der Anzeige gehen sie vor.
- Abgelehnte Bezeichnungen und beendete Zuordnungen gleicher Bezeichnung kommen nicht von selbst zurück.
- Funktionsbezeichnungen („Oberbürgermeisterin“, „Beigeordneter“, „Kämmerer“) sind keine Fraktion. Der Aufrufer
  filtert sie; der Dienst fängt die üblichen zusätzlich ab und gibt `None` zurück.
- Nebenläufig: Sperre der Zuordnungen von Person und Körperschaft, Unique-Constraints und neuer Versuch, wenn eine
  gleichzeitige Lesung die Zuordnung eben angelegt hat.

### Aus Live-Übertragungen

Das Abonnement `insight.fraktionen_live` (`insight_core/services/fraktionen_live.py`, registriert in
`insight_core/subscribers.py`) ruft `fraktion_aus_einblendung` für jede neue Wortmeldung einer Live-Übertragung
auf (`ris.broadcast.speaker_changed`, [Live-Übertragungen](LIVE_UEBERTRAGUNG.md)):

- Angaben über `hub.live.selectors.wortmeldung`; nur Wortmeldungen mit zugeordneter Person und gelesener Fraktion,
  Funktionen zählen nicht.
- `eindeutig`: Person eindeutig zugeordnet und mindestens zweimal gleich gelesen; sonst ein Vorschlag.
- Idempotent: `insight_core.PersonFraktionBeleg` (Tabelle `insight_person_fraktion_belege`, Migration
  `insight_core/0054_person_fraktion_beleg`) hält jede verbuchte Wortmeldung fest, nur mit Kennungen. Erneute
  Zustellung und Nachholen zählen sie nicht noch einmal.
- Registriert, solange `LIVE_UEBERTRAGUNG_AKTIV` an ist; transaktional, ohne Schattenbetrieb (es gibt keinen
  bisherigen Weg). Abschalten: Abonnement im Admin pausieren.
- Nachholen für vorhandene Wortmeldungen, etwa aus eingespielten Protokollen (die schreiben keine Ereignisse):

  ```bash
  python manage.py fraktionen_aus_wortmeldungen --meeting <uuid> --probelauf   # nur zählen
  python manage.py fraktionen_aus_wortmeldungen --meeting <uuid>               # oder --body <uuid>, ohne: alle
  ```

### Anzeige der Fraktion

Personenverzeichnis und Personenseite zeigen die bestätigte Zuordnung nur, wenn OParl keine Fraktion nennt
(`services/personen_liste.py`, `angaben_fuer`; eine zusätzliche Abfrage je Seite). Die Quelle bleibt erkennbar: in
der Liste gepunktet unterstrichen mit Tooltip („laut Einblendung der Live-Übertragung“ bzw. „redaktionell
gepflegt“) und Text für Bildschirmleser, auf der Personenseite als kleiner Zusatz hinter der Fraktion.
Vorschläge erscheinen nirgends öffentlich.

### Pflege im Admin

Admin → Insight → Fraktionszuordnungen (Filter Körperschaft, Quelle, Status; Suche nach Person, Fraktion, Partei).

- **Anlegen:** immer `quelle=hand`, `status=bestaetigt`; ohne Körperschaft gilt die der Person.
- **Bestätigen** (Aktion): Vorschlag wird bestätigt; eine bisherige laufende Zuordnung derselben Person endet am
  Vortag des Beginns.
- **Ablehnen** (Aktion): auch für bestätigte Zuordnungen, etwa nach falscher Personenzuordnung.
- Wer Bezeichnung, Partei oder Gremium ändert, macht die Zuordnung zur Handpflege (`quelle=hand`).

Personen und Körperschaften mit Fraktionszuordnungen werden beim Aufräumen (`purge_deleted`) und beim Entfernen einer
Kommune nicht gelöscht; die Verweisprüfung (`services/external_references.py`) führt sie wie Arbeitsdaten.

### Meldung an die Datendrehscheibe

Jede Änderung an einer bestätigten Zuordnung und die Ablehnung einer bisher bestätigten meldet
`ris.person.faction_assigned` v1 (`oeffentlich`, Aggregat `Person`, Erzeuger `hub/ris/faction_assignment.py`) im
selben `transaction.atomic`-Block. Nutzlast nur mit Kennungen, Codes und Daten (siehe
[Ereigniskatalog](EREIGNISKATALOG.md)); Bezeichnung und Partei stehen nie darin. Schalter wie beim Ingestor:
`INGESTOR_EVENTS_ENABLED`, eine Quelle mit `sync_config["events_enabled"] = false` bleibt ausgenommen.

## 3. Ratsfragen

Bürger:innen stellen Mandatsträger:innen öffentliche Fragen; Fragen und Antworten sind
für alle sichtbar. Eigener Reiter **Ratsfragen** im Insight-Portal (`/insight/fragen/`).

**Pausiert (Standard).** Bis zum Neuaufbau sind die Ratsfragen über `INSIGHT_QUESTIONS_ENABLED=false`
eingefroren (Issue #734):

- Portal, Detailseiten und der Reiter „Fragen“ der Personen zeigen die bisherigen Fragen und Antworten mit
  einem Hinweis. Antwortquoten und Antwortzeiten je Person und Fraktion sind ausgeblendet; offene Fragen
  tragen „Ohne Antwort“ statt „Offen seit … Tagen“.
- Stellen, Bestätigen, Antworten und die Bestätigungsseite antworten mit 404.
- Keine Mails (Bestätigung, Moderation, Ratsmitglied, Fragesteller:in), keine Erinnerungen;
  `send_question_reminders` endet mit Hinweis. Die Admin-Aktionen zum Freischalten und Erinnern ändern
  nichts; Ablehnen bleibt möglich.

Mit `INSIGHT_QUESTIONS_ENABLED=true` gilt der folgende Ablauf.

**Ablauf**

1. Frage stellen (`/insight/fragen/stellen/` → Person wählen → Formular mit Themenbereich)
2. E-Mail-Verifizierung durch die Fragesteller:in
3. Moderation im Django-Admin (`/admin/insight_core/publicquestion/`) — Moderator:innen
   erhalten bei jeder verifizierten Frage und jeder eingereichten Antwort eine Hinweis-Mail
4. Freischaltung: Ratsmitglied erhält Antwort-Link (Token, kein Login), Fragesteller:in den
   öffentlichen Link
5. Antwort → Moderation → Veröffentlichung; Fragesteller:in wird informiert

**Wer darf gefragt werden?** `question_service.is_mandate_holder()`: aktive Mitgliedschaft in
einem Hauptorgan (Rat, Stadtrat, Kreistag, Regionalrat, …; Verwaltungs-/Protokollrollen
ausgenommen) **oder** in einer Fraktion. Damit funktioniert die Erkennung unabhängig von den
RIS-spezifischen Rollenbezeichnungen.

**Konfiguration**

| Variable | Bedeutung |
|----------|-----------|
| `INSIGHT_QUESTIONS_ENABLED` | `true` schaltet die Ratsfragen ein; Standard `false` (pausiert, siehe oben) |
| `INSIGHT_MODERATION_EMAILS` | Kommagetrennte Empfänger der Moderations-Hinweise; leer → alle aktiven Superuser mit E-Mail |
| `SITE_URL` | Basis für alle Links in E-Mails |

**Zeitplan im Worker** `befehl:send_question_reminders` (täglich 07:30, erinnert Ratsmitglieder
14 Tage nach Veröffentlichung, danach alle 14 Tage; `DEPLOYMENT.md`, „Geplante Aufgaben“).

## 4. Personenfotos

Fotos werden aus dem Ratsinformationssystem geladen und **lokal zwischengespeichert**
(`OParlPerson.photo`, max. 400 px JPEG). Vorteile: keine kaputten Hotlinks bei RIS-Umzügen oder
Bot-Schutz, keine 404-Requests im Browser, klarer Initialen-Fallback bei „kein Foto“.

**Konfiguration je Kommune** (Admin → Kommunen → „Personenfotos“): URL-Template mit `{id}` und
Regex mit einer Capture-Group auf die `external_id`. Für bekannte RIS greifen Presets automatisch
(`insight_core/services/person_photos.py`, z. B. SessionNet Stadt Münster:
`https://www.stadt-muenster.de/sessionnet/sessionnetbi/im/pe{id}.jpg` + `/people/(\d+)$`).

Manuell im Admin hochgeladene Fotos (`photo_status = manual`) werden nie überschrieben.

**Zeitplan im Worker** `befehl:fetch_person_photos` (montags 03:00; prüft nur ungeprüfte oder
>30 Tage alte Einträge).

Einmalig alles neu laden: `python manage.py fetch_person_photos --body <kurzname> --force`

Wie die Fotos abgerufen werden (Kennung, robots.txt, Takt, Schonung abgeschalteter oder gestörter Quellen),
steht in den [Abrufregeln](ABRUFREGELN.md#personenfotos).
