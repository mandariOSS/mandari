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

## Nachtrag zur Umsetzung im Ingestor (#513)

Umgesetzt in `ingestor/src/storage/events.py` (Schreiben ins Journal) und
`ingestor/src/storage/ris_events.py` (welches Ereignis aus welcher Änderung entsteht). Die
Entscheidung bleibt; präzisiert wurde:

- **Eine Transaktion je Objekt:** Der Ingestor schreibt jedes Objekt in einer eigenen Transaktion.
  Das Ereignis wird in derselben Sitzung nach dem Upsert bzw. der Löschmarkierung und vor dem
  Commit geschrieben. Scheitert das Ereignis, bleibt auch die Änderung aus. Mehrere Ereignisse
  einer Transaktion gehen als eine Anweisung in die Datenbank.
- **Zuordnungen gehören zur Änderung:** Die Gremien einer Sitzung (`oparl_meetings_organizations`)
  und die Orte einer Vorlage (`oparl_papers_locations`) schreibt der Upsert in derselben
  Transaktion, vor dem Ereignis. Eingebettete Orte einer Vorlage stehen vorher im Bestand. Wer auf
  ein Ereignis hin beim Bestand nachliest, sieht das Objekt mit seinen Zuordnungen; bricht der
  Ingestor ab, gibt es weder Zeile noch Zuordnung noch Ereignis. Ein Fehler beim Zuordnen nimmt den
  Upsert zurück. Fehlt die Tabelle der Ortszuordnung (Migration nicht eingespielt), entfällt nur
  die Zuordnung; das wird vorab geprüft. Eingebettete Tagesordnungspunkte, Dateien und Beratungen
  folgen wie bisher in eigenen Transaktionen mit eigenen Ereignissen.
