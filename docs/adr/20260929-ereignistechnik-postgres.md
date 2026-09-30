# Ereignistechnik in PostgreSQL: Outbox-Journal und Worker, kein Broker

- Status: angenommen
- Datum: 2026-09-29
- Issue: #476
- Paket: Datendrehscheibe, A2
- Hängt ab von: [A1 Schichtenmodell](20260929-schichtenmodell.md)

## Kontext

Modulübergreifende Folgen laufen heute über Django-Signale, direkte Schreibzugriffe in fremde
Tabellen und einen HTTP-Abruf der eigenen Installation. Hintergrundarbeit läuft als Thread im
Webprozess, per Cron auf dem Host oder synchron in der Anfrage. Ein Redis-Kanal, auf dem der
Ingestor Änderungen meldet, hat keinen Empfänger. Es gibt keine Stelle, an der sichtbar ist, was
geschehen ist, was davon verarbeitet wurde und was hängt.

Anforderungen: Kein Ereignis darf verloren gehen oder vor dem Commit sichtbar werden. Eine
Installation muss weiterhin auf einem Server laufen, auch bei Kommunen im eigenen Rechenzentrum.
Derselbe Strom soll später den öffentlichen Änderungsfeed speisen
([Feed-Format](20260929-aenderungsfeed-format.md)).

## Entscheidung

1. **Journal als transaktionale Outbox.** Die Tabelle `events_event` liegt in der vorhandenen
   PostgreSQL-Datenbank. `publish()` schreibt das Ereignis in derselben Transaktion wie die
   fachliche Änderung und wirft eine Ausnahme, wenn kein `transaction.atomic()`-Block offen ist.
   Der Ingestor schreibt über eine kleine Hilfe in seiner SQLAlchemy-Transaktion in dieselbe
   Tabelle.
2. **Kein Broker.** Kein Kafka, NATS oder RabbitMQ. Redis bleibt Cache und Channels-Layer und trägt
   keine Ereignisse. Ein Broker kommt nur per neuem ADR und erst, wenn mehrere Knoten ihn
   nachweislich brauchen.
3. **Hülle.** `event_id`, `type`, `version`, `aggregate_type` und `aggregate_id` (kanonisch, siehe
   [Kanonisches Modell](20260929-kanonisches-modell.md)), `tenant_ref`, `body_id`, `visibility`
   (Pflicht), `operation`, `occurred_at`, `actor_ref` (Kennung, kein Name), `correlation_id`,
   `causation_id`, `payload`.
4. **Minimale Nutzlast.** Kennungen und die Namen geänderter Felder. Nie entschlüsselte Inhalte,
   Dateiinhalte, Passwörter, Tokens, Mailadressen oder Klarnamen außerhalb öffentlicher
   Mandatsträgerdaten. Empfänger holen Inhalte mit Rechteprüfung beim Eigentümer.
5. **Fachlicher Anlass.** Ereignisse entstehen in Fachfunktionen, nie aus `post_save`-Signalen
   ohne fachlichen Anlass.
6. **Reihenfolge.** Die Folgenummer `seq` vergibt nach dem Commit ein Sequenzierer
   ([Sequenzierer](20260929-sequenzierer.md)).
7. **Zustellung.** Abonnements haben einen Namen, Typmuster und einen Cursor. Der Worker liest
   `seq > cursor` in Batches und ruft den Handler in `seq`-Reihenfolge auf. Zustellung mindestens
   einmal, jeder Handler ist idempotent.
   - Sichten in der Datenbank: Handler und Cursor-Fortschritt in **einer** Transaktion; der Effekt
     tritt genau einmal ein.
   - Externe Effekte: Idempotenz im Handler, z. B. Suchindex mit externer Version gleich `seq`,
     Mail mit Schlüssel aus `event_id` und Empfänger.
   - Fehler: Das Ereignis wird je Abonnement geparkt und mit wachsender Wartezeit wiederholt, nach
     acht Versuchen als tot markiert und gemeldet. Folgeereignisse desselben Aggregats werden
     mitgeparkt, andere Aggregate laufen weiter.
   - Schattenbetrieb für Umstellungen und Nachspielen ab `seq` oder Datum.
