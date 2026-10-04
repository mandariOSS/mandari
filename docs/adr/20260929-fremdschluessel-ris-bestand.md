# Fremdschlüssel auf den RIS-Bestand: nie CASCADE

- Status: angenommen
- Datum: 2026-09-29
- Issue: #476
- Paket: Datendrehscheibe, A8
- Hängt ab von: [A7 Kanonisches Modell](20260929-kanonisches-modell.md)

## Kontext

Work und die Mandantenkonfiguration zeigen per Fremdschlüssel auf Objekte des RIS-Bestands:
Notizen, Positionen, Anmerkungen und Kommentare an Tagesordnungspunkten und Vorlagen; Kommune und
Gremien einer Organisation. Ein Teil dieser Felder ist mit `on_delete=CASCADE` angelegt.

RIS-Daten sind fremdbestimmt. Eine Quelle kann Objekte jederzeit löschen, zurücknehmen oder neu
veröffentlichen; eine Kommune kann aus dem Portal entfernt werden; ein Aufräumlauf entfernt
gelöschte Objekte. Mit `CASCADE` würde jeder dieser Vorgänge die daran hängenden Inhalte der
Fraktionen mitlöschen, ohne dass deren Eigentümer davon erfährt. Diese Inhalte gehören Work und
sind oft nicht wiederherstellbar.

## Entscheidung

- Fachmodule und Mandantendaten dürfen auf den RIS-Bestand zeigen; die Abhängigkeit geht nach
  unten ([A1](20260929-schichtenmodell.md)).
- `on_delete` auf ein Modell des RIS-Bestands ist **`PROTECT` oder `SET_NULL`, nie `CASCADE`**.
  - `PROTECT` für Inhalte, die an genau einem RIS-Objekt hängen (Notiz am Tagesordnungspunkt,
    Position zur Vorlage) und für Konfiguration (Kommune einer Organisation).
  - `SET_NULL` nur für optionale Verknüpfungen, deren Inhalt ohne das RIS-Objekt sinnvoll bleibt
    (z. B. Aufgabe mit Bezug auf eine Sitzung).
- RIS-Objekte werden nicht hart gelöscht, sondern markiert (`deleted`, `depublished` mit Grund,
  siehe [A7](20260929-kanonisches-modell.md)). Oberflächen zeigen „zurückgezogen“ bzw. „in der
  Quelle gelöscht“; die Inhalte bleiben sichtbar.
- Hartes Löschen im RIS-Bestand (Aufräumen nach Frist, Entfernen einer Kommune) prüft vorher alle
  Verweise, bricht mit einer Liste der Betroffenen ab und bietet einen Probelauf.
- Fremdschlüssel zwischen Fachmodulen sind unzulässig; sie verbinden sich über kanonische
  Kennungen, Ereignisse und Befehle.
- Wo ein Modul den RIS-Bestand künftig nicht mehr in derselben Datenbank findet (getrennte
  Installation), ersetzen kanonische Kennungen den Fremdschlüssel. Die Regel gilt dann sinngemäß:
  Der Verweis bleibt, die Oberfläche zeigt „zurückgezogen“.
- Umstellung der bestehenden Felder per Migration. Der Wechsel auf `PROTECT` ändert nur das
  Verhalten in Django, nicht das Datenbankschema; `SET_NULL` macht eine Spalte nullbar. Beides ist
  mit einem Rückfall per Image ohne Migrationsrückbau verträglich.

## Alternativen

- **`CASCADE` behalten und Löschungen organisatorisch vermeiden.** Ein Fehlgriff im Admin oder ein
  Aufräumlauf genügt für Datenverlust. Verworfen.
- **`DO_NOTHING`.** Hinterlässt Verweise ins Leere oder scheitert erst an der Datenbank.
  Verworfen.
- **Kopie der RIS-Daten in Work.** Nutzerinhalte wären unabhängig, aber die Daten lägen doppelt
  und veralteten. Verworfen; eine RIS-Sicht für Work als Projektion bleibt eine spätere Option.
- **Sofort ohne Fremdschlüssel, nur mit kanonischen Kennungen.** Sauber für getrennte Datenbanken,
  kostet aber heute referenzielle Integrität und Joins und ist ein großer Umbau. Verworfen für
  jetzt, Zielbild für getrennte Installationen.

## Folgen

**Positiv**

