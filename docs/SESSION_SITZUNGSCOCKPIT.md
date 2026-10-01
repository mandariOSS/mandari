# Sitzungscockpit (Session)

Issue: #140. Grundlagen: #138 (Sitzungsformat, Landesprofile), #139 (Teilnahmeart, Störungen).
Nicht enthalten: Selbst-Abstimmung am eigenen Gerät (#141).

## Zweck

Sitzungsleitung und Protokollführung steuern die laufende Sitzung auf einer Seite; alle anderen mit
Sichtrecht verfolgen denselben Stand als **Mitlese-Ansicht**. Die Zeitmarken landen ohne Nacharbeit in der
Niederschrift.

Aufruf: Sitzungsseite → „Cockpit“ (mit Steuerungsrecht) bzw. „Live-Ansicht“,
`/session/<mandant>/meetings/<sitzung>/cockpit/`.

## Funktionen

| Bereich | Verhalten |
|---------|-----------|
| Sitzung | Eröffnen (tatsächlicher Beginn, Status „Laufend“), schließen (Ende, Status „Abgeschlossen“), fortsetzen. Beim Schließen enden der laufende TOP und andauernde Störungen mit der aktuellen Uhrzeit. |
| TOP | Aufrufen füllt `start_time`, ein neuer Aufruf beendet den laufenden TOP (`end_time`). „Weiter mit TOP …“ springt zum nächsten offenen, nicht abgesetzten TOP. Der erste Aufruf eröffnet die Sitzung. Wiederaufruf behält den Beginn. |
| Anwesenheit | Anwesend, kommt (verspätet, mit Uhrzeit), geht (vorzeitig, mit Uhrzeit), zurück, abwesend. „Zurück“ vermerkt die Unterbrechung als Zeitraum (`SessionAttendance.interruptions`), auch mehrfach; die Niederschrift nennt sie im Teilnahmevermerk („abwesend 18:30–18:50 Uhr“, bei Zugeschalteten „getrennt …“). Beschlussfähigkeit live, für den aufgerufenen TOP nach den Regeln des Landesprofils. |
| Störungsprotokoll | Störung Zugeschalteter „ab jetzt“ mit Ursache und internem Vermerk (z. B. „telefonisch gemeldet“ als zweiter Meldeweg), Ende „jetzt“. Während der Störung zählt die Person nicht zur Beschlussfähigkeit. |
| Abstimmung | Öffnen zum aufgerufenen TOP (Art, Wahl), schließen mit Ergebnis. Summen: bei „Nur Summen“ und „Geheim“ aus dem Formular (Prüfung gegen die stimmberechtigten Anwesenden; ohne eine einzige Stimme kein Ergebnis), bei „Offen“ und „Namentlich“ aus den Einzelstimmen der Abstimmungserfassung. Abbrechen ohne Ergebnis möglich. |

Während eine Abstimmung offen ist, lassen sich weder TOP wechseln noch die Sitzung schließen.
Abgesagte Sitzungen und Sitzungen mit genehmigter Niederschrift zeigt das Cockpit nur an.

## Rechte

- Steuern: Recht **„Sitzungen leiten (Cockpit)“** (`can_conduct_meetings`) plus Sichtrecht für Sitzungen.
  Standardrollen „Sachbearbeiter“ und „Protokollant“ haben es; Migration `session.0050_sitzungscockpit`
  gibt es allen Bestandsrollen, die Anwesenheit verwalten und Protokolle bearbeiten dürfen.
- Mitlesen: Sichtrecht für Sitzungen. Nichtöffentliche TOPs und Sitzungen nur mit dem NÖ-Recht; ohne es
  zeigt das Cockpit „Nichtöffentlicher Teil“. Interne Störungsvermerke sehen nur Steuernde.
- Steuern ohne NÖ-Recht greift nicht in den nichtöffentlichen Teil ein: Ist dort ein TOP aufgerufen, lassen
  sich weder ein anderer TOP aufrufen noch die Sitzung schließen (Meldung ohne Nummer des TOP); beenden kann
  ihn nur, wer ihn sieht.
- Der Service prüft das Steuerungsrecht selbst (`cockpit_service.perform`), die View zusätzlich.

## Echtzeit

- WebSocket `ws/session/<mandant>/cockpit/<sitzung>/` (`apps/session/consumers.py`), nur lesend. Nach jeder
  Änderung (nach dem Commit) geht an die Gruppe der Sitzung nur der Hinweis `{"type": "stand"}` – nie Inhalt.
  Auslöser sind die Modell-Signale von Sitzung, TOP, Anwesenheit, Störung und Niederschrift
  (`apps/session/signals.py`, `cockpit_service.notify_on_commit`): Änderungen über die Sitzungsseite, die
  Abstimmungserfassung oder den Admin erscheinen ebenso sofort wie Cockpit-Aktionen. Je Transaktion und
  Sitzung geht ein Hinweis hinaus.
- Jede Ansicht holt daraufhin ihren Stand per HTMX mit den eigenen Rechten
  (`…/cockpit/stand/?v=<Merkmal>`). Ist das Merkmal (`state_version`, eine Abfrage) unverändert, antwortet
  der Server mit 204.
- Rückfall ohne WebSocket: Polling alle zwei Sekunden; mit Verbindung zur Sicherheit alle 30 Sekunden.
  Alpine-Komponente `meetingCockpit` (`frontend/alpine/meeting-cockpit.ts`).
- Eingaben gehen beim Nachladen nicht verloren: Formulare im Stand tragen `data-cockpit-form`; nach der ersten
  Eingabe gelten sie als ungespeichert, und das neue Formular gleichen Schlüssels übernimmt Stimmenzahlen,
  Ergebnis, Vermerk und Auswahl. Nur das gerade abgeschickte Formular erscheint frisch. Eine abgewiesene
  Aktion tauscht den Stand nicht aus (`HX-Reswap: none`, Meldung per Toast), die Ansicht lädt danach selbst
  nach. Feste ids halten den Fokus. Während jemand in einem Textfeld tippt, wartet die Aktualisierung.

## Nebenläufigkeit

Cockpit, Abstimmungserfassung und Niederschrift schreiben dieselben TOPs, oft gleichzeitig (Leitung im
Cockpit, Protokollführung in der Erfassung bzw. Niederschrift):

- Alle drei nehmen dieselbe Zeilensperre auf der Sitzung (`cockpit_service.lock_meeting`) und laden den TOP
  erst darunter.
- Erfassung und Niederschrift speichern nur ihre Felder (`update_fields`), nie Zeiten oder
  Abstimmungszeitpunkte des Cockpits.
- Ergebnis, Art, Wahl und Summen übernehmen sie nur, wenn sie im Formular geändert wurden – verglichen mit dem
  Stand beim Laden (versteckte Felder `geladen_…`, `voting_service.form_value`). Ein im Cockpit inzwischen
  festgestelltes Ergebnis bleibt so stehen.

## Audit und Niederschrift

- Jede Aktion erzeugt Einträge über die Modell-Signale (Sitzung, TOP, Anwesenheit, Störung); das Schließen
  einer Abstimmung zusätzlich „Abstimmungsergebnis festgestellt“ mit Quelle „Sitzungscockpit“.
- Niederschrift (PDF, Ansicht, öffentliche Fassung): „Verlauf: eröffnet …, geschlossen …“ und je TOP
  „Behandelt 18:03–18:20 Uhr“, im Teilnehmerverzeichnis die Unterbrechungen. Nach der Genehmigung sind die
  TOP-Zeiten gesperrt (`protocol_lock`).
- Bestand: Verlauf und Behandlungszeiten erscheinen nur, wenn `SessionProtocol.show_timings` gesetzt ist –
  für neue Niederschriften und solche, die bei der Einführung im Entwurf oder in der Prüfung waren. Bereits
  genehmigte bzw. veröffentlichte behalten ihren genehmigten Inhalt (Migration `session.0050`, DB-Default
  `false`); der Schalter gehört nach der Genehmigung zum gesperrten Inhalt.

## Dateien

- Service: `apps/session/services/cockpit_service.py`
- Views: `apps/session/views/cockpit.py`, Templates `templates/session/cockpit/`
- Tests: `apps/session/tests/test_sitzungscockpit.py`, E2E `tests_e2e/test_sitzungscockpit.py`
  (zwei Browser, Wechsel binnen zwei Sekunden, Polling-Rückfall, Eingaben überstehen das Nachladen)
- Performance-Budget: `session_cockpit_stand` und `session_cockpit_stand_204` in
  `scripts/performance_budgets.json`; ein Test sichert, dass die Abfragen des Stands nicht mit Tagesordnung
  und Anwesenheitsliste wachsen.
