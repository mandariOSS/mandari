# Datendrehscheibe: Zusagen, Erzeuger, Umstellung, Kennzahlen

## 1. Überblick

Implementierungsnotiz zum Epic #476. Die Datendrehscheibe verbindet die Module über Ereignisse und Befehle in
PostgreSQL (`apps/events`, `hub`). Die Entscheidungen stehen in den ADRs:
[Ereignistechnik in PostgreSQL](adr/20260929-ereignistechnik-postgres.md),
[Sequenzierer](adr/20260929-sequenzierer.md),
[Aufträge und Zeitpläne](adr/20260929-auftraege-und-zeitplaene.md),
[Ereignisverträge](adr/20260929-ereignisvertraege.md),
[Befehle synchron](adr/20260929-befehle-synchron.md),
[Kanonisches Modell](adr/20260929-kanonisches-modell.md),
[Schichtenmodell](adr/20260929-schichtenmodell.md),
[Adapter-Rahmen](adr/20260929-adapter-rahmen.md),
[Änderungsfeed](adr/20260929-aenderungsfeed-format.md),
[Fremdschlüssel auf den RIS-Bestand](adr/20260929-fremdschluessel-ris-bestand.md),
[Körperschaften im Mandanten](adr/20261002-koerperschaften-im-mandanten.md).
Welche Ereignisse und Befehle es gibt, steht im erzeugten [Ereigniskatalog](EREIGNISKATALOG.md), die Regeln für
Schemas in `mandari/hub/contracts/schemas/README.md`.

| Abschnitt | Inhalt |
|---|---|
| 1 | Überblick |
| 2 | Zusagen der Ereignistechnik und ihre Nachweise |
| 3 | Session als Erzeuger von Ereignissen |
| 4 | Umstellung eines bestehenden Weges auf ein Abonnement |
| 5 | Architektur-Kennzahlen |

## 2. Zusagen der Ereignistechnik und Nachweise

