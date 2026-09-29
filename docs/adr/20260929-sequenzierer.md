# Folgenummer nach dem Commit: Sequenzierer mit `pg_snapshot_xmin`

- Status: angenommen
- Datum: 2026-09-29
- Issue: #476
- Paket: Datendrehscheibe, A3
- Hängt ab von: [A2 Ereignistechnik](20260929-ereignistechnik-postgres.md)

## Kontext

Abonnenten und der öffentliche Änderungsfeed lesen „alles nach dem Cursor“. Das ist nur korrekt,
wenn eine bereits gelesene Position nie nachträglich aufgefüllt wird. Eine Datenbanksequenz, die
beim Einfügen eine Nummer vergibt, erfüllt das nicht: Nummern werden in der Transaktion vergeben,
sichtbar werden sie in Commit-Reihenfolge.

Beispiel: Transaktion T1 erhält Nummer 41, T2 erhält 42. T2 wird zuerst festgeschrieben. Ein Leser
sieht 42 und setzt seinen Cursor auf 42. Wird danach T1 festgeschrieben, liegt 41 hinter dem
Cursor und wird nie gelesen. Der Fehler entsteht nur unter Nebenläufigkeit, bleibt still und ist
in Tests mit SQLite nicht sichtbar. Der niederländische Vorschlag „Event polling 0.0.2“ zu ODS Open
Raadsinformatie verlangt ausdrücklich, dass nie vor einer bereits gelesenen Nummer eingefügt wird.

## Entscheidung

- Jede Journalzeile trägt `xid xid8 NOT NULL DEFAULT pg_current_xact_id()`, die Kennung der
  schreibenden Transaktion (bei Sicherungspunkten die der Haupttransaktion). `seq` bleibt beim
  Schreiben leer. Kein Schreiber vergibt `seq`, auch der Ingestor nicht.
- **Ein** Sequenzierer (Leader-Lease `sequencer`, Ablauf 30 s, Erneuerung alle 10 s) vergibt `seq`
  aus einer eigenen Sequenz, und zwar nur an Zeilen, deren Transaktion älter ist als die älteste
  noch laufende Transaktion:

  ```sql
  WITH frei AS (
    SELECT id FROM events_event
    WHERE seq IS NULL
      AND xid < pg_snapshot_xmin(pg_current_snapshot())
    ORDER BY xid, id
    LIMIT 1000
    FOR UPDATE SKIP LOCKED
  )
  UPDATE events_event e SET seq = nextval('events_seq')
  FROM frei WHERE e.id = frei.id;
  ```

  Alle Transaktionen mit kleinerer Kennung als `xmin` sind beendet; ihre Zeilen sind entweder
  festgeschrieben und sichtbar oder verworfen. Danach weckt `NOTIFY` die Zustellung.
- Leser lesen ausschließlich `seq > cursor`. Weil nur ein Prozess vergibt und jeder Lauf atomar
  festgeschrieben wird, werden die Nummern in aufsteigender Reihenfolge sichtbar. Lücken in der
  Nummernfolge selbst sind unschädlich; ausgeschlossen ist nur, dass eine kleinere Nummer nach
  einer größeren sichtbar wird.
- `seq` ist eine gewöhnliche Spalte, übersteht Sicherung und Wiederherstellung und ist die einzige
  Grundlage für Cursor, intern wie im öffentlichen Feed. Transaktionskennungen verlassen den
  Sequenzierer nie.
- **Ordnungsgarantie:** Ereignisse einer Transaktion bleiben in Schreibreihenfolge. Erhält eine
  Transaktion ihre Kennung erst nach dem Commit einer anderen, stehen ihre Ereignisse dahinter.
  Überlappen sich zwei Transaktionen, die dasselbe Objekt ändern, ist die Commit-Reihenfolge nicht
  in jedem Fall garantiert. Handler behandeln Ereignisse deshalb als Benachrichtigung und lesen
  den aktuellen Stand beim Eigentümer, statt Werte aus der Nutzlast fortzuschreiben (siehe
  minimale Nutzlast in [A2](20260929-ereignistechnik-postgres.md)).
- Schreibtransaktionen bleiben kurz, der Ingestor schreibt in Batches. Die Metrik
  `mandari_events_sequencer_blocked_seconds` misst das Alter der ältesten blockierenden
  Transaktion; ab fünf Minuten wird alarmiert.
