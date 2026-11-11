# Weiterentwicklung

Wie mandari geplant, priorisiert, gebaut, ausgeliefert und gepflegt wird. Diese Seite ist
verbindlich für alle, die am Repository arbeiten, Menschen wie KI-Agenten. Was wir nach außen
zusagen (Takt, Supportzeiträume, Fristen), steht in der [Release-Politik](RELEASE_POLITIK.md);
hier steht, wie wir es einhalten.

> **Stand:** 11.11.2026, gültig ab Release 0.13.0 (Issue #1341). Ersetzt `docs/PROJEKTSTEUERUNG.md`.

## 1. Grundsätze

1. **Eine Quelle.** Aufgaben, Planung und Stand stehen in Issues, Meilensteinen und im
   Projekt-Board. Dateien, Konzepte und Chats sind Hintergrund, keine Aufgabenliste.
2. **Jede Arbeit hat ein Issue** mit Motivation, Umfang und Akzeptanzkriterien. Ausnahmen:
   Abhängigkeits-PRs von Dependabot, Release-Vorbereitung und Rückportierungen (sie verweisen auf
   ihr Original). Sicherheitslücken werden nie öffentlich geführt ([SECURITY.md](../SECURITY.md)).
3. **Der Zug fährt pünktlich.** Alle zwei Wochen erscheint ein Release. Was nicht fertig ist,
   fährt mit dem nächsten Zug; der Termin wird nicht verschoben.
4. **Fertig machen vor neu anfangen.** Begonnene Arbeit, rote Prüfungen und offene Befunde
   gehen jeder neuen Aufgabe vor.
5. **Entscheidungen sind nachvollziehbar.** Offene Fragen tragen das Label `entscheidung` und
   einen Vorschlag mit Standard; die Antwort steht im Issue. Architekturentscheidungen stehen
   als ADR unter [`docs/adr/`](adr/README.md).

## 2. Release-Zug

Jeder Meilenstein ist ein Zug: Er trägt die Version und als Fälligkeit den Release-Dienstag.

| Zug | Stichtag (Do) | Release (Di) | Hinweis |
|---|---|---|---|
| 0.13 | 12.11.2026 | 17.11.2026 | erstes Release im Zwei-Wochen-Takt |
| 0.14 | 26.11.2026 | 01.12.2026 | |
| 0.15 | 10.12.2026 | 15.12.2026 | |
| – | – | 29.12.2026 | entfällt (Feiertage) |
| 0.16 | 07.01.2027 | 12.01.2027 | **Pflegeversion** 1. Quartal 2027 |
| 0.17 | 21.01.2027 | 26.01.2027 | |
| 0.18 | 04.02.2027 | 09.02.2027 | |
| 0.19 | 18.02.2027 | 23.02.2027 | |
| 0.20 | 04.03.2027 | 09.03.2027 | |
| 0.21 | 18.03.2027 | 23.03.2027 | |
| 0.22 | 01.04.2027 | 06.04.2027 | **Pflegeversion** 2. Quartal 2027 |

Danach weiter alle 14 Tage; maßgeblich sind die Meilensteine. Pflegeversion ist immer der
erste Zug eines Quartals.

### Ablauf einer Release-Woche

| Tag | Schritt |
|---|---|
| **Donnerstag (Stichtag)** | 1. Meilenstein bereinigen: Was nicht fertig ist, in den nächsten Zug verschieben. 2. Vorbereitungs-PR gegen `dev` „chore(release): 0.13.0 vorbereiten“: Version in `mandari/pyproject.toml` und `mandari/package.json`, `uv lock` im Ordner `mandari`, im CHANGELOG `[Unreleased]` zu `[0.13.0] – 2026-11-17` und einen leeren Abschnitt `[Unreleased]` darüber. 3. Release-PR `dev` → `main` „Release v0.13.0“, CI grün, Merge. 4. Deploy in Produktion nach den Betriebsregeln. |
| **Freitag bis Montag** | Produktion beobachten (Statusseite, Fehlerprotokolle, Rückmeldungen). Ein Fehler, der das Release verhindert (Datenverlust, Sicherheitslücke, Ausfall, gescheiterte Migration), wird über `dev` behoben und mit einem weiteren Release-PR ausgeliefert. Getaggt wird dann der korrigierte Stand, wenn er bis Dienstag mindestens einen Werktag in Produktion läuft; sonst entfällt der Zug. Der korrigierte Stand enthält auch, was seit dem Stichtag in `dev` kam. Das ist gewollt: Freigegeben ist, was mindestens einen Werktag in Produktion lief. Isolierte Korrekturen auf dem Stichtagsstand gibt es nicht. |
| **Dienstag (Release)** | Tag und GitHub-Release auf den getesteten Commit (Befehle unten). Der Release-Workflow baut die Images und legt `release/v0.13.0` an; `latest` setzt er nur, wenn der getaggte Commit noch der Kopf von `main` ist (lief `main` weiter, steht `latest` schon auf dem neueren Stand). Meilenstein schließen. Eintrag auf mandari.de/releases/. Bei einer **Pflegeversion** zusätzlich: Pflegelinie `release/v0.16` anlegen und die Tabelle „Unterstützte Versionen“ in `SECURITY.md` per PR ergänzen. Danach den übernächsten Zug planen (Abschnitt 3). |

```bash
git fetch origin --tags
# <commit>: der main-Stand, der seit dem Stichtag in Produktion läuft
git tag -a v0.13.0 <commit> -m "mandari 0.13.0"
git push origin v0.13.0
# Notizen: der Abschnitt [0.13.0] aus CHANGELOG.md
gh release create v0.13.0 --verify-tag --title "mandari 0.13.0" --notes-file notizen.md
```

Zwischen den Stichtagen darf Produktion weiter aus `main` aktualisiert werden (Release-PR
„Release JJJJ-MM-TT“). Getaggt wird nur der Stand vom Stichtag.

## 3. Planung

- **Zwei Züge voraus.** Geplant sind immer der laufende und der nächste Zug. Alles Spätere liegt
  im Rückstand: Epics und Issues ohne Meilenstein.
- **Passt in einen Zug.** Ein Issue kommt nur in einen Meilenstein, wenn es bis zum Stichtag
  fertig werden kann, also höchstens etwa eine Woche Arbeit. Größeres wird geschnitten;
  Teilschritte verweisen mit „Teil von #…“ auf das Ganze.
- **Planung am Release-Dienstag.** Nach dem Release wird der übernächste Zug nach den
  Prioritäten aus Abschnitt 4 gefüllt. Termine aus Zusagen werden dabei rückwärts eingeplant:
  Eine Zusage für ein Quartal muss im letzten Zug davor fertig sein.
- **Unfertiges** rutscht am Stichtag ohne Diskussion in den nächsten Zug.

## 4. Prioritäten

Gilt in dieser Reihenfolge für alle, die Arbeit auswählen:

1. **Sicherheitskorrekturen** innerhalb ihrer Frist, einschließlich Rückportierung.
2. **Störungen und kritische Fehler** in Produktion (Datenverlust, Nichtöffentliches sichtbar,
   Ausfall einer Kernfunktion, falsche Rechtswirkung).
3. **Zusagen mit Termin:** öffentliche Roadmap „Zugesagt“, Verträge.
4. **Der laufende Zug:** Issues im aktuellen Meilenstein, begonnene vor neuen.
5. **Rückstand.**

Innerhalb einer Stufe: Begonnenes vor Neuem; bei gleichem Nutzen das Kleinere zuerst.

## 5. Neue Wünsche und Umplanen

- Ein neuer Wunsch wird ein Issue und kommt in den nächsten oder übernächsten Zug, nie still in
  den laufenden.
- In den laufenden Zug kommt Neues nur bei Stufe 1 oder 2 oder auf ausdrückliche Anweisung der
  Projektleitung. Dann wandert ein gleich großes Issue in den nächsten Zug („eins rein, eins raus“),
  sichtbar im Meilenstein.
- Prioritäten laufender Arbeit werden am Zugwechsel umgestellt, nicht mittendrin.

## 6. Gleichzeitige Arbeit

- Höchstens **sechs Issues** gleichzeitig „In Arbeit“.
- Je Person bzw. Agent **ein Issue zur Zeit**, ein Branch und in der Regel ein Pull Request je Issue.
- Ein eigener offener Pull Request mit roter CI oder offenen Befunden geht jeder neuen Arbeit vor.
- Liegt ein Issue fünf Werktage ohne Fortschritt, geht es mit einem Kommentar zum Stand zurück
  auf „Bereit“.
- **Schalter:** Eine Funktion hinter einem Schalter, der zwei Züge lang in Produktion aus ist,
  kommt in die Planung: einschalten, als Option für Betreiber dokumentieren oder entfernen.
  Abgelöste Wege werden spätestens zwei Züge nach dem Umschalten entfernt.

## 7. Ablauf einer Aufgabe

| Status | Bedeutung |
|---|---|
| Eingang | neu, noch nicht geprüft |
| Bereit | Akzeptanzkriterien klar, Abhängigkeiten erledigt, kann beginnen |
| In Arbeit | Branch von `dev`, Umsetzung läuft |
| Review | Pull Request offen oder gemergt, Auslieferung steht aus |
| Wartet auf Entscheidung | eine fachliche oder geschäftliche Frage ist offen |
| Erledigt | in Produktion, Issue geschlossen |

1. **Issue** mit Akzeptanzkriterien (Vorlagen „Bug Report“ bzw. „Feature Request“).
2. **Branch** von `dev`, Umsetzung mit Tests ([CONTRIBUTING.md](../CONTRIBUTING.md)).
3. **Pull Request** gegen `dev`: `Closes #…`, wenn er das Issue erledigt, sonst `Teil von #…`.
   CHANGELOG-Zeile unter `[Unreleased]` im selben PR (Sicherheitskorrekturen neutral formuliert).
4. **Prüfung und Merge** über die Merge-Queue (`gh pr merge <nummer> --squash --auto`).
5. **Auslieferung** mit dem nächsten Release-PR `dev` → `main`; das Issue schließt, sobald der
   Stand auf `main` ankommt.

## 8. Wann ist etwas fertig

- Akzeptanzkriterien erfüllt, Tests ergänzt, CI grün, über die Merge-Queue in `dev`.
- CHANGELOG-Zeile vorhanden; brechende Änderungen vor 1.0 mindestens vier Wochen vorher unter
  „Abgekündigt“ angekündigt ([Release-Politik](RELEASE_POLITIK.md), Abschnitt 1).
- Implementierungsnotiz in `docs/` bzw. Hinweis für docs.mandari.de angepasst, wenn sich
  Bedienung, Betrieb oder eine Schnittstelle ändert.
- In Produktion; ein Schalter ist an oder bewusst als Option dokumentiert.

## 9. Rückportierung in Pflegelinien

**Was** zurückportiert wird, regelt die [Release-Politik](RELEASE_POLITIK.md), Abschnitt 5:
Sicherheitskorrekturen ab „mittel“ und kritische Fehler, keine Migrationen. Ob ein Fix dazugehört,
entscheidet, wer ihn baut. Im Zweifel bei Sicherheit ja, sonst die Projektleitung fragen. Welche
Linien unterstützt werden, steht in [SECURITY.md](../SECURITY.md#unterstützte-versionen).

**Ablauf:**

1. **Erst `dev`.** Die Korrektur entsteht wie jede andere: Branch von `dev`, PR, Merge-Queue.
   - **Kritischer Fehler:** im PR-Text `Rückportieren: ja`, dazu das Label `rueckportieren`.
   - **Sicherheitskorrektur:** im öffentlichen PR **nichts** kennzeichnen, weder Zeile noch Label noch
     Schweregrad; der PR bleibt neutral. Dass und wohin zurückportiert wird, steht im privaten
     Advisory. So verrät der öffentliche PR nicht, welche Versionen noch ungeschützt sind.
2. **Übernehmen.** Nach dem Merge in `dev`:

   ```bash
   python scripts/rueckportieren.py <PR-Nummer> --grund sicherheit-hoch
   ```

   Das Skript legt je unterstützter Linie den Zweig `rueckport/<PR>-v<Linie>` an, übernimmt den
   Commit mit `cherry-pick -x` und öffnet einen PR gegen `release/v<Linie>` (Label
   `rueckportierung`, Text ohne Grund und Schweregrad). Fehlt eine Pflegelinie, legt es sie auf dem
   Tag `v<Linie>.0` an. PRs mit Migrationen lehnt es ab, ebenso Linien ohne CI-Auslöser (unten).
   Den Grund (`--grund`) braucht nur das Protokoll des Aufrufs.
3. **Konflikte** nur so weit lösen, dass dieselbe Korrektur auf dem alten Stand wirkt; keine
   Nebenarbeiten. Gibt es den betroffenen Code auf einer Linie nicht, ist sie nicht betroffen:
   im Original-PR kommentieren („0.11 nicht betroffen, weil …“), kein PR.
4. **Mergen**, sobald die CI grün ist: `gh pr merge <nummer> --squash`. Pflegelinien haben keine
   Merge-Queue. Eine zweite fachliche Prüfung entfällt; geprüft wird, dass der Diff dem Original
   entspricht.
   **CI-Auslöser:** Linien ab 0.13 lösen die CI für PRs gegen `release/v*` selbst aus. Für Linien
   davor (0.11, 0.12) braucht es einmalig einen PR, der in deren `.github/workflows/pr-check.yml`
   den Zweig `release/v*` unter `pull_request` ergänzt; dieser PR startet die CI selbst, weil er die
   Datei ändert. Erst danach Rückportierungen öffnen.
5. **Patch-Version** innerhalb der Frist (Release-Politik, Abschnitt 4), mehrere Korrekturen
   gern gebündelt:
   - PR gegen `release/v<Linie>`: Version `X.Y.N` in `pyproject.toml` und `package.json`,
     `uv lock`, CHANGELOG-Abschnitt `[X.Y.N]`.
   - Tag auf den frisch abgerufenen Kopf der Linie. Als neuestes Release markiert wird es nur, wenn
     es die höchste veröffentlichte Version ist (Patch der jüngsten Linie); sonst `--latest=false`:

     ```bash
     git fetch origin release/v0.12 --tags
     git log -1 --oneline origin/release/v0.12   # muss die Versionsanhebung zeigen
     git tag -a v0.12.1 origin/release/v0.12 -m "mandari 0.12.1"
     git push origin v0.12.1
     gh release create v0.12.1 --verify-tag --latest=false --title "mandari 0.12.1" --notes-file notizen.md
     ```

     Der Release-Workflow baut die Images, legt `release/v0.12.1` an und setzt das Linien-Tag `v0.12`;
     `latest` setzt eine Patch-Version nie zurück, und das Website-Image bleibt bei dem von `v0.12.0`.
     Zwei Releases nie gleichzeitig veröffentlichen.
   - Abschnitt `[0.12.1]` mit dem nächsten Vorbereitungs-PR im CHANGELOG von `dev` nachtragen.
6. **Abschluss:** Im Original-PR kommentieren, in welchen Versionen die Korrektur steckt
   („Rückportiert: 0.11.2, 0.12.1“), und das Label `rueckportieren` entfernen.

**Nie:** Commits direkt auf eine Pflegelinie oder auf `main` ohne Pull Request, Cherry-Picks auf
einen Produktionsstand, neue Funktionen oder Migrationen in einer Pflegelinie.

## 10. Wo was steht

| Ort | Inhalt |
|---|---|
| Issues, Meilensteine, Projekt-Board | Aufgaben, Planung, Stand (die einzige Aufgabenquelle) |
| [`docs/RELEASE_POLITIK.md`](RELEASE_POLITIK.md) | Zusagen nach außen: Takt, Support, Fristen, Abkündigung |
| diese Seite | wie wir planen, ausliefern und zurückportieren |
| [`SECURITY.md`](../SECURITY.md) | Meldewege und unterstützte Versionen |
| [`docs/adr/`](adr/README.md) | Architekturentscheidungen |
| [`docs/README.md`](README.md) | Index der Implementierungsnotizen je Funktion |
| [docs.mandari.de](https://docs.mandari.de) | Dokumentation für Nutzer:innen, Betreiber und Integrator:innen |
| [`AGENTS.md`](../AGENTS.md), [`CLAUDE.md`](../CLAUDE.md) | Kontext und Regeln für KI-Assistenten (Kurzfassung dieser Seite) |
| [`CHANGELOG.md`](../CHANGELOG.md) | Änderungen je Version |

Regeln für `docs/`: Eine Implementierungsnotiz beschreibt, was im Code existiert. Pläne und
Konzepte gehören in Issues oder ADRs. Überholte Notizen werden entfernt, nicht als „veraltet“
markiert; die Historie bleibt in Git.