- **Schalter:** `INGESTOR_EVENTS_ENABLED`, Standard aus. Eingeschaltet wird erst, wenn die
  Migrationen der Ereignistechnik eingespielt sind und der Sequenzierer läuft; sonst sammeln sich
  Ereignisse ohne Folgenummer, und `mandari_events_sequencer_lag_seconds` wächst. Ausgeschaltet
  läuft jeder Upsert ohne Abfrage des bisherigen Stands. Der Schalter gilt auch für die Anwendung:
  Markiert sie selbst eine Zeile des Bestands (Session nimmt ein Objekt sofort zurück), meldet sie
  die Rücknahme mit denselben Ereignissen (`hub.ris.retraction`, Issue #707).
- **Ausnahme je Quelle:** `sync_config["events_enabled"] = false` an einer Quelle nimmt nur sie
  aus; ihre Upserts laufen dann wie ausgeschaltet. Gedacht für eine Quelle, die bei jedem
  Vollabgleich Änderungen meldet, obwohl sich nichts geändert hat (sie liefert eingebettete und
  einzeln abgerufene Objekte unterschiedlich). Erkennbar ist sie an
  `mandari_ingestor_events_published_total{type, source}` (`source` ist der Name der Kommune). Die
  Ausnahme wirkt ab dem nächsten Abgleichzyklus. Was sich währenddessen ändert, erfahren die
  Empfänger nicht; nach dem Aufheben meldet der Ingestor nur, was sich von da an ändert.
- **Nur echte Änderungen:** Vor dem Upsert liest der Ingestor den bisherigen Stand der Zeile mit
  Zeilensperre und vergleicht das Objekt der Quelle Feld für Feld. `created`, `modified` und der
  Content-Hash zählen auf keiner Ebene, ebenso wenig der Rückverweis eines eingebetteten Objekts
  auf sein übergeordnetes (`meeting` am Tagesordnungspunkt, `paper` an der Beratung,
  `paper`/`meeting`/`agendaItem` an der Datei): Ein Vollabgleich ohne Änderung schreibt kein
  Ereignis. Die Sperre gilt bis zum Commit; schreiben zwei Abgleiche dasselbe Objekt gleichzeitig,
  vergleicht der zweite mit dem Stand des ersten. Der Upsert selbst bleibt, wie er war.
- **Sperren:** Gesperrt wird mit `FOR NO KEY UPDATE`, so stark wie der Upsert selbst. Einfügungen
  mit Fremdschlüssel auf das Objekt (Tagesordnungspunkte, Dateien, Beratungen, Zeilen aus Django)
  warten darauf nicht und halten den Abgleich nicht auf. Ein neues Objekt hat noch keine Zeile;
  dafür nimmt der Abgleich eine Sperre auf die Kennung bis zum Ende der Transaktion
  (`pg_advisory_xact_lock`, verträglich mit einem Pooler im Transaktionsmodus) und liest noch
  einmal. Schreiben zwei Abgleiche dasselbe neue Objekt gleichzeitig, meldet es nur der erste als
  neu.
- **Zuordnungen zählen als Änderung:** Liefert die Quelle dasselbe Objekt, aber der Abgleich
  ordnet erstmals zu, ist das eine Änderung: Eine Datei hängt erstmals an einer Vorlage oder
  Sitzung (`ris.file.changed` mit `added` und der neuen Zugehörigkeit), ein Gremium der Sitzung
  oder ein Ort der Vorlage steht erst jetzt im Bestand (`ris.meeting.changed` bzw.
  `ris.paper.changed` mit `organization` bzw. `location`). Der Wechsel einer Datei von einer
  Vorlage zu einer anderen zählt nicht: Hängt sie an mehreren, trägt die Zeile die zuletzt
  abgeglichene.
- **Ereignisse:** `ris.meeting.scheduled` und `ris.meeting.changed`, `ris.paper.released` und
  `ris.paper.changed`, `ris.agendaitem.changed` (`added`, `changed`, `moved`, `deleted`),
  `ris.consultation.changed` (`added`, `scheduled`, `changed`), `ris.file.changed` (`added`,
  `replaced`, `renamed`) und für die Löschmarkierung jedes Typs `ris.object.depublished` mit
  Grund `quelle_geloescht` und Operation `delete`. Ein nach einer Löschmarkierung wieder
  geliefertes Objekt gilt als neu. Für Änderungen an Gremien, Personen, Mitgliedschaften, Orten,
  Wahlperioden und Kommunen gibt es noch keinen Vertrag; sie melden nur ihre Löschmarkierung.
- **Hülle:** Mandant ist die Quelle (`source:<uuid>`), `body_id` die Kommune, Auslöser
  `system:ingestor`. `occurred_at` ist der Änderungszeitpunkt laut Quelle, sofern er nicht in der
  Zukunft liegt, sonst der Zeitpunkt des Abgleichs. Alle Ereignisse des Abgleichs einer Kommune
  tragen dieselbe Korrelations-ID. Die Hülle wird wie in Django am Format geprüft.
- **Keine Folgenummer:** Die Tabellenbeschreibung des Ingestors kennt `seq`, `xid` und
  `recorded_at` nicht. `events_event` steht im Schema-Vertrag
  ([20260909-schema-contract-django-ingestor](20260909-schema-contract-django-ingestor.md)).
- **Sichtbarkeit:** Was der Ingestor liest, hat die Quelle veröffentlicht, die Ereignisse sind
  `oeffentlich`. Ausnahme sind Tagesordnungspunkte mit `public: false`: Ihre Ereignisse sind
  `nichtoeffentlich`. Wird ein bisher öffentlicher Punkt nichtöffentlich, meldet zusätzlich
  `ris.object.depublished` (Grund `nichtoeffentlich`) die Rücknahme an öffentliche Empfänger; wird
  er öffentlich, erscheint er ihnen als `added`. Löscht die Quelle einen nichtöffentlichen Punkt,
  gibt es keine öffentliche Rücknahme (öffentliche Empfänger haben ihn nie gesehen, das Ereignis
  nennte ihnen erstmals seine Kennung), sondern `ris.agendaitem.changed` mit `deleted`,
  Sichtbarkeit `nichtoeffentlich` und Operation `delete`. Nutzlasten enthalten nie Inhalte, nur
  Kennungen, Codes und Namen geänderter Felder.
- **Vertragstreue ohne Register:** Der Ingestor hat keinen Zugriff auf `hub.contracts`. Ein
  Vertragstest der Drehscheibe (`hub/contracts/tests/test_ingestor_events.py`) lädt die Abbildung
  des Ingestors und prüft jedes Ereignis, das sie bilden kann, gegen die ausgelieferten Schemas; er
  läuft bei Änderungen am Ingestor wie an den Schemas.
- **Last:** Eingeschaltet kostet jeder Upsert eine zusätzliche Abfrage (bisheriger Stand), bei
  einer bekannten Sitzung eine weitere (bisherige Gremien) und bei echter Änderung ein INSERT. Ein
  neues Objekt kostet zwei Abfragen mehr (Sperre auf die Kennung, erneutes Lesen). Gemessen mit
  7 800 Upserts je Abgleich auf einem Arbeitsplatzrechner, ein Schreiber: ohne Änderung bis etwa
  0,5 ms je Upsert zusätzlich, im Erstabgleich einer Kommune (jedes Objekt neu, je Objekt ein
  Ereignis) rund 1,5 bis 2,5 ms. Das Journal wächst um rund 540 Bytes je Ereignis einschließlich
  Indizes. Im Betrieb bestimmt der Abruf bei der Quelle die Dauer eines Abgleichs.

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
  er alle Schleifen, weil Meldungen verloren sein können. Lässt sich die Prüfung gar nicht senden
  (Standardverbindung gestört, etwa kurz nach einem Neustart der Datenbank), ist das kein Befund
  über das Lauschen: Er versucht es mit wachsender Pause (1 s bis 60 s) erneut, ohne
  PgBouncer-Hinweis.
- **Protokolle** nennen bei Datenbankfehlern des Weckrufs nur Fehlerklasse und SQLSTATE, nie die
  Meldung: libpq nennt darin bei Verbindungsfehlern Host, Port und Benutzernamen.
- **Nachweis:** Die CI führt die Tests von `apps/events` zusätzlich hinter PgBouncer im
  Transaktionsmodus aus (Job „Ereignistechnik hinter PgBouncer“), einschließlich der Messung
  Commit → Sicht (p95 ≤ 5 s) und des Nachweises, dass `LISTEN` über den Pooler als wirkungslos
  erkannt wird.

## Nachtrag zur Umsetzung von `publish()` (#502)

Umgesetzt in `apps/events/publishing.py`, exportiert als `apps.events.publish`. Die Entscheidung
bleibt; präzisiert wurde:

- **Aufruf:** `publish(typ, version=…, aggregate=CanonicalRef("Paper", id), tenant=tenant_ref("session", id),
  visibility=…, payload=…, body_id=…)`, wahlweise mit `operation`, `occurred_at`, `actor_ref`,
  `correlation_id` und `causation_id`. Mandant und Sichtbarkeit sind Pflicht und kommen nie aus
  einem Kontext: Ein falscher Mandant wäre ein Fehler der Mandantentrennung.
- **Transaktionsprüfung:** Ohne offenen `transaction.atomic()`-Block wirft `publish()`
  `PublishOutsideTransactionError`. Der Block, den Django-Tests um jeden Test legen, zählt dabei
  nicht (wie bei `atomic(durable=True)`); ein fehlendes `atomic()` fällt so im Test auf und nicht
  erst im Betrieb.
- **Korrelation:** in dieser Reihenfolge: Angabe am Aufruf, `event_context()`, Request-Kennung der
  laufenden Anfrage (`X-Request-ID`, `apps.common.observability`), Trace-Kennung des
  OpenTelemetry-Kontexts, sonst eine neue Kennung je Ereignis. Eine Request-Kennung, die selbst eine
  UUID ist, wird übernommen; aus jeder anderen wird eine feste UUID abgeleitet
  (`correlation_id_for_request`), sodass Logzeilen und Ereignisse einer Anfrage zusammenfinden.
- **Auslöser:** `actor_ref` ist `user:<uuid>` oder `system:<auftrag>` und wird immer am Format
  geprüft; ein Name oder eine Mailadresse wird abgelehnt. Ohne Angabe gilt `event_context()`, sonst
  das angemeldete Konto der laufenden Anfrage, sonst bleibt das Feld leer.
- **`event_context()`** setzt Korrelation, Auslöser und auslösendes Ereignis für alle
  `publish()`-Aufrufe eines Blocks, auch in aufgerufenen Funktionen. Aufträge und Befehle ohne
  Anfrage geben damit allen Ereignissen eines Vorgangs dieselbe Korrelation
  (`event_context(actor_ref=system_ref("abgleich"))`); Handler setzen für Folgeereignisse
  `event_context(caused_by=ereignis)`. Der Befehls-Dispatcher (`hub.commands`) legt den Kontext um
  jeden Handler: Ereignisse eines Befehls tragen dessen `correlation_id` und `actor_ref`.
- **Formatprüfung der Hülle, immer:** Typname, Version, Objekttyp, Kennungen, Mandant, Sichtbarkeit,
  Operation und Zeitzone. Das sind Vergleiche gegen feste Muster ohne Schema; sie gelten auch im
  Betrieb. Meldungen nennen das Feld und die Regel, nie den Wert.
- **Vertragsprüfung in Tests und bei `DEBUG`:** Mit `EVENTS_VALIDATE_CONTRACTS` (Standard: wie
  `DEBUG`; die Testeinstellungen schalten ein, weil die CI mit `DEBUG=false` läuft) prüft
  `publish()` Hülle, Typ und Version, Sichtbarkeit und Nutzlast gegen das Register, bevor die Zeile
  geschrieben wird. Die Plattform importiert die Drehscheibe nicht
  ([Schichtenmodell](20260929-schichtenmodell.md)): `hub.contracts` hängt seine Prüfung beim Start
  über `set_contract_validator()` ein. Ist die Prüfung eingeschaltet, aber nichts eingehängt, ist
  das ein Konfigurationsfehler; die Prüfung bleibt nie still aus.
- **Folgenummer:** `publish()` vergibt keine; das zurückgegebene Ereignis hat `seq = None`.

## Nachtrag zur Überwachung (#510)

- **Rückstand je Abonnement** (`mandari_events_lag_seconds{subscription}`): das Alter ab Erfassung
  des ältesten nummerierten Ereignisses hinter dem Cursor, das zu den Typmustern des Abonnements
  passt. Beim Abruf von `/metrics/` aus der Datenbank gemessen, nur für Abonnements, die im Code
  registriert sind; eine Zeile ohne Handler würde sonst dauerhaft alarmieren.
  `mandari_events_subscription_paused` nimmt pausierte Abonnements vom Alarm aus.
- **Veröffentlichte Ereignisse** (`mandari_events_published_total{type}`) zählt der Sequenzierer beim
  Vergeben der Folgenummer. Nur er sieht jedes festgeschriebene Ereignis genau einmal, auch die des
  Ingestors. `publish()` könnte nur die eigenen zählen; eine Zählung über das ganze Journal bei jedem
  Abruf wäre bei wachsendem Journal zu teuer und bei mehreren Webprozessen mehrfach.
- **Admin-Seite** (`apps/events/admin.py`) nur für Superuser, mit Eingriffen über die Funktionen der
  Zustellung (`set_state`, `retry_parked`, `discard_parked`). Jeder Eingriff steht in derselben
  Transaktion im Sicherheitsprotokoll (`SecurityAuditLog`, Ereignis `betrieb`). Geparkte Ereignisse
  erscheinen als Ketten je Objekt (Kopf mit Zahl der Folgeereignisse).
- **Alarmregeln** als Prometheus-Regeln (`deploy/monitoring/prometheus-alerts.example.yml`,
  `docs/MONITORING.md`): Rückstand über fünf Minuten, tote Ereignisse, Sequenzierer-Stau über fünf
  Minuten, dazu Hinweise auf viele blockierte Ereignisse und einen gestörten Weckruf.
- **Offen:** Nachspielen eines Abonnements ab einer Folgenummer und die Push-Prüfung „Worker lebt“.
  Heartbeat und `/metrics` im Worker-Prozess bringt der folgende Nachtrag (#508).

## Nachtrag zur Umsetzung des Workers (#508)

Umgesetzt in `apps/events/worker.py`, Befehl `manage.py events_worker`. Die Entscheidung bleibt;
präzisiert wurde:

- **Rollen als Fäden:** Jede Rolle (`sequencer`, `dispatch`, `tasks`, `scheduler`) läuft mit
  derselben Schleife wie im Einzelbefehl in einem eigenen Faden und erneuert ihre Lease selbst.
  Ein Listener je Prozess weckt Sequenzierer und Zustellung. `--roles` und `--queues` wählen aus;
  `--queues` gilt für Aufträge und Abonnements. Die Einzelbefehle bleiben für die Fehlersuche.
- **Neustart des Runners ohne Stillstand:** Will der Runner neu starten (Zahl der Aufträge,
  Speichergrenze, Zeitgrenze), wartet er auf seine laufenden Aufträge, während Zeitpläne,
  Sequenzierer und Zustellung in ihren Fäden weiterarbeiten. Erst danach endet der Prozess wie bei
  SIGTERM und ersetzt sich per `exec`; die Leases sind dann frei.
- **SIGTERM:** Der laufende Batch wird festgeschrieben, bei der Zustellung samt Cursor; laufende
  Aufträge dürfen bis `--shutdown-timeout` (20 s) zu Ende laufen und werden danach freigegeben.
- **Heartbeat:** Jede Schleife meldet jeden Durchlauf. Nur wenn jede Rolle lebt und sich innerhalb
  von `--stale-after` (300 s) gemeldet hat, erneuert der Worker die Heartbeat-Datei und seine Zeile
  in `events_worker` (`apps/events/presence.py`); sonst veralten beide, und `/health` antwortet
  503. Die Frist ist großzügig, weil ein Neustart einen langen Batch nur wiederholen würde.
- **Metriken** liefert der Worker auf einem eigenen Port (`/metrics`, Standard 9091) mit denselben
  Zugriffsregeln wie die Anwendung, dazu `mandari_worker_role_up{role}`.
- **Verbindungsbudget:** Fäden der Rollen, Ausführungsplätze des Runners, Selbstprüfung des
  Listeners, `/metrics` und Hauptfaden plus Reserve; der Worker vergrößert seinen Pool darauf
  (`DEPLOYMENT.md`, Abschnitt Datenbankverbindungen).

## Nachtrag zur Umsetzung in Session (#533, #534, #535)

Umgesetzt in `apps/session/hub_events.py` (Erfassung in den Fachfunktionen) und
`hub/ris/session_events.py` (Ereignisse aus Zuständen, Eigentümer der Verträge `ris.*`); Überblick in
`docs/SESSION_EREIGNISSE.md`. Die Entscheidung bleibt; präzisiert wurde:

- **Fachlicher Anlass als Zustandsvergleich:** Eine Fachfunktion nennt vor der Änderung die betroffenen
  Objekte (`hub_events.track`), am Ende desselben Blocks vergleicht die Drehscheibe den Zustand im
  kanonischen Modell vorher und nachher. Ereignisse entstehen genau für geänderte Objekte, eines je Objekt
  und Empfängerkreis; Änderungen außerhalb des kanonischen Modells ergeben keines. Signale schreiben nie
  Ereignisse.
- **Schalter mit Schattenbetrieb:** `SESSION_EVENTS` (`aus`, `schatten`, `aktiv`), je Mandant
  überschreibbar. Im Schatten läuft das Schreiben in einem Sicherungspunkt, ein Fehler lässt die
  Änderung bestehen; erst `aktiv` gibt die Garantie „Ereignis und Änderung atomar“.
- **Herkunft:** Mandant `session:<uuid>`, `body_id` die kanonische Kennung des Body der
  Session-Schnittstelle; so liest der Änderungsfeed die Ereignisse beider Erzeuger unter derselben
  Kommune.
- **Abhängige Objekte:** Mit einer Vorlage beobachtet die Erfassung ihre Stationen, Anlagen und TOPs, mit
  einer Sitzung ihre Anlagen und Beratungen; deren Sichtbarkeit folgt dem Träger, Rücknahmen ziehen sie mit.
- **Abstimmung ohne Adresse:** Session führt je TOP eine Abstimmung; ihre Kennung bildet sich wie jede
  kanonische aus der Adresse des TOP mit dem Zusatz `voting`. Die Rücknahme eines Ergebnisses meldet
  `ris.object.depublished` der Abstimmung (`zurueckgenommen`) und die Änderung des TOP.

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
