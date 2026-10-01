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
| Anwesenheit | Anwesend, kommt (verspätet, mit Uhrzeit), geht (vorzeitig, mit Uhrzeit), zurück (Unterbrechung als Notiz), abwesend. Beschlussfähigkeit live, für den aufgerufenen TOP nach den Regeln des Landesprofils. |
| Störungsprotokoll | Störung Zugeschalteter „ab jetzt“ mit Ursache und internem Vermerk (z. B. „telefonisch gemeldet“ als zweiter Meldeweg), Ende „jetzt“. Während der Störung zählt die Person nicht zur Beschlussfähigkeit. |
| Abstimmung | Öffnen zum aufgerufenen TOP (Art, Wahl), schließen mit Ergebnis. Summen: bei „Nur Summen“ und „Geheim“ aus dem Formular (Prüfung gegen die stimmberechtigten Anwesenden), bei „Offen“ und „Namentlich“ aus den Einzelstimmen der Abstimmungserfassung. Abbrechen ohne Ergebnis möglich. |

Während eine Abstimmung offen ist, lassen sich weder TOP wechseln noch die Sitzung schließen.
Abgesagte Sitzungen und Sitzungen mit genehmigter Niederschrift zeigt das Cockpit nur an.

## Rechte

- Steuern: Recht **„Sitzungen leiten (Cockpit)“** (`can_conduct_meetings`) plus Sichtrecht für Sitzungen.
  Standardrollen „Sachbearbeiter“ und „Protokollant“ haben es; Migration `session.0050_sitzungscockpit`
  gibt es allen Bestandsrollen, die Anwesenheit verwalten und Protokolle bearbeiten dürfen.
- Mitlesen: Sichtrecht für Sitzungen. Nichtöffentliche TOPs und Sitzungen nur mit dem NÖ-Recht; ohne es
  zeigt das Cockpit „Nichtöffentlicher Teil“. Interne Störungsvermerke sehen nur Steuernde.
- Der Service prüft das Steuerungsrecht selbst (`cockpit_service.perform`), die View zusätzlich.

## Echtzeit

- WebSocket `ws/session/<mandant>/cockpit/<sitzung>/` (`apps/session/consumers.py`), nur lesend. Nach jeder
  Aktion (nach dem Commit) geht an die Gruppe der Sitzung nur der Hinweis `{"type": "stand"}` – nie Inhalt.
- Jede Ansicht holt daraufhin ihren Stand per HTMX mit den eigenen Rechten
  (`…/cockpit/stand/?v=<Merkmal>`). Ist das Merkmal (`state_version`, eine Abfrage) unverändert, antwortet
  der Server mit 204.
- Rückfall ohne WebSocket: Polling alle zwei Sekunden; mit Verbindung zur Sicherheit alle 30 Sekunden.
  Alpine-Komponente `meetingCockpit` (`frontend/alpine/meeting-cockpit.ts`).
- Die Abstimmungserfassung (Einzelstimmen) benachrichtigt das Cockpit ebenfalls.

## Audit und Niederschrift

- Jede Aktion erzeugt Einträge über die Modell-Signale (Sitzung, TOP, Anwesenheit, Störung); das Schließen
  einer Abstimmung zusätzlich „Abstimmungsergebnis festgestellt“ mit Quelle „Sitzungscockpit“.
- Niederschrift (PDF, Ansicht, öffentliche Fassung): „Verlauf: eröffnet …, geschlossen …“ und je TOP
  „Behandelt 18:03–18:20 Uhr“. Nach der Genehmigung sind die TOP-Zeiten gesperrt (`protocol_lock`).

## Dateien

- Service: `apps/session/services/cockpit_service.py`
- Views: `apps/session/views/cockpit.py`, Templates `templates/session/cockpit/`
- Tests: `apps/session/tests/test_sitzungscockpit.py`, E2E `tests_e2e/test_sitzungscockpit.py`
  (zwei Browser, Wechsel binnen zwei Sekunden, Polling-Rückfall)