8. **Weckruf.** Ein Trigger ruft je Statement `pg_notify` auf. Der Worker hört per `LISTEN` auf
   einer Direktverbindung und fragt zusätzlich alle zwei Sekunden ab.
9. **Worker.** Ein Container aus demselben Image (`manage.py events_worker`) mit den Rollen
   Sequenzierer, Zustellung, Aufträge und Zeitpläne
   ([Aufträge und Zeitpläne](20260929-auftraege-und-zeitplaene.md)). Leader-Rollen laufen über
   eine Lease-Tabelle; sitzungsgebundene Advisory-Locks sind verboten, damit ein Verbindungspooler
   im Transaktionsmodus möglich bleibt. Bei SIGTERM beendet der Worker den Batch und schreibt den
   Cursor fest. Heartbeat und Metriken sind abrufbar.
10. **Aufbewahrung und Datenschutz.** Journal mindestens 90 Tage, danach werden Monatspartitionen
    gelöscht; partitioniert wird ab einer Größenschwelle. Bei DSGVO-Löschung veröffentlicht der
    Eigentümer `redact`, personenbezogene Nutzlasten im Journal werden neutralisiert.
11. **Audit bleibt synchron** in der fachlichen Transaktion. Das Journal ist kein Audit-Log.
12. **Umstellung** je Konsument: Schattenbetrieb, Vergleich, Umschalten per Einstellung, Entfernen
    des alten Weges erst im Folge-Release.

**Qualitätsziele:** 0 verlorene Ereignisse; Commit bis Sicht p95 ≤ 5 s im Normalbetrieb;
≥ 100 Ereignisse/s je Abonnement; Wiederanlauf nach Worker-Absturz ≤ 60 s ohne Handarbeit.

## Alternativen

- **Broker (Kafka, NATS JetStream, RabbitMQ).** Ein weiterer Dienst in jeder Installation mit
  Betrieb, Härtung und Sicherung. Das Problem des doppelten Schreibens (Datenbank und Broker)
  bliebe und bräuchte trotzdem eine Outbox. Verworfen bis zum nachgewiesenen Bedarf.
- **Redis Streams oder Pub/Sub.** Redis ist in mandari ein flüchtiger Cache, das Schreiben läge
  außerhalb der Datenbanktransaktion, Pub/Sub verliert Nachrichten ohne Empfänger. Verworfen.
- **Signale mit `transaction.on_commit`.** Der Rückruf ist nicht Teil der Transaktion; stürzt der
  Prozess zwischen Commit und Rückruf ab, ist das Ereignis verloren. Kein Nachspielen, kein
  Rückstandsbild. Verworfen.
- **Change Data Capture aus dem WAL (logische Dekodierung, Debezium).** Braucht `wal_level=logical`,
  Replikationsslots und weitere Dienste und liefert Tabellenänderungen statt fachlicher
  Ereignisse. Verworfen.
- **Ein Auftrag je Empfänger statt eines Ereignisstroms.** Kein gemeinsamer Cursor, kein Feed,
  kein Nachspielen, und der Erzeuger müsste seine Empfänger kennen. Verworfen; Aufträge bleiben
  ein eigener Mechanismus für technische Arbeit.
- **Event Sourcing.** Nicht nötig: Die Fachtabellen bleiben die Wahrheit, das Journal ist der
  Integrationsstrom zwischen Modulen.

## Folgen

**Positiv**

- Kein neues Infrastrukturteil; eine Installation bleibt ein Server mit einem Container mehr.
- Ereignis und Änderung sind atomar; nichts geht verloren, nichts ist vor dem Commit sichtbar.
- Ein Bild für Rückstand, Fehler und tote Ereignisse; Sichten sind jederzeit neu aufbaubar.
- Grundlage für Suchindex, Benachrichtigungen, Änderungsfeed, Webhooks und Adapter.

**Negativ**

- Jeder Handler muss idempotent sein; das braucht Disziplin und Tests.
- Folgen zwischen Modulen sind kurz verzögert.
- PostgreSQL trägt zusätzliche Schreiblast; das Journal wächst und braucht Aufbewahrung und später
  Partitionierung.
