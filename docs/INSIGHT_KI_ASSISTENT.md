# KI-Assistent in Insight: Antworten aus den Ratsdaten

Stand: Issue #899 (10/2026). Seite im Bürgerportal: `/insight/chat/`, Endpunkt `POST /insight/chat/api/message/`.

## Überblick

Der KI-Assistent beantwortet Fragen zu Sitzungen, Tagesordnungen, Vorlagen, Beschlüssen, Gremien und Personen
der gewählten Kommune. Er arbeitet mit **Werkzeugen** (OpenAI-kompatibles Function Calling) statt mit Volltext:
Das Modell fragt die strukturierten Ratsdaten ab und verlinkt die Insight-Seiten in seiner Antwort.

Bis 10/2026 bekam das Modell die fünf besten Suchtreffer mit je bis zu 4.000 Token Text und kannte weder das
heutige Datum noch Termine. „Welche Sitzungen finden diese Woche statt?“ endete mit einem Verweis auf fremde
Kalender.

| Datei | Inhalt |
|-------|--------|
| `insight_ai/services/chat_service.py` | Ablauf je Frage: Systemprompt, Werkzeugrunden, Antwort, Verbrauch |
| `insight_ai/services/chat_tools.py` | Beschreibung und Umsetzung der Werkzeuge, Quellen-Kacheln |
| `insight_ai/services/prompts.py` | Systemprompt mit Datum, Wochentag, Uhrzeit und Kommune |
| `insight_ai/providers/openai_compatible.py` | `chat_with_tools`: eine Runde mit Werkzeugen, Ausweichmodell, Antwort ohne Werkzeuge |
| `insight_ai/providers/__init__.py` | `chat_provider()`: einzige Stelle, an der der Anbieter gewählt wird |
| `hub/ris/selectors.py` | Lese-Fassade, Abschnitt „Öffentlicher Bestand“ |
| `insight_core/views/chat.py` | Endpunkt: Einwilligung, Nutzungsgrenzen, Filter, Protokoll |

## Ablauf

1. Der Endpunkt prüft wie bisher Einwilligung, Nutzungsgrenzen (Gäste 5 am Tag/10 in der Woche, Konten 25/100)
   und die Filter für personenbezogene Angaben und Anweisungsversuche.
2. Der Systemprompt nennt das heutige Datum (Europe/Berlin), Wochentag, Uhrzeit, Beginn und Ende der laufenden
   Woche und die Kommune. Er verlangt Sie-Form, den Produktnamen „mandari Insight“ und Links auf die
   Insight-Seiten; Verweise auf fremde Kalender sind ausgeschlossen, wenn die Daten in mandari stehen.
3. Höchstens `INSIGHT_CHAT_MAX_TOOL_ROUNDS` Runden (Standard 4) ruft das Modell Werkzeuge auf, je Runde höchstens
   fünf. Danach, bei Ablauf der Zeit oder mit eigenem Werkzeugmodell folgt eine abschließende Runde ohne
   Werkzeuge (`tool_choice: none`). Das Zeitlimit je Antwort ist `INSIGHT_CHAT_TIME_LIMIT_SECONDS` (Standard 90 s).
4. Die Quellen-Kacheln unter der Antwort sind die Einträge aus den Werkzeugergebnissen, die die Antwort verlinkt;
   verlinkt sie keinen, die Einträge der Detailwerkzeuge.

Eine Frage zählt für die Nutzungsgrenzen einmal, unabhängig von der Zahl der Runden.

## Werkzeuge

| Werkzeug | Liefert |
|----------|---------|
| `sitzungen_im_zeitraum(von, bis, gremium?, tagesordnung?)` | Sitzungen (höchstens 92 Tage, 30 Einträge) als Zeilen „Beginn \| Gremium \| Ort \| Link“; mit `tagesordnung=true` bei bis zu drei Sitzungen gleich die Tagesordnungen |
| `sitzung(id)` | Sitzung mit Tagesordnung (Punkte, Ergebnisse, beratene Vorlagen) |
| `vorgaenge_suchen(text, von?, bis?, gremium?)` | Vorgänge mit Stand-Satz; trifft die Drucksachennummer genau einen Vorgang, gleich dessen Einzelheiten |
| `vorgang(id)` | Stand, Beratungsfolge, Beschlusstext, Zusammenfassung, Dokumentliste |
| `gremien(suchtext?)` | bestehende Gremien |
| `personen(name)` | Personen mit laufenden Mitgliedschaften (ohne E-Mail, Telefon, Geschlecht, Foto) |
| `dokumente_suchen(text)` | Dokumente mit kurzem Ausschnitt (höchstens 300 Zeichen) |
| `dokument_abschnitt(id, frage)` | die zur Frage passenden Abschnitte eines Dokuments (höchstens 3 × 1.000 Zeichen) |

Kennungen nehmen die Werkzeuge als UUID oder als Insight-Link entgegen. Ergebnisse sind knappes JSON, Listen als
Zeilen; zu lange Ergebnisse kürzt `dump` am Ende der längsten Liste und nennt die Zahl der weggelassenen Einträge.
Fehler kommen mit festen Texten zurück, nie mit Ausnahmetexten.

## Datenschutz und Grenzen

- **Nur die gewählte Kommune:** Jede Abfrage ist auf die Kommune der Sitzung beschränkt; Treffer der Volltextsuche
  werden gegen die Datenbank geprüft. Ist die Veröffentlichung der Kommune abgeschaltet oder zurückgenommen
  (`insight_core.publication`), gibt es keine Werkzeuge.