- `xid8`, `pg_current_xact_id()` und `pg_snapshot_xmin()` gibt es ab PostgreSQL 13; mandari setzt
  PostgreSQL 16 voraus. `xid8` ist 64 Bit breit und läuft nicht über.

## Alternativen

- **Sequenz in der schreibenden Transaktion (`bigserial`).** Überspringt Ereignisse wie oben
  beschrieben. Verworfen.
- **Globale Sperre um `publish()`** (Tabellen- oder Zählersperre bis zum Commit). Lückenlos und in
  Commit-Reihenfolge, serialisiert aber alle schreibenden Transaktionen einschließlich des
  Ingestors; Durchsatz sinkt, Verklemmungen werden wahrscheinlicher. Verworfen.
- **Leser lesen selbst nur bis `xmin`, Cursor aus `(xid, id)`.** Funktioniert, aber jeder Leser
  bräuchte die Logik, und Transaktionskennungen sind nach Wiederherstellung oder Umzug in einen
  neuen Cluster nicht mehr gültig; als öffentlicher Cursor ungeeignet. Verworfen; der Mechanismus
  lebt nur im Sequenzierer.
- **Commit-Zeitstempel (`track_commit_timestamp`).** Erfordert einen Serverparameter in jeder
  Installation und garantiert keine Sichtbarkeitsreihenfolge. Verworfen.
- **Logische Dekodierung** (Commit-Reihenfolge aus dem WAL). Exakte Reihenfolge, aber
  `wal_level=logical`, Replikationsslot und das Risiko eines hängenden Slots. Verworfen; bleibt
  Option, falls strikte Commit-Reihenfolge je Objekt später nötig wird.
- **Zeitstempel-Cursor (`modified_since`).** Nicht monotone Uhren und rückdatierte Änderungen
  verlieren Daten. Verworfen.

## Folgen

**Positiv**

- Kein Leser überspringt je ein Ereignis; Leser bleiben einfach (`seq > cursor`).
- Ein dichter, stabiler Cursor für interne Abonnements und den öffentlichen Feed.
- Schreibende Transaktionen werden nicht serialisiert.

**Negativ**

- Ein Einzelprozess mit Lease; fällt er aus, übernimmt ein anderer Worker nach spätestens 30 s.
- Lange Schreibtransaktionen halten alle Abonnenten auf. Das gilt clusterweit: Auch lange
  Transaktionen anderer Datenbanken im selben PostgreSQL-Cluster und vorbereitete Transaktionen
  zählen mit.
- Zusätzliche Latenz um einen Sequenzierlauf.
- Strikte Commit-Reihenfolge je Objekt gilt nicht in jedem Überlappungsfall; Handler dürfen sich
  darauf nicht verlassen.

## Prüfung (Fitnessfunktion)

- Nebenläufigkeitstest in der CI gegen echtes PostgreSQL: 20 parallele Transaktionen mit
  zufälliger Commit-Reihenfolge und zusätzlich offenen Langläufern; ein Leser mit Cursor sieht
  jedes festgeschriebene Ereignis genau einmal, verworfene nie.
- Test der Lease-Übernahme: Sequenzierer beenden, zweiter Worker übernimmt in höchstens 30 s,
  keine Nummer doppelt.
- Test: Eine Transaktion, die nach dem Commit einer anderen beginnt, erhält höhere Nummern.
- Schema-Vertrag: Der Ingestor schreibt `seq` nie.
- Monitoring: `mandari_events_sequencer_blocked_seconds` mit Alarm ab fünf Minuten.

## Bezug

- [A2 Ereignistechnik](20260929-ereignistechnik-postgres.md),
  [A10 Feed-Format](20260929-aenderungsfeed-format.md)
- PostgreSQL, Funktionen zu Transaktions-IDs und Snapshots:
  https://www.postgresql.org/docs/current/functions-info.html
- PostgreSQL `SELECT … FOR UPDATE SKIP LOCKED`: https://www.postgresql.org/docs/current/sql-select.html
- ODS Open Raadsinformatie, Event polling 0.0.2:
  https://github.com/VNG-Realisatie/ODS-Open-Raadsinformatie/blob/master/docs/event%20polling/versie%200.0.2/readme.md
