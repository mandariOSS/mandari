# Umstellung auf die Datendrehscheibe: Checkliste je Weg

Stand: 4. Oktober 2026 · Issue #577 · Entscheidung:
[Ereignistechnik in PostgreSQL](adr/20260929-ereignistechnik-postgres.md), Punkt 12 („Umstellung je
Konsument: Schattenbetrieb, Vergleich, Umschalten per Einstellung, Entfernen des alten Weges erst im
Folge-Release“)

Jeder bestehende Weg, der auf ein Abonnement der Drehscheibe umzieht (Suchindex, Benachrichtigungen,
Änderungsfeed, Adapter …), folgt demselben Ablauf in vier Phasen: **Schatten → Vergleich → Umschalten →
Aufräumen**. Jede Phase ist umkehrbar, bis der alte Weg im Folge-Release entfernt wird.

**So wird die Liste benutzt:** Das Issue der Umstellung bekommt eine Kopie von Abschnitt 2 als
Kommentar; abgehakt wird dort, mit Datum und Beleg (Ausgabe des Vergleichs, Kennzahl, PR). Eine Phase
beginnt erst, wenn die vorige vollständig abgehakt ist. Trifft ein Abbruchkriterium (Abschnitt 3) zu,
gilt der Rückfall der jeweiligen Phase, und die Umstellung beginnt wieder mit der Phase, in der der
Fehler lag.

## 1. Einmalige Voraussetzungen (E1-Gate, Epic #483)

Gelten für alle Umstellungen; erst danach wird zum ersten Mal ein Weg umgeschaltet.

