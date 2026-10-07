# Fraktionen ohne OParl-Fraktion

Issue #916. Viele Ratsinformationssysteme liefern über OParl keine Fraktionsmitgliedschaften; die Fraktionen
erscheinen dort nicht als Gremium (Beispiel: eine Kommune mit 160 Gremien, keines als Fraktion gekennzeichnet).
Das Personenverzeichnis konnte dann keine Fraktion zeigen. Insight pflegt die Zuordnung deshalb selbst.

## Datenmodell

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

## Regeln der automatischen Übernahme

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

## Anzeige

Personenverzeichnis und Personenseite zeigen die bestätigte Zuordnung nur, wenn OParl keine Fraktion nennt
(`services/personen_liste.py`, `angaben_fuer`; eine zusätzliche Abfrage je Seite). Die Quelle bleibt erkennbar: in
der Liste gepunktet unterstrichen mit Tooltip („laut Einblendung der Live-Übertragung“ bzw. „redaktionell
gepflegt“) und Text für Bildschirmleser, auf der Personenseite als kleiner Zusatz hinter der Fraktion.
Vorschläge erscheinen nirgends öffentlich.

## Pflege im Admin

Admin → Insight → Fraktionszuordnungen (Filter Körperschaft, Quelle, Status; Suche nach Person, Fraktion, Partei).

- **Anlegen:** immer `quelle=hand`, `status=bestaetigt`; ohne Körperschaft gilt die der Person.
- **Bestätigen** (Aktion): Vorschlag wird bestätigt; eine bisherige laufende Zuordnung derselben Person endet am
  Vortag des Beginns.
- **Ablehnen** (Aktion): auch für bestätigte Zuordnungen, etwa nach falscher Personenzuordnung.
- Wer Bezeichnung, Partei oder Gremium ändert, macht die Zuordnung zur Handpflege (`quelle=hand`).

Personen und Körperschaften mit Fraktionszuordnungen werden beim Aufräumen (`purge_deleted`) und beim Entfernen einer
Kommune nicht gelöscht; die Verweisprüfung (`services/external_references.py`) führt sie wie Arbeitsdaten.

## Datendrehscheibe

Jede Änderung an einer bestätigten Zuordnung und die Ablehnung einer bisher bestätigten meldet
`ris.person.faction_assigned` v1 (`oeffentlich`, Aggregat `Person`, Erzeuger `hub/ris/faction_assignment.py`) im
selben `transaction.atomic`-Block. Nutzlast nur mit Kennungen, Codes und Daten (siehe
[Ereigniskatalog](EREIGNISKATALOG.md)); Bezeichnung und Partei stehen nie darin. Schalter wie beim Ingestor:
`INGESTOR_EVENTS_ENABLED`, eine Quelle mit `sync_config["events_enabled"] = false` bleibt ausgenommen.