- Ein zusätzlicher Container. Die Update-Reihenfolge lautet Migration, Worker, Web. Ohne laufenden
  Worker meldet der Health-Check `degraded`, und der Admin zeigt einen Hinweis.

## Prüfung (Fitnessfunktion)

- Tests: `publish()` ohne `atomic` wirft; jedes Abonnement verarbeitet jedes Ereignis zweimal ohne
  abweichenden Zustand; Absturztest mitten im Batch ohne Verlust und ohne doppelte
  Datenbankeffekte; Nachspieltest (Sicht löschen, nachspielen, Hash-Vergleich); Lasttest mit
  Vollsynchronisation einer großen Kommune.
- Monitoring: `mandari_events_lag_seconds` je Abonnement, `mandari_events_parked` nach Zustand,
  Heartbeat des Workers. Alarm bei Rückstand über fünf Minuten und bei mehr als null toten
  Ereignissen.
- Kennzahl: Hintergrundjobs im Webprozess gleich null (siehe
  [Aufträge und Zeitpläne](20260929-auftraege-und-zeitplaene.md)).

## Nachtrag zur Umsetzung der Zustellung (#504)

Umgesetzt in `apps/events/registry.py` (`@subscriber`) und `apps/events/dispatch.py`, Befehl
`manage.py events_dispatch`. Die Entscheidung bleibt; präzisiert bzw. ergänzt wurde:

- **Handler-Signatur** `handler(events, delivery)`: `delivery` nennt Abonnement, Schattenbetrieb
  (`delivery.shadow`) und ob es eine Wiederholung ist. Ohne diese Angabe könnte ein Handler im
  Schattenbetrieb nicht in sein Schattenziel schreiben.
- **Datenbank-Sicht oder externer Effekt** wählt `@subscriber(..., transactional=True|False)`.
  Bei `True` sperrt ein Lauf die Zeile des Abonnements und ruft den Handler in einem
  Sicherungspunkt derselben Transaktion auf, in der Parken und Cursor festgeschrieben werden; zwei
  gleichzeitige Zusteller warten aufeinander, der Effekt tritt genau einmal ein. Weil diese
  Transaktion eine Transaktionskennung hält, hält sie den Sequenzierer für ihre Dauer auf; Handler
  von Sichten müssen kurz sein. Bei `False` läuft der Handler außerhalb einer Transaktion, und der
  Cursor wird nur festgeschrieben, wenn er unter Zeilensperre noch derselbe ist. Welche Objekte
  geparkt sind, liest ein solcher Lauf ohne Sperre; wurde das erste Ereignis einer Kette
  inzwischen zugestellt oder verworfen, rückt beim Festschreiben das nächste nach, sonst bliebe
  die Kette ohne Kopf liegen.
- **Ziel nicht erreichbar:** Wirft der Handler `TargetUnavailableError`, wird nichts geparkt und
  kein Versuch gezählt; die Schleife pausiert mit wachsender Wartezeit (5 s bis 5 min) und stellt
  denselben Batch erneut zu. Sonst würde ein Ausfall etwa des Suchindex jedes Objekt parken und
  nach acht Versuchen für tot erklären.
- **Wartezeiten** nach dem 1. bis 7. Fehlversuch: 10 s, 30 s, 2 min, 10 min, 1 h, 3 h, 6 h (zusammen
  gut zehn Stunden); der achte Fehlversuch macht das Ereignis `tot`. Gescheiterte Batches werden
  einzeln zugestellt, damit nur die fehlerhaften Ereignisse zurückbleiben.
- **Kette je Objekt:** Die geparkten Ereignisse eines Objekts sind nach Folgenummer geordnet; nur
  das erste ist `wiederholen` oder `tot`, alle weiteren `blockiert`. Ist das erste zugestellt oder
  verworfen, rückt das nächste nach und wird sofort zugestellt. Eingriffe (`--retry-parked`,
  `--discard-parked`, später die Admin-Seite) gehen über dieselben Funktionen, ein Folgeereignis
  lässt sich nicht vorziehen.
