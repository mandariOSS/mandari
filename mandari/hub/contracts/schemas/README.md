# Schemas der Ereignisse und Befehle

Grundlage: [`docs/adr/20260929-ereignisvertraege.md`](../../../../docs/adr/20260929-ereignisvertraege.md)
(mit Nachtrag zur Umsetzung) und [`docs/adr/20260929-befehle-synchron.md`](../../../../docs/adr/20260929-befehle-synchron.md).
Das Register (`hub.contracts.get_registry()`) lädt jede Datei `<typ>/v<n>.json` in diesem Ordner
und prüft sie; ein Verstoß lässt `manage.py check` (Kennung `hub_contracts.E002`) und die Tests
fehlschlagen.

Ausgeliefert ist der Startumfang: 27 Ereignistypen und die Befehle `submission.submit`,
`submission.withdraw`, `attendance.respond` und `invitation.acknowledge`. Art, Eigentümer und
Sichtbarkeit je Typ, die Inhaltsfelder mit jedem Unterfeld und seiner Längengrenze sowie die
verwendeten Muster hält `hub/contracts/tests/test_schemas.py` fest.

## Aufbau einer Datei

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:mandari:event:ris.paper.changed:v1",
  "title": "Vorlage geändert",
  "description": "Eine Vorlage wurde geändert: Inhalt, Fassung oder Kennzeichen. Aggregat: Paper. …",
  "x-kind": "event",
  "x-owner": "hub.ris",
  "x-visibility": ["oeffentlich", "nichtoeffentlich"],
  "type": "object",
  "additionalProperties": false,
  "required": ["paper", "changed"],
  "properties": {
    "paper": {"description": "Kanonische Kennung der Vorlage.", "type": "string", "format": "uuid"},
    "changed": {
      "description": "Geänderte Felder im kanonischen Modell (Namen wie in OParl, z. B. name, paperType).",
      "type": "array",
      "items": {"type": "string", "pattern": "^[a-z][A-Za-z0-9_]{0,63}$"},
      "uniqueItems": true,
      "minItems": 1,
      "maxItems": 64
    }
  },
  "examples": [{"paper": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c", "changed": ["name", "paperType"]}]
}
```

## Regeln (vom Register geprüft)

- **Name** `<bereich>.<objekt>.<ereignis>`, Kleinbuchstaben, Wörter mit einfachem Unterstrich; ist
  das Objekt der Bereich selbst, entfällt es (`submission.status_changed`). Ereignisse in der
  Vergangenheitsform, Befehle im Imperativ (`submission.submit`). Bereiche: `ris`, `submission`,
  `attendance`, `invitation`, `session`, `work`, `portal`, `core`.
- **Version** nur im Dateinamen (`v1.json`, `v2.json`) und in der Hülle, nie im Namen.
- **`$id`** `urn:mandari:<event|command>:<typ>:v<n>`, **`title`** und **`description`** sind Pflicht.
- **`x-kind`** `event` oder `command`; alle Versionen eines Typs haben dieselbe Art.
- **`x-owner`**: Modul, das den Typ veröffentlicht bzw. den Befehl ausführt. `ris.*` gehört der
  Drehscheibe (`hub.ris`), weil Session und Ingestor dieselben Typen erzeugen; `session.*`, `work.*`
  und `portal.*` gehören ihrem Fachmodul, `core.*` der Plattform.
- **`x-visibility`**: eine Klasse oder eine Liste aus `oeffentlich`, `nichtoeffentlich`, `intern`,
  `personenbezogen`. Die Hülle jedes Ereignisses muss eine davon tragen. Interne Bereiche sind nie
  `oeffentlich`.
- **Keine Freitextfelder** bei `nichtoeffentlich` oder `personenbezogen`: Zeichenketten nur mit
  `enum`/`const`, Format `uuid`, `date`, `date-time`, `time`, `duration` oder einem Kennungsmuster
  (siehe unten). Objekte mit `additionalProperties: false`, Listen mit `items`. Die Formate prüft
  das Register selbst (`formats.py`), unabhängig von optionalen Paketen von `jsonschema`. Auch
  Feldnamen sind kein Freitext: Ein Objekt mit frei wählbaren Schlüsseln (`additionalProperties` als
  Schema, `patternProperties` mit einem Muster, das kein Kennungsmuster ist) braucht `propertyNames`
  mit `enum`/`const`, Kennungsformat oder Kennungsmuster.
- **Kennungsmuster** (`patterns.py`): vorn und hinten mit `^` und `$` verankert, ohne Leerraum und
  mit Längengrenze von höchstens 255 Zeichen, entweder durch Quantoren mit Obergrenze
  (`^[a-z][a-z0-9_]{0,31}$`) oder durch `maxLength` am selben Feld. `^[A-Z]{2}`, `^[a-z ]+$` und
  `^[a-z_]+$` genügen nicht. `\A`, `\Z` und Schalter wie `(?i)` oder `(?m)` kennt nur Python, nicht
  ECMA-262; solche Muster gelten als Freitext. **Grenze der Regel:** Sie schließt Sätze aus, nicht
  jedes einzelne Wort; `^\S{1,64}$` ließe einen Namen ohne Leerzeichen oder eine Mailadresse zu.
  Deshalb stehen die verwendeten Muster als feste Liste im Test, ein neues Muster ist eine bewusste
  Entscheidung.
- **Inhaltsfelder nur in Befehlen:** Felder, deren Wert ein Inhalt ist (Antragstext, Grund einer
  Absage), tragen `"x-content": true`. Vom Freitextverbot ausgenommen ist nur die Zeichenkette
  selbst: Im Teilbaum eines Inhaltsfelds braucht jede freie Zeichenkette `maxLength` und jede Liste
  `maxItems`; Knoten ohne Typ, `true`, Objekte ohne `additionalProperties: false`, Listen ohne bzw.
  mit leerem `items` und Verweise (`$ref`) sind dort Verstöße. Ein Inhaltsfeld nimmt also nie
  beliebiges JSON an. Ereignisse haben nie Inhaltsfelder, auch öffentliche nicht.
- **`examples`**: mindestens ein gültiges Beispiel.
- **Meldungen ohne Werte:** Verstöße nennen JSON-Pfad, Regel und Feldnamen. Schlüssel aus der
  Nutzlast erscheinen nur, wenn sie wie ein Feldname aussehen (`^[a-z][a-z0-9_]{0,39}$`), sonst als
  `<Feld>`.

## Vereinbarungen (von den Tests geprüft)

- **Nutzlast minimal:** Ereignisse tragen Kennungen, Codes und Namen geänderter Felder, nie Inhalte,
  Namen, Mailadressen, Beträge oder Bankdaten. Die Kennung des Objekts steht zusätzlich zur Hülle in
  der Nutzlast (`paper`, `meeting` …), Bezüge daneben (`paper` einer Beratung, `meeting` eines TOP).
  Ein Ereignis, das `oeffentlich` sein darf, nennt keine Kennung eines nichtöffentlichen Objekts:
  Die Einreichung hinter einer Vorlage steht nur in `ris.paper.created`, nicht in
  `ris.paper.released`.
- **Feldnamen der Nutzlast** in `snake_case` (`agenda_item`, `previous_status`) und mit
  `description`. Namen geänderter Felder (`changed`) folgen dem Modell des Eigentümers: bei `ris.*`
  den OParl-Namen (`paperType`), sonst den Feldnamen des Fachmoduls. Erweiterungen mit Namensraum
  erscheinen mit Unterstrich statt Doppelpunkt (`mandari:meetingFormat` als `mandari_meetingFormat`),
  weil das Muster nur Buchstaben, Ziffern und Unterstrich zulässt; so meldet sie der Ingestor.
- **Codes** übernehmen die Werte des Eigentümers (`confirmed`/`declined`, `motion`, `in_review` …);
  die Gründe einer Rücknahme stehen wie im Änderungsfeed (`quelle_geloescht`, `zurueckgenommen`,
  `nichtoeffentlich`, `datenschutz`).
- **Nur additive Änderungen** an einer bestehenden Version: neue Felder optional, neue Codes in einer
  Codeliste. Entfernen, Umbenennen, Typwechsel, neue Pflichtfelder und jede Lockerung (Pflichtfeld wird
  optional, Format oder Muster entfällt, Grenze steigt) ergeben eine neue Version `v<n+1>.json` neben der
  alten; eine Version wird nie gelöscht.

## Prüfung in der CI und Ereigniskatalog

`scripts/check_event_contracts.py` (Job „Qualität“, Issue #519) prüft, dass jeder Ereignistyp im Code ein
Schema hat, dass bestehende Versionen sich gegenüber der Zielverzweigung nur additiv ändern
(`hub/contracts/compatibility.py`), und dass der Katalog
[`docs/EREIGNISKATALOG.md`](../../../../docs/EREIGNISKATALOG.md) aktuell ist. Nach jeder Änderung an einem
Schema den Katalog neu erzeugen und mit committen:

```bash
python scripts/check_event_contracts.py --write-catalog
python scripts/check_event_contracts.py --base origin/dev
```
