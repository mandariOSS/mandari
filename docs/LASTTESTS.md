# Lasttests, Mengengerüste und Größenempfehlungen

Stand: September 2026, erster Schritt zu Issue #228. Vergaben legen ein
Mengengerüst fest — Mandanten, Gremien, Sitzungen pro Jahr, Vorlagen,
Dokumente, gleichzeitige Nutzung. Wir brauchen dafür Nachweise, die sich
wiederholen lassen. Diese Seite hält fest, **was** wir messen, **womit** und
**was dabei herauskam**; sie kennzeichnet, welche Zahl gemessen und welche
abgeleitet ist.

Bausteine:

| Baustein | Ort |
|---|---|
| Datengenerator nach Mengengerüst | `manage.py generate_load_data --profile klein\|mittel\|gross` |
| Lastszenarien (Locust) | `loadtest/locustfile.py`, Befehle in `loadtest/README.md` |
| Performance-Budgets als CI-Gate | `scripts/check_performance_budgets.py`, Budgets in `scripts/performance_budgets.json` |

## 1. Referenz-Mengengerüste

Die drei Klassen orientieren sich an typischen Vergaben: eine kreisangehörige
Gemeinde, eine Mittelstadt mit Eigenbetrieb und eine Großstadt mit
Bezirksvertretungen. Die Zahlen sind bewusst am oberen Rand der jeweiligen
Klasse, damit ein bestandener Test den Regelfall abdeckt.

| Kennzahl | klein | mittel | groß | Begründung |
|---|---:|---:|---:|---|
| Einwohner (Orientierung) | 20.000 | 100.000 | 500.000+ | Größenklassen der Vergaben, die uns erreichen |
| Mandanten (Session) | 1 | 2 | 3 | Stadt; dazu Eigenbetrieb bzw. Zweckverband mit eigenem Sitzungsdienst |
| Gremien je Hauptmandant | 6 | 20 | 60 | Rat, Haupt- und Fachausschüsse; groß: dazu 9 Bezirksvertretungen, Beiräte, Kommissionen |
| Personen (Mandatsträger, sachkundige Bürger) | 60 | 250 | 900 | Ratsgröße nach Gemeindeordnung plus Ausschuss- und Bezirksbesetzung |
| Sitzungen pro Jahr | 40 | 200 | 800 | Rat monatlich, Ausschüsse 6- bis 10-mal, Bezirksvertretungen monatlich |
| Tagesordnungspunkte je Sitzung | 8 | 12 | 12 | Ratssitzungen einer Großstadt haben 20 bis 40, der Mittelwert über alle Gremien liegt darunter |
| Vorlagen pro Jahr | 300 | 1.500 | 6.000 | Köln: 42.247 Vorgänge über rund zehn Jahre im Ingestor-Bestand |
| Dokumente (Anlagen, PDF) | 600 | 4.000 | 20.000 | zwei bis drei Anlagen je Vorlage, zwei Jahre Historie |
| Gleichzeitige Nutzer | 20 | 100 | 400 | Sitzungsdienst plus Fraktionen plus Publikum während einer Ratssitzung |
| Abstimmungsteilnehmer (Rat) | 30 | 60 | 90 | Ratsgröße; Köln hat 90 Ratsmitglieder plus Oberbürgermeisterin |
| Fraktionsmitglieder (Work, je Fraktion) | 8 | 15 | 25 | größte Fraktion im Rat |
| Anträge je Fraktion (zwei Jahre) | 100 | 400 | 1.500 | Anträge, Anfragen, Entwürfe im Editor |

Der Generator erzeugt zwei Jahre Historie (die Tabelle nennt Werte pro Jahr),
Nebenmandanten erhalten ein Zehntel der Mengen des Hauptmandanten. Das Profil
`klein` legt zum Beispiel 80 Sitzungen, 640 Tagesordnungspunkte, 600 Vorlagen,
600 Dateien, 1.340 Anwesenheiten und 2.412 Einzelstimmen an, dazu die
Spiegelung im Bürgerportal (Insight) und eine Fraktion mit 100 Anträgen. Alle
Daten sind synthetisch, mit `last-<profil>` gekennzeichnet und mit `--reset`
rückstandslos zu entfernen.

