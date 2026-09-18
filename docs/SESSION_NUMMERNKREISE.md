# Nummernkreise für Vorlagen und Drucksachen (Session)

Stand: 09/2026 · Issue #150 (Teil Nummernkreise)

Jede Vorlage bekommt ihre Nummer automatisch aus einem **Nummernkreis** des Mandanten. Die
Nummer ist eindeutig, wird nach der Vergabe nicht mehr verändert und nie ein zweites Mal
vergeben. Einstellungen → **Nummernkreise** legt fest, wie sie aussieht und wann sie entsteht.

## Begriffe

| Begriff | Bedeutung |
|---|---|
| **Vorlagen- bzw. Drucksachennummer** | Öffentliche Nummer einer Vorlage (`reference`, OParl `reference`). Die Bezeichnung ist je Mandant einstellbar: „Drucksache“ (Hamburger Bezirke), „Vorlagen-Nr.“ (NRW-Kommunen). |
| **Eingangsnummer** | Interne Nummer eines Antrags beim Eingang (`A/<Jahr>/0001`), unabhängig von der Drucksachennummer. |
| **Unternummer** | Ergänzung, Neufassung, Änderungsantrag, Antwort oder Beschlussempfehlung zu einer Vorlage, z. B. `22-0593.1` oder `V/0599/2026/1`. |
| **Zählerbereich** | Wann der Zähler neu beginnt: jährlich, je Wahlperiode oder nie. |

## Muster

| Platzhalter | Wert |
|---|---|
| `{lfd}` / `{lfd:4}` | laufende Nummer, mit `:4` auf vier Stellen aufgefüllt |
| `{jahr}` / `{jj}` | Jahr der Vergabe, vier- bzw. zweistellig |
| `{wp}` | Nummer der Wahlperiode (Einstellungen → Wahlperioden, Feld „Nr.“) |
| `{prefix}` | Präfix des Kreises, z. B. `AN` |
| `{gremium}` | Kurzname des federführenden Gremiums (eigener Zähler je Gremium) |

Unternummern: `{parent}` (Nummer der Bezugsvorlage) und `{sub}` bzw. `{sub:2}`.

Das Muster muss den Zählerbereich enthalten – jährlich also `{jahr}` oder `{jj}`, je
Wahlperiode `{wp}`. Sonst entstünden in jedem Jahr dieselben Nummern; die Einstellungsseite
lehnt solche Muster ab.

## Vorlagen für verbreitete Praxis (Presets)

Recherchiert an öffentlichen Ratsinformationssystemen (Stand 09/2026):

| Preset | Beispiel | Zählerbereich |
|---|---|---|
| Bezirksversammlung (Hamburg) | `22-0593`, Unternummer `22-0593.1`; ein Zähler je Bezirk über alle Drucksachenarten | Wahlperiode |
| Verwaltung und Politik getrennt (z. B. Köln) | `1344/2026`, `AN/1492/2026`, Fortschreibung `/1` | Jahr |
| Präfix je Vorlagenart (z. B. Münster) | `V/0599/2026`, `A/0012/2026`, `AF/0003/2026` | Jahr |
| VO mit zweistelligem Jahr (z. B. Wuppertal) | `VO/1044/26`, `VO/1044/26/1` | Jahr |
| Fünfstellig mit Jahr (z. B. Dortmund) | `22957-26`, `22957-26-E1` | Jahr |
| Kleine Gemeinde | `126/2026` | Jahr |
| Standard | `V/2026/0001` | Jahr |

Mehrere Bezirke einer Stadt sind je ein eigener Mandant mit eigenem Zähler – so wie in
Hamburg jede Bezirksversammlung ihre Drucksachen unabhängig nummeriert.

## Zeitpunkt der Vergabe

- **Beim Anlegen:** Die Nummer entsteht mit dem ersten Speichern (Anträge der Politik,
  Hamburger Drucksachen).
- **Bei der Freigabe:** Entwürfe bleiben ohne Nummer („Nummer folgt“) und erhalten sie erst mit
  der Freigabe. Verworfene Entwürfe hinterlassen keine Lücken.

Ein Antrag, den die Verwaltung in eine Vorlage umwandelt, gilt als angenommene Drucksache und
bekommt sofort seine Nummer aus dem Kreis seiner Art (z. B. `AN/…`).

## Umstieg aus einem Altsystem

1. Preset wählen und bei Hamburger Mustern die Wahlperiode mit Nummer pflegen.
2. Beim Kreis „Nächste laufende Nummer setzen“: zuletzt im Altsystem vergebene Nummer + 1
   (z. B. `2615`, wenn dort bis `22-2614` vergeben ist). Der Zähler lässt sich nur erhöhen.
3. Altbestände mit ihrer Originalnummer übernehmen: Nutzer mit Einstellungsrecht dürfen beim
   Anlegen eine Nummer von Hand setzen. Kollidiert eine automatisch gebildete Nummer mit dem
   Altbestand, wird sie übersprungen.

## Garantien und technische Umsetzung

- Eindeutig je Mandant (Datenbank-Constraint, auch für Unternummern je Bezugsvorlage).
- Atomar: Zeilensperre auf dem Zählerstand (`SessionNumberCounter`), Vergabe in derselben
  Transaktion wie das Speichern der Vorlage – scheitert das Speichern, verfällt keine Nummer.
- Jahreswechsel nach deutscher Ortszeit, nicht UTC.
- Ein Kreis, aus dem schon Nummern vergeben wurden, behält Muster und Zählerbereich; für ein
  neues Schema wird ein neuer Kreis angelegt und der alte deaktiviert (nie gelöscht).
- Code: `apps/session/services/numbering_service.py`, Tests: `apps/session/tests/test_numbering.py`.
