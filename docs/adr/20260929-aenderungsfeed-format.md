# Änderungsfeed: Cursor, upsert/delete/redact, Snapshot-Übergabe, 410

- Status: angenommen
- Datum: 2026-09-29
- Issue: #476
- Paket: Datendrehscheibe, A10
- Hängt ab von: [A3 Sequenzierer](20260929-sequenzierer.md), [A5 Verträge](20260929-ereignisvertraege.md),
  [A7 Kanonisches Modell](20260929-kanonisches-modell.md)

## Kontext

Abnehmer wie die App, die Open-Data-Plattform, Aggregatoren, Fremd-Clients und andere
mandari-Installationen müssen Änderungen verlässlich nachziehen. OParl 1.1 bietet dafür nur
Abfragen nach Änderungszeit. Solche Abfragen verlieren Daten: Uhren von Quellsystemen sind nicht
monoton, nachträglich an alte Sitzungen gehängte Dokumente fallen durch, Blättern per Offset
überspringt Einträge. Löschungen sind nur als `deleted`-Objekte erkennbar, das Entfernen
personenbezogener Inhalte gar nicht.

Internationale Vorbilder lösen das mit einem Log statt einer Tabellenabfrage: ODS Open
Raadsinformatie („Event polling 0.0.2“) und OpenBesluitvorming in den Niederlanden, die
Delta-Dateien von Lokaal Beslist in Flandern.

## Entscheidung

- **Endpunkt je Kommune:** `GET …/body/{id}/changes?after=<cursor>&limit=<n>`. Aggregator und
  Session-Mandanten liefern dasselbe Format aus einer Serialisierung.
- **Cursor:** ein opakes Token über `seq` ([A3](20260929-sequenzierer.md)). Abnehmer speichern es
  unverändert und interpretieren es nicht. Es ist monoton; es gibt keine Zeitstempel-Cursor.
- **Eintrag:** `{cursor, operation, type, id, modified, reason?}`, ohne Inhalte. Inhalte holt der
  Abnehmer über den OParl-Endpunkt des Objekts.
  - `upsert`: Objekt neu oder geändert.
  - `delete`: Objekt entfernt oder nicht mehr öffentlich; `reason` ist Pflicht und codiert
    (`quelle_geloescht`, `zurueckgenommen`, `nichtoeffentlich`).
  - `redact`: Inhalte sind aus der Historie zu entfernen (`reason` `datenschutz`); Abnehmer löschen
    ihre Kopien und geben die Operation weiter.
- **Seiten:** höchstens `limit` Einträge in aufsteigender Reihenfolge, dazu der Cursor für die
  nächste Abfrage. Eine leere Seite bedeutet: Der Abnehmer ist aktuell.
- **Aufbewahrung:** mindestens 30 Tage. Ein älterer Cursor ergibt `410 Gone` mit einer
  Fehlerbeschreibung nach RFC 9457 und dem Verweis auf den Snapshot.
- **Snapshot:** `GET …/body/{id}/snapshot` liefert NDJSON. Der `snapshot_cursor` wird **vor** dem
  Lesen der Daten festgehalten und mitgeliefert. Danach liest der Abnehmer
  `changes?after=snapshot_cursor`. Objekte, die sich während des Snapshots ändern, erscheinen
  danach erneut als `upsert`; Abnehmer verarbeiten das idempotent.
- **Sichtbarkeit:** Der öffentliche Feed enthält nur Objekte und Ereignisse der Sichtbarkeit
  `oeffentlich`. Wird ein Objekt nichtöffentlich, erscheint es als `delete` mit Grund
  `nichtoeffentlich`. Feeds für nichtöffentliche Inhalte setzen ein eigenes Zugriffsmodell voraus
  und sind nicht Gegenstand dieses ADR.
- **Föderation:** Aus Fremd-RIS geerntete Kommunen erscheinen im selben Format; die Aktualität je
  Quelle wird ausgewiesen.