## 2. Lastszenarien

Locust-Szenarien mit Gewichtung; die Mischung bildet eine Ratssitzung mit
Publikum nach (viele Lesezugriffe, wenige Schreibvorgänge):

| Szenario | Gewicht | Was passiert |
|---|---:|---|
| Portalbesucher | 5 | Startseite, Sitzungsliste, Vorgangsliste, Detailseiten, Suche (Seite und Ergebnis-Partial) |
| Sitzungsdienst | 3 | Anmeldung, Sitzungsliste, Sitzungsdetail, Sitzungsmappe (PDF), Vorlagenliste, Vorlage, übergreifende Suche |
| Fraktionsmitglied | 2 | Anmeldung, Dashboard, Dokumentliste, Editor-Seite |
| Live-Abstimmung | 1 | Abstimmungsseite der laufenden Ratssitzung, Einzelstimmen aller Anwesenden per Formular |

Noch nicht abgebildet (Folgeschritte laut Issue): Sitzungsgeldlauf,
Synchronisation durch den Ingestor, die WebSocket-Verteilung der Abstimmung
an Endgeräte.

## 3. Gemessene Ergebnisse

### Profil `klein` — Entwicklerrechner, nicht repräsentativ

Umgebung: AMD Ryzen 7 3700X (8 Kerne, 16 Threads), 64 GB RAM, Windows 11,
Python 3.12, **ein** Daphne-Prozess (`manage.py runserver`, `DEBUG=true`, ohne
Redis, ohne Elasticsearch — die Suche fällt auf die Datenbank zurück). Locust
auf demselben Rechner: 20 Nutzer, Anlauf 5/s, Laufzeit 3 Minuten. Diese Zahlen
zeigen, dass die Szenarien laufen und wo die teuren Seiten liegen. Sie sind kein
Nachweis für ein Zielsystem: `DEBUG=true` und der Entwicklungsserver kosten
Zeit, der Lastgeber teilt sich die CPU mit der Anwendung.

Lauf 1, SQLite (17.09.2026): 1.378 Anfragen, 7,7 Anfragen/s, 6 Fehler (0,44 %) —
alle sechs `database is locked` beim Abstimmungs-POST, ein SQLite-Artefakt bei
gleichzeitigen Schreibern, das PostgreSQL nicht kennt. Deshalb Lauf 2.

Lauf 2, PostgreSQL 16 (17.09.2026): Datenbank im Docker-Container (Docker
Desktop, ohne Verbindungspool), sonst wie Lauf 1. 1.315 Anfragen, 7,3 Anfragen/s, **0 Fehler**. Zeiten in
Millisekunden, p95 aus der Locust-Zusammenfassung (Spalte `95%`):

| Szenario / Endpunkt | Anfragen | Median | p95 | Anfragen/s |
|---|---:|---:|---:|---:|
| Portal: Startseite `/insight/` | 144 | 97 | 240 | 0,80 |
| Portal: Sitzungsliste | 123 | 88 | 160 | 0,68 |
| Portal: Vorgangsliste | 110 | 120 | 240 | 0,61 |
| Portal: Vorgang (Detail) | 114 | 99 | 210 | 0,63 |
| Portal: Suchseite | 74 | 84 | 180 | 0,41 |
| Portal: Suchergebnisse (Datenbank-Rückfall) | 74 | 99 | 180 | 0,41 |
| Sitzungsdienst: Anmeldung (POST) | 11 | 1.600 | 1.700 | — |
| Sitzungsdienst: Sitzungsliste | 87 | 180 | 330 | 0,48 |
| Sitzungsdienst: Sitzungsdetail | 89 | 200 | 380 | 0,50 |
| Sitzungsdienst: Sitzungsmappe (PDF) | 17 | 240 | 1.200 | 0,09 |
| Sitzungsdienst: Vorlagenliste | 68 | 180 | 320 | 0,38 |
| Sitzungsdienst: Vorlage (Detail) | 46 | 210 | 330 | 0,26 |
| Sitzungsdienst: Suche | 28 | 210 | 600 | 0,16 |
| Work: Dashboard | 95 | 190 | 630 | 0,53 |
| Work: Dokumentliste | 97 | 270 | 450 | 0,54 |
| Work: Editor-Seite | 62 | 220 | 450 | 0,35 |
| Live-Abstimmung: Abstimmungsseite (GET) | 28 | 200 | 360 | 0,16 |
| Live-Abstimmung: 30 Einzelstimmen erfassen (POST) | 28 | 2.000 | 2.700 | 0,16 |
| **Gesamt** | **1.315** | **160** | **580** | **7,32** |

