# Ereignistechnik: Nachweise der Zusagen

Stand: 4. Oktober 2026 · Issue #514 · Entscheidung:
[Ereignistechnik in PostgreSQL](adr/20260929-ereignistechnik-postgres.md),
[Sequenzierer](adr/20260929-sequenzierer.md),
[Aufträge und Zeitpläne](adr/20260929-auftraege-und-zeitplaene.md)

Die Ereignistechnik (`apps/events`) verspricht sechs Dinge. Diese Seite nennt je Zusage den Test, der
sie in jeder CI prüft, die gemessenen Werte und die Grenzen, ab denen eine Zusage nicht mehr gilt. Sie
ist Voraussetzung, bevor ein bestehender Weg auf die Datendrehscheibe umgeschaltet wird (E1-Gate,
Epic #483).

## 1. Übersicht

| Zusage | Nachweis (Tests unter `mandari/apps/events/tests/`) | Ergebnis |
|---|---|---|
| **Verlustfreiheit** | `test_sequencer.py::test_zwanzig_parallele_transaktionen_ohne_luecke` (20 parallele Transaktionen, zufällige Commit-Reihenfolge, Langläufer), `test_sequencer.py::test_uebernahme_nach_ausfall_ohne_doppelte_nummern`, `test_absturz.py` (Worker-Prozess mitten im Batch abgeschossen) | kein Ereignis verloren, keine Nummer doppelt |
| **Doppelzustellung** | `test_absturz.py` (nach dem Absturz), `test_dispatch_nebenlaeufig.py::test_zwei_zusteller_ohne_lease_wirken_genau_einmal`, `test_zusagen.py::test_jedes_abonnement_hat_einen_doppelzustellungstest` | Sichten genau einmal; externe Effekte höchstens der Batch im Flug doppelt; jedes Abonnement hat einen Test, der jedes Ereignis zweimal zustellt |
| **Reihenfolge je Objekt** | `test_zusagen.py::test_verschachtelte_transaktionen_halten_die_reihenfolge_je_objekt`, `test_dispatch_nebenlaeufig.py::test_parallele_schreiber_mit_sequenzierer_und_fehlern_halten_die_reihenfolge_je_objekt`, `test_absturz.py` | Schreibreihenfolge auch mit Sicherungspunkten und über einen Absturz hinweg |
| **Latenz** p95 ≤ 5 s normal, ≤ 60 s bei Rückstau | `test_weckruf.py::test_latenz_vom_commit_bis_zur_sicht` (auch hinter PgBouncer), `test_last.py` | siehe Abschnitt 3 |
| **Durchsatz** ≥ 100 Ereignisse/s je Abonnement (Batch 200) | `test_last.py` (Rückstau abbauen) | siehe Abschnitt 3 |
| **Wiederanlauf** ≤ 60 s ohne Handarbeit | `test_absturz.py` | Zustellung nach 29 s wieder vollständig; neue Aufträge nach 3 s; ein unterbrochener Auftrag nach 45 bis 90 s (Abschnitt 2.2) |

Die Absturz- und Lasttests starten den Worker als **eigenen Prozess** (`tests/prozess.py`) und beenden
ihn hart (SIGKILL, wie ein OOM-Kill): Im selben Prozess ließe sich ein Absturz nicht nachstellen, ein
Faden lässt sich nicht hart beenden und seine Verbindung bliebe offen. Sie messen mit den Fristen des
Betriebs (Lease 30 s, Sperre eines Auftrags 60 s) und dauern deshalb je rund eine Minute.

## 2. Absturz

### 2.1 Mitten im Batch (`test_absturz_mitten_im_batch_ohne_verlust_und_ohne_doppelte_sichteffekte`)

Ablauf: 600 Ereignisse über 40 Objekte, Worker mit Sequenzierer und Zustellung an zwei Abonnements
(Datenbank-Sicht und externer Effekt, Batch 50). Nach drei Batches halten beide Handler an, nachdem sie
ihre Effekte geschrieben haben und bevor der Cursor festgeschrieben ist; dann wird der Prozess
abgeschossen. Während er tot ist, kommen 100 weitere Ereignisse hinzu, und ein neuer Prozess startet
sofort (wie die Neustartregel des Containers).

Geprüft und gemessen (4. Oktober 2026, PostgreSQL 16):

- Unmittelbar nach dem Absturz enthält die Sicht genau die Ereignisse bis zu ihrem Cursor: Der offene
  Batch ist mit der Verbindung zurückgerollt.
- Der externe Effekt des offenen Batches besteht (50 Ereignisse jenseits des Cursors); genau diese
  werden nach dem Neustart noch einmal zugestellt.
- Nach dem Neustart: jedes der 700 Ereignisse genau einmal in der Sicht, mindestens einmal extern,
  doppelt nur der Batch im Flug, nichts geparkt, Reihenfolge je Objekt eingehalten.
- **Wiederanlauf 29,4 s** vom Absturz bis alles zugestellt ist. Er wird von der Lease bestimmt: Der
  abgeschossene Prozess gibt seine Leases (Sequenzierer und je Abonnement) nicht frei, sie laufen nach
  höchstens 30 s ab (`leases.LEASE_TTL`). Obergrenze damit rund 30 s plus ein Abfrageabstand.

### 2.2 Mitten im Auftrag (`test_absturz_mitten_im_auftrag_wird_ohne_handarbeit_wiederholt`)

Ein Auftrag läuft, der Worker wird abgeschossen, ein neuer startet sofort; danach wird ein zweiter
Auftrag eingereiht.

- Der neue Auftrag läuft nach **3,3 s**: Die Warteschlange arbeitet sofort weiter.
- Der unterbrochene Auftrag läuft nach **75,8 s** erneut (zweiter Versuch, danach `erledigt`). Seine
  Sperre (`task_runner.LOCK_TTL`, 60 s, alle 15 s verlängert) muss erst ablaufen, freigegeben wird alle
  30 s (`MAINTENANCE_INTERVAL`). **Grenze: 45 bis 90 s** nach dem Absturz. Das ist gewollt: Eine
  kürzere Sperre würde einen Auftrag, dessen Prozess nur kurz hängt (Speicherbereinigung, langsame
  Datenbank), doppelt ausführen. Aufträge mit `max_attempts = 1` (etwa ein Sync) werden nach einem
  Absturz nicht wiederholt, sondern `tot`; der nächste Lauf holt sie nach.

## 3. Last (`test_last.py`)

Der Worker läuft als eigener Prozess mit Sequenzierer und Zustellung an zwei Abonnements, Batch 200:
eine Datenbank-Sicht (eine Zeile je Ereignis in der Transaktion der Zustellung) und ein externer Effekt
(eigene Verbindung). Geschrieben wird wie vom Ingestor: eine Transaktion je Objekt, mehrere Schreiber
gleichzeitig. Gemessen wird auf der Uhr der Datenbank vom Erfassen im Journal bis zum Effekt.

**Realistisches Volumen.** Seit dem 4. Oktober 2026 schreibt der Ingestor in Produktion ins Journal.
Ein Vollabgleich ohne Änderung schreibt nichts (nur echte Änderungen erzeugen Ereignisse); ein
nächtlicher Abgleich bringt einige hundert bis wenige tausend Ereignisse. Der schwerste Fall ist der
Erstabgleich einer großen Kommune, bei dem jedes Objekt neu ist: Die größte angebundene Kommune hat
rund 300 000 Objekte (Sitzungen, Vorlagen, Dateien, Tagesordnungspunkte, Beratungen). Diesen Fall
misst der Lauf mit 300 000 Ereignissen.

Gemessen am 4. Oktober 2026 auf einem Arbeitsplatzrechner (16 logische Kerne, PostgreSQL 16 im
Container ohne `fsync`, Testdatenbank), 300 000 Ereignisse, vier Schreiber:

| Phase | Kennzahl | `probe.sicht` (Datenbank-Sicht) | `probe.extern` (externer Effekt) | Zusage |
|---|---|---|---|---|
| Normalbetrieb (100 Ereignisse, alle 50 ms) | Latenz p50 / p95 / max | 0,05 / 0,14 / 0,30 s | 0,04 / 0,13 / 0,28 s | p95 ≤ 5 s |
| Vollabgleich (300 000, Schreiber mit 705/s) | Latenz p50 / p95 / max | 0,07 / 0,14 / 0,76 s | 0,06 / 0,12 / 0,58 s | p95 ≤ 60 s |
| Rückstau (300 000 auf einmal) | Abbau, Durchsatz | 79 s, **3 811/s** | 70 s, **4 296/s** | ≥ 100/s |
| | Rückstau, der in 60 s abgebaut ist | rund 229 000 | rund 258 000 | |

Das Journal wuchs um rund 460 bis 570 Bytes je Ereignis samt Indizes (je nach Lauf).

**Einordnung.**

- Der Worker hält mit den Schreibern Schritt: Auch der Erstabgleich der größten Kommune mit vier
  Schreibern ohne jede Wartezeit auf die Quelle erzeugt keinen nennenswerten Rückstau (p95 0,14 s). Im
  Betrieb bestimmt der Abruf bei der Quelle das Tempo, nicht die Datenbank.
- Die Zusage „≤ 60 s bei Rückstau“ hängt an der Größe des Rückstaus: Er wird mit rund 3 800 Ereignissen
  je Sekunde und Abonnement abgebaut. Ein Rückstau entsteht nur, wenn die Zustellung ruht (Worker
  gestoppt, Abonnement pausiert, Ziel nicht erreichbar); bis rund 200 000 Ereignisse ist er nach dem
  Fortsetzen innerhalb einer Minute abgebaut, darüber wächst die Latenz linear.
- Gemessen ist die Plattform: Lesen, Batch, Cursor, Parken, eine Zeile je Ereignis. Was ein Handler
  selbst kostet (der Suchindex baut je Objekt ein Dokument aus dem Bestand und schreibt es per
  Bulk-Anfrage), kommt hinzu und wird bei jeder Umstellung im Schattenbetrieb gemessen
  (`docs/DREHSCHEIBE_UMSTELLUNG.md`).
- Die CI (4 vCPU, PostgreSQL mit `fsync`) misst dasselbe mit 10 000 Ereignissen bei jedem Pull Request
  und schreibt die Werte in die Zusammenfassung des Jobs; die Prüfung scheitert, wenn eine Zusage
  verletzt ist.

## 4. Grenzwerte im Betrieb

| Kennzahl | Grenze | Alarm |
|---|---|---|
| Rückstand je Abonnement (`mandari_events_lag_seconds`) | unter 1 s im Betrieb; ein Rückstau von mehr als Durchsatz × 60 s (rund 200 000 Ereignisse) braucht länger als eine Minute | über 5 min (`docs/MONITORING.md`) |
| Sequenzierer-Stau (`mandari_events_sequencer_blocked_seconds`) | lange Schreibtransaktionen halten alle Abonnements auf | über 5 min |
| Wiederanlauf der Zustellung | ≤ Lease (30 s) plus Abfrageabstand | Push-Prüfung „Worker lebt“, `/health/worker/` |
| Wiederholung eines unterbrochenen Auftrags | 45 bis 90 s | `mandari_tasks_*` |
| Journal | rund 460 bis 570 Bytes je Ereignis samt Indizes | Aufbewahrung 90 Tage (ADR, Punkt 10) |

## 5. Wiederholen

```bash
cd mandari
# PostgreSQL nötig; mit SQLite werden die Tests übersprungen
DATABASE_URL=postgresql://…/mandari pytest apps/events/tests/test_absturz.py apps/events/tests/test_zusagen.py -s
# Lasttest mit 300 000 Ereignissen, Bericht als JSON
EVENTS_LAST_EREIGNISSE=300000 EVENTS_LAST_BERICHT=/tmp/last.json DATABASE_URL=… pytest apps/events/tests/test_last.py -s
```

In der CI laufen alle drei Dateien bei jedem Pull Request mit (Standard: 10 000 Ereignisse), im Job
„Ereignistechnik hinter PgBouncer“ zusätzlich über den Verbindungspooler; der Lasttest schreibt seine
Zahlen in die Zusammenfassung des Jobs.