- **HTTP:** `ETag` und `304` für Seiten ohne Änderung, Ratenbegrenzung mit `Retry-After`.
- **Herkunft:** Der Feed ist eine Sicht auf das Journal (Abonnement `feed`,
  [A2](20260929-ereignistechnik-postgres.md)) und aus ihm neu aufbaubar.
- **Einordnung:** Der Feed ist das Profil „Änderungsfeed“ der kompatiblen Erweiterungen
  von OParl 1.1 ([A7](20260929-kanonisches-modell.md)). OParl-1.1-Clients sind
  nicht betroffen; `modified_since` bleibt erhalten.
- **Inhalts-Hash:** Jeder Eintrag kann ab Version 1 optional `content_hash` tragen (SHA-256 über die nach
  RFC 8785 kanonisierte JSON-Darstellung des Objekts). Abnehmer erkennen damit unveränderte Wiederholungen
  und prüfen die Unversehrtheit; fehlt das Feld, gilt der Eintrag als geändert.

## Alternativen

- **Nur `modified_since` wie in OParl 1.1.** Verliert Änderungen und kennt kein Entfernen von
  Inhalten. Bleibt für Kompatibilität, nicht als Synchronisationsgarantie.
- **Offset- oder Seitennummern über eine Änderungsliste.** Einträge verrutschen, Seiten werden
  übersprungen. Verworfen.
- **CRUD-Operationen (create, update, delete).** Die Unterscheidung von create und update nützt
  Abnehmern nicht, `redact` fehlt. Verworfen.
- **Push-Strom (SSE, WebSocket) als Hauptweg.** Ohne Synchronisationsgarantie und mit Last durch
  Verteilung an unbekannt viele Abnehmer. Verworfen; Webhooks sind nur Weckruf
  ([A9](20260929-adapter-rahmen.md)).
- **Inhalte im Feed.** Erzeugt Kopien, erschwert `redact` und macht Einträge groß. Verworfen.
- **Unbegrenzte Aufbewahrung.** Speicherbedarf und Datenschutz. Verworfen zugunsten des Snapshots.
- **Nur Vollabzüge.** Große Downloads ohne zeitnahe Aktualität. Verworfen als alleiniger Weg.

## Folgen

**Positiv**

- Abnehmer ziehen Änderungen ohne Verlust nach; Löschungen und Datenschutzentfernungen lassen sich
  weitergeben.
- Das Journal darf verfallen, weil neue oder zu lange ausgefallene Abnehmer per Snapshot einsteigen.
- Ein Format für eigene und geerntete Kommunen; Grundlage für App, Open-Data-Plattform und Dritte.

**Negativ**

- Abnehmer müssen Inhalte nachladen; das erhöht die Zahl der Anfragen (gemildert durch Caching).
- Snapshots großer Kommunen sind groß.
- Abnehmer brauchen Logik für Cursor, `410` und Snapshot.
- `redact` setzt die Mitwirkung der Abnehmer voraus und lässt sich technisch nicht erzwingen.

## Prüfung (Fitnessfunktion)

- Nebenläufigkeitstest über den Feed: parallele Änderungen, ein Leser über `changes` sieht jede
  festgeschriebene Änderung.
- Test: Ein Cursor älter als die Aufbewahrung ergibt `410` mit Snapshot-Verweis.
- Übergabetest: Änderungen während eines Snapshots erscheinen nach `snapshot_cursor`.
- Sichtbarkeitstest: Kein nichtöffentliches Objekt im öffentlichen Feed; ein Wechsel auf
  nichtöffentlich erscheint als `delete`.
- Offene Konformitätstests für das Profil laufen gegen die eigenen Implementierungen und gegen
  mindestens eine fremde; der OParl-Validator läuft weiter für den Kern.

## Nachtrag zur Umsetzung (#562)

- **Antwort:** `data` (Einträge), `cursor` (Stand für die nächste Anfrage) und `links` mit `self`,
  `next` (fertige Adresse der nächsten Anfrage, immer vorhanden) und `snapshot`. `limit` ist
  höchstens 1000, Vorgabe 100.