Die Rohdaten (`klein-postgres_stats.csv`, `klein-postgres.html`) liegen nach dem
Lauf unter `loadtest/results/` und sind nicht eingecheckt.

Was auffällt (gilt für beide Läufe):

- Die Anmeldung dauert über eine Sekunde: PBKDF2-Hashing mit den
  Django-Standardrunden, gewollt und einmalig je Sitzung.
- Das Erfassen von 30 Einzelstimmen dauert rund zwei Sekunden (PostgreSQL) —
  die Stimmen werden einzeln gespeichert und protokolliert. Für den Rat einer
  Großstadt (90 Stimmen) wäre das die langsamste Aktion des Systems; ein
  klarer Kandidat für `bulk_create` in `voting_service.capture_votes`.
- Die Sitzungsmappe (PDF) ist mit 240 ms Median günstig; die Tagesordnungen des
  Profils `klein` sind mit acht Punkten allerdings kurz.
- Die Dokumentliste in Work ist die teuerste Listenseite (47 Abfragen im
  Budget-Gate). Gegen PostgreSQL im Container kostet jede Abfrage rund 2–3 ms
  Netzwerk — Seiten mit vielen Abfragen leiden dort am stärksten, was die
  Abfrage-Budgets als Steuergröße bestätigt.

### Profil `gross` — steht aus

Der Lauf „Großstadt“ auf repräsentativer Hardware ist der Nachweis für die
Vergabemappe und noch nicht erfolgt. Die Anleitung steht in
`loadtest/README.md` (Abschnitt „Lauf Großstadt“); die Ergebnisse gehören hierher,
mit Hardware, Nutzerzahl, Laufzeit, p95 je Szenario, Durchsatz, Fehlerquote und
Ressourcenverbrauch (`docker stats`, `pg_stat_activity`).

## 4. Performance-Budgets in der CI

`scripts/check_performance_budgets.py` misst zwölf Kernseiten mit dem
Django-Test-Client gegen die Daten des Profils `klein` (kalter Cache, Minimum
aus drei Läufen) und vergleicht mit `scripts/performance_budgets.json`:

- **Abfragen** sind hart: mehr Abfragen als im Budget lassen den CI-Lauf
  scheitern. Budgets dürfen nur sinken (`--update` zieht nach unten nach).
- **Zeit** ist eine Warnung mit großzügiger Obergrenze (500 ms bei gemessenen
  10–80 ms), weil CI-Läufer um den Faktor zwei bis drei schwanken.

Stand der Budgets (Abfragen, gemessen 17.09.2026):

| Seite | Abfragen |
|---|---:|
| Insight: Startseite | 16 |
| Insight: Suchseite | 8 |
| Insight: Suchergebnisse (Datenbank-Rückfall) | 12 |
| Insight: Sitzungsliste | 10 |
| Insight: Vorgangsliste | 13 |
| Session: Sitzungsliste | 22 |
| Session: Sitzungsdetail | 26 |
| Session: Vorlagenliste | 21 |
| Session: Abstimmungserfassung | 22 |
| Work: Dashboard | 37 |
| Work: Dokumentliste | 47 |
| Work: Editor-Seite | 53 |

Die Zahlen sind mit Paginierung konstant, weil die Listen 20 Einträge je Seite
zeigen; ein N+1-Zugriff würde hier sofort als Sprung sichtbar.

