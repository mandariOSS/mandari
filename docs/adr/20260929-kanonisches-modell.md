# Kanonisches RIS-Modell: OParl-Kern mit Erweiterungen, stabile uuid5-Kennungen, Löschsemantik

- Status: angenommen
- Datum: 2026-09-29
- Issue: #476
- Paket: Datendrehscheibe, A7
- Hängt ab von: [A1 Schichtenmodell](20260929-schichtenmodell.md),
  [A5 Verträge](20260929-ereignisvertraege.md)

## Kontext

RIS-Daten kommen aus zwei Richtungen in den RIS-Bestand: aus fremden Ratsinformationssystemen über
den Ingestor und aus Session für eigene Mandanten. Heute gibt es dafür zwei OParl-Serialisierungen.
Django vergibt Kennungen per uuid4, der Ingestor per uuid5; die Kennungen der Session-OParl-
Schnittstelle hängen vom Host der Anfrage ab. Work liest RIS-Tabellen direkt.

Fraktionen hängen Notizen und Positionen an Tagesordnungspunkte und Vorlagen. Wechseln deren
Kennungen bei einer Neuveröffentlichung der Tagesordnung, verlieren die Notizen ihren Bezug; das
ist aus anderen Systemen als verbreiteter Mangel bekannt. Umgekehrt zeigt die Literatur zu
kanonischen Datenmodellen, dass ein Modell für alles (einschließlich interner Arbeitsdaten) mit
optionalen Feldern überladen wird.

## Entscheidung

- **Umfang: nur der RIS-Kern.** Die OParl-1.1-Typen (`Body`, `Organization`, `Person`,
  `Membership`, `LegislativeTerm`, `Meeting`, `AgendaItem`, `Paper`, `Consultation`, `File`,
  `Location`) und klar gekennzeichnete Erweiterungen: Abstimmung (`Voting`), Beschluss und
  Umsetzung am Tagesordnungspunkt, Genehmigung der Niederschrift, Codelisten. Interne Daten von
  Work gehören nicht dazu.
- **Standard führt.** Das Modell folgt OParl 1.1 mit dokumentierten, kompatiblen Erweiterungen
  (Profile). Es ist eine strikte Obermenge:
  Jede mandari-Schnittstelle bleibt ein gültiger OParl-1.1-Server, Erweiterungen liegen in einem
  eigenen Namensraum. Zwischen den internen Session-Modellen und dem kanonischen Modell liegt eine
  versionierte Abbildung (`hub/ris/mapping/`), damit sich beide Seiten unabhängig entwickeln können.
- **Kanonische Kennung:** `uuid5(NS_MANDARI_RIS, kanonische_uri)`.
  - Fremd-RIS: Die URI ist die `id` des Objekts in der Quelle.
  - Eigene Session-Objekte: Die URI ist die host-unabhängige öffentliche OParl-URL auf Basis der
    konfigurierten Adresse der Installation, nicht des Request-Hosts.
  - Ingestor und Django verwenden dieselbe Funktion (`shared/mandari_oparl/ids.py`). Abweichende
    Bestandskennungen werden per Prüfskript erfasst und, wo nötig, mit Weiterleitung umgeschlüsselt.
- **Stabilität:** Tagesordnungspunkte, Vorlagen und Dateien behalten ihre Kennung über
  Neuveröffentlichungen hinweg.
- **Schreiber des RIS-Bestands** (beide in der Drehscheibe): der Ingestor für Fremdquellen und ein
  RIS-Projektor für Session-Mandanten, der aus deren Ereignissen schreibt. Die Quellen sind je
  `source` disjunkt. Anreicherungsspalten (erkannter Text, Zusammenfassung, Verortung) schreiben nur
  Aufträge; die Liste steht in `ENRICHMENT_FIELDS`.
