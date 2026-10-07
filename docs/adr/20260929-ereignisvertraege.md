# Verträge für Ereignisse und Befehle: JSON Schema, nur additive Änderungen

- Status: angenommen
- Datum: 2026-09-29
- Issue: #476
- Paket: Datendrehscheibe, A5
- Hängt ab von: [A1 Schichtenmodell](20260929-schichtenmodell.md),
  [A2 Ereignistechnik](20260929-ereignistechnik-postgres.md)

## Kontext

Die Verbindungen zwischen den Modulen sind heute unsichtbar: Signale und direkte Zugriffe haben
keine Beschreibung, welche Felder ein Empfänger erwarten darf. Ein Produzent kann Empfänger
unbemerkt brechen. Künftig werden Ereignisse und Befehle auch außerhalb des Django-Prozesses
genutzt: vom Ingestor (eigenes Programm), zwischen getrennt installiertem Work und Session, von
Adaptern, Webhook-Empfängern und im öffentlichen Änderungsfeed. Dafür braucht es sprachneutrale,
versionierte und automatisch geprüfte Verträge.

## Entscheidung

- **Namen:** `<bereich>.<objekt>.<ereignis>`, Kleinbuchstaben, Ereignis in der Vergangenheitsform,
  z. B. `ris.paper.released`, `submission.status_changed`. Befehle im Imperativ, z. B.
  `submission.submit`. Bereiche: `ris`, `submission`, `attendance`, `invitation`, `session`, `work`,
  `portal`, `core` (`invitation` nachgetragen, siehe unten).
- **Öffentliche Sprache:** `ris.*` spricht das kanonische Modell
  ([Kanonisches Modell](20260929-kanonisches-modell.md)). `session.*`, `work.*` und `portal.*`
  sind intern und gelangen nie in öffentliche Schnittstellen.
- **Schemas:** JSON Schema 2020-12 unter `hub/contracts/schemas/<typ>/v<n>.json`, je mit
  `x-owner` (Eigentümermodul, z. B. `apps.session`), `x-visibility` (`oeffentlich`,
  `nichtoeffentlich`, `intern`, `personenbezogen`) und Beispielen. Die Hülle aus A2 ist selbst ein
  Schema.
- **Register:** `hub.contracts` liefert das Schema je Typ und Version. Die Version steht in der
  Hülle, nicht im Typnamen.
- **Eigentum:** Welche Ereignisse es gibt, legt der Eigentümer fest. `publish()` für einen Typ
  steht nur im Paket, das `x-owner` nennt.
- **Kompatibilität:** Nur ergänzen; neue Felder sind optional. Entfernen, Umbenennen, Typwechsel
  und neue Pflichtfelder sind brechend und ergeben eine neue Version. Der Eigentümer veröffentlicht
  dann mindestens ein Release lang beide Versionen, bis die Empfänger umgestellt sind. Empfänger
  ignorieren unbekannte Felder.
- **Datenschutz im Schema:** Für `personenbezogen` und `nichtoeffentlich` sind Freitextfelder
  verboten; erlaubt sind Kennungen, Codes und Feldnamen.
- **Prüfung zur Laufzeit:** `publish()` prüft das Schema in Tests und bei `DEBUG`, im
  Produktivbetrieb nicht (Einstellung `EVENTS_VALIDATE_CONTRACTS`, Umsetzung siehe Nachtrag zu
  `publish()` in [A2](20260929-ereignistechnik-postgres.md)).
- **Katalog:** Die lesbare Übersicht aller Ereignisse und Befehle wird aus dem Register erzeugt,
  nicht von Hand gepflegt.
- **Startumfang:** 27 Ereignistypen und vier Befehle
  ([Befehle](20260929-befehle-synchron.md)).

## Alternativen

- **Nur Python-Klassen ohne Schema.** Nicht sprachneutral (Ingestor, Partner, getrennte
  Installationen) und ohne prüfbaren Unterschied zwischen Versionen. Verworfen.
- **pydantic-Modelle als Quelle, Schemas generiert.** Bequem für Typisierung, aber Unterschiede in
  generierten Dateien sind schwer zu prüfen, und der Vertrag hinge an einer Bibliothek. Verworfen
  als Quelle; Typen dürfen aus den Schemas erzeugt werden.
- **Avro oder Protobuf mit Schema-Registry.** Binärformat und zusätzlicher Dienst, im
  JSON-geprägten Umfeld von Verwaltung und OParl unüblich. Verworfen.