- [ ] Zusagen der Ereignistechnik nachgewiesen: Absturz, Doppelzustellung, Reihenfolge, Latenz,
      Durchsatz, Wiederanlauf (`docs/EREIGNISTECHNIK_NACHWEISE.md`, #514)
- [ ] Sicherung umfasst Journal und Aufträge, Wiederherstellung mit Anheben der Folgenummer und
      Nachspielen erprobt (`docs/BACKUP.md`, Abschnitt „Journal und Aufträge“, #573)
- [ ] Überwachung: Rückstand je Abonnement, tote Ereignisse, Sequenzierer-Stau mit Alarm; Worker-Prüfung
      auf der Statusseite; Admin-Seite für Abonnements (mit Nachspielen), geparkte Ereignisse und Worker
      (`docs/MONITORING.md`, #510)
- [ ] Erzeuger der benötigten Ereignisse laufen in Produktion (Ingestor, Session); ihre Verträge decken
      ab, was der neue Weg braucht

## 2. Checkliste je Umstellung

### 2.1 Vorbereitung

- [ ] Issue der Umstellung: alter Weg (Code, Auslöser), neues Abonnement (Name, Typmuster, Batch,
      Warteschlange, `transactional`), Schalter, Verantwortliche
- [ ] Alter Weg per Einstellung abschaltbar, ohne Deploy umkehrbar
- [ ] Abonnement schreibt im Schattenbetrieb nur in ein Schattenziel (`delivery.shadow`), ist idempotent
      und hat einen Doppelzustellungstest (Eintrag in `DOPPELZUSTELLUNG`,
      `apps/events/tests/test_zusagen.py`)
- [ ] Vergleichswerkzeug alter gegen neuen Weg: Zahl der Objekte, fehlende, überzählige, Stichprobe mit
      abweichenden Feldern
- [ ] Bekannte Lücken (Ereignisse, die der neue Weg noch nicht sieht) aufgelistet, je Lücke behoben oder
      mit Entscheidung im Issue
- [ ] Last abgeschätzt: Ereignisse je Tag und Vollabgleich × Kosten je Ereignis, Speicher des
      Schattenziels, Wirkung auf den Worker (Speichergrenze 1 GB)
- [ ] Rückfall je Phase beschrieben (Schalter, Befehle)

### 2.2 Schatten

- [ ] Schalter auf Schatten, Deploy; Abonnement steht in `events_subscription` im Zustand `schatten`
- [ ] Schattenziel einmal aus dem Bestand gebaut (Vollbau), Zeitpunkt notiert
- [ ] Nach 24 h: Rückstand unter 5 min (außer im nächtlichen Vollabgleich), keine toten Ereignisse,
      Worker ohne Neustart durch die Speichergrenze

### 2.3 Vergleich (mindestens 7, besser 14 Tage)

- [ ] Zeitraum enthält mindestens einen nächtlichen Vollabgleich und einen Werktag mit Sitzungen
- [ ] Vergleich täglich (oder nach jedem Vollabgleich) ausgeführt, Ausgabe im Issue
- [ ] Jede Abweichung erklärt (zeitlicher Versatz, bekannte Lücke mit Entscheidung) oder behoben;
      am Ende **keine unerklärte Abweichung**
- [ ] Rückfall geprobt: Schattenziel verworfen und neu gebaut, ohne dass der alte Weg es merkt

### 2.4 Umschalten mit Rückfallprobe

- [ ] Zeitfenster ohne Sitzungen und ohne Vollabgleich, angekündigt im Issue
- [ ] Ziel einmal aus dem Bestand gebaut, Schalter auf aktiv, alter Weg per Einstellung aus
- [ ] Prüfung nach 1 h und nach 24 h: Funktion von außen (Stichprobe), Rückstand, tote Ereignisse
- [ ] **Rückfallprobe** innerhalb der ersten Woche: Schalter zurück, alter Weg an, Funktion geprüft,
      wieder umgeschaltet; Dauer und Ergebnis im Issue
- [ ] 7 Tage aktiv ohne Abbruchkriterium

### 2.5 Aufräumen (Folge-Release)

- [ ] Alter Weg entfernt (Code, Signale, Befehle, Schalter des alten Weges), Schattenziel gelöscht
- [ ] Doku und Nachtrag im ADR, CHANGELOG (Rubrik „Entfernt“)
- [ ] Issue geschlossen, Erkenntnisse für die nächste Umstellung hier ergänzt

## 3. Abbruchkriterien

Ein Kriterium genügt. Abbruch heißt: Rückfall der Phase ausführen, Ursache als Issue, Umstellung ab der
betroffenen Phase neu beginnen.

| Kriterium | Schatten / Vergleich | nach dem Umschalten |
|---|---|---|
| Rückstand des Abonnements (`mandari_events_lag_seconds`) über 5 min länger als 1 h außerhalb des Vollabgleichs | Schattenbetrieb anhalten (Abonnement pausieren) | sofort zurückschalten |
| Tote Ereignisse (`mandari_events_parked{state="tot"}`) ohne behobene Ursache nach einem Werktag | Vergleich beginnt nach der Behebung neu | sofort zurückschalten |
| Unerklärte Abweichung im Vergleich | Vergleich verlängern, Ursache beheben | – |
| Fehler in der Funktion, die Nutzer sehen (falsche, fehlende oder veraltete Ergebnisse) | – | sofort zurückschalten |
| Worker: Neustarts durch die Speichergrenze oder Speicher dauerhaft über 80 % der Grenze | Schattenbetrieb anhalten | zurückschalten, wenn die Zustellung leidet |
| Sequenzierer-Stau über 5 min (`mandari_events_sequencer_blocked_seconds`) wiederholt | Ursache (lange Transaktion) beheben | Ursache beheben; Rückfall nur bei Funktionsfehler |

## 4. Erste Anwendung: Suchindex (#526 → #527)

Heute schreiben drei Wege den Suchindex: Django-Signale (`insight_core/signals.py`), der Ingestor und
`reindex_elasticsearch`. Künftig schreibt ihn nur das Abonnement `suchindex`
(`insight_search/abonnement.py`, externe Version gleich Folgenummer).

| | Schatten | Aktiv | Rückfall |
|---|---|---|---|
| Abonnement | `SEARCH_INDEX_SUBSCRIPTION=schatten` (schreibt `schatten-*`) | `SEARCH_INDEX_SUBSCRIPTION=aktiv`, Zustand in `events_subscription` auf `aktiv` (Admin: pausieren, dann „Fortsetzen (aktiv)“) | `SEARCH_INDEX_SUBSCRIPTION=schatten` |
| Signale | an (`ELASTICSEARCH_AUTO_INDEX=true`) | aus (`ELASTICSEARCH_AUTO_INDEX=false`) | wieder an |
| Ingestor | an (`ELASTICSEARCH_INDEXING_ENABLED=true`) | aus (`ELASTICSEARCH_INDEXING_ENABLED=false`) | wieder an |
| Vollbau | `manage.py suchindex_schatten aufbauen` | Live-Index einmal aus dem Bestand (Vollbau mit Folgenummer als Version, #527) | `reindex_elasticsearch` |
| Vergleich | `manage.py suchindex_schatten vergleichen` | – | – |
| Stand | `manage.py suchindex_schatten status` | `/metrics`: `mandari_events_lag_seconds{subscription="suchindex"}`, `mandari_search_subscription_documents_total` | – |

Stand der Phasen (in #527 als Kommentar fortgeschrieben):

- [ ] Voraussetzungen der Ereignistechnik (Abschnitt 1): Nachweise #514, Sicherung #573, Überwachung
      #510 – umgesetzt, abzuhaken nach dem Deploy
- [x] Abonnement mit Schattenziel, Vollbau, Vergleich und Doppelzustellungstest (#526, PR #819)
- [x] Schatten in Produktion seit 4. Oktober 2026 für eine Kommune (`SEARCH_INDEX_SHADOW_BODIES`)
- [ ] Bekannte Lücken geschlossen (#821): Sitzung über die Beratung, Tagesordnungspunkte, Gremien
- [ ] Vergleich mindestens 7 Tage ohne unerklärte Abweichung, danach weitere Kommunen in den Schatten
- [ ] Vollbau des Live-Index mit externer Version und Umschalten (#527), Rückfallprobe
- [ ] Signale und Ingestor-Indexierung im Folge-Release entfernt (#527)

Früheste Umschaltung damit: nach Abschluss von #821 und 7 Tagen Vergleich danach.

## 5. RIS-Projektor für Session-Mandanten (#536 → #537)

Heute übernehmen der Ingestor (Abruf der eigenen Session-Schnittstelle) bzw. `SessionMirror` und
Direktschreiber in Session die Daten der Session-Mandanten in den RIS-Bestand. Künftig schreibt ihn nur das
Abonnement `ris.session_projektor` (`hub/projections/ris_session.py`, transaktional).

| | Schatten | Aktiv | Rückfall |
|---|---|---|---|
| Abonnement | `RIS_SESSION_PROJECTOR=schatten` (schreibt die Schatten-Quelle `hub_ris_schatten`) | mit #537 | `RIS_SESSION_PROJECTOR=aus` |
| Alter Weg | Ingestor-Abgleich der Session-Quelle, Direktschreiber an | mit #537 aus | wieder an |
| Vollbau | `manage.py ris_projektor_schatten aufbauen` | mit #537 | – |
| Vergleich | `manage.py ris_projektor_schatten vergleichen [--json]` | – | – |
| Stand | `manage.py ris_projektor_schatten status`, `mandari_ris_projector_rows_total` | – | `ris_projektor_schatten loeschen --ja --abonnement` |

Bekannte Lücken: Session meldet Körperschaft, Wahlperioden, Gremien, Personen und Mitgliedschaften noch
nicht (#860). Stand der Phasen (in #536/#537 fortgeschrieben):

- [x] Abonnement mit Schattenziel, Vollbau, Vergleich und Doppelzustellungstest (#536)
- [ ] Schatten in Produktion für einen Mandanten (Session-Ereignisse für ihn eingeschaltet)
- [ ] Vergleich 14 Tage ohne unerklärte Abweichung (Akzeptanz #536)
- [ ] Lücken aus #860 geschlossen, Meldungen der Übernahme und Umschalten (#537), Rückfallprobe

## 6. Dokumentkette (#919)

Heute laden OCR-Worker und Sync des Ingestors, Dokument-Cache, Vorschau und Auftrag `file.extract_text` dieselbe
Datei unabhängig voneinander; ein Abruffehler endet als gescheiterte Texterkennung. Künftig gibt es einen Weg zur
Quelle (`hub.ris.abruf`) und einen Weg zur Erkennung (Auftrag `file.extract_text` im Dienst `worker-heavy`, liest
nur aus Ablage und Objektspeicher), später verbunden über Ereignisse
(`docs/adr/20261007-dokumentkette.md`). Ablauf und Befehle: `DEPLOYMENT.md`, „Dokumentkette einschalten“.

| | Etappe 2: Erkennung im Worker | Etappe 3: Ereigniskette | Rückfall |
|---|---|---|---|
| Schalter | `TASKS_BACKEND=journal`, dann `TEXT_EXTRACTION_RUNNER=worker` (Anwendung, Worker, Ingestor gleich) | `DOCUMENT_FETCH_SUBSCRIPTION=schatten`, dann `aktiv` | `TEXT_EXTRACTION_RUNNER=ingestor` bzw. `DOCUMENT_FETCH_SUBSCRIPTION=aus` |
| Alter Weg | OCR-Worker und Sync des Ingestors ruhen, laden keine Dateien | Zeitpläne `cache_files` und `texterkennung_einplanen` gehen in `dokumentkette_nachholen` auf | wieder an |
| Bestand | `dokumentkette umschalten` (Stichtag je Quelle) vor dem Schalter; `dokumentkette nacharbeiten` nach Freigabe | – | vor einem älteren Image `dokumentkette zuruecksetzen` |
| Stand | `mandari_files_*`, `mandari_tasks_*{queue="ocr"}` (`docs/MONITORING.md`, „Dokumentkette“) | dazu `mandari_events_lag_seconds{subscription="ris.dokumentkette"}` | – |

Abbruchkriterien zusätzlich zu Abschnitt 3: Abrufe einer Quelle scheitern häufiger als vor dem Umschalten; eine
Datei wird mehr als einmal bei der Quelle abgerufen (außer im Löschabgleich); der Rückstand
(`mandari_files_stored_without_text`, `mandari_files_fetch_retry_due`) wächst über Stunden.

- [ ] ADR angenommen (#919)
- [ ] Etappe 1 ausgeliefert (Abrufweg, Zustände, Befehle `dokumentkette …`)
- [ ] Etappe 2 eingeschaltet, Rückfallprobe (`TEXT_EXTRACTION_RUNNER=ingestor` und zurück) in der ersten Woche, 7 Tage ohne Abbruchkriterium; Nacharbeit nach Freigabe
- [ ] Etappe 3 im Schatten, Vergleich, aktiv mit Rückfallprobe
- [ ] Aufräumen im Folge-Release: Extractor und `extract-daemon` im Ingestor, `INGESTOR_STORES_FILES`, `TEXT_EXTRACTION_RUNNER`, Zeitpläne `cache_files` und `texterkennung_einplanen`

## 7. Nachfolger der Texterkennung (#919)

Neuer Text (`ris.file.text_extracted`) stößt die Verortung seines Vorgangs an und verwirft dessen
KI-Zusammenfassung (ADR Dokumentkette, `docs/adr/20261007-dokumentkette.md`, Abschnitt 9). Beide Abonnements
sind transaktional, ergänzen den bisherigen Weg und lösen ihn nicht ab: Der Zeitplan
`verortung_automatisch` bleibt das Sicherheitsnetz, Zusammenfassungen entstehen weiter nur auf Abruf.
Einen Vergleich mit einem alten Weg gibt es daher nicht; der Schatten zählt nur, was geschähe.

| | Schatten | Aktiv | Rückfall |
|---|---|---|---|
| Verortung | `GEOREF_SUBSCRIPTION=schatten` (zählt `mandari_georef_subscription_total{target="schatten"}`) | `GEOREF_SUBSCRIPTION=aktiv`, Zustand in `events_subscription` auf `aktiv` (Admin: pausieren, dann „Fortsetzen (aktiv)“); markiert Vorgänge und reiht mit `TASKS_BACKEND=journal` den Auftrag `verortung_vorgang` ein | `GEOREF_SUBSCRIPTION=aus` |
| Zusammenfassung | `SUMMARY_SUBSCRIPTION=schatten` (zählt `mandari_summary_subscription_total{target="schatten"}`) | `SUMMARY_SUBSCRIPTION=aktiv`, Zustand wie oben; verwirft die Zusammenfassung des Vorgangs | `SUMMARY_SUBSCRIPTION=aus` |
| Stand | `/metrics`: `mandari_events_lag_seconds{subscription="insight.verortung"}` bzw. `"insight.zusammenfassung"` | | |