- Kein stiller Datenverlust in Work durch Vorgänge im RIS-Bestand.
- Löschungen und Rücknahmen werden sichtbar statt still ausgeführt.
- Voraussetzung dafür, Rücknahmen als Ereignisse zu behandeln und im Feed weiterzugeben.

**Negativ**

- Der RIS-Bestand behält markierte Objekte länger; Aufräumen braucht eine Verweisprüfung.
- Oberflächen müssen zurückgezogene Objekte darstellen können.
- `PROTECT` blockiert bewusst Löschungen im Admin; die Meldung muss verständlich sagen, was noch
  daran hängt.

## Prüfung (Fitnessfunktion)

- CI-Test über alle Modelle: Jede Relation auf ein Modell des RIS-Bestands hat `PROTECT` oder
  `SET_NULL`. Bestehende Ausnahmen stehen in einer Liste, die nur kürzer werden darf; Ziel null.
- Test: Löschen einer Kommune und Aufräumen gelöschter Objekte brechen bei Verweisen aus Work mit
  einer Liste ab; der Probelauf zeigt die Betroffenen.
- Test: Ein zurückgezogenes Objekt erscheint in Work als „zurückgezogen“, die Notiz bleibt
  erhalten.
- Nächtliche Konsistenzprüfung zählt Verweise auf markierte Objekte (Metrik), ohne zu löschen.

## Nachtrag zur Umsetzung (#524)

- **Grund der Markierung:** Jedes Objekt des Bestands trägt neben `deleted` den Grund `deletion_reason`
  (`quelle_geloescht`, `zurueckgenommen`, `nichtoeffentlich`, `datenschutz`, wie in `ris.object.depublished`).
  `depublished` ist kein eigenes Feld, sondern ein Grund außer `quelle_geloescht`. Ingestor und Spiegel markieren mit
  `quelle_geloescht`, die Rücknahme aus Session (`hub.ris.retraction`) mit ihrem Grund, das Beenden der
  Veröffentlichung einer Kommune mit `zurueckgenommen`. Liefert die Quelle das Objekt wieder, werden Markierung und
  Grund aufgehoben. Markierungen von vor diesem Stand haben keinen Grund; Session-Objekte gelten dann als
  zurückgezogen.
- **Verweise aus Work:** Notizen, Positionen, Redebeiträge, Ergänzungsdokumente, Sitzungsvorbereitungen, Kommentare,
  Datei-Anmerkungen, Aussetzungsregeln und regional geteilte Anträge zeigen mit `PROTECT` auf den Bestand, der
  optionale Sitzungsbezug eines Redebeitrags mit `SET_NULL`. Die Verweise aus Session und `tenants` waren schon
  `SET_NULL`.
- **Fitnessfunktion:** `hub/ris/tests/test_loeschsemantik.py` prüft alle Relationen auf den Bestand. Ausnahmen sind die
  Daten des Bürgerportals und abgeleitete Geodaten in insight_core (Abos, Ratsfragen, Rückmeldungen, Straßen,
  Adressen, Verortungen); über sie steht eine Entscheidung aus (Issue #524). Die Liste darf nur kürzer werden.
- **Work** zeigt markierte Sitzungen und Tagesordnungspunkte mit dem Hinweis „Zurückgezogen“ bzw. „In der Quelle
  gelöscht“; Vorbereitung, Notizen und Positionen bleiben.
- **Dokumente:** Der Löschabgleich (#787) entfernt Kopie und Text einer in der Quelle gelöschten Datei nach 30 Tagen
  Sperre; der Datensatz bleibt als markiertes Objekt. Verweise aus Work (Anmerkungen) bleiben dadurch gültig.
- **Hartes Aufräumen** bleibt vorerst manuell (`purge_deleted`, mit Probelauf und Verweisprüfung); ob ein Auftrag nach
  einer Frist aufräumt, ist offen (Issue #524).

## Bezug

- [A1 Schichtenmodell](20260929-schichtenmodell.md), [A7 Kanonisches Modell](20260929-kanonisches-modell.md),
  [A10 Feed-Format](20260929-aenderungsfeed-format.md)
- `docs/DSGVO_LOESCHKONZEPT.md`
- Django, `ForeignKey.on_delete`:
  https://docs.djangoproject.com/en/stable/ref/models/fields/#django.db.models.ForeignKey.on_delete
