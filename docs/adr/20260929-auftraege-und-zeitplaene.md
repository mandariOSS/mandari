# Aufträge und Zeitpläne: eigenes Django-Tasks-Backend auf PostgreSQL

- Status: angenommen
- Datum: 2026-09-29
- Issue: #476
- Paket: Datendrehscheibe, A4
- Hängt ab von: [A2 Ereignistechnik](20260929-ereignistechnik-postgres.md)

## Kontext

mandari nutzt an einigen Stellen die Tasks-Schnittstelle von Django (`@task`, `enqueue()`), aber mit
einem Backend, das sofort in der Anfrage ausführt. Mailversand, DSGVO-Export und
KI-Zusammenfassungen blockieren deshalb Anfragen. Wiederkehrende Arbeit läuft an zwei weiteren
Orten: Ein Thread im Webprozess erledigt fünf Aufgaben (u. a. Aufräumen nach der Synchronisation,
Verortung, Erinnerungen und Einladungen für Fraktionssitzungen); bei mehreren Webprozessen
verhindern nur Cache-Sperren doppelte Läufe. Andere Aufgaben stehen als Cronjobs auf dem Host und
sind je Betriebsart (Compose, Helm, Selbstbetrieb) unterschiedlich oder gar nicht eingerichtet.

Aufträge sind technische Arbeit ohne fachliche Aussage (Mail senden, Text erkennen, indexieren).
Sie unterscheiden sich von Ereignissen: Sie haben genau einen Ausführenden, einen Status, Versuche
und ein Ergebnis und gehören nicht in den Änderungsfeed.

## Entscheidung

**Aufträge**

- Ein eigenes Backend `apps.events.tasks_backend.JournalBackend` für die Tasks-Schnittstelle von
  Django schreibt in die Tabelle `events_task` derselben Datenbank. Es wird per Einstellung
  `TASKS` gewählt; vorhandene `@task`- und `enqueue()`-Aufrufe bleiben unverändert.
- `enqueue()` innerhalb einer Transaktion ist erwünscht: Der Auftrag existiert genau dann, wenn die
  fachliche Änderung festgeschrieben ist.
- Argumente enthalten nur Kennungen, nie verschlüsselte oder entschlüsselte Inhalte.
- Warteschlangen mit Standard-Parallelität: `default` 4, `mail` 2, `index` 2, `ocr` 1, `ai` 1,
  `adapter` 2. Die Texterkennung kann in einem zweiten Worker-Container mit eigener Speichergrenze
  laufen.
- Abholen per `SELECT … FOR UPDATE SKIP LOCKED` mit Sperrfrist `locked_until`; ein hängender
  Auftrag wird nach Ablauf wieder frei. Höchstens acht Versuche mit wachsender Wartezeit, danach
  Status `tot` und Meldung. Optionaler, eindeutiger `idempotency_key`. Zeitgrenze je Auftragstyp.
  Der Worker startet sich nach einer Zahl von Aufträgen oder bei Überschreiten der Speichergrenze
  neu.
- Ausführung im selben Worker-Prozess wie die Ereigniszustellung (Rolle `tasks`), eine Überwachung
  für beides.
- Aufbewahrung: erledigte Aufträge 14 Tage, tote 90 Tage.
- Ereignisse lösen Aufträge aus, indem ein Abonnent `enqueue()` aufruft; Aufträge melden
  fachliche Ergebnisse, falls nötig, über `publish()` beim Eigentümer.

**Zeitpläne**

- Zeitpläne stehen im Code (`apps/events/schedule.py`), z. B. `every(minutes=15)` oder
  `cron("30 3 * * *")`, und sind damit versioniert und in allen Betriebsarten gleich.
- Der Leader `scheduler` (Lease wie in A2) legt fällige Läufe als Aufträge an; bei mehreren Workern
  läuft jeder Plan genau einmal.
- Sie ersetzen den Thread im Webprozess und alle Cronjobs, die Django-Befehle aufrufen. Aufgaben
  des Betriebssystems, etwa die Datensicherung, bleiben außerhalb der Anwendung.