## 5. Größenempfehlungen

Grundlage: der gemessene Lauf `klein` (ein Daphne-Prozess trägt 20 Nutzer bei
p95 unter 400 ms auf einem Entwicklerrechner), die Datenbankeinstellungen aus
`DEPLOYMENT.md` („PostgreSQL: Grundeinstellungen“, „Datenbankverbindungen:
Budget“) und der Betrieb der eigenen Instanz (Köln, 42.247 Vorgänge). Alles
außer der Klasse `klein` ist **abgeleitet**: Wir skalieren die Zahl der
Anwendungsprozesse mit den gleichzeitigen Nutzern und den Datenbankspeicher
mit dem Bestand. Der Lauf `gross` ersetzt die Ableitung durch Messung.

| Empfehlung | klein | mittel | groß | Status |
|---|---|---|---|---|
| vCPU (gesamt) | 2 | 4 | 8 | klein gemessen (Last unter einem Kern), Rest abgeleitet |
| RAM (gesamt) | 8 GB | 16 GB | 32 GB | abgeleitet aus den Dienstgrenzen unten |
| Anwendungsprozesse (Daphne) | 1 | 2 | 4 | ein Prozess je 20–100 Nutzer, ASGI-Thread-Pool min(32, CPU+4) |
| RAM je Anwendungsprozess | 1 GB | 1 GB | 1 GB | gemessen im Betrieb (Limit in `docker-compose.yml`) |
| PostgreSQL `mem_limit` | 1 GB | 4 GB | 8 GB | Bestand: 0,1 / 0,5 / 2 GB je Mandant und Jahr, Index soll in den Cache passen |
| `shared_buffers` / `effective_cache_size` | 256 MB / 768 MB | 1 GB / 3 GB | 2 GB / 6 GB | Faustregel ein Viertel / drei Viertel (`DEPLOYMENT.md`) |
| `work_mem` | 8 MB | 8 MB | 16 MB | gilt je Sortiervorgang; mit `max_connections` gemeinsam ändern |
| `max_connections` | 100 | 200 | 300 | Summe der Dienstgrenzen plus Reserve, siehe unten |
| `DB_POOL_MAX` je Anwendungsprozess | 10 | 10 | 10 | passt zum Thread-Pool; mehr Prozesse statt größerer Pools |
| Elasticsearch Heap | 1 GB | 2 GB | 4 GB | abgeleitet aus Dokumentzahl (600 / 4.000 / 20.000 Volltexte) |
| Redis | 256 MB | 512 MB | 1 GB | Cache, Sessions, Channel-Layer für Abstimmungen |
| Ingestor (nur mit Bürgerportal) | 1 GB | 2 GB | 4 GB | OCR-Worker je nach Dokumentvolumen zusätzlich |

Verbindungsbudget je Klasse (Muster aus `DEPLOYMENT.md`):

| Dienst | klein | mittel | groß |
|---|---:|---:|---:|
| Anwendung (Prozesse × `DB_POOL_MAX`) | 10 | 20 | 40 |
| Ingestor | 30 | 30 | 30 |
| OCR-Worker | 30 | 30 | 30 |
| Sicherung (`pg_dump`) | 2 | 2 | 2 |
| Reserve Superuser | 3 | 3 | 3 |
| **Summe** | **75** | **85** | **105** |
| `max_connections` | 100 | 200 | 300 |

Wer einen Dienst hinzufügt, trägt ihn ein und prüft die Summe — dieselbe
Regel wie in `DEPLOYMENT.md`.

## 6. Wiederholen

```bash
# Daten
cd mandari && python manage.py generate_load_data --profile klein

# Budget-Gate (wie in der CI)
python scripts/check_performance_budgets.py

# Lasttest, Befehle und Umgebungsvariablen in loadtest/README.md
locust -f loadtest/locustfile.py --headless --host http://127.0.0.1:8000 -u 20 -r 5 -t 3m \
       --csv loadtest/results/klein --html loadtest/results/klein.html
```
