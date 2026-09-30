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
  Produktivbetrieb nicht.
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

## Bezug

- [A2 Ereignistechnik](20260929-ereignistechnik-postgres.md),
  [A6 Befehle](20260929-befehle-synchron.md),
  [A7 Kanonisches Modell](20260929-kanonisches-modell.md),
  [A10 Feed-Format](20260929-aenderungsfeed-format.md)
- JSON Schema 2020-12: https://json-schema.org/draft/2020-12
- GitLab EventStore (Schemas, additive Versionierung): https://docs.gitlab.com/development/eventstore/
- M. Verraes, Forgettable Payloads:
  https://verraes.net/2019/05/eventsourcing-patterns-forgettable-payloads/