- **Beginn eines neuen Abonnements:** am Ende des Journals, weil Sichten beim Umschalten einmal aus
  dem Bestand gebaut werden; `from_beginning=True` beginnt am Anfang. `shadow=True` legt es im
  Zustand `schatten` an. Danach gilt die Zeile in `events_subscription`.
- **Eine Leader-Lease je Abonnement** (`dispatch:<name>`) statt einer für die ganze Zustellung:
  Mehrere Worker teilen sich die Abonnements, nach Warteschlange wählbar
  (`events_dispatch --queues`). Für die Korrektheit sorgen Zeilensperre und Vergleich.
- **Datenbankverbindungen:** Im Dauerbetrieb läuft je Abonnement ein Faden. Er gibt seine
  Verbindung vor jedem Warten zurück (mit Verbindungspool an den Pool), die Zahl belegter
  Verbindungen hängt also an der gleichzeitigen Arbeit, nicht an der Zahl der Abonnements.
- **Alarm bei toten Ereignissen:** Fehlerprotokoll, `mandari_events_dead_total` und
  `mandari_events_parked{state="tot"}`, dazu sofort die Alarmmail der Dienstgüteprüfung
  (`check_service_levels`, höchstens eine je Abonnement und Tag).

## Nachtrag zur Umsetzung des Weckrufs (#505)

Umgesetzt in `apps/events/wakeup.py`, eingebunden in `events_sequencer` und `events_dispatch`
(abschaltbar mit `--no-listen`):

- **Ein Listener je Prozess** hört auf `mandari_events` (weckt den Sequenzierer) und
  `mandari_events_seq` (weckt die Zustellung). Die Schleifen fragen weiterhin ab, der
  Sequenzierer jede Sekunde, die Zustellung alle zwei Sekunden; so bleibt die Latenz auch ohne
  Weckruf unter fünf Sekunden.
- **Direktverbindung nur für `LISTEN`:** `EVENTS_DB_DIRECT_URL`, sonst die Verbindungsdaten von
  `DATABASE_URL`. Die Spezifikation nannte die Direktverbindung auch für die Leader-Leases; das
  ist nicht nötig und wäre falsch. Leases sind Zeilen in `events_lease`, `fence()` prüft und
  sperrt sie in der Transaktion der Arbeit; liefe die Lease über eine andere Verbindung, wäre die
  Abgrenzung nicht mehr Teil derselben Transaktion.
- **Selbstprüfung:** Nach dem Verbinden und alle 30 s schickt der Listener über die
  Standardverbindung ein `NOTIFY` an sich selbst. Bleibt es aus (PgBouncer im
  Transaktionsmodus, abgerissene Verbindung), warnt er, baut neu auf bzw. versucht es nach
  fünf Minuten erneut; `mandari_events_listener_up` zeigt den Zustand. Nach jedem Neuaufbau weckt
  er alle Schleifen, weil Meldungen verloren sein können.
- **Nachweis:** Die CI führt die Tests von `apps/events` zusätzlich hinter PgBouncer im
  Transaktionsmodus aus (Job „Ereignistechnik hinter PgBouncer“), einschließlich der Messung
  Commit → Sicht (p95 ≤ 5 s) und des Nachweises, dass `LISTEN` über den Pooler als wirkungslos
  erkannt wird.

## Bezug

- [A1 Schichtenmodell](20260929-schichtenmodell.md), [A3 Sequenzierer](20260929-sequenzierer.md),
  [A4 Aufträge](20260929-auftraege-und-zeitplaene.md),
  [A5 Verträge](20260929-ereignisvertraege.md),
  [A10 Feed-Format](20260929-aenderungsfeed-format.md)
- [20260909-schema-contract-django-ingestor](20260909-schema-contract-django-ingestor.md): Die
  Journal-Tabelle wird in den Schema-Vertrag aufgenommen.
- `docs/MONITORING.md`
- Transactional Outbox: https://microservices.io/patterns/data/transactional-outbox.html
- Idempotent Consumer: https://microservices.io/patterns/communication-style/idempotent-consumer.html
- PostgreSQL `NOTIFY`: https://www.postgresql.org/docs/current/sql-notify.html
- Django, Transaktionen und `on_commit`: https://docs.djangoproject.com/en/stable/topics/db/transactions/
