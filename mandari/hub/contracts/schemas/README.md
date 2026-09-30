# Schemas der Ereignisse und Befehle

Grundlage: [`docs/adr/20260929-ereignisvertraege.md`](../../../../docs/adr/20260929-ereignisvertraege.md).
Das Register (`hub.contracts.get_registry()`) lädt jede Datei `<typ>/v<n>.json` in diesem Ordner
und prüft sie; ein Verstoß lässt `manage.py check` (Kennung `hub_contracts.E002`) und die Tests
fehlschlagen.

## Aufbau einer Datei

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:mandari:event:ris.paper.released:v1",
  "title": "Vorlage freigegeben",
  "description": "Eine Vorlage wurde freigegeben bzw. veröffentlicht.",
  "x-kind": "event",
  "x-owner": "apps.session",
  "x-visibility": ["oeffentlich", "nichtoeffentlich"],
  "type": "object",
  "additionalProperties": false,
  "required": ["paper", "changed"],
  "properties": {
    "paper": {"type": "string", "format": "uuid"},
    "changed": {"type": "array", "items": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"}}
  },
  "examples": [{"paper": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c", "changed": ["status"]}]
}
```

## Regeln

- **Name** `<bereich>.<objekt>.<ereignis>`, Kleinbuchstaben, Wörter mit einfachem Unterstrich; ist
  das Objekt der Bereich selbst, entfällt es (`submission.status_changed`). Ereignisse in der
  Vergangenheitsform, Befehle im Imperativ (`submission.submit`). Bereiche: `ris`, `submission`,
  `attendance`, `invitation`, `session`, `work`, `portal`, `core`.
- **Version** nur im Dateinamen (`v1.json`, `v2.json`) und in der Hülle, nie im Namen.
- **`$id`** `urn:mandari:<event|command>:<typ>:v<n>`, **`title`** und **`description`** sind Pflicht.
- **`x-kind`** `event` oder `command`; alle Versionen eines Typs haben dieselbe Art.
- **`x-owner`**: Modul, das den Typ veröffentlicht bzw. den Befehl ausführt (z. B. `apps.session`).
  `session.*`, `work.*` und `portal.*` gehören ihrem Fachmodul, `core.*` der Plattform.
- **`x-visibility`**: eine Klasse oder eine Liste aus `oeffentlich`, `nichtoeffentlich`, `intern`,
  `personenbezogen`. Die Hülle jedes Ereignisses muss eine davon tragen. Interne Bereiche sind nie
  `oeffentlich`.
- **Keine Freitextfelder** bei `nichtoeffentlich` oder `personenbezogen`: Zeichenketten nur mit
  `enum`/`const`, Format `uuid`, `date`, `date-time`, `time`, `duration` oder einem Muster ohne
  Leerzeichen; Objekte mit `additionalProperties: false`, Listen mit `items`. Diese Formate prüft das
  Register selbst (`formats.py`), unabhängig von optionalen Paketen von `jsonschema`. Auch Feldnamen
  sind kein Freitext: Ein Objekt mit frei wählbaren Schlüsseln (`additionalProperties` als Schema,
  `patternProperties` mit Leerzeichen im Muster) braucht `propertyNames` mit `enum`/`const`,
  Kennungsformat oder einem Muster ohne Leerzeichen.
- **Meldungen ohne Werte:** Verstöße nennen JSON-Pfad, Regel und Feldnamen. Schlüssel aus der
  Nutzlast erscheinen nur, wenn sie wie ein Feldname aussehen (`^[a-z][a-z0-9_]{0,39}$`), sonst als
  `<Feld>`.
- **`examples`**: mindestens ein gültiges Beispiel.
- **Nur additive Änderungen** an einer bestehenden Version: neue Felder optional. Entfernen,
  Umbenennen, Typwechsel und neue Pflichtfelder ergeben eine neue Version.
- **Nutzlast minimal:** Kennungen und Namen geänderter Felder, nie Inhalte, Mailadressen oder Namen.
