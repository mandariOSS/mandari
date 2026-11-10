# Architektur-Kennzahlen der Datendrehscheibe

Stand: 10. November 2026 · Issue #569 · Entscheidung:
[Vier Schichten mit durchgesetzten Abhängigkeitsregeln](adr/20260929-schichtenmodell.md), Abschnitt „Prüfung“

Ob die Entkopplung der Module vorankommt, soll messbar sein. Nach jeder Etappe der Datendrehscheibe
(Epic #476) erheben wir dieselben Kennzahlen aus dem Quellstand und halten sie hier fest:

```bash
python scripts/architektur_kennzahlen.py            # Tabelle wie in Abschnitt 1
python scripts/architektur_kennzahlen.py --json     # dazu Einzelheiten: Funktionen, Dateien, Richtungen
```

Das Skript prüft nichts und bricht keinen Lauf ab. Dass Werte nicht steigen, sichern die Ratchets und
Fitnessfunktionen, aus denen ein Teil der Zahlen stammt (Spalte „Quelle“).

## 1. Letzte Erhebung

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
  ([Umstellung](DREHSCHEIBE_UMSTELLUNG.md), Abschnitt 4).
- **K7:** je Vertrag 34 (`schichten`), 31 (`module-unabhaengig`) und 29 (`plattform-fachfrei`) Einträge;
  derselbe Import steht oft in mehreren Verträgen.

## 2. Verlauf

Eine Zeile je Erhebung, die neueste unten. „Stand“ nennt den Commit auf `dev`, auf dem die Erhebung
aufsetzt, und den Pull Request, dessen Änderungen mitgezählt sind. K8 trägt ein, wer die Zeile schreibt
(Abschnitt 4).

| Datum | Stand | Anlass | K1 | K2a | K2b | K3a | K3b | K4 | K5 | K6a | K6b | K7 | K8 |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-11-10 | `3a63808d` + #1304 | erste Erhebung | 55 | 4 | 7 | 32 | 6 | 0 | 126 | 0 | 13 | 54 | – |

## 3. Zählregeln

Gezählt wird der Betriebscode unter `mandari/` (für K2 auch `ingestor/src/`). Tests, Migrationen und
E2E-Tests zählen nie mit. Die eigenen Zählungen lesen den Syntaxbaum, ohne Django zu laden; sie sind
Näherungen mit festen Regeln, vergleichbar von Erhebung zu Erhebung, nicht mit früheren Auszählungen von
Hand. Die Ziele stammen aus den genannten Entscheidungen; „–“ heißt, dass es kein Zahlenziel gibt.

**K1 – Funktionen, die Daten eines fremden Moduls schreiben.** Regel 2 des Schichtenmodells: Jeder
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

**K2 – Schreibwege des Suchindex.** Ziel ist ein Weg: Nur das Abonnement `suchindex` schreibt den Index
([Umstellung](DREHSCHEIBE_UMSTELLUNG.md), Abschnitt 4).

- K2a zählt die Dateien, K2b die Aufrufe darin, die Dokumente in Elasticsearch schreiben oder löschen:
  am Client `index`, `delete_by_query`, `update_by_query` mit `index=`, `create`, `update`, `delete` mit
  `index=` und `id=`, `bulk` mit `operations=`, die Helfer aus `elasticsearch.helpers` (`bulk`,
  `streaming_bulk` …) sowie HTTP-Aufrufe (`post`, `put`, `delete`) auf `/_bulk`, `/_doc`, `/_update` und
  verwandte Pfade, wie sie der Ingestor nutzt.
- Lesen (`search`, `count`, `mget`) und Index-Verwaltung (`.indices.…`) zählen nicht. Das Schattenziel
  des Abonnements schreibt dieselbe Stelle wie das aktive Ziel und zählt nicht eigens.

**K3 – Mailversand.** Mails gehen nur über den Mail-Dienst `apps.common.mail` (#528); ob eine Mail im Aufruf
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

**K4 – Hintergrund-Threads im Webprozess.** Ziel null: Wiederkehrende Arbeit läuft als Auftrag oder
Zeitplan im Worker ([Aufträge und Zeitpläne](adr/20260929-auftraege-und-zeitplaene.md), #515).

- Gezählt werden Aufrufe von `threading.Thread` und `threading.Timer` und davon abgeleitete Klassen
  außerhalb des Codes, den die Fitnessfunktion als Worker führt (Liste `ERLAUBT`: `apps/events/` und der
  Metrik-Server). Die Fitnessfunktion hält den Wert bei null; `--json` nennt auch die Stellen im Worker.
- Pools, die innerhalb einer Anfrage laufen (`ThreadPoolExecutor` der Bereitschaftsprüfung), zählen nicht.

**K5 – `OParl*.objects` außerhalb von `hub` und `insight_core`.** Summe des Ratchets für die Lese-Fassade
`hub.ris.selectors` ([Kanonisches Modell](adr/20260929-kanonisches-modell.md), #522); je Datei steht der
Stand in `scripts/ris_access_baseline.json`.

**K6 – `CASCADE` auf den RIS-Bestand.** Fremdschlüssel auf den RIS-Bestand sind `PROTECT` oder `SET_NULL`
([Fremdschlüssel auf den RIS-Bestand](adr/20260929-fremdschluessel-ris-bestand.md), #524). Die
Fitnessfunktion prüft alle Modelle zur Laufzeit; ihre Ausnahmeliste ist der Bestand. K6a zählt die
Einträge aus anderen Apps als `insight_core` (seit #524 null), K6b die Daten des Bürgerportals und
abgeleitete Geodaten in `insight_core`, über die eine Entscheidung aussteht.

**K7 – import-linter-Ausnahmen.** Verschiedene Einträge in `ignore_imports` der drei Verträge
(`schichten`, `module-unabhaengig`, `plattform-fachfrei`) ohne die Freistellung der Tests, gelesen vom
Ratchet der Schichtregeln (#567). `--json` nennt die Zahl je Vertrag.

**K8 – Ereignis-Rückstand p95.** Nicht aus dem Quellstand erhebbar. Der Wert kommt aus dem Monitoring der
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
Anmerkung an der Verlaufszeile. Zugesagt sind p95 ≤ 5 s im Normalbetrieb
([Nachweise](EREIGNISTECHNIK_NACHWEISE.md)).

## 4. Fortschreibung

**Wann:** nach Abschluss jeder Etappe des Epics #476 und mit dem Aufräumen jeder Umstellung eines Weges
(Punkt in der [Checkliste](DREHSCHEIBE_UMSTELLUNG.md), Abschnitt 2.5), weil dort der alte Weg aus dem Code
verschwindet.

**Wie:** im Pull Request, der die Etappe oder Umstellung abschließt, oder in einem eigenen direkt danach.
Der Zweig steht dabei auf dem aktuellen `dev` (`git fetch origin`, dann rebasen oder `origin/dev`
einmischen). Belegt wird der Stand, auf dem der Zweig aufsetzt, zusammen mit der Nummer des Pull Requests:
Den Commit des Zweigs fasst die Merge-Queue zu einem neuen zusammen, den es vorher nicht gibt. So stimmt die
Zeile ohne Nacharbeit nach dem Merge.

1. `python scripts/architektur_kennzahlen.py` ausführen und die Tabelle in Abschnitt 1 ersetzen, mit
   Datum, Stand und Anlass in der Zeile darüber.
2. `python scripts/architektur_kennzahlen.py --verlauf --anlass "Etappe 2" --pr <Nummer>` ausführen und die
   letzte Zeile der Ausgabe an die Tabelle in Abschnitt 2 anhängen. Die Spalte „Stand“ nennt den Commit auf
   `dev`, auf dem der Zweig aufsetzt (`git merge-base HEAD origin/dev`), und den Pull Request. Ohne `--pr`,
   auf `dev` selbst ausgeführt, nennt sie nur den Commit.
3. K8 aus dem Monitoring ablesen (Abschnitt 3) und in der Verlaufszeile statt „–“ eintragen, mit Einheit
   (`0,4 s`).
4. Gestiegene Werte in zwei Sätzen unter Abschnitt 1 erklären; `--json` zeigt, welche Funktionen oder
   Dateien dazugekommen sind.
5. Die Zeile aus Abschnitt 2 als Kommentar in das Issue der Etappe bzw. Umstellung kopieren.

Kommt eine Kennzahl hinzu oder ändert sich eine Zählregel, bekommt die Verlaufstabelle eine neue Spalte
bzw. die Regel einen Vermerk mit Datum in Abschnitt 3; ältere Zeilen bleiben, wie sie erhoben wurden.
`mandari/apps/common/tests/test_architektur_kennzahlen.py` hält Tabellen und Skript beieinander.