- **Nur Öffentliches:** keine gelöschten oder zurückgenommenen Einträge, keine Dateien, die die Kommune entfernt
  hat (`source_missing_since`). Nichtöffentliche Tagesordnungspunkte erscheinen nur mit Nummer; Name, Ergebnis und
  Beschlusstext bleiben weg, auch im Beratungsverlauf einer Vorlage.
- **Personen:** nur Name, Link und laufende Mitgliedschaften (Gremium, Rolle).
- **Nur lesend:** Die Werkzeuge lesen ausschließlich über die Lese-Fassade `hub/ris/selectors.py`.
- Texte aus Dokumenten gelten im Systemprompt als Daten, nicht als Anweisungen.

## Einstellungen

| Variable | Standard | Wirkung |
|----------|----------|---------|
| `INSIGHT_CHAT_TOOL_MODEL` | leer | eigenes Modell nur für die Werkzeugrunden; die Antwort schreibt das Hauptmodell des Anbieters |
| `INSIGHT_CHAT_MAX_TOOL_ROUNDS` | 4 | Runden mit Werkzeugen je Antwort |
| `INSIGHT_CHAT_TIME_LIMIT_SECONDS` | 90 | Zeitlimit je Antwort (mindestens 45) |

**Anbieter:** Die Werkzeugrunden setzen nur eine OpenAI-kompatible Schnittstelle mit Function Calling voraus
(`OpenAICompatibleProvider`); Adresse, Schlüssel und Modelle sind Konfiguration. Welcher Anbieter in Betrieb ist,
entscheidet `chat_provider()` (Anbieterwahl nach Issue #950: Verarbeitung nur in Europa). Ein Planungsschritt mit
JSON-Ausgabe ist nicht nötig. Scheitert ein Modell, versucht der Anbieter Haupt- und Ausweichmodell; lehnt er
Werkzeuge ab (HTTP 400/422), antwortet dasselbe Modell ohne sie, der bisherige Verlauf geht als Text mit. Scheitert
eine Werkzeugrunde ganz, antwortet die Schlussrunde mit den bis dahin geholten Ergebnissen.

**Günstigeres Werkzeugmodell:** Die Werkzeugwahl ist eine einfache Zuordnung (Frage → Werkzeug und Argumente);
dafür genügt technisch ein Modell ohne Denkphase, die Schleife ist vom Modell unabhängig getestet. Ob die Qualität
mit echten Fragen reicht, muss ein Vergleich im Betrieb zeigen: `INSIGHT_CHAT_TOOL_MODEL` auf ein solches Modell
setzen und die Verbrauchswerte (siehe unten) sowie Stichproben der Antworten vergleichen. Der Standard bleibt
das Hauptmodell für alles.

## Verbrauch

Je Antwort protokolliert der Dienst Eingabe- und Ausgabe-Token, Runden, Werkzeuge und Modelle (Log
`insight_ai.services.chat_service`, Felder `chat_prompt_tokens`, `chat_completion_tokens`, `chat_rounds`,
`chat_tool_calls`) und speichert Eingabe-, Ausgabe-Token und Modellaufrufe in `ChatUsage` (Verwaltung →
Chat-Nutzungen; Migration `insight_core 0056`).

Messung vorher/nachher: `insight_ai/tests/test_token_messung.py` (lokal gezählt, Anbieter gemockt, drei Zeichen
je Token wie die bisherige Schätzung; `pytest -s` druckt die Tabelle). Grundlage für „vorher“ sind fünf
Suchtreffer mit 40.000, 15.000, 9.000, 4.000 und 1.500 Zeichen Text.

| Frage | vorher | nachher | Modellaufrufe | Anteil |
|---|---:|---:|---:|---:|
| Welche Sitzungen finden diese Woche statt? | 13.290 | 3.771 | 2 | 28 % |
| Was steht morgen im Rat auf der Tagesordnung? | 13.291 | 5.126 | 2 | 39 % |
| Wie ist der Stand von Vorlage V/2026/0123? | 13.290 | 3.310 | 2 | 25 % |
| Was wurde zum Radweg an der Musterstraße beschlossen? | 13.293 | 7.739 | 4 | 58 % |
| Welche Ausschüsse gibt es? | 13.284 | 3.065 | 2 | 23 % |
| Summe | 66.448 | 23.011 | | 35 % |

Die vierte Frage ist der ungünstige Fall mit Dokumentabschnitt; mit dem Beschlusstext aus `vorgang` genügen drei
Aufrufe. Echte Tokenizer zählen Kennungen (UUID in Links) teurer als drei Zeichen je Token; das Verhältnis bleibt
ähnlich, weil die Werte vorher fast nur aus Fließtext bestehen.

## Tests

- `insight_ai/tests/test_chat_werkzeuge.py` – Werkzeuge mit festen Daten der Musterstadt
- `insight_ai/tests/test_chat_service.py` – Fragen von Anfang bis Ende mit Drehbuch-Anbieter, Endpunkt
- `insight_ai/tests/test_anbieter_werkzeuge.py` – OpenAI-kompatibler Anbieter mit ersetzter Schnittstelle
- `insight_ai/tests/test_token_messung.py` – Vorher-/Nachher-Messung
- `hub/ris/tests/test_selectors_oeffentlich.py` – Lese-Fassade, öffentlicher Bestand