- **CloudEvents unverändert als Hülle.** Sichtbarkeit, Mandant und Aggregat wären
  Erweiterungsattribute mit eigenen Regeln; der Mehrwert im internen Journal ist gering. Verworfen;
  eine Abbildung nach außen bleibt möglich.
- **Version im Typnamen** (`….v2`). Macht Abonnementmuster instabil. Verworfen.
- **Brechende Änderungen im Gleichschritt eines Releases.** Im Monolithen verlockend, bricht aber
  getrennte Installationen und externe Empfänger. Verworfen.

## Folgen

**Positiv**

- Verbindungen zwischen Modulen sind sichtbar, dokumentiert und in der CI geprüft.
- Externe Nutzung (Adapter, Webhooks, Feed, getrennte Installationen) braucht keinen Umbau.
- Der Katalog ist immer aktuell; Datenschutzregeln werden am Schema geprüft, nicht im Review.

**Negativ**

- Mehraufwand je Ereignis: Schema, Beispiele, Sichtbarkeitsentscheidung.
- Brechende Änderungen erfordern einen Parallelbetrieb zweier Versionen.
- Die Sichtbarkeitsklasse muss je Typ bewusst gewählt werden; Fehler dort wirken bis in den
  öffentlichen Feed.

## Prüfung (Fitnessfunktion)

In der CI, blockierend:

1. Jeder `publish(type, version)` im Code hat ein Schema im Register.
2. Der Unterschied jedes Schemas zum Stand der Zielverzweigung ist rein additiv; sonst muss eine
   neue Version angelegt sein.
3. Der erzeugte Katalog ist aktuell (neu erzeugen, Unterschied gleich null).
4. Alle Beispiele sind schemagültig. Produzententests erzeugen schemagültige Ereignisse,
   Empfängertests verarbeiten die Beispiele (Vertragstests).
5. `publish()`-Aufrufe liegen im Paket aus `x-owner`.
6. Schemas der Klassen `personenbezogen` und `nichtoeffentlich` enthalten keine Freitextfelder.

## Nachtrag zur Umsetzung (#517, #518)

Die Umsetzung präzisiert drei Punkte. Die ersten beiden lassen die Entscheidung unverändert. Der
dritte ist eine **Ausnahme von Regel 6** („keine Freitextfelder“): Für Ereignisse gilt die Regel
ohne Einschränkung, für Befehle mit der unten beschriebenen, feldgenau festgelegten Ausnahme. Wer
diesen Nachtrag annimmt, nimmt die Ausnahme an.

- **Bereich `invitation`.** Die Bereichsliste oben nennt ihn nicht, der Startumfang der Befehle
  ([A6](20260929-befehle-synchron.md)) braucht ihn für `invitation.acknowledge`. Er gehört zu den
  erlaubten Bereichen.
- **Eigentümer der `ris.*`-Verträge ist die Drehscheibe (`hub.ris`).** `ris.*` spricht das
  kanonische Modell, und dieselben Typen entstehen aus zwei Quellen: aus Session für eigene
  Mandanten und aus dem Ingestor für fremde RIS. Session veröffentlicht sie über die Abbildung in
  `hub/ris/` ([A7](20260929-kanonisches-modell.md)), nicht mit eigenem `publish()`.
  `submission.*`, `attendance.*`, `invitation.*` und `session.*` gehören `apps.session`, `work.*`
  gehört `apps.work`, `core.*` dem jeweiligen Plattformmodul.
- **Inhaltsfelder in Befehlen.** Ein Befehl bittet den Eigentümer, Daten zu speichern; manche davon
  sind Inhalte (Antragstext, Grund einer Absage) und lassen sich nicht als Kennung ausdrücken. Solche
  Felder tragen im Schema `"x-content": true` und sind vom Freitextverbot ausgenommen. Ausgenommen
  ist nur die Zeichenkette selbst: Im Teilbaum eines Inhaltsfelds gelten dieselben Regeln zur
  Offenheit wie außerhalb (kein Knoten ohne Typ, Objekte mit `additionalProperties: false`, Listen
  mit `items`, keine Verweise), jede freie Zeichenkette braucht `maxLength`, jede Liste `maxItems`.
  Ein Inhaltsfeld nimmt also nie beliebiges JSON an. Ereignisse haben nie Inhaltsfelder. Befehle
  gehen nur an den Eigentümer; der Befehlsweg gibt ihren Inhalt weder in Logs noch in
  Fehlermeldungen oder Ereignisse weiter, der Idempotenzspeicher hält nur einen Hash. Die Liste der
  Inhaltsfelder und ihr Zuschnitt (jedes Unterfeld mit seiner Längengrenze) stehen als Test fest
  (`hub/contracts/tests/test_schemas.py`); ein neues Feld, ein neues Unterfeld oder eine höhere
  Grenze ist eine bewusste Entscheidung.

