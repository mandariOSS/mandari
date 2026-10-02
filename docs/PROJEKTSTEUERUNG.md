# Projektsteuerung

Wie mandari Arbeit plant, verfolgt und ausliefert. Für Beitragende und für alle, die wissen wollen, woran
gerade gearbeitet wird.

## Grundsätze

- **Jede Arbeit hat ein Issue.** Fehler, Funktionen, Umbauten und Dokumentation beginnen als Issue mit
  Motivation, Umfang und Akzeptanzkriterien. Pull Requests verweisen darauf (`Closes #123`).
- **Eine Quelle.** Planung und Stand stehen in den Issues, Meilensteinen und im Projekt-Board – nicht in
  Chats oder verstreuten Dateien.
- **Entscheidungen sind nachvollziehbar.** Offene Fragen tragen das Label `entscheidung` und enthalten einen
  Vorschlag mit Standard. Die Antwort wird im Issue festgehalten; Architekturentscheidungen stehen als ADR
  unter [`docs/adr/`](adr/README.md).
- **Vertrauliches bleibt vertraulich.** Sicherheitslücken werden nie öffentlich geführt, sondern nach
  [SECURITY.md](../SECURITY.md) gemeldet. Kundendaten, Preise und Infrastrukturdetails stehen nicht in
  öffentlichen Issues.

## Aufbau

| Element | Zweck |
|---|---|
| **Epic** (Label `epic`) | größeres Vorhaben mit Unter-Issues, z. B. Datendrehscheibe (#476), mandari Data (#112), Session 2.0 (#496) |
| **Unter-Issue** | eine lieferbare Aufgabe; Fortschritt des Epics ergibt sich aus seinen Unter-Issues |
| **Meilenstein** | ein Release nach der [Release-Politik](RELEASE_POLITIK.md), z. B. `0.12` (erste Novemberwoche 2026), `0.13` (Januar 2027) |
| **Labels** | Art (`bug`, `enhancement`, `documentation`), Bereich (`app:session`, `app:work`, `app:insight`, …), Priorität (`priority:high/medium/low`), `entscheidung`, `good first issue` |
| **Projekt-Board** | Status aller Issues über die Repositories hinweg (intern gepflegt) |

Issues ohne Meilenstein bilden den Rückstand. Sie werden bei der Planung eines Releases eingeordnet.

## Ablauf einer Aufgabe

| Status | Bedeutung |
|---|---|
| Eingang | neu, noch nicht geprüft |
| Bereit | Akzeptanzkriterien klar, Abhängigkeiten erledigt – kann beginnen |
| In Arbeit | Branch von `dev`, Umsetzung läuft |
| Review | Pull Request offen oder gemergt, Prüfung bzw. Auslieferung steht aus |
| Wartet auf Entscheidung | eine fachliche oder geschäftliche Frage ist offen |
| Erledigt | ausgeliefert, Issue geschlossen |

1. **Issue** mit Akzeptanzkriterien anlegen (Vorlagen „Bug Report“ bzw. „Feature Request“).
2. **Branch** von `dev`, Umsetzung mit Tests (siehe [CONTRIBUTING.md](../CONTRIBUTING.md)).
3. **Pull Request** gegen `dev` mit `Closes #…`; alle Prüfungen der CI müssen grün sein.
4. **Review und Merge** nach `dev` über die Merge-Queue (`gh pr merge <nummer> --squash --auto`): Sie prüft
   den kombinierten Stand mit allen PRs davor, bevor er auf `dev` landet. Danach holt sich die
   Staging-Umgebung den neuen Stand selbst ([DEPLOYMENT.md](../DEPLOYMENT.md#staging-als-prüfstand)).
5. **Auslieferung:** Produktion wird aus `dev` aktualisiert, außerhalb der Sitzungszeiten der Kunden; danach
   wird `main` nachgezogen. Issues schließen mit dem Nachziehen von `main`.
6. **Release:** Alle zwei Monate wird der Meilenstein abgeschlossen, das CHANGELOG zusammengeführt und ein
   Tag gesetzt.

## Rhythmus

- **Laufend:** Auslieferung einzelner Korrekturen nach grüner CI.
- **Alle zwei Monate:** MINOR-Release (erste Woche des Monats), Planung des nächsten Meilensteins.
- **Sicherheitskorrekturen:** nach den Fristen der Release-Politik, unabhängig vom Releasetakt.