- **Cursor:** mit einem Schlüssel der Installation verschlüsselt (AES-SIV) und an die Kommune
  gebunden. Er enthält die Folgenummer und den Tag seiner Ausgabe. Abnehmer sehen weder Folgenummern
  noch Lücken zwischen ihnen.
- **Aufbewahrung als Gültigkeit des Cursors:** „Ein älterer Cursor“ heißt: vor mehr als
  `OPARL_CHANGES_RETENTION_DAYS` Tagen ausgegeben (Vorgabe 90, mindestens 30). Jede Antwort, auch eine
  leere, gibt einen frischen Cursor aus. Damit hängt die Gültigkeit nicht davon ab, wann das Journal
  aufräumt: Was nach der Ausgabe eines gültigen Cursors geschah, ist jünger als die Aufbewahrung und
  noch vorhanden, solange das Journal mindestens so lange aufbewahrt. Ein Sicherheitsnetz antwortet
  mit `410`, wenn Zeilen fehlen, die nach der Ausgabe entstanden sein können.
- **Ohne Cursor** liest ein Abnehmer von vorn, solange der Anfang des Journals vorhanden ist; sonst
  `410` mit dem Verweis auf den Snapshot.
- **Nichtöffentliches ist nicht mittelbar erkennbar:** Der Cursor einer Antwort rückt nur mit
  ausgegebenen Einträgen vor, nie mit übersprungenen Ereignissen. Antwort und `ETag` ändern sich
  deshalb nicht, wenn Nichtöffentliches geschieht; der Ausgabetag ändert sie einmal am Tag.
- **Operation nach Vertrag:** `ris.object.depublished` ergibt immer `delete`, beim Grund
  `datenschutz` `redact` – auch wenn die Hülle eine andere Operation trägt.
- **Objekte mit eigener Adresse:** Einträge nennen Objekte, die die Ausgabe unter einer Adresse
  ausliefert (OParl-1.1-Typen). Ein Ereignis zu einem Typ ohne eigene Adresse (derzeit `Voting`)
  erscheint als `upsert` des Objekts, das ihn ausgibt (des Tagesordnungspunkts aus der Nutzlast).
- **Session-Mandanten:** Die Adresse eines Objekts enthält die Kennung des Session-Objekts und lässt
  sich aus der kanonischen Kennung nicht zurückrechnen. Die Schnittstelle sucht sie unter den
  Objekten, die öffentlich sind oder es waren; was nie öffentlich war, hat keine Adresse und bekommt
  keinen Eintrag.
- **Schalter:** `OPARL_CHANGES_ENABLED` je Installation, Vorgabe aus, bis die Erzeuger laufen.
- **Abweichung:** Der Feed liest das Journal unmittelbar (Index je Kommune) statt über ein eigenes
  Abonnement `feed` mit Tabelle. Die Reihenfolge garantiert der Sequenzierer; eine zweite Tabelle
  bräuchte eigene Folgenummern und ein eigenes Aufräumen.

## Bezug

- [A3 Sequenzierer](20260929-sequenzierer.md), [A7 Kanonisches Modell](20260929-kanonisches-modell.md),
  [A9 Adapter](20260929-adapter-rahmen.md)
- Epic #112 (mandari Data), Epic #113 (App)
- `docs/OPARL_API.md`, `docs/SESSION_OPARL_API.md`, `docs/RELEASE_POLITIK.md`
- ODS Open Raadsinformatie, Event polling 0.0.2:
  https://github.com/VNG-Realisatie/ODS-Open-Raadsinformatie/blob/master/docs/event%20polling/versie%200.0.2/readme.md
- OpenBesluitvorming, API: https://github.com/ontola/openbesluitvorming/blob/HEAD/API.md
- Lokaal Beslist, Delta-Konsument: https://github.com/lblod/delta-consumer
- RFC 9457: https://www.rfc-editor.org/rfc/rfc9457