Ein Muster (`pattern`) zählt nur als Kennung, wenn es vorn und hinten mit `^` und `$` verankert
ist, keinen Leerraum zulässt und die Länge auf höchstens 255 Zeichen begrenzt (Quantoren mit
Obergrenze oder `maxLength` am Feld). JSON Schema wendet `pattern` als Suche an; `^[A-Z]{2}` ließe
sonst beliebigen Text nach zwei Großbuchstaben zu (`hub/contracts/patterns.py`). `\A`, `\Z` und
Schalter wie `(?i)` kennt nur Python, nicht ECMA-262; Muster damit gelten als Freitext, damit die
Schemas für fremde Prüfer dasselbe bedeuten.

**Grenze dieser Regel:** „Ohne Leerraum“ schließt Sätze aus, nicht jedes einzelne Wort. Ein Muster
wie `^\S{1,64}$` ließe einen Namen ohne Leerzeichen oder eine Mailadresse zu. Das Register kann
nicht erkennen, was ein Muster fachlich bedeutet. Deshalb stehen die Muster der ausgelieferten
Schemas als feste Liste im Test (derzeit vier: Feldname, Code, SHA-256, Eingangsnummer); ein neues
Muster ist wie ein neues Inhaltsfeld eine bewusste Entscheidung im Review.

Ein Ereignis, das `oeffentlich` sein darf, nennt keine Kennung eines nichtöffentlichen Objekts:
`ris.paper.released` führt die Einreichung nicht, aus der die Vorlage entstand; diesen Bezug meldet
`ris.paper.created` (nur `nichtoeffentlich`).

## Nachtrag zur Prüfung in der CI (#519)