Die Ereignistechnik (`apps/events`) verspricht sechs Dinge. Diese Seite nennt je Zusage den Test, der
sie in jeder CI prüft, die gemessenen Werte und die Grenzen, ab denen eine Zusage nicht mehr gilt. Sie
ist Voraussetzung, bevor ein bestehender Weg auf die Datendrehscheibe umgeschaltet wird (Abschnitt 4.1,
Epic #483). Gemessen ist der Stand vom 4. Oktober 2026 (Issue #514).

### 2.1 Übersicht

| Zusage | Nachweis (Tests unter `mandari/apps/events/tests/`) | Ergebnis |
|---|---|---|
| **Verlustfreiheit** | `test_sequencer.py::test_zwanzig_parallele_transaktionen_ohne_luecke` (20 parallele Transaktionen, zufällige Commit-Reihenfolge, Langläufer), `test_sequencer.py::test_uebernahme_nach_ausfall_ohne_doppelte_nummern`, `test_absturz.py` (Worker-Prozess mitten im Batch abgeschossen) | kein Ereignis verloren, keine Nummer doppelt |
| **Doppelzustellung** | `test_absturz.py` (nach dem Absturz), `test_dispatch_nebenlaeufig.py::test_zwei_zusteller_ohne_lease_wirken_genau_einmal`, `test_zusagen.py::test_jedes_abonnement_hat_einen_doppelzustellungstest` | Sichten genau einmal; externe Effekte höchstens der Batch im Flug doppelt; jedes Abonnement hat einen Test, der jedes Ereignis zweimal zustellt |
| **Reihenfolge je Objekt** | `test_zusagen.py::test_verschachtelte_transaktionen_halten_die_reihenfolge_je_objekt`, `test_dispatch_nebenlaeufig.py::test_parallele_schreiber_mit_sequenzierer_und_fehlern_halten_die_reihenfolge_je_objekt`, `test_absturz.py` | Schreibreihenfolge auch mit Sicherungspunkten und über einen Absturz hinweg |
| **Latenz** p95 ≤ 5 s normal, ≤ 60 s bei Rückstau | `test_weckruf.py::test_latenz_vom_commit_bis_zur_sicht` (auch hinter PgBouncer), `test_last.py` | siehe Abschnitt 2.3 |
| **Durchsatz** ≥ 100 Ereignisse/s je Abonnement (Batch 200) | `test_last.py` (Rückstau abbauen) | siehe Abschnitt 2.3 |
| **Wiederanlauf** ≤ 60 s ohne Handarbeit | `test_absturz.py` | Zustellung nach 29 s wieder vollständig; neue Aufträge nach 3 s; ein unterbrochener Auftrag nach 45 bis 90 s (Abschnitt 2.2.2) |

Die Absturz- und Lasttests starten den Worker als **eigenen Prozess** (`tests/prozess.py`) und beenden
ihn hart (SIGKILL, wie ein OOM-Kill): Im selben Prozess ließe sich ein Absturz nicht nachstellen, ein
Faden lässt sich nicht hart beenden und seine Verbindung bliebe offen. Sie messen mit den Fristen des
Betriebs (Lease 30 s, Sperre eines Auftrags 60 s) und dauern deshalb je rund eine Minute.

### 2.2 Absturz

#### 2.2.1 Mitten im Batch (`test_absturz_mitten_im_batch_ohne_verlust_und_ohne_doppelte_sichteffekte`)

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

#### 2.2.2 Mitten im Auftrag (`test_absturz_mitten_im_auftrag_wird_ohne_handarbeit_wiederholt`)

Ein Auftrag läuft, der Worker wird abgeschossen, ein neuer startet sofort; danach wird ein zweiter
Auftrag eingereiht.

- Der neue Auftrag läuft nach **3,3 s**: Die Warteschlange arbeitet sofort weiter.
- Der unterbrochene Auftrag läuft nach **75,8 s** erneut (zweiter Versuch, danach `erledigt`). Seine
  Sperre (`task_runner.LOCK_TTL`, 60 s, alle 15 s verlängert) muss erst ablaufen, freigegeben wird alle
  30 s (`MAINTENANCE_INTERVAL`). **Grenze: 45 bis 90 s** nach dem Absturz. Das ist gewollt: Eine
  kürzere Sperre würde einen Auftrag, dessen Prozess nur kurz hängt (Speicherbereinigung, langsame
  Datenbank), doppelt ausführen. Aufträge mit `max_attempts = 1` (etwa ein Sync) werden nach einem
  Absturz nicht wiederholt, sondern `tot`; der nächste Lauf holt sie nach.

### 2.3 Last (`test_last.py`)

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
  (Abschnitt 4).
- Die CI (4 vCPU, PostgreSQL mit `fsync`, Anwendung über PgBouncer) misst dasselbe mit 10 000
  Ereignissen bei jeder Änderung an der Ereignistechnik und schreibt die Werte in die Zusammenfassung
  des Jobs; die Prüfung scheitert, wenn eine Zusage verletzt ist.

### 2.4 Grenzwerte im Betrieb

| Kennzahl | Grenze | Alarm |
|---|---|---|
| Rückstand je Abonnement (`mandari_events_lag_seconds`, Alter des ältesten offenen Ereignisses) | unter 1 s im Betrieb; ein Rückstau von mehr als Durchsatz × 60 s (rund 200 000 Ereignisse) braucht länger als eine Minute | über 5 min (`docs/MONITORING.md`) |
| Sequenzierer-Stau (`mandari_events_sequencer_blocked_seconds`) | lange Schreibtransaktionen halten alle Abonnements auf | über 5 min |
| Wiederanlauf der Zustellung | ≤ Lease (30 s) plus Abfrageabstand | Push-Prüfung „Worker lebt“, `/health/worker/` |
| Wiederholung eines unterbrochenen Auftrags | 45 bis 90 s | `mandari_tasks_*` |
| Journal | rund 460 bis 570 Bytes je Ereignis samt Indizes | Aufbewahrung 90 Tage (ADR, Punkt 10) |

### 2.5 Wiederholen

```bash
cd mandari
# PostgreSQL nötig; mit SQLite werden die Tests übersprungen
DATABASE_URL=postgresql://…/mandari pytest apps/events/tests/test_absturz.py apps/events/tests/test_zusagen.py -s
# Lasttest mit 300 000 Ereignissen, Bericht als JSON
EVENTS_LAST_EREIGNISSE=300000 EVENTS_LAST_BERICHT=/tmp/last.json DATABASE_URL=… pytest apps/events/tests/test_last.py -s
```

In der CI läuft `test_zusagen.py` in jedem Testlauf. Absturz- und Lasttest messen Zeiten und laufen
deshalb nicht neben parallelen Testprozessen (deren offene Transaktionen halten den Sequenzierer
clusterweit auf), sondern im Job „Ereignistechnik hinter PgBouncer“ bei jeder Änderung an
`apps/events`, also über den Verbindungspooler (Standard: 10 000 Ereignisse). Der Lasttest schreibt
seine Zahlen in die Zusammenfassung des Jobs.

## 3. Session als Erzeuger

Etappe E4 der Datendrehscheibe (Epic #488): mandari Session meldet ihre
fachlichen Änderungen als Ereignisse `ris.*` im Journal der Ereignistechnik. Bürgerportal, Suche, Work
und der Änderungsfeed sollen daraus lesen statt aus Session-Tabellen und Signalketten.

Grundlagen: [Ereignistechnik](adr/20260929-ereignistechnik-postgres.md),
[Ereignisverträge](adr/20260929-ereignisvertraege.md),
[Kanonisches Modell](adr/20260929-kanonisches-modell.md),
[Körperschaften im Mandanten](adr/20261002-koerperschaften-im-mandanten.md).

### Umfang

| Stand | Bereich | Issue |
|---|---|---|
| umgesetzt | Sitzungen, Tagesordnung, Ladung | #533 |
| umgesetzt | Vorlagen, Beratungsfolge, Anlagen | #534 |
| umgesetzt | Abstimmung, Beschluss, Niederschrift, Rücknahme | #535 |

### Aufbau

- **`apps/session/hub_events.py`** (Session): Schalter und Erfassung. Eine Fachfunktion legt
  `hub_events.track(tenant)` um ihre Änderung und nennt die betroffenen Objekte, **bevor** sie sie ändert
  (`tracked.meeting(...)`, `tracked.agenda(...)`, `tracked.paper(...)`, `tracked.file(...)`,
  `tracked.protocol(...)`, `tracked.invited(...)`). Mit einer Sitzung beobachtet `track` auch ihre Anlagen und die Beratungen in ihr, mit
  einer Tagesordnung die Anlagen und Beratungen ihrer Punkte, mit einer Vorlage ihre Beratungsfolge, Anlagen und
  die TOPs, auf denen sie steht; deren Sichtbarkeit hängt daran. Am Ende des Blocks liest
  `track` den Zustand erneut und schreibt die Ereignisse in derselben Transaktion.
- **`hub/ris/session_events.py`** (Drehscheibe): bildet aus Zustand vorher und nachher die Ereignisse und
  schreibt sie mit `publish()`. Die Verträge `ris.*` gehören der Drehscheibe (`x-owner: hub.ris`, die Übertragungen `ris.broadcast.*` `hub.live`); dieselben
  Typen erzeugt der Ingestor für fremde RIS. Die Drehscheibe importiert Session nicht.
- **Keine Signale.** Ereignisse entstehen nur in Fachfunktionen. Änderungen im Django-Admin oder per
  Shell melden nichts.
- **Verschachtelung.** Ein `track` in einem anderen für denselben Mandanten schließt sich dem äußeren an;
  jedes Objekt wird einmal gemeldet (Sitzung anlegen mit Standard-TOPs: erst die Sitzung, dann die TOPs).
  Die Erfassung endet noch in ihrer Transaktion. Ein Rückruf nach dem Commit (`transaction.on_commit`), der
  selbst `track` aufruft, öffnet deshalb eine eigene Erfassung mit eigener Transaktion.
- **Gleichzeitige Änderungen.** `track` liest den Zustand davor ohne Sperre (außer wo die Fachfunktion selbst
  sperrt, etwa Sitzungscockpit und Erfassung). Ändern zwei Anfragen dasselbe Objekt gleichzeitig, können beide
  dieselbe Änderung melden. Verloren geht dabei keine. „Genau ein Ereignis je Änderung“ gilt also nur ohne
  Gleichzeitigkeit; Abnehmer verarbeiten Ereignisse ohnehin idempotent (Ereignistechnik, ADR).

### Schalter

| Wert | Wirkung |
|---|---|
| `aus` (Standard) | nichts; keine zusätzliche Abfrage, keine zusätzliche Transaktion |
| `schatten` | Ereignisse werden geschrieben und erreichen wie im aktiven Betrieb den Änderungsfeed und alle Abonnenten; „Schatten“ heißt beim Erzeuger nicht „unsichtbar“ (anders als bei einem Abonnement im Schatten, Abschnitt 4.2.1). Anders ist nur der Fehlerfall: Scheitert das Bilden oder Schreiben, bleibt die Änderung bestehen (eigener Sicherungspunkt); das Protokoll nennt Schritt, Fehlerklasse und Aufrufstellen, keine Inhalte. Für den Parallelbetrieb neben den bisherigen Wegen |
| `aktiv` | Änderung und Ereignisse sind atomar: Scheitert ein Ereignis, bleibt auch die Änderung aus |

- Installation: `SESSION_EVENTS` (`.env`, Compose reicht es an Anwendung und Worker durch).
- Mandant: Admin → Session-Mandant → „Ereignisse an die Datendrehscheibe“ (leer = wie die Installation).
  So lässt sich ein einzelner Mandant im Schatten betreiben, bevor die Installation umschaltet.
- Die Ereignisse brauchen den Sequenzierer (Dienst `worker`). Ist `SESSION_EVENTS` nicht `aus`, meldet
  `/health/` ohne Worker `degraded`. Wer nur einzelne Mandanten einschaltet, setzt
  `EVENTS_WORKER_REQUIRED=true`; der Admin weist beim Speichern eines solchen Mandanten darauf hin.

### Ereignisse je Fachfunktion (#533)

| Fachfunktion | Ereignisse |
|---|---|
| Sitzung anlegen (Formular, Jahresplanung) | `ris.meeting.scheduled`, danach je Standard-TOP `ris.agendaitem.changed` (`added`) |
| Sitzung bearbeiten, absagen | `ris.meeting.changed` mit den geänderten Feldern (`name`, `start`, `end`, `meetingState`, `cancelled`, `organization`, `location`, `meetingFormat`), bei Absage `cancelled: true` |
| Öffentlichkeit der Sitzung ändern | siehe Sichtbarkeit; die Tagesordnung folgt mit |
| TOP anlegen, bearbeiten, Ö/NÖ wechseln, absetzen, löschen, verschieben, Reihenfolge | `ris.agendaitem.changed` (`added`, `changed`, `withdrawn`, `deleted`), Rücknahmen als `ris.object.depublished`; neu nummerierte Punkte mit `changed: [number, order]` |
| TOP aus der Beratungsfolge terminieren, Tagesordnungsvorlage anwenden | wie TOP anlegen; ggf. `ris.meeting.changed` (`meetingFormat`) |
| Sitzungscockpit: eröffnen, schließen | `ris.meeting.changed` (`meetingState`) |
| Ladung, Nachtrag, Vertretungsanfrage versenden | `ris.meeting.invited` (nur `nichtoeffentlich`, Versandart, Kennung des Versandvorgangs; keine Empfänger) mit dem Versandvorgang, vor dem Versand der Mails |
| Erstladung | zusätzlich `ris.meeting.changed` (`meetingState`): Mit der Ladung ist die Tagesordnung veröffentlicht |

### Ereignisse je Fachfunktion (#534)

| Fachfunktion | Ereignisse |
|---|---|
| Vorlage anlegen (Formular, Unternummer, aus einem Antrag) | `ris.paper.created` (nur `nichtoeffentlich`; aus einem Antrag mit `submission`, der Kennung der Einreichung) |
| Freigabelauf: vorlegen, zurückweisen, Mitzeichnung zurückgewiesen | `ris.paper.changed` (`status`, nur `nichtoeffentlich`) |
| Freigeben (öffentliche Vorlage) | `ris.paper.released` (öffentlich, ohne Einreichung), dazu ihre öffentlichen Anlagen und Stationen als `added` |
| Vorlage bearbeiten, Fassung wiederherstellen | `ris.paper.changed`: öffentlich die Felder der Schnittstelle (`name`, `reference`, `date`, `paperType`, `originatorPerson`, `originatorOrganization`, `underDirectionOf`), intern `status`, `public`, `mainText`, `resolutionText` |
| Zurückziehen | `ris.paper.changed` (`status`, intern; die Vorlage bleibt veröffentlicht) |
| Zurück in den Entwurf, nichtöffentlich stellen | `ris.object.depublished` für Vorlage, Stationen und Anlagen (`zurueckgenommen` bzw. `nichtoeffentlich`), die Änderung intern |
| Station anlegen, ändern, verschieben, entfernen | `ris.consultation.changed` (`added`, `changed` mit `organization`, `meeting`, `agendaItem`, `role`, `authoritative`, `order`; `result` nur intern), entfernte öffentliche Station als `ris.object.depublished` |
| Station terminieren, weiterleiten | `ris.paper.changed` (`status`, intern), `ris.agendaitem.changed` (`added`), `ris.consultation.changed` (`scheduled` mit dem TOP) |
| Anlage hochladen, umbenennen, ersetzen | `ris.file.changed` (`added`, `renamed`, `replaced`) mit Vorlage, Sitzung bzw. TOP; öffentlich nur an veröffentlichten Objekten |
| Anlage nichtöffentlich stellen, löschen | `ris.object.depublished` (öffentliche Anlage) bzw. `ris.file.changed` (`removed`, `nichtoeffentlich`, Operation `delete`) |

Die öffentliche Fassung der Niederschrift ist keine Anlage in diesem Sinn; sie meldet #535.

Rücknahmen eingebetteter Objekte (`ris.object.depublished` für Tagesordnungspunkte, Stationen und Anlagen) nennen
das Objekt, in das sie eingebettet waren: `meeting` beim Tagesordnungspunkt, `paper` bei der Station, `paper`,
`meeting` und `agenda_item` bei der Anlage, jeweils nur, wenn es öffentlich war. Session löscht die Zeilen; so nennt
der Änderungsfeed auch das einbettende Objekt (#1229). Hängt eine öffentliche Anlage nicht mehr an einem öffentlichen
Objekt, an dem sie hing (neue Zugehörigkeit), nennt `ris.file.changed` es als `previous_paper`, `previous_meeting`
bzw. `previous_agenda_item`.

### Ereignisse je Fachfunktion (#535)

| Fachfunktion | Ereignisse |
|---|---|
| Abstimmung erfassen (Erfassung, Sitzungscockpit, Niederschrift bearbeiten) | `ris.voting.recorded` (Art und Ergebnis, nie Einzelstimmen) bei angenommen/abgelehnt, dazu `ris.resolution.adopted` (`changed: [result]`) |
| Nur Summen korrigiert | `ris.voting.recorded` |
| Vertagen, zurückziehen, zur Kenntnis nehmen | `ris.resolution.adopted` ohne Abstimmung |
| Ergebnis zurücknehmen (wieder offen bzw. ohne Abstimmung) | `ris.object.depublished` der Abstimmung (`zurueckgenommen`, nur wenn sie öffentlich war) und `ris.agendaitem.changed` (`result`) bzw. `ris.resolution.adopted` |
| TOP bzw. Sitzung nach der Abstimmung nichtöffentlich, TOP gelöscht | mit dem TOP auch seine Abstimmung als `ris.object.depublished` (`nichtoeffentlich` bzw. `quelle_geloescht`) |
| TOP mit Abstimmung wird veröffentlicht | mit dem TOP (`added`) `ris.voting.recorded` (öffentlich) |
| Beschlussnummern vergeben | `ris.resolution.adopted` (`changed: [resolutionNumber]`) |
| Beschlusskontrolle | `ris.resolution.implementation_changed` (öffentlich), wenn der Umsetzungsstand nach der Regel der Beschlussseiten im Bürgerportal öffentlich ist: Opt-in der Verwaltung (`SessionTenant.implementation_publish`) und am Beschluss, angenommen, nicht abgesetzt, öffentlicher TOP, Mandant veröffentlicht im Bürgerportal (`decision_tracking.is_publicly_visible`). Sonst `ris.agendaitem.changed` (`implementationStatus`, intern). Wird der Stand öffentlich (Freigabe, Annahme), ist er für die Öffentlichkeit neu; endet das bei weiter veröffentlichtem TOP, meldet `ris.agendaitem.changed` (`implementationStatus`, öffentlich), dass sie den TOP neu lesen soll |
| Niederschrift anlegen, zur Prüfung geben, zurückweisen | `ris.meeting.changed` (`protocol`, intern) |
| Niederschrift genehmigen | `ris.protocol.approved` (intern, `mode: follow_up`, mit `approved_in`, wenn die Genehmigungssitzung gewählt ist) |
| Niederschrift ohne Genehmigungsschritt veröffentlichen (aus der Prüfung) | `ris.protocol.approved` (intern, `mode: direct`), dazu `ris.protocol.published` |
| Öffentliche Fassung veröffentlichen, erneuern, nach einer Berichtigung neu erzeugen | `ris.protocol.published` (`published`, `renewed`, `corrected`) mit der Datei; eine ersetzte Fassung als `ris.object.depublished` |
| Öffentlichen Inhalt nach der Veröffentlichung ändern (TOP nichtöffentlich, Sitzung umbenannt …) | nach dem Commit erneuert `refresh_meeting` die Fassung in eigener Erfassung: `ris.protocol.published` (`renewed`) und `ris.object.depublished` der alten Datei |
| Veröffentlichung zurücknehmen (auch wenn die Sitzung nichtöffentlich wird) | `ris.object.depublished` der Datei (`zurueckgenommen`) und `ris.meeting.changed` (`resultsProtocol`) |
| Berichtigung übernehmen | wie Abstimmung und Beschluss für die berichtigten TOPs |

Die Abstimmung hat in Session keine eigene Adresse: Ihre Kennung bildet sich aus der Adresse des TOP mit dem
Zusatz `voting`; im Änderungsfeed erscheint sie am TOP. Ebenso die Niederschrift (`protocol`): Adresse der
Sitzung mit dem Zusatz `protocol`, nie die interne Kennung. Eine Niederschrift ohne öffentliche Fassung
(nichtöffentliche Sitzung, Schnittstelle nicht freigeschaltet) meldet nichts Öffentliches.

Umlaufbeschlüsse (`SessionCircularResolution`) melden noch nichts: `ris.resolution.adopted` verlangt einen
Tagesordnungspunkt und kann sie nicht ausdrücken. Dafür braucht es eine Erweiterung des Vertrags.

Nicht gemeldet, weil das kanonische Modell es nicht kennt: interne Notizen, Zugangsweg der
Zugeschalteten, Einladungstext, tatsächliche Zeiten im Sitzungsverlauf, Unterpunkt-Zuordnung,
Geheimhaltungsmerkmal, Frist, federführendes Amt, finanzielle Auswirkungen, Mitzeichnungen, Umlaufbeschlüsse (siehe
oben). Speichern ohne Änderung meldet nichts, ebenso eine ersetzte Anlage mit gleichem Inhalt. Umschalten am
Mandanten (Freischaltung, Veröffentlichung des Umsetzungsstands) meldet ebenfalls nichts.

### Kennungen und Hülle

- `aggregate_id` und alle Kennungen der Nutzlast sind kanonisch: `uuid5` über die Adresse des Objekts in
  der Session-Schnittstelle auf der festgeschriebenen Basis (`SessionUris.canonical_id`). Es sind dieselben
  Kennungen, die der RIS-Bestand für das gespiegelte Objekt führt.
- Mandant `session:<uuid>`, `body_id` die kanonische Kennung des Body der Schnittstelle (bis #758 einer je
  Mandant). Damit nennt der Änderungsfeed der Session-Schnittstelle und des Aggregators die Ereignisse.
- Ausnahme: `submission` in `ris.paper.created` ist die Kennung der Einreichung in Session (wie in `submission.*`).
- Auslöser ist das angemeldete Konto (`user:<uuid>`), nie ein Name; im Befehl
  `session_publish_protocols` `system:niederschriften` mit einer Korrelation für den ganzen Lauf.
- Ausnahme: `dispatch` in `ris.meeting.invited` ist die Kennung des Versandvorgangs in Session (kein
  Objekt des kanonischen Modells).

### Sichtbarkeit

- `oeffentlich` nur, was die Session-Schnittstelle nach der Änderung ausliefert, und nur mit deren
  Feldern. Von einer nichtöffentlichen Sitzung mit veröffentlichtem Termin sind das Name, Zeit, Status,
  Absage und Gremien. Ändert sich nur, was die Öffentlichkeit nicht sieht (etwa der Ort einer solchen
  Sitzung), geht das Ereignis nur an `nichtoeffentlich`; der Feed bleibt unverändert.
- Vor der Freischaltung der Schnittstelle (`SessionTenant.oparl_public_since`, #319) und bei deaktiviertem
  Mandanten liefert sie nichts aus: Alle Ereignisse sind dann `nichtoeffentlich`, auch die zu öffentlichen
  Sitzungen (Einführung, Testdaten, Schulung, Umstieg).
- Wird ein Objekt öffentlich, erscheint es öffentlichen Empfängern als neu (`ris.meeting.scheduled`,
  `added`).
- Endet die Veröffentlichung, meldet `ris.object.depublished` (Operation `delete`) die Rücknahme mit
  Grund `nichtoeffentlich`; mit der Sitzung auch ihr Ort und ihre öffentlichen TOPs. Die Änderung selbst
  geht als `nichtoeffentlich` an die übrigen Empfänger.
- Gelöscht: Öffentliches als `ris.object.depublished` (`quelle_geloescht`), Nichtöffentliches als
  `ris.agendaitem.changed` (`deleted`, nur `nichtoeffentlich`), wie beim Ingestor (`hub.ris.retraction`).
- Ein öffentliches Ereignis nennt keine Kennung eines nichtöffentlichen Objekts, etwa die Vorlage eines
  TOP nur, solange sie veröffentlicht ist, und von einer Station in einer nichtöffentlichen Sitzung weder
  Gremium noch Sitzung (wie die Schnittstelle: öffentlich bleibt nur, dass die Vorlage dort beraten wird).
- Eine Vorlage ist veröffentlicht, wenn sie öffentlich und freigegeben ist; ihre Stationen sind es mit ihr,
  ihre Anlagen, wenn sie selbst öffentlich sind.
- `order` einer Station ist öffentlich, obwohl das Objekt der Beratung das Feld nicht nennt: Die Vorlage
  bettet ihre Beratungsfolge in dieser Reihenfolge ein (`SessionMapping.paper`). Eine verschobene Station
  ändert also, was die Schnittstelle ausliefert.

### Freischaltung der Schnittstelle

Die Freischaltung selbst meldet nichts. Öffentliche Abnehmer steigen danach über den Snapshot der
Session-Schnittstelle ein (`…/api/oparl/body/kommparl/snapshot/`, mit Cursor für den Feed; die ältere Adresse
`…/api/oparl/body/snapshot/` ist abgekündigt). Ereignisse von vorher sind
`nichtoeffentlich` und erscheinen im öffentlichen Feed nicht. Auch die Rücknahme der Freischaltung meldet
nichts; die Schnittstelle samt Feed antwortet dann wieder mit 404 (möglich nur, solange der Mandant nicht im
Bürgerportal veröffentlicht).

### Übergang

Bis der RIS-Projektor den Spiegel ablöst (#488), gleicht der Ingestor die Session-Schnittstelle weiter ab
und meldet mit `INGESTOR_EVENTS_ENABLED` dieselben Objekte unter dem Mandanten `source:<uuid>`. Der
Änderungsfeed nennt ein Objekt dann zweimal (sofort aus Session, später aus dem Abgleich). Das ist für
Abnehmer unschädlich: Einträge sind Hinweise zum Nachlesen, `upsert` und `delete` sind idempotent.
Wer das vermeiden will, nimmt die Quelle des Mandanten mit `sync_config["events_enabled"] = false` von
den Ereignissen des Ingestors aus, sobald Session `aktiv` meldet.

### Reihenfolge

Innerhalb einer Änderung: Sitzungen, Vorlagen, Tagesordnungspunkte in der Reihenfolge der Tagesordnung,
Beratungen je Vorlage in der Reihenfolge der Stationen, Anlagen, dann ausdrücklich gemeldete Vorgänge
(Ladung). Der Sequenzierer übernimmt die Reihenfolge des Schreibens.

### Tests der Session-Ereignisse

- `apps/session/tests/test_drehscheibe_sitzungen.py`: je Fachfunktion, Schalter, Sichtbarkeit, Reihenfolge,
  Schattenbetrieb und Atomarität, Ankunft im Änderungsfeed der Session-Schnittstelle.
- `apps/session/tests/test_drehscheibe_vorlagen.py`: Vorlagen, Freigabelauf, Rücknahme samt Stationen und
  Anlagen, Antrag, Beratungsfolge, Terminieren, Anlagen.
- `apps/session/tests/test_drehscheibe_beschluesse.py`: Abstimmung, Vertagung, Rücknahme eines Ergebnisses,
  nichtöffentliche TOPs, Beschlussnummer, Beschlusskontrolle, Niederschrift vom Entwurf bis zur Rücknahme.
- `hub/ris/tests/test_session_events.py`: Übergänge der Sichtbarkeit ohne Datenbank.
- Jedes Ereignis prüft `publish()` in Tests gegen seinen Vertrag (`EVENTS_VALIDATE_CONTRACTS`).

## 4. Umstellung eines Weges

Jeder bestehende Weg, der auf ein Abonnement der Drehscheibe umzieht (Suchindex, Benachrichtigungen,
Änderungsfeed, Adapter …), folgt demselben Ablauf in vier Phasen: **Schatten → Vergleich → Umschalten →
Aufräumen**. Jede Phase ist umkehrbar, bis der alte Weg im Folge-Release entfernt wird.

**So wird die Liste benutzt:** Das Issue der Umstellung bekommt eine Kopie von Abschnitt 4.2 als
Kommentar; abgehakt wird dort, mit Datum und Beleg (Ausgabe des Vergleichs, Kennzahl, PR). Eine Phase
beginnt erst, wenn die vorige vollständig abgehakt ist. Trifft ein Abbruchkriterium (Abschnitt 4.3) zu,
gilt der Rückfall der jeweiligen Phase, und die Umstellung beginnt wieder mit der Phase, in der der
Fehler lag.

**Kennzahlen:** Mit dem Aufräumen jeder Umstellung (Abschnitt 4.2.5) und nach Abschluss jeder Etappe des
Epics #476 werden die Architektur-Kennzahlen neu erhoben und in Abschnitt 5 fortgeschrieben (#569). So zeigt der Verlauf, ob
eine Umstellung die Kopplung tatsächlich gesenkt hat.

### 4.1 Einmalige Voraussetzungen (E1-Gate, Epic #483)

Gelten für alle Umstellungen; erst danach wird zum ersten Mal ein Weg umgeschaltet. Erfüllt seit dem 4. Oktober 2026:

- Zusagen der Ereignistechnik nachgewiesen: Absturz, Doppelzustellung, Reihenfolge, Latenz, Durchsatz,
  Wiederanlauf (Abschnitt 2, #514)
- Sicherung umfasst Journal und Aufträge, Wiederherstellung mit Anheben der Folgenummer und Nachspielen erprobt
  (`docs/BACKUP.md`, Abschnitt „Journal und Aufträge“, #573)
- Überwachung: Rückstand je Abonnement, tote Ereignisse, Sequenzierer-Stau mit Alarm; Worker-Prüfung auf der
  Statusseite; Admin-Seite für Abonnements (mit Nachspielen), geparkte Ereignisse und Worker
  (`docs/MONITORING.md`, #510)

Je Umstellung bleibt zu prüfen, ob die Erzeuger der benötigten Ereignisse in Produktion laufen und ihre Verträge
abdecken, was der neue Weg braucht: Der Ingestor meldet seit dem 4. Oktober 2026, Session je Mandant nach dem
Schalter `SESSION_EVENTS` (Abschnitt 3).

### 4.2 Checkliste je Umstellung

#### 4.2.1 Vorbereitung

- [ ] Issue der Umstellung: alter Weg (Code, Auslöser), neues Abonnement (Name, Typmuster, Batch,
      Warteschlange, `transactional`), Schalter, Verantwortliche
- [ ] Alter Weg per Einstellung abschaltbar, ohne Deploy umkehrbar
- [ ] Abonnement schreibt im Schattenbetrieb nur in ein Schattenziel (`delivery.shadow`) oder zählt nur, was es
      täte (wenn es keinen alten Weg zum Vergleichen gibt, etwa `benachrichtigung` oder die Nachfolger der
      Texterkennung), ist idempotent und hat einen Doppelzustellungstest (Eintrag in `DOPPELZUSTELLUNG`,
      `apps/events/tests/test_zusagen.py`)
- [ ] Vergleichswerkzeug alter gegen neuen Weg: Zahl der Objekte, fehlende, überzählige, Stichprobe mit
      abweichenden Feldern
- [ ] Bekannte Lücken (Ereignisse, die der neue Weg noch nicht sieht) aufgelistet, je Lücke behoben oder
      mit Entscheidung im Issue
- [ ] Last abgeschätzt: Ereignisse je Tag und Vollabgleich × Kosten je Ereignis, Speicher des
      Schattenziels, Wirkung auf den Worker (Speichergrenze 1 GB)
- [ ] Rückfall je Phase beschrieben (Schalter, Befehle)

#### 4.2.2 Schatten

- [ ] Schalter auf Schatten, Deploy; Abonnement steht in `events_subscription` im Zustand `schatten`
- [ ] Schattenziel einmal aus dem Bestand gebaut (Vollbau), Zeitpunkt notiert
- [ ] Nach 24 h: Rückstand unter 5 min (außer im nächtlichen Vollabgleich), keine toten Ereignisse,
      Worker ohne Neustart durch die Speichergrenze

#### 4.2.3 Vergleich (mindestens 7, besser 14 Tage)

- [ ] Zeitraum enthält mindestens einen nächtlichen Vollabgleich und einen Werktag mit Sitzungen
- [ ] Vergleich täglich (oder nach jedem Vollabgleich) ausgeführt, Ausgabe im Issue
- [ ] Jede Abweichung erklärt (zeitlicher Versatz, bekannte Lücke mit Entscheidung) oder behoben;
      am Ende **keine unerklärte Abweichung**
- [ ] Rückfall geprobt: Schattenziel verworfen und neu gebaut, ohne dass der alte Weg es merkt

#### 4.2.4 Umschalten mit Rückfallprobe

- [ ] Zeitfenster ohne Sitzungen und ohne Vollabgleich, angekündigt im Issue
- [ ] Ziel einmal aus dem Bestand gebaut, Schalter auf aktiv, alter Weg per Einstellung aus
- [ ] Prüfung nach 1 h und nach 24 h: Funktion von außen (Stichprobe), Rückstand, tote Ereignisse
- [ ] **Rückfallprobe** innerhalb der ersten Woche: Schalter zurück, alter Weg an, Funktion geprüft,
      wieder umgeschaltet; Dauer und Ergebnis im Issue
- [ ] 7 Tage aktiv ohne Abbruchkriterium

#### 4.2.5 Aufräumen (Folge-Release)

- [ ] Alter Weg entfernt (Code, Signale, Befehle, Schalter des alten Weges), Schattenziel gelöscht
- [ ] Doku und Nachtrag im ADR, CHANGELOG (Rubrik „Entfernt“)
- [ ] Architektur-Kennzahlen neu erhoben (`python scripts/architektur_kennzahlen.py`) und in Abschnitt 5
      fortgeschrieben (Tabelle, Verlaufszeile, K8 aus dem Monitoring; Abschnitt 5.4, #569); die Verlaufszeile
      steht als Beleg im Issue der Umstellung
- [ ] Issue geschlossen, Erkenntnisse für die nächste Umstellung hier ergänzt

### 4.3 Abbruchkriterien

Ein Kriterium genügt. Die Grenzwerte im Betrieb stehen in Abschnitt 2.4. Abbruch heißt: Rückfall der Phase ausführen, Ursache als Issue, Umstellung ab der
betroffenen Phase neu beginnen.

| Kriterium | Schatten / Vergleich | nach dem Umschalten |
|---|---|---|
| Rückstand des Abonnements (`mandari_events_lag_seconds`) über 5 min länger als 1 h außerhalb des Vollabgleichs | Schattenbetrieb anhalten (Abonnement pausieren) | sofort zurückschalten |
| Tote Ereignisse (`mandari_events_parked{state="tot"}`) ohne behobene Ursache nach einem Werktag | Vergleich beginnt nach der Behebung neu | sofort zurückschalten |
| Unerklärte Abweichung im Vergleich | Vergleich verlängern, Ursache beheben | – |
| Fehler in der Funktion, die Nutzer sehen (falsche, fehlende oder veraltete Ergebnisse) | – | sofort zurückschalten |
| Worker: Neustarts durch die Speichergrenze oder Speicher dauerhaft über 80 % der Grenze | Schattenbetrieb anhalten | zurückschalten, wenn die Zustellung leidet |
| Sequenzierer-Stau über 5 min (`mandari_events_sequencer_blocked_seconds`) wiederholt | Ursache (lange Transaktion) beheben | Ursache beheben; Rückfall nur bei Funktionsfehler |

### 4.4 Umstellungen und ihre Schalter

Den Stand der Phasen führt das jeweilige Issue (Kommentar mit der Checkliste aus Abschnitt 4.2), die Schalter und
Befehle beschreibt `DEPLOYMENT.md`.

| Weg | Abonnement | Schalter | Betrieb (`DEPLOYMENT.md`) | Issue |
|---|---|---|---|---|
| Suchindex | `suchindex` | `SEARCH_INDEX_SUBSCRIPTION` (`aus`, `schatten`, `aktiv`) | „Suchindex als Abonnement (Schattenbetrieb)“ | #526, #527 |
| RIS-Bestand der Session-Mandanten | `ris.session_projektor` | `RIS_SESSION_PROJECTOR` (`aus`, `schatten`; aktiv erst mit #537) | „RIS-Projektor für Session-Mandanten (Schattenbetrieb)“ | #536, #537 |
| Benachrichtigungen in Work | `benachrichtigung` | `WORK_NOTIFICATION_SUBSCRIPTION` (`aus`, `schatten` zählt nur, `aktiv`) | „Benachrichtigungen als Abonnement“ | #529 |
| Verortung und KI-Zusammenfassung nach neuem Text | `insight.verortung`, `insight.zusammenfassung` | `GEOREF_SUBSCRIPTION`, `SUMMARY_SUBSCRIPTION` (Schatten zählt nur) | „Nachfolger der Texterkennung“ | #919 |
| Dokumentkette (Abruf und Erkennung) | – (Etappe 2 über Aufträge) | `TASKS_BACKEND=journal`, `TEXT_EXTRACTION_RUNNER=worker` | „Dokumentkette einschalten (Issue #919)“ | #919 |

Ohne Umstellung eines alten Weges laufen außerdem die Abonnements `insight.urheber` (`URHEBER_SUBSCRIPTION`,
Standard `aktiv`), `insight.indexnow` (mit `INDEXNOW_KEY`) und `insight.fraktionen_live` (Live-Übertragungen).
Die Etappe 3 der Dokumentkette (Abonnement für den Abruf über Ereignisse) ist im Code noch nicht angelegt; Stand in
#919 und ADR `docs/adr/20261007-dokumentkette.md`.

## 5. Architektur-Kennzahlen

Stand: 10. November 2026 · Issue #569 · Entscheidung:
[Vier Schichten mit durchgesetzten Abhängigkeitsregeln](adr/20260929-schichtenmodell.md), Abschnitt „Prüfung“

Ob die Entkopplung der Module vorankommt, soll messbar sein. Nach jeder Etappe der Datendrehscheibe
(Epic #476) erheben wir dieselben Kennzahlen aus dem Quellstand und halten sie hier fest:

```bash
python scripts/architektur_kennzahlen.py            # Tabelle wie in Abschnitt 5.1
python scripts/architektur_kennzahlen.py --json     # dazu Einzelheiten: Funktionen, Dateien, Richtungen
```

Das Skript prüft nichts und bricht keinen Lauf ab. Dass Werte nicht steigen, sichern die Ratchets und
Fitnessfunktionen, aus denen ein Teil der Zahlen stammt (Spalte „Quelle“).

### 5.1 Letzte Erhebung

Erhoben am 10. November 2026 am Stand `3a63808d` (`dev`) mit #1304, Anlass: erste Erhebung.

| Nr. | Kennzahl | Wert | Ziel | Quelle |
|---|---|---:|---:|---|
| K1 | Funktionen, die Daten eines fremden Moduls schreiben | 55 | 0 | `scripts/architektur_kennzahlen.py` |
| K2a | Schreibwege des Suchindex (Dateien mit Schreibzugriff) | 4 | 1 | `scripts/architektur_kennzahlen.py` |
| K2b | Schreibstellen des Suchindex (Aufrufe in diesen Dateien) | 7 | – | `scripts/architektur_kennzahlen.py` |
| K3a | Funktionen, die Mails über den Mail-Dienst senden | 32 | – | `scripts/architektur_kennzahlen.py` |
| K3b | Mails, die immer im Aufruf rausgehen (Stellen mit `sofort=True`) | 6 | – | `SOFORT` in `mandari/apps/common/tests/test_mail_dienst.py` |
| K4 | Hintergrund-Threads im Webprozess (Thread-Starts außerhalb des Workers) | 0 | 0 | `scripts/architektur_kennzahlen.py` mit `ERLAUBT` aus `mandari/apps/events/tests/test_zeitplaene_statt_faden.py` |
| K5 | `OParl*.objects` außerhalb von `hub` und `insight_core` | 126 | 0 | `scripts/check_ris_access_ratchet.py` |
| K6a | `CASCADE` von Fachmodulen auf den RIS-Bestand (alle Apps außer `insight_core`) | 0 | 0 | `AUSNAHMEN_CASCADE` in `mandari/hub/ris/tests/test_loeschsemantik.py` |
| K6b | `CASCADE` auf den RIS-Bestand aus `insight_core` selbst (Bürgerportal, Geodaten) | 13 | 0 | `AUSNAHMEN_CASCADE` in `mandari/hub/ris/tests/test_loeschsemantik.py` |
| K7 | import-linter-Ausnahmen (verschiedene Importe über alle Verträge) | 54 | 0 | `scripts/check_import_linter_ratchet.py` |
| K8 | Ereignis-Rückstand p95 | – | ≤ 5 s | Produktionsmetrik `mandari_events_lag_seconds` (`docs/MONITORING.md`), nicht aus dem Quellstand |

Zur Einordnung der ersten Erhebung:

- **K1:** 32 der 55 Funktionen stehen in den drei Befehlen, die Demo- und Lastdaten anlegen
  (`setup_demo_environment`, `setup_demo_praesentation`, `generate_load_data`). Die übrigen 23 verteilen
  sich auf Work → Mandantenverwaltung (15: Mitglieder, Einladungen, Rollen, Themen), Session → Plattform
  und RIS-Bestand (4) und die Drehscheibe → Ereignistechnik (4: Aufträge und Abonnements direkt in deren
  Tabellen).
- **K2a:** Die vier Wege sind die Signale (`insight_core/signals.py`), der Ingestor, der Neuaufbau
  (`reindex_elasticsearch`) und das Abonnement `suchindex`, das die anderen ablöst
  (Issue #527, `DEPLOYMENT.md`, „Suchindex als Abonnement“).
- **K7:** je Vertrag 34 (`schichten`), 31 (`module-unabhaengig`) und 29 (`plattform-fachfrei`) Einträge;
  derselbe Import steht oft in mehreren Verträgen.

### 5.2 Verlauf

Eine Zeile je Erhebung, die neueste unten. „Stand“ nennt den Commit auf `dev`, auf dem die Erhebung
aufsetzt, und den Pull Request, dessen Änderungen mitgezählt sind. K8 trägt ein, wer die Zeile schreibt
(Abschnitt 5.4).

| Datum | Stand | Anlass | K1 | K2a | K2b | K3a | K3b | K4 | K5 | K6a | K6b | K7 | K8 |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-11-10 | `3a63808d` + #1304 | erste Erhebung | 55 | 4 | 7 | 32 | 6 | 0 | 126 | 0 | 13 | 54 | – |

### 5.3 Zählregeln

Gezählt wird der Betriebscode unter `mandari/` (für K2 auch `ingestor/src/`). Tests, Migrationen und
E2E-Tests zählen nie mit. Die eigenen Zählungen lesen den Syntaxbaum, ohne Django zu laden; sie sind
Näherungen mit festen Regeln, vergleichbar von Erhebung zu Erhebung, nicht mit früheren Auszählungen von
Hand. Die Ziele stammen aus den genannten Entscheidungen; „–“ heißt, dass es kein Zahlenziel gibt.

**K1: Funktionen, die Daten eines fremden Moduls schreiben.** Regel 2 des Schichtenmodells: Jeder
Datenbestand hat genau einen Eigentümer, nur er schreibt.

- Eigentümer sind die Einheiten der import-linter-Verträge in `mandari/pyproject.toml`: jedes Fachmodul
  des Vertrags `module-unabhaengig` für sich (`apps.session`, `apps.work`, `apps.minutes`), die Drehscheibe
  als eine Einheit (`hub`, `insight_*`) und die Plattform als eine Einheit (`apps.events`, `apps.common`,
  `apps.accounts`, `apps.tenants`, `apps.provisioning`). Ein Modell gehört der Einheit, aus deren
  `models`-Modul es importiert wird.
- Eine Funktion zählt einmal, wenn sie ein Modell einer anderen Einheit über das ORM schreibt: `create`,
  `bulk_create`, `bulk_update`, `get_or_create`, `update_or_create`, `update`, `delete` an
  `Modell.objects…` (auch `_default_manager`, `_base_manager`, asynchrone Varianten) oder `save`/`delete`
  an einem Objekt, das die Funktion selbst aus dem Modell holt oder erzeugt (`Modell(…)`,
  `Modell.objects.get(…)`, `get_object_or_404(Modell, …)`, Schleife über ein Queryset).
- Nicht erfasst: Objekte, die als Parameter oder über eine Relation hereinkommen, Modelle über
  `apps.get_model()` und `get_user_model()`, eigene Manager, Änderungen an Mehrfachbeziehungen und rohes
  SQL. Der Aufruf eines fremden Dienstes ist kein Schreibzugriff in diesem Sinn; er erscheint als
  Importausnahme in K7.
- `--json` nennt jede Funktion und die Zahl je Richtung (`apps.work -> apps.tenants`).

**K2: Schreibwege des Suchindex.** Ziel ist ein Weg: Nur das Abonnement `suchindex` schreibt den Index
(Issue #527, `DEPLOYMENT.md`, „Suchindex als Abonnement“).

- K2a zählt die Dateien, K2b die Aufrufe darin, die Dokumente in Elasticsearch schreiben oder löschen:
  am Client `index`, `delete_by_query`, `update_by_query` mit `index=`, `create`, `update`, `delete` mit
  `index=` und `id=`, `bulk` mit `operations=`, die Helfer aus `elasticsearch.helpers` (`bulk`,
  `streaming_bulk` …) sowie HTTP-Aufrufe (`post`, `put`, `delete`) auf `/_bulk`, `/_doc`, `/_update` und
  verwandte Pfade, wie sie der Ingestor nutzt.
- Lesen (`search`, `count`, `mget`) und Index-Verwaltung (`.indices.…`) zählen nicht. Das Schattenziel
  des Abonnements schreibt dieselbe Stelle wie das aktive Ziel und zählt nicht eigens.

**K3: Mailversand.** Mails gehen nur über den Mail-Dienst `apps.common.mail` (#528); ob eine Mail im Aufruf
oder als Auftrag im Worker rausgeht, entscheidet der Betrieb je Mailart mit `MAIL_QUEUE`
([Aufträge und Zeitpläne](adr/20260929-auftraege-und-zeitplaene.md)). Aus dem Quellstand lässt sich deshalb
nicht ablesen, wie viele Mails eine Anfrage tatsächlich aufhalten, wohl aber die Obergrenze und die festen
Ausnahmen.

- K3a zählt die Funktionen, die `mail.send` oder `mail.send_template` aufrufen, ohne Management-Befehle
  (eigene Prozesse). Mit `MAIL_QUEUE=*` und `TASKS_BACKEND=journal` versendet davon nur noch im Aufruf,
  was unter K3b fällt, was über das eigene SMTP einer Organisation ohne Ersatzweg geht und was größer ist
  als `MAIL_QUEUE_MAX_BYTES`.
- K3b ist die Summe der Liste `SOFORT`: Stellen mit `sofort=True`, deren Ergebnis die Oberfläche oder der
  Vorgang sofort braucht (Testmails, Einreichung per E-Mail, Ladung). Der Test daneben hält die Liste und
  den Code gleich.
- Versand am Mail-Dienst vorbei ist keine Kennzahl: `test_mails_gehen_nur_ueber_den_mail_dienst` in
  derselben Datei lässt ihn nicht zu.

**K4: Hintergrund-Threads im Webprozess.** Ziel null: Wiederkehrende Arbeit läuft als Auftrag oder
Zeitplan im Worker ([Aufträge und Zeitpläne](adr/20260929-auftraege-und-zeitplaene.md), #515).

- Gezählt werden Aufrufe von `threading.Thread` und `threading.Timer` und davon abgeleitete Klassen
  außerhalb des Codes, den die Fitnessfunktion als Worker führt (Liste `ERLAUBT`: `apps/events/` und der
  Metrik-Server). Die Fitnessfunktion hält den Wert bei null; `--json` nennt auch die Stellen im Worker.
- Pools, die innerhalb einer Anfrage laufen (`ThreadPoolExecutor` der Bereitschaftsprüfung), zählen nicht.

**K5: `OParl*.objects` außerhalb von `hub` und `insight_core`.** Summe des Ratchets für die Lese-Fassade
`hub.ris.selectors` ([Kanonisches Modell](adr/20260929-kanonisches-modell.md), #522); je Datei steht der
Stand in `scripts/ris_access_baseline.json`.

**K6: `CASCADE` auf den RIS-Bestand.** Fremdschlüssel auf den RIS-Bestand sind `PROTECT` oder `SET_NULL`
([Fremdschlüssel auf den RIS-Bestand](adr/20260929-fremdschluessel-ris-bestand.md), #524). Die
Fitnessfunktion prüft alle Modelle zur Laufzeit; ihre Ausnahmeliste ist der Bestand. K6a zählt die
Einträge aus anderen Apps als `insight_core` (seit #524 null), K6b die Daten des Bürgerportals und
abgeleitete Geodaten in `insight_core`, über die eine Entscheidung aussteht.

**K7: import-linter-Ausnahmen.** Verschiedene Einträge in `ignore_imports` der Verträge
(`schichten`, `module-unabhaengig`, `plattform-fachfrei`, `abruf-ein-weg`) ohne die Freistellung der Tests, gelesen vom
Ratchet der Schichtregeln (#567). `--json` nennt die Zahl je Vertrag.

**K8: Ereignis-Rückstand p95.** Nicht aus dem Quellstand erhebbar. Der Wert kommt aus dem Monitoring der
Produktion: `mandari_events_lag_seconds` je Abonnement ([MONITORING.md](MONITORING.md), Abschnitt
„Metriken“), als 95. Perzentil über die sieben Tage vor der Erhebung, davon der höchste Wert über alle
Abonnements, die nicht pausiert sind. Der Rückstand eines pausierten Abonnements wächst bewusst und zählt
nicht, wie bei der Alarmregel für den Rückstand:

```promql
max(
  quantile_over_time(0.95, mandari_events_lag_seconds[7d])
  unless on (subscription) mandari_events_subscription_paused == 1
)
```

War ein Abonnement in den sieben Tagen zeitweise pausiert, steckt diese Zeit im Wert; das steht dann als
Anmerkung an der Verlaufszeile. Ziel ≤ 5 s: Die Nachweise (Abschnitt 2) sagen eine Latenz vom Commit bis zur
Sicht von p95 ≤ 5 s im Normalbetrieb zu. Der Rückstand misst etwas anderes, das Alter des ältesten offenen
Ereignisses je Abonnement, und liegt im Betrieb unter 1 s (Abschnitt 2.4). Ein Rückstand über der zugesagten
Latenz hieße, dass Ereignisse länger als zugesagt bis zur Sicht brauchen; deshalb gilt dieselbe Grenze. Der Alarm
greift erst bei 5 Minuten.

### 5.4 Fortschreibung

**Wann:** nach Abschluss jeder Etappe des Epics #476 und mit dem Aufräumen jeder Umstellung eines Weges
(Punkt in der Checkliste, Abschnitt 4.2.5), weil dort der alte Weg aus dem Code
verschwindet.

**Wie:** im Pull Request, der die Etappe oder Umstellung abschließt, oder in einem eigenen direkt danach.
Der Zweig steht dabei auf dem aktuellen `dev` (`git fetch origin`, dann rebasen oder `origin/dev`
einmischen). Belegt wird der Stand, auf dem der Zweig aufsetzt, zusammen mit der Nummer des Pull Requests:
Den Commit des Zweigs fasst die Merge-Queue zu einem neuen zusammen, den es vorher nicht gibt. So stimmt die
Zeile ohne Nacharbeit nach dem Merge.

1. `python scripts/architektur_kennzahlen.py` ausführen und die Tabelle in Abschnitt 5.1 ersetzen, mit
   Datum, Stand und Anlass in der Zeile darüber.
2. `python scripts/architektur_kennzahlen.py --verlauf --anlass "Etappe 2" --pr <Nummer>` ausführen und die
   letzte Zeile der Ausgabe an die Tabelle in Abschnitt 5.2 anhängen. Die Spalte „Stand“ nennt den Commit auf
   `dev`, auf dem der Zweig aufsetzt (`git merge-base HEAD origin/dev`), und den Pull Request. Ohne `--pr`,
   auf `dev` selbst ausgeführt, nennt sie nur den Commit.
3. K8 aus dem Monitoring ablesen (Abschnitt 5.3) und in der Verlaufszeile statt „–“ eintragen, mit Einheit
   (`0,4 s`).
4. Gestiegene Werte in zwei Sätzen unter Abschnitt 5.1 erklären; `--json` zeigt, welche Funktionen oder
   Dateien dazugekommen sind.
5. Die Zeile aus Abschnitt 5.2 als Kommentar in das Issue der Etappe bzw. Umstellung kopieren.

Kommt eine Kennzahl hinzu oder ändert sich eine Zählregel, bekommt die Verlaufstabelle eine neue Spalte
bzw. die Regel einen Vermerk mit Datum in Abschnitt 5.3; ältere Zeilen bleiben, wie sie erhoben wurden.
`mandari/apps/common/tests/test_architektur_kennzahlen.py` hält Tabellen und Skript beieinander.