- **Lesen** nur über die Fassade `hub/ris/selectors.py` mit fachlichen Abfragen.
- **Löschsemantik:** Objekte werden nicht hart gelöscht, sondern als `deleted` oder `depublished`
  mit Grundcode markiert: `quelle_geloescht`, `zurueckgenommen`, `nichtoeffentlich`,
  `datenschutz`. Daraus entstehen Ereignisse mit `operation` `delete` bzw. `redact`
  ([Feed-Format](20260929-aenderungsfeed-format.md)). Hart aufgeräumt wird nur per Auftrag nach
  einer Frist und nur ohne bestehende Verweise ([Fremdschlüssel](20260929-fremdschluessel-ris-bestand.md)).
- Die Tabellen `oparl_*` und das Label `insight_core` bleiben ([Portal-Modul](20260929-portal-modul.md)).

## Nachtrag: Basis der Kennungen festgeschrieben (#733)

Die konfigurierte Adresse der Installation wird für die Kennungen einmal festgehalten
(`apps.common.models.IdentifierBase`, beim Update aus dem damaligen `SITE_URL`) und ändert sich danach
nicht mehr. Ausgegebene Adressen folgen weiter `SITE_URL`; die kanonische URI eines Session-Objekts ist
seine Adresse auf der festgeschriebenen Basis (`canonical_uri` in `shared/mandari_oparl/ids.py`).
Zieht eine Quelle um, hält `sync_config["id_base"]` die bisherige Basis fest; Ingestor und Django
rechnen damit gleich (gemeinsame Testvektoren `umzuege`). Der Umzug der Adressen im Bestand ist ein
eigener Befehl (`move_session_sources`). Die Entscheidung selbst bleibt unverändert.

## Nachtrag: Umzug mit neuer Form der Adressen (#88)

Ändert eine Fremd-Quelle beim Umzug auch den Aufbau ihrer Adressen (ALLRIS: `…/public/oparl/papers?id=5` wird
`…/oparl/papers/5`), bilden Abbildungsregeln in `sync_config["id_rules"]` den Rest der neuen Adresse auf den der
bisherigen ab; `id_address` nennt den Präfix der neuen Adressen, `id_base` den der bisherigen. Es gilt die erste
Regel, deren Muster den ganzen Rest erfasst; ohne passende Regel ist die neue Adresse kanonisch. Ingestor und Django
rechnen gleich (`IdBases.add_source`, gemeinsame Testvektoren `regeln`). Ungültige Regeln brechen den Abgleich
der Quelle ab. Der Bestand wird in place umgeschrieben (`external_id` neu, `id` bleibt).

Lässt sich die alte Adresse aus der neuen nicht ableiten (ALLRIS: Dokumenttyp `dtyp` bei Dateien, Tagesordnungs-
bezug `bi` bei Beratungen), behalten die umgeschriebenen Objekte ihre Kennung trotzdem; `check_ris_ids` zählt sie
als „Kennung abweichend“. Das ist gewollt: Links, Suchindex und Dateivorschau bleiben gültig, und Ereignisse
verweisen auf diese Typen nur über die Zeile im Bestand, nie über eine URL. Solche Abweichungen dürfen nicht durch
Umschlüsseln „behoben“ werden.

## Nachtrag: Beschlussfassung im Bestand (#525)

Die Erweiterungen Abstimmung, Beschluss, Umsetzung und Genehmigung der Niederschrift stehen im RIS-Bestand in
eigenen, nullable Spalten, nicht nur in `raw_json`:

- **Tagesordnungspunkt:** `resolution_number`, Abstimmung (`vote_method`, `vote_result`, `votes_yes`, `votes_no`,
  `votes_abstain`), Einzelstimmen `roll_call` (nur bei namentlicher Abstimmung) und der veröffentlichte
  Umsetzungsstand (`implementation_status`, `implementation_deadline`, `implementation_public_note`,
  `implementation_modified`). Die Statusmeldung heißt wie ihr Gegenstück in Session (`implementation_public_note`),
  nicht wie der interne Erledigungsvermerk dort (`implementation_note`): Eine Abbildung nach Feldnamen, etwa im
  RIS-Projektor (#537), gibt so nie den internen Vermerk aus.
- **Sitzung:** `protocol_approval_mode`, `protocol_approved_on`, `protocol_approved_in_external_id`.
- **Abstimmung am Tagesordnungspunkt:** Session führt eine Abstimmung je Punkt, die Schnittstelle bettet sie als
  `mandari:vote` ein. Der Bestand speichert sie deshalb am Punkt; ihre kanonische Kennung leitet sich wie in
  `ris.voting.recorded` aus der Adresse des Punkts ab. Liefern Quellen später mehrere Abstimmungen je Punkt, wird
  daraus eine eigene Tabelle (additive Änderung).
- **Eine Übersetzung:** `shared/mandari_oparl/extensions.py` liest die Erweiterungen eines OParl-Objekts für
  Ingestor, Spiegel und Datenmigration gleich und verwirft, was nicht passt (unbekannte Codes, falsche Typen,
  Einzelstimmen ohne namentliche Abstimmung). Bezeichnungen der Codes stehen dort einmal.
- **Lesen:** `hub/ris/selectors.py` (`decision`, `protocol_approval`) und die Abbildung des Bestands geben die
  Spalten in derselben Form aus, die Session liefert. Das Beschluss-Tracking in Bürgerportal und Work liest
  noch Session-Tabellen; es zieht mit #538 auf diese Felder um (nach dem Umschalten auf den Projektor, #537).

## Nachtrag: Verknüpfungen aus Work über Neuveröffentlichungen (#547)

Stabile Kennungen allein reichen nicht: Manche Quellen veröffentlichen einen Stand unter neuen Adressen (mit oder
ohne Löschmeldung), und beim Abruf aus Sitzungsseiten steht die Nummer in der Adresse, sodass eine Einfügung die
Inhalte über die Zeilen verschiebt. Work hält deshalb je verknüpftem Tagesordnungspunkt und je verknüpfter Vorlage
einen **fachlichen Anker** (`apps.work.ris.models.RisAnker`): Sitzung, Nummer, Name, öffentlich, beratene Vorlagen
mit Drucksachennummer bzw. Kommune und Drucksachennummer. Was „derselbe Punkt“ ist, entscheidet die Drehscheibe
(`hub/ris/neuveroeffentlichung.py`): dieselbe Vorlage, sonst derselbe Name, öffentlich/nichtöffentlich gleich;
berät die Sitzung dieselbe Vorlage an mehreren Punkten, zählen diese Geschwister nie als Nachfolger; sind sie
unbekannt (Punkt schon vor dem ersten Abgleich abgesetzt), zählt nur ein Punkt unter derselben Nummer. Nachfolger nur
aus derselben Sitzung bzw. Kommune und nur eindeutig. Ein Zeitplan im Worker hängt die Work-Daten um (nur der
Fremdschlüssel, Inhalte bleiben verschlüsselt und unverändert) und protokolliert jeden Umzug (`RisNeuzuordnung`).
Datensätze, deren Gegenstück am Ziel steht oder die nach der letzten Bestätigung an einer Zeile angelegt wurden,
die noch auf der Tagesordnung steht (sie können deren neuen Inhalt meinen), bleiben als „zurückgelassen“ am alten
Punkt und ziehen nie wieder automatisch um; danach beschreibt der Anker, was am Punkt
steht, sodass ein weiterer Lauf nichts bewegt. Im Zweifel bleibt alles, wo es ist, und die Vorbereitung zeigt
„Nicht zugeordnet“. Der Weg ist ein Schalter (`WORK_RIS_RELINK`, Standard aus) mit Probe und Rückweg
(`ris_neuzuordnung_zurueckdrehen`). Damit ist die Fitnessfunktion „Neuveröffentlichung erhält Kennungen und
Verknüpfungen von Notizen“ als Test umgesetzt (`apps/work/ris/tests/test_neuveroeffentlichung.py`).
Neuveröffentlichte Sitzungen (neue Kennung der Sitzung) und Dateien deckt das nicht ab (#868).

## Alternativen

- **Interne Session-Modelle als gemeinsames Modell.** Bindet Work, Portal und App an Session;
  Fremd-RIS passen nicht hinein. Verworfen.
- **Ein eigenes mandari-Modell.** Braucht nach außen eine zweite Übersetzung und ist kein Standard.
  Verworfen.
- **Ein Modell für alles, einschließlich Work-Interna.** Überladen, jede Änderung in Work würde
  zur Standardfrage. Verworfen.
- **Andere Standards** (Popolo, Open Civic Data, ELI, ODS Open Raadsinformatie). Decken die
  deutsche Kommunalpraxis und die bestehenden OParl-Quellen nicht ab. Verworfen.
- **uuid4 je Installation.** Nicht reproduzierbar zwischen Ingestor, Django und Installationen.
  Verworfen.
- **Quell-URL direkt als Kennung.** Hostabhängig und ändert sich bei jedem Umzug der Quelle.
  Verworfen; die URL bleibt als Attribut erhalten.
- **Hartes Löschen mit Kaskade.** Führt zu Datenverlust in abhängigen Modulen. Verworfen.

## Folgen

**Positiv**

- Ereignisse und Sichten sehen gleich aus, egal ob Session oder ein Fremd-RIS dahintersteht. Work
  wird RIS-neutral; Prüfstein ist der Betrieb von Work mit einer OParl-Kommune ohne Session.
- Kennungen sind reproduzierbar, auch über Installationen hinweg; Notizen überstehen
  Neuveröffentlichungen.
- Eine Serialisierung für alle OParl-Ausgaben; die offene Schnittstelle bekommt eine
  Referenzimplementierung.

**Negativ**

- Bestehende Kennungen müssen umgeschlüsselt werden, mit Weiterleitungen für alte URLs.
- Die Abbildungsschicht ist zusätzlicher Code, der mit dem Standard mitwachsen muss.
- Erweiterungen bleiben gekennzeichnet, bis sie standardisiert sind.
- Die Kennungen eigener Objekte hängen an der konfigurierten Adresse der Installation; ein
  Domainwechsel darf sie nicht verändern. Die Basis-URI muss daher dauerhaft festgehalten werden.
- Der RIS-Bestand wächst um markierte, nicht gelöschte Objekte.

## Prüfung (Fitnessfunktion)

- Gemeinsame Testvektoren für die Kennungsfunktion laufen in den Django- und den Ingestor-Tests
  und müssen gleiche Ergebnisse liefern.
- Test: Die Session-OParl-Schnittstelle liefert bei unterschiedlichen Host-Headern identische
  Kennungen.
- Test: Neuveröffentlichung einer Tagesordnung erhält Kennungen und Verknüpfungen von Notizen.
- OParl-Validator in der CI für alle OParl-Ausgaben.
- Ratchet: Zugriffe auf `OParl*.objects` außerhalb von `hub` und `insight_core` dürfen nur sinken,
  Ziel null.
- Test: Hartes Aufräumen bricht ab, solange Verweise bestehen.

## Bezug

- [A5 Verträge](20260929-ereignisvertraege.md), [A8 Fremdschlüssel](20260929-fremdschluessel-ris-bestand.md),
  [A10 Feed-Format](20260929-aenderungsfeed-format.md), [A11 Portal-Modul](20260929-portal-modul.md)
- [20260909-schema-contract-django-ingestor](20260909-schema-contract-django-ingestor.md)
- `docs/OPARL_API.md`, `docs/SESSION_OPARL_API.md`
- OParl 1.1: https://oparl.org/spezifikation/online-ansicht/
- Enterprise Integration Patterns, Canonical Data Model:
  https://www.enterpriseintegrationpatterns.com/patterns/messaging/CanonicalDataModel.html
- S. Tilkov, Thoughts on a canonical data model:
  https://www.innoq.com/en/blog/2015/03/thoughts-on-a-canonical-data-model/