Die Prüfungen 1 bis 3 laufen im Job „Qualität“ als `scripts/check_event_contracts.py`, die Prüfungen 4
und 6 bereits im Register und seinen Tests (#517, #518). Die Umsetzung legt fest, was „rein additiv“
heißt; die Entscheidung bleibt unverändert.

- **Schema je Ereignis im Code (1).** Statisch geprüft werden alle Zeichenketten in `mandari/`,
  `ingestor/src/` und `shared/` (ohne Tests und Migrationen), die nach den Namensregeln ein Ereignis
  sind; das trifft Erzeuger und Abonnenten. Die Version prüft das Skript, wo sie im Quelltext feststeht:
  `publish("<typ>", version=<n>)` und die Modulkonstante `VERSION` eines Moduls, das Ereignistypen nennt
  (so führen `hub/ris` und der Ingestor ihre Schemaversion). Was erst zur Laufzeit feststeht, prüft
  `publish()` in den Tests gegen das Register.
- **Nur additiv (2).** Verglichen wird jede Datei unter `hub/contracts/schemas/` und die Hülle mit dem
  Stand, in den gemergt wird. Erlaubt sind ein neues optionales Feld, ein neuer Code in einer Codeliste
  (`enum`), eine weitere Sichtbarkeitsklasse, neue Einträge in `$defs` und geänderte Erläuterungen
  (`title`, `description`, `examples`, Anmerkungen `x-…` außer `x-kind` und `x-visibility`). Neue Codes
  zählen als Ergänzung wie neue Felder: Empfänger behandeln einen unbekannten Code wie einen fehlenden
  Wert. Brechend ist alles andere, ausdrücklich auch jede Lockerung (ein Pflichtfeld wird optional, ein
  Format oder Muster entfällt, eine Grenze steigt): Ein Empfänger, der heute eine Kennung erwartet, darf
  morgen keinen beliebigen Text bekommen. Eine Version, die verschwindet, ist brechend, weil das Journal
  noch Ereignisse dieser Version enthalten kann. Die Regeln stehen in `hub/contracts/compatibility.py`.
- **Katalog (3).** `docs/EREIGNISKATALOG.md` erzeugt `hub/contracts/catalog.py` aus Register und Hülle
  (Übersicht, Felder je Version, Beispiele); neu schreiben mit
  `python scripts/check_event_contracts.py --write-catalog`.
- Prüfung 5 (`publish()` nur im Paket aus `x-owner`) ist damit noch nicht automatisiert: Abonnenten nennen
  dieselben Typen, und `ris.*` entsteht in `hub.ris` und im Ingestor.

## Nachtrag: Änderungen an Gremien und Personen (#821)

Der Startumfang kannte für Gremien und Personen nur die Rücknahme (`ris.object.depublished`). Das
Abonnement `suchindex` braucht auch ihre Änderungen, etwa wenn ein Gremium umbenannt wird. Neu sind
`ris.organization.changed` und `ris.person.changed` (je v1, `oeffentlich`): Kennung, Art der Änderung
(`added` für neu erkannt oder nach einer Rücknahme wieder geliefert, `changed`) und die Namen der
geänderten Felder, wie bei `ris.agendaitem.changed`. Erzeuger ist vorerst der Ingestor mit demselben
fachlichen Vergleich wie für die übrigen Typen (Listen als Mengen, leere Werte gleich fehlenden).
Session meldet Gremien und Personen nicht; eine weitere Sichtbarkeitsklasse ließe sich additiv ergänzen.

## Nachtrag: Personenfelder für die DSGVO-Löschung (#511)

- Felder der Nutzlast, die eine Person nennen, tragen `"x-person": true`: `attendance.response_recorded.person`,
  `core.membership.changed.user`, `core.user.registered.user`, `session.allowance.approved.person`. Erlaubt nur
  in Ereignissen der Klasse `personenbezogen`, nur auf der obersten Ebene und nur an Zeichenketten im Format
  `uuid` (Register und Tests prüfen das). `session.payment.exported` nennt keine Person, nur
  Abrechnungspositionen.
- Die Kennzeichnung ist eine Anmerkung `x-…` und damit additiv. Veröffentlicht ein Eigentümer ein Ereignis mit
  `operation=redact` zu einer Person, leert die Plattform die Nutzlast der personenbezogenen Journaleinträge,
  deren Objekt die Person ist oder deren Personenfeld sie nennt (Forgettable Payloads in der einfachsten Form:
  Die Kennung bleibt, die Felder werden leer). Danach erfüllen diese Einträge ihr Schema nicht mehr; Abonnenten
  personenbezogener Typen müssen eine leere Nutzlast vertragen. Heute abonniert niemand solche Typen.
- **Welche Person ein `redact` meint**, bestimmt das Ereignis selbst: die Personenfelder seiner Nutzlast laut
  Vertrag seines Typs und seiner Version, dazu sein Objekt nur, wenn das Objekt eine Person ist (Objekttyp
  `User`). Das Objekt eines `redact` ist sonst kein Personenmerkmal: Bei `attendance.response_recorded` ist es
  die Sitzung, bei `core.membership.changed` die Mitgliedschaft, bei `session.allowance.approved` die
  Abrechnungsposition. „Zu dieser Person“ gehören Einträge, deren Objekt diese Person ist (Objekttyp `User`)
  oder deren Personenfeld sie nennt. Nennt das Ereignis keine Person (etwa ein `redact` öffentlicher
  RIS-Daten), entsteht kein Auftrag.
- Der Auftrag `journal_neutralisieren` entsteht je genannter Person in derselben Transaktion wie das Ereignis
  und immer in `events_task` (`tasks_backend.journal_backend`), auch wenn `TASKS_BACKEND` Aufträge sonst sofort
  ausführt: Das Durchsuchen des Journals gehört in den Worker, nicht in die Anfrage des Eigentümers.
- Die Plattform liest die Kennzeichnung nicht selbst aus den Schemas (sie kennt die Drehscheibe nicht):
  `hub.contracts` hängt die Liste beim Start bei `apps.events.datenschutz` ein.

## Nachtrag: Mitgliedschaften, Orte, Wahlperioden und Kommunen (#553)

Nach demselben Muster melden jetzt alle Typen des RIS-Bestands ihre Änderungen, nicht nur ihre
Rücknahme: `ris.membership.changed`, `ris.location.changed`, `ris.legislativeterm.changed` und
`ris.body.changed` (je v1, `oeffentlich`, `added` oder `changed` mit den geänderten Feldern). Die
Mitgliedschaft nennt zusätzlich Person und Gremium (optionale Kennungen), damit etwa die Suche die
Funktion einer Person nachzieht, ohne die Mitgliedschaft zu lesen; sie ist eine öffentliche Person des
RIS, kein Personenfeld (`x-person`) im Sinne des Nachtrags zu #511. Rückverweise, die eine Quelle nur
außerhalb der Einbettung ausgibt (`person` an der Mitgliedschaft, `body` an der Wahlperiode, die
Rückreferenzen eines Orts), zählen nicht als Änderung. `ris.body.changed` meldet die Kommune, wie ihre
Quelle sie beschreibt; ob sie im Bürgerportal veröffentlicht ist, bleibt `ris.source.published`.
Erzeuger ist der Ingestor; Session meldet diese Typen nicht.

## Nachtrag: Fraktionszuordnung ohne OParl-Fraktion (#916)

Viele Kommunen liefern keine Fraktionsmitgliedschaften. Insight ordnet Personen deshalb selbst eine Fraktion zu
(`insight_core.PersonFraktion`): aus der Einblendung einer Live-Übertragung, von Hand oder aus OParl. Neu ist
`ris.person.faction_assigned` (v1, `oeffentlich` wie `ris.person.changed`, Aggregat `Person`). Die Nutzlast nennt
Kennung der Zuordnung, Person, Quelle (`einblendung`, `hand`, `oparl`), Stand (`bestaetigt`, `abgelehnt`) und
optional das Gremium im RIS sowie den Zeitraum (`valid_from`, `valid_until`, Format `date`). Bezeichnung und Partei
sind Freitext und stehen nicht darin. Gemeldet wird jede Änderung an einer bestätigten Zuordnung und die Ablehnung
einer bisher bestätigten; Vorschläge bleiben intern. Beendet eine neue Zuordnung die bisherige, gibt es zwei
Ereignisse (die alte mit `valid_until`, die neue). Erzeuger ist `hub.ris.faction_assignment`, aufgerufen von
`insight_core.services.fraktionen` im selben `transaction.atomic`-Block wie die Änderung, mit den Schaltern des
Ingestors (`INGESTOR_EVENTS_ENABLED`, `sync_config["events_enabled"]`). Im Änderungsfeed erscheint das Ereignis
als Änderung der Person, die Suche aktualisiert sie wie bei `ris.person.changed`.

## Nachtrag: Live-Übertragungen von Sitzungen (#915)

Die Drehscheibe meldet, was sie während einer Live-Übertragung erkennt (`hub.live`,
[Live-Übertragung](20261007-live-uebertragung.md)): `ris.broadcast.started`, `ris.broadcast.agenda_item_started`,
`ris.broadcast.ended` (je v1, `oeffentlich`) und `ris.broadcast.speaker_changed` (v1, `intern`). Eigentümer ist
`hub.live`, Mandant wie beim Ingestor die Quelle der Kommune.

- **Nur Kennungen und Codes**: Übertragung, Sitzung, Gremium, Abschnitt, Tagesordnungspunkt, Wortmeldung, Person,
  die TOP-Nummer in Normalform (Muster `^[0-9]{1,3}(?:[.][0-9]{1,3}){0,3}$`, neu in der Liste der Muster) und
  Codes (Herkunft, Zuordnung, Grund des Endes). Gelesene Texte (Name, Fraktion, Titel) stehen nie in der
  Nutzlast; ein berechtigter Empfänger liest sie über `hub.live.selectors`.
- **Eigener Objekttyp `Broadcast`**: Der OParl-Änderungsfeed nimmt nur die Objekttypen des kanonischen Modells
  (`hub.api.changes`), Suchindex und RIS-Projektor abonnieren `ris.meeting.*` und die übrigen Bestandstypen,
  nicht `ris.broadcast.*`. Eine Übertragung ändert den RIS-Bestand nicht und löst dort nichts aus.
- `ris.broadcast.speaker_changed` ist `intern`: Die Person ist zwar eine öffentliche Person des RIS, die
  Zuordnung aus der Texterkennung kann aber falsch sein. Abonnent wird die Fraktionszuordnung (#916).

## Bezug

- [A2 Ereignistechnik](20260929-ereignistechnik-postgres.md),
  [A6 Befehle](20260929-befehle-synchron.md),
  [A7 Kanonisches Modell](20260929-kanonisches-modell.md),
  [A10 Feed-Format](20260929-aenderungsfeed-format.md)
- JSON Schema 2020-12: https://json-schema.org/draft/2020-12
- GitLab EventStore (Schemas, additive Versionierung): https://docs.gitlab.com/development/eventstore/
- M. Verraes, Forgettable Payloads:
  https://verraes.net/2019/05/eventsourcing-patterns-forgettable-payloads/
