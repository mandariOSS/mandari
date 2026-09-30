# Schemas der Ereignisse und Befehle

Grundlage: [`docs/adr/20260929-ereignisvertraege.md`](../../../../docs/adr/20260929-ereignisvertraege.md)
(mit Nachtrag zur Umsetzung) und [`docs/adr/20260929-befehle-synchron.md`](../../../../docs/adr/20260929-befehle-synchron.md).
Das Register (`hub.contracts.get_registry()`) lädt jede Datei `<typ>/v<n>.json` in diesem Ordner
und prüft sie; ein Verstoß lässt `manage.py check` (Kennung `hub_contracts.E002`) und die Tests
fehlschlagen.

Ausgeliefert ist der Startumfang: 27 Ereignistypen und die Befehle `submission.submit`,
`submission.withdraw`, `attendance.respond` und `invitation.acknowledge`. Art, Eigentümer,
Sichtbarkeit und Inhaltsfelder je Typ hält `hub/contracts/tests/test_schemas.py` fest.

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
  `enum`/`const`, Format `uuid`, `date`, `date-time`, `time`, `duration` oder einem Muster, das vorn
  und hinten verankert ist (`^…$`) und keinen Leerraum zulässt (`patterns.py`; `^[A-Z]{2}` oder
  `^[a-z ]+$` genügen nicht). Objekte mit `additionalProperties: false`, Listen mit `items`. Die
  Formate prüft das Register selbst (`formats.py`), unabhängig von optionalen Paketen von
  `jsonschema`. Auch Feldnamen sind kein Freitext: Ein Objekt mit frei wählbaren Schlüsseln
  (`additionalProperties` als Schema, `patternProperties` mit einem Muster, das Leerraum zulässt)
  braucht `propertyNames` mit `enum`/`const`, Kennungsformat oder einem solchen Muster.
- **Inhaltsfelder nur in Befehlen:** Felder, deren Wert ein Inhalt ist (Antragstext, Grund einer
  Absage), tragen `"x-content": true`. Sie sind vom Freitextverbot ausgenommen; jede Zeichenkette
  darin braucht `maxLength`. Ereignisse haben nie Inhaltsfelder, auch öffentliche nicht.
- **`examples`**: mindestens ein gültiges Beispiel.
- **Meldungen ohne Werte:** Verstöße nennen JSON-Pfad, Regel und Feldnamen. Schlüssel aus der
  Nutzlast erscheinen nur, wenn sie wie ein Feldname aussehen (`^[a-z][a-z0-9_]{0,39}$`), sonst als
  `<Feld>`.

## Vereinbarungen (von den Tests geprüft)

- **Nutzlast minimal:** Ereignisse tragen Kennungen, Codes und Namen geänderter Felder, nie Inhalte,
  Namen, Mailadressen, Beträge oder Bankdaten. Die Kennung des Objekts steht zusätzlich zur Hülle in
  der Nutzlast (`paper`, `meeting` …), Bezüge daneben (`paper` einer Beratung, `meeting` eines TOP).
- **Feldnamen der Nutzlast** in `snake_case` (`agenda_item`, `previous_status`) und mit
  `description`. Namen geänderter Felder (`changed`) folgen dem Modell des Eigentümers: bei `ris.*`
  den OParl-Namen (`paperType`), sonst den Feldnamen des Fachmoduls.
- **Codes** übernehmen die Werte des Eigentümers (`confirmed`/`declined`, `motion`, `in_review` …);
  die Gründe einer Rücknahme stehen wie im Änderungsfeed (`quelle_geloescht`, `zurueckgenommen`,
  `nichtoeffentlich`, `datenschutz`).
- **Nur additive Änderungen** an einer bestehenden Version: neue Felder optional. Entfernen,
  Umbenennen, Typwechsel und neue Pflichtfelder ergeben eine neue Version.