## Alternativen

- **Datenbank-Backend des Pakets `django-tasks` mit eigenem `db_worker`.** Nah am Standard, aber
  ein zweiter Mechanismus neben der Ereigniszustellung mit eigenem Prozess und eigener
  Überwachung; die Reife mit der eingesetzten Django-Version war vorab offen. Verworfen; bleibt
  Rückfalloption, weil beide dieselbe Tasks-Schnittstelle bedienen.
- **Celery, RQ oder Dramatiq mit Redis oder RabbitMQ.** Braucht einen Broker, und das Einreihen ist
  nicht Teil der Datenbanktransaktion (Verlust- oder Geisterauftrag-Fenster). Verworfen.
- **Fremde PostgreSQL-Warteschlangen-Bibliotheken.** Transaktional, aber mit eigenem Schema,
  eigenem Worker und eigener Überwachung neben dem Ereignis-Worker. Verworfen zugunsten eines
  Mechanismus.
- **Host-Cron, systemd-Timer oder Kubernetes-CronJobs** für Anwendungsaufgaben. Je Betriebsart
  verschieden, nicht im Repository versioniert, ohne gemeinsame Überwachung. Verworfen.
- **Threads im Webprozess.** Doppelte Läufe bei mehreren Prozessen, Abbruch bei jedem Neustart,
  Last in den Prozessen, die Anfragen beantworten. Verworfen.
- **Aufträge als Ereignisse im Journal.** Vermischt Integrationsstrom und Arbeitsliste, verlangt
  Status und Versuche im Journal und würde technische Arbeit in den Feed tragen. Verworfen.

## Folgen

**Positiv**

- Anfragen warten nicht mehr auf Mail, KI, Texterkennung oder Indexierung.
- Aufträge entstehen atomar mit der fachlichen Änderung; keine Geisteraufträge nach einem
  Rollback.
- Wiederholung, Zeitgrenzen und Überwachung sind für alle Aufträge gleich.
- Zeitpläne sind im Code sichtbar und laufen in jeder Betriebsart genau einmal.

**Negativ**

- PostgreSQL trägt die Warteschlangenlast; häufige Statuswechsel erzeugen tote Zeilen, Aufräumen
  und Autovacuum sind zu beachten.
- Eigener Code statt einer Bibliothek; wir warten ihn selbst.
- Zustellung mindestens einmal: Jeder Auftrag muss idempotent sein.
- Das Laufzeitverhalten ändert sich sichtbar (Mails und Erinnerungen kommen Sekunden später).
  Umstellungen laufen deshalb mit Schalter und Vergleich.

## Prüfung (Fitnessfunktion)

- Tests: `enqueue()` in einer zurückgerollten Transaktion hinterlässt keinen Auftrag; ein
  hängender Auftrag wird nach `locked_until` erneut ausgeführt; ein Zeitplan läuft mit zwei Workern
  genau einmal; ein doppelter `idempotency_key` führt nicht zu doppelter Ausführung.
- System-Check für `check --deploy`: In Produktionseinstellungen ist nicht das sofort ausführende
  Backend konfiguriert.
- CI-Prüfung: Kein Code außerhalb des Workers startet Threads oder Timer (Suche nach
  `threading.Thread`/`Timer` mit Positivliste).
- Monitoring: `mandari_tasks_queued`, `mandari_tasks_duration_seconds`, `mandari_tasks_failed_total`,
  `mandari_worker_rss_bytes`; Alarm bei toten Aufträgen und bei fehlendem Heartbeat.
- Betriebsdoku: Die Beispiel-Crontab enthält nur noch Aufgaben des Betriebssystems.

## Bezug

- [A2 Ereignistechnik](20260929-ereignistechnik-postgres.md) (Worker, Leases, Überwachung)
- `docs/ENGINEERING_STANDARDS.md`, Abschnitt 7 (Hintergrundarbeit idempotent)
- Django Tasks: https://docs.djangoproject.com/en/stable/topics/tasks/
