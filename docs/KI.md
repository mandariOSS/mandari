# KI-Funktionen: Assistent, MCP-Server und Kennzeichnung

Stand: 11/2026 (Issues #899, #938, #950, #1208, #1222). Diese Notiz fasst zusammen, wie mandari Sprachmodelle
einsetzt: den KI-Assistenten im Bürgerportal, den MCP-Server für KI-Werkzeuge anderer Anbieter, beide mit denselben
Werkzeugen, und die Kennzeichnung aller KI-Texte nach EU AI Act Art. 50.

## 1. Überblick, Anbieter und Rollen

Der **KI-Assistent** beantwortet Fragen zu Sitzungen, Tagesordnungen, Vorlagen, Beschlüssen, Gremien und Personen
der gewählten Kommune (Seite `/insight/chat/`, Endpunkt `POST /insight/chat/api/message/`). Er arbeitet mit
**Werkzeugen** (OpenAI-kompatibles Function Calling) statt mit Volltext: Das Modell fragt die strukturierten
Ratsdaten ab und verlinkt die Insight-Seiten in seiner Antwort. Der **MCP-Server** stellt dieselben Werkzeuge über
das [Model Context Protocol](https://modelcontextprotocol.io) bereit (Abschnitt 4).

| Datei | Inhalt |
|-------|--------|
| `insight_ai/services/chat_service.py` | Ablauf je Frage: Systemprompt, Werkzeugrunden, Antwort, Verbrauch |
| `insight_ai/services/chat_tools.py` | Beschreibung und Umsetzung der Werkzeuge, Quellen-Kacheln |
| `insight_ai/services/prompts.py` | Systemprompt mit Datum, Wochentag, Uhrzeit und Kommune |
| `insight_ai/providers/openai_kompatibel.py` | `OpenAIKompatiblerProvider.chat_with_tools`: eine Runde mit Werkzeugen, Ausweichmodell, Antwort ohne Werkzeuge |
| `insight_ai/providers/__init__.py` | `get_insight_provider()`: Endpunkt des Bürgerportals aus der zentralen KI-Konfiguration (Issue #950); `get_summary_provider()`: Anbieter der Zusammenfassungen |
| `insight_ai/mcp.py` | MCP-Server |
| `hub/ris/selectors.py` | Lese-Fassade, Abschnitt „Öffentlicher Bestand“ |
| `insight_core/views/chat.py` | Endpunkt des Assistenten: Einwilligung, Nutzungsgrenzen, Filter, Protokoll |

**Anbieter.** Es gibt keinen fest eingebauten Anbieter (Issue #950). mandari nutzt das Sprachmodell eines Anbieters,
das über eine OpenAI-kompatible Schnittstelle angebunden ist (`apps.common.ki_anbieter`, Admin → KI-Einstellungen):
Anbieter, Bürgerportal-Modell, Ausweichmodell, Schlüssel und Antwortlänge stehen dort, die Adresse muss in
`KI_ERLAUBTE_HOSTS` stehen. Ohne Einrichtung zeigt das Bürgerportal keinen Chat, und es geht keine Anfrage hinaus.
Für die Zusammenfassungen öffentlicher Dokumente kann der Betreiber stattdessen die Claude API von Anthropic wählen
(Issue #1208, `get_summary_provider()`). Dieselbe Anfrage im Tagesbatch prüft dann auch die automatisch gefundenen
Orte eines Vorgangs (Issue #1222, `insight_core/services/verortung_pruefung.py`). Bei Claude erfolgt die
Verarbeitung auch außerhalb der EU (USA).

**Rollen nach Art. 50.** mandari baut die KI-Funktionen selbst und ist damit **Anbieter** nach Abs. 1 und 2. Das gilt
auch für die quelloffene Software, die Dritte selbst betreiben. **Betreiber** nach Abs. 4 ist, wer die Plattform
einsetzt und KI-Text veröffentlicht. Auf mandari.de ist das für Insight mandari selbst. Bei Work ist es die Fraktion
bzw. Organisation. Betreibt eine Kommune oder Partei mandari selbst, ist sie Betreiberin. Die Kennzeichnung ist in
der Software eingebaut, damit Betreiber sie nicht selbst nachrüsten müssen.

## 2. KI-Assistent im Bürgerportal

### Ablauf

1. Der Endpunkt prüft Einwilligung, Nutzungsgrenzen (Gäste 5 am Tag/10 in der Woche, Konten 25/100) und die Filter
   für personenbezogene Angaben und Anweisungsversuche.
2. Der Systemprompt nennt das heutige Datum (Europe/Berlin), Wochentag, Uhrzeit, Beginn und Ende der laufenden
   Woche und die Kommune. Er verlangt Sie-Form, den Produktnamen „mandari Insight“ und Links auf die
   Insight-Seiten; Verweise auf fremde Kalender sind ausgeschlossen, wenn die Daten in mandari stehen.
3. Höchstens `INSIGHT_CHAT_MAX_TOOL_ROUNDS` Runden (Standard 4) ruft das Modell Werkzeuge auf, je Runde höchstens
   fünf. Danach oder bei Ablauf der Zeit folgt eine abschließende Runde ohne Werkzeuge (`tool_choice: none`). Das
   Zeitlimit je Antwort ist `INSIGHT_CHAT_TIME_LIMIT_SECONDS` (Standard 90 s).
4. Die Quellen-Kacheln unter der Antwort sind die Einträge aus den Werkzeugergebnissen, die die Antwort verlinkt;
   verlinkt sie keinen, die Einträge der Detailwerkzeuge.

Eine Frage zählt für die Nutzungsgrenzen einmal, unabhängig von der Zahl der Runden. Scheitert eine Antwort,
nachdem schon Modellaufrufe gelaufen sind, zählt sie trotzdem, mit ihrem Verbrauch.

**Kostenbremse:** Vor jeder Frage summiert der Endpunkt Modellaufrufe und Token aller Chat-Nutzungen seit Mitternacht
(alle Besucher und Konten zusammen). Ist `INSIGHT_CHAT_DAILY_MAX_CALLS` oder `INSIGHT_CHAT_DAILY_MAX_TOKENS`
erreicht, geht bis zum nächsten Tag keine Anfrage mehr an den Anbieter; die Seite meldet, dass das Kontingent für
heute ausgeschöpft ist (HTTP 503, `daily_budget_reached`). Die Nutzungsgrenzen je Besucher lassen sich über viele
Adressen umgehen, die Tagesobergrenze nicht. Eine laufende Antwort kann sie um höchstens eine Antwort überschreiten.

**Modell und Ausweichmodell.** Die Werkzeugrunden setzen nur eine OpenAI-kompatible Schnittstelle mit Function
Calling voraus (`OpenAIKompatiblerProvider`, dieselbe Klasse wie für die Verortung und für die Zusammenfassungen,
sofern dort nicht Claude gewählt ist). Ein Planungsschritt mit JSON-Ausgabe ist nicht nötig. Das Ausweichmodell gilt
wie bei allen KI-Aufrufen des Bürgerportals nur nach 404, 408, 429, 5xx oder Zeitüberschreitung. Lehnt der Anbieter
Werkzeuge ab (HTTP 400/422), antwortet dasselbe Modell ohne sie, der bisherige Verlauf geht als Text mit. Scheitert
eine Werkzeugrunde ganz, antwortet die Schlussrunde mit den bis dahin geholten Ergebnissen.

**Günstigeres Modell:** Die Werkzeugwahl ist eine einfache Zuordnung (Frage → Werkzeug und Argumente); dafür genügt
technisch ein Modell ohne Denkphase, die Schleife ist vom Modell unabhängig getestet. Ob die Qualität mit echten
Fragen reicht, zeigt ein Vergleich im Betrieb: das Bürgerportal-Modell in den KI-Einstellungen umstellen und die
Verbrauchswerte sowie Stichproben der Antworten vergleichen. Ein eigenes Modell nur für die Werkzeugrunden gibt es
nicht.

### Einstellungen des Assistenten

| Variable | Standard | Wirkung |
|----------|----------|---------|
| `INSIGHT_CHAT_MAX_TOOL_ROUNDS` | 4 | Runden mit Werkzeugen je Antwort |
| `INSIGHT_CHAT_TIME_LIMIT_SECONDS` | 90 | Zeitlimit je Antwort (mindestens 45) |
| `INSIGHT_CHAT_DAILY_MAX_CALLS` | 500 | Modellaufrufe aller Antworten zusammen je Tag (Kostenbremse, mindestens 1) |
| `INSIGHT_CHAT_DAILY_MAX_TOKENS` | 1.000.000 | Token aller Antworten zusammen je Tag (Kostenbremse, mindestens 1) |

Die Standardwerte der Kostenbremse reichen nach der Messung unten (2 bis 4 Modellaufrufe und 3.000 bis 8.000 Token
je Frage) für etwa 125 bis 250 Fragen am Tag. Sie sind bewusst knapp; den Kostenrahmen legt der Betreiber fest.

### Verbrauch

Je Antwort protokolliert der Dienst Eingabe- und Ausgabe-Token, Runden, Werkzeuge und Modelle (Log
`insight_ai.services.chat_service`, Felder `chat_prompt_tokens`, `chat_completion_tokens`, `chat_rounds`,
`chat_tool_calls`) und speichert Eingabe-, Ausgabe-Token und Modellaufrufe in `ChatUsage` (Verwaltung →
Chat-Nutzungen; Migration `insight_core 0058`). Dieselben Werte nutzt die Kostenbremse.

Messung: `insight_ai/tests/test_token_messung.py` (lokal gezählt, Anbieter gemockt, drei Zeichen je Token;
`pytest -s` druckt die Tabelle).

| Frage | Token | Modellaufrufe |
|---|---:|---:|
| Welche Sitzungen finden diese Woche statt? | 3.771 | 2 |
| Was steht morgen im Rat auf der Tagesordnung? | 5.126 | 2 |
| Wie ist der Stand von Vorlage V/2026/0123? | 3.310 | 2 |
| Was wurde zum Radweg an der Musterstraße beschlossen? | 7.739 | 4 |
| Welche Ausschüsse gibt es? | 3.065 | 2 |

Die vierte Frage ist der ungünstige Fall mit Dokumentabschnitt; mit dem Beschlusstext aus `vorgang` genügen drei
Aufrufe. Echte Tokenizer zählen Kennungen (UUID in Links) teurer als drei Zeichen je Token.

## 3. Werkzeuge

Assistent und MCP-Server nutzen dieselben Werkzeuge (`insight_ai/services/chat_tools.py`). Im MCP-Server nennt jedes
Werkzeug zusätzlich die Kommune (`kommune`: Kurzname aus `kommunen` oder Kennung einer gelisteten Kommune); im
Assistenten gilt die gewählte Kommune. `kommunen` gibt es nur im MCP-Server.

| Werkzeug | Liefert | Argumente |
|----------|---------|-----------|
| `kommunen` | gelistete Kommunen mit Kurzname, ohne abgeschaltete oder zurückgenommene (nur MCP) | `suchtext?` |
| `sitzungen_im_zeitraum` | Sitzungen (höchstens 92 Tage, 30 Einträge) als Zeilen „Beginn \| Gremium \| Ort \| Link“; mit `tagesordnung=true` bei bis zu drei Sitzungen gleich die Tagesordnungen | `von`, `bis`, `gremium?`, `tagesordnung?` |
| `sitzung` | Sitzung mit Tagesordnung (Punkte, Ergebnisse, beratene Vorlagen) | `id` |
| `vorgaenge_suchen` | Vorgänge mit Stand-Satz; trifft die Drucksachennummer genau einen Vorgang, gleich dessen Einzelheiten | `text`, `von?`, `bis?`, `gremium?` |
| `vorgang` | Stand, Beratungsfolge, Beschlusstext, Zusammenfassung (dazu `zusammenfassung_ki_erzeugt: true`, Abschnitt 6), Dokumentliste | `id` |
| `gremien` | bestehende Gremien | `suchtext?` |
| `personen` | Personen mit laufenden Mitgliedschaften (ohne E-Mail, Telefon, Geschlecht, Foto) | `name` |
| `dokumente_suchen` | Dokumente mit kurzem Ausschnitt (höchstens 300 Zeichen) | `text` |
| `dokument_abschnitt` | die zur Frage passenden Abschnitte eines Dokuments (höchstens 3 × 1.000 Zeichen) | `id`, `frage` |

Kennungen nehmen die Werkzeuge als UUID oder als Insight-Link entgegen. Ergebnisse sind knappes JSON, Listen als
Zeilen; zu lange Ergebnisse kürzt `dump` am Ende der längsten Liste und nennt die Zahl der weggelassenen Einträge.
Fehler kommen mit festen Texten zurück, nie mit Ausnahmetexten.

## 4. MCP-Server

KI-Werkzeuge anderer Anbieter (Desktop-Assistenten, Entwicklungsumgebungen, eigene Agenten) können die öffentlichen
Ratsinformationen aus mandari Insight über das Model Context Protocol abfragen. Der Server ist **öffentlich und nur
lesend**.

**Standard: aus.** Die Freischaltung entscheidet der Betrieb (`INSIGHT_MCP_ENABLED=true`). Ausgeschaltet antwortet
der Endpunkt mit 404 wie ein unbekannter Pfad.

### Endpunkt

`https://<instanz>/insight/mcp` – Transport „Streamable HTTP“ (Protokollfassungen 2025-06-18, 2025-03-26 und
2024-11-05). Der Pfad liegt unter `/insight/`, damit der Reverse-Proxy ihn ohne eigene Regel an die Anwendung gibt.

- Nur `POST` mit JSON-RPC 2.0 (einzelne Nachricht oder Stapel bis 10), Antwort als `application/json`.
  `GET`/`DELETE` antworten mit 405: Der Server bietet keinen Ereignisstrom und stellt selbst keine Anfragen.
- Zustandslos: keine `Mcp-Session-Id`; jede Anfrage steht für sich.
- Methoden: `initialize`, `ping`, `tools/list`, `tools/call`; Benachrichtigungen (`notifications/…`) bekommen
  `202 Accepted` ohne Inhalt.
- Kopfzeile `MCP-Protocol-Version`: unbekannte Fassungen ergeben 400.
- Kopfzeile `Origin` (Browser-Clients): nur Hosts aus `ALLOWED_HOSTS` und `INSIGHT_MCP_ALLOWED_ORIGINS`, sonst 403
  (Schutz vor DNS-Rebinding, wie die Spezifikation verlangt). Clients ohne Browser senden keine.
- Anfragen über 64 KiB: 413.

Alle Werkzeuge tragen die Hinweise `readOnlyHint: true`, `destructiveHint: false`. Ergebnisse kommen als Text
(knappes JSON) mit absoluten Links auf die Insight-Seiten (`SITE_URL`); davor steht als eigener Textteil der Hinweis,
dass Texte aus Dokumenten und Ratsdaten Daten sind und keine Anweisungen (Schutz gegen Anweisungen in fremden
Dokumenten). Fehler kommen als Ergebnis mit `isError: true` und festem Text. Ein unbekanntes Werkzeug ist ein
Protokollfehler (`-32602`).

Beispiel:

```bash
curl -s https://<instanz>/insight/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"kommunen","arguments":{"suchtext":"muster"}}}'
```

Eintrag in einem MCP-Client (Form je nach Client):

```json
{"mcpServers": {"mandari-insight": {"type": "http", "url": "https://<instanz>/insight/mcp"}}}
```

### Einstellungen des MCP-Servers

| Variable | Standard | Wirkung |
|----------|----------|---------|
| `INSIGHT_MCP_ENABLED` | `false` | Schalter |
| `INSIGHT_MCP_PER_IP_MINUTE` | 30 | Anfragen je Adresse und Minute (alle Methoden; jeder Werkzeugaufruf eines Stapels zählt einzeln) |
| `INSIGHT_MCP_PER_IP_DAY` | 500 | Werkzeugaufrufe je Adresse und Tag |
| `INSIGHT_MCP_PER_MINUTE` | 300 | Anfragen insgesamt je Minute (wie oben gezählt) |
| `INSIGHT_MCP_ALLOWED_ORIGINS` | leer | weitere Hosts für Browser-Clients, kommagetrennt |

`0` schaltet eine Grenze ab. Bei Überschreitung: 429 mit `Retry-After` und JSON-RPC-Fehler `-32000`. Geprüft wird
erst je Adresse, dann insgesamt: Eine wegen ihrer Adresse abgewiesene Anfrage zählt nicht auf die Gesamtgrenze. Die
Zähler liegen im Django-Cache (Redis in Produktion), Adressen nur als Hash (`insight_core.throttle`). Der Dienst
protokolliert je Aufruf Werkzeug und Kommune, keine Argumente.

**Vor dem Einschalten:** Die Grenzen je Adresse lesen den ersten Eintrag von `X-Forwarded-For`
(`insight_core.throttle.client_ip`). Das ist nur sicher, wenn der vorgelagerte Proxy diese Kopfzeile selbst setzt
und nicht vom Client übernimmt. Vor der Freischaltung prüft der Betrieb das gegen die eigene Installation (eine
gefälschte Kopfzeile darf die gezählte Adresse nicht ändern).

## 5. Datenschutz und Grenzen

Für Assistent und MCP-Server gleich:

- **Nur gelistete Kommunen, je Abfrage eine:** Jede Abfrage ist auf eine Kommune beschränkt; Treffer der
  Volltextsuche werden gegen die Datenbank geprüft. Nicht gelistete Kommunen (etwa Pilotquellen oder die
  Demo-Kommune) sind nicht erreichbar. Ist die Veröffentlichung der Kommune abgeschaltet oder zurückgenommen
  (`insight_core.publication`), gibt es keine Werkzeuge bzw. keine Ergebnisse.
- **Nur Öffentliches:** keine gelöschten oder zurückgenommenen Einträge, keine Dateien, die die Kommune entfernt
  hat (`source_missing_since`). Nichtöffentliche Tagesordnungspunkte erscheinen nur mit Nummer; Name, Ergebnis und
  Beschlusstext bleiben weg, auch im Beratungsverlauf einer Vorlage.
- **Personen:** nur Name, Link und laufende Mitgliedschaften (Gremium, Rolle).
- **Nur lesend:** Die Werkzeuge lesen ausschließlich über die Lese-Fassade `hub/ris/selectors.py`; es gibt keine
  Schreibwege.
- Texte aus Dokumenten gelten als Daten, nicht als Anweisungen (Systemprompt bzw. Hinweis vor den MCP-Ergebnissen).

## 6. Kennzeichnung nach EU AI Act Art. 50

Die Transparenzpflichten aus Art. 50 gelten seit dem 02.08.2026. Für Systeme, die vorher in Verkehr gebracht wurden,
gilt die Kennzeichnungspflicht nach Abs. 2 ab dem 02.12.2026 (Issue #938). Die rechtliche Bewertung ist eine
Einschätzung aus der Entwicklung und ersetzt keine Rechtsberatung.

### Was Art. 50 verlangt

- **Abs. 1 (Anbieter):** Wer mit einer KI interagiert, erfährt das, sofern es nicht offensichtlich ist.
- **Abs. 2 (Anbieter):** Systeme, die synthetischen Text erzeugen, kennzeichnen ihre Ausgaben maschinenlesbar als
  KI-erzeugt. Ausgenommen sind unterstützende Funktionen für die übliche Bearbeitung, die den Inhalt nicht wesentlich
  verändern.
- **Abs. 4 (Betreiber):** Wer KI-Text veröffentlicht, um die Öffentlichkeit über Angelegenheiten von öffentlichem
  Interesse zu informieren, legt offen, dass er KI-erzeugt ist. Das entfällt, wenn ein Mensch den Text geprüft hat
  und jemand redaktionell verantwortlich ist.
- Freie und quelloffene KI-Systeme sind von der Verordnung weitgehend ausgenommen, **Art. 50 gilt für sie aber**
  (Art. 2 Abs. 12). Die Pflichten treffen deshalb auch den Selbstbetrieb von mandari.

### Bestandsaufnahme

| Funktion | Code | Wer sieht den Text | Menschliche Prüfung | Rolle mandari | Kennzeichnung |
|----------|------|--------------------|---------------------|---------------|---------------|
| Zusammenfassung eines Vorgangs (Insight), auch in Einfacher Sprache | `insight_ai/services/summarizer.py`, `insight_core/views/papers.py`, `templates/partials/paper_summary.html`, Vorschau `insight_core/services/kurzfassung.py`, `templates/cotton/insight/ki_vorschau.html`; Einfache Sprache in `OParlPaper.summary_einfach` (#1220) | **öffentlich:** Vorgangsseite („Worum es geht“, umschaltbar auf Einfache Sprache), Vorgangsliste und Suchtreffer (erster Absatz als Vorschauzeile, #1271), Description und JSON-LD der Seite, OParl-API, MCP-Werkzeug `vorgang` | keine; entsteht auf Abruf durch Besucher oder vorab im Batch (Claude API, #1208) und wird gespeichert; vorher prüft ein Programm Zahlen, Daten, Fraktionen, wörtliche Übernahmen und Hausnummern gegen das Material (`insight_ai/services/zusammenfassung_pruefung.py`, #1271), das ersetzt keine menschliche Prüfung | Anbieter (Abs. 2) und auf mandari.de Betreiber (Abs. 4) | sichtbar: „KI-Kurzfassung“ am Titel „Worum es geht“ und „Mit KI zusammengefasst; Fehler sind möglich. Maßgeblich sind die Dokumente.“ unter dem Text; über Vorgangsliste und Suchtreffern einmal „Die Zeilen unter den Titeln fasst eine KI aus den Dokumenten zusammen. Maßgeblich sind die Dokumente.“, vor jeder Zeile für Screenreader „KI-Kurzfassung:“ (Anbieter, Modell und Verarbeitungsort nennt die Datenschutzerklärung; gespeichert in `OParlPaper.summary_hinweis` für die Auswertung je Modell), und „KI-Zusammenfassung:“ in Description/JSON-LD; maschinenlesbar: `data-ai-generated="true"` (auch an jeder Vorschauzeile und an der Fassung in Einfacher Sprache), `mandari:summaryAiGenerated`, `zusammenfassung_ki_erzeugt` |
| Dieselbe Zusammenfassung in Work | `templates/work/ris/neu/vorgang.html`, `templates/work/ris/paper_detail.html` | intern: Mitglieder der Organisation | keine | Anbieter (Abs. 2) | sichtbar: „Die Zusammenfassung wurde mit Hilfe von KI erstellt. Sie kann Fehler enthalten.“ (seit #938 auch im bisherigen Rahmen); `data-ai-generated="true"` |
| KI-Assistent (Insight) | `insight_core/views/chat.py`, `insight_ai/services/chat_service.py`, `templates/partials/chat_nachrichten.html` | nur die fragende Person; Antworten werden nicht gespeichert | keine (Antwort an eine Person, keine Veröffentlichung) | Anbieter (Abs. 1 und 2) | sichtbar: Seitentitel „KI-Assistent“, Einwilligung mit Anbieter und Ort, Satz „Antworten erstellt eine KI aus öffentlichen Ratsdokumenten. Sie können Fehler enthalten.“; maschinenlesbar: `data-ai-generated="true"` an jeder Antwort, Feld `ai_generated: true` in `POST /insight/chat/api/message/` |
| Fragen an die Ratsdaten (Work) | `apps/work/ris/fragen.py`, `templates/work/ris/partials/frage_antwort.html` | intern: die fragende Person | keine (nur für die eigene Recherche) | Anbieter (Abs. 1 und 2) | sichtbar: „KI-Antwort aus den öffentlichen Ratsdaten – maßgeblich sind die Quellen.“; `data-ai-generated="true"` |
| Schreibhilfe und Co-Editor (Work) | `apps/work/motions/services.py`, `apps/work/motions/views/actions.py`, `templates/work/motions/partials/_editor_ai_panel.html` | intern: die schreibende Person im KI-Bereich des Editors | ja: Vorschläge werden erst mit „Übernehmen“ Teil des Dokuments, das die Fraktion verantwortet | Anbieter (Abs. 1 und 2); die Fraktion ist Betreiberin, Abs. 4 entfällt durch Prüfung und redaktionelle Verantwortung | sichtbar: KI-Bereich mit Hinweis „Ich bin Ihr KI-Assistent“; maschinenlesbar: Feld `ai_generated: true` in `POST /work/<org>/documents/ai/`, `data-ai-generated` an jeder KI-Nachricht |
| KI-Verortung (Insight) | `insight_core/services/georeferencing.py` | öffentlich als Ort auf der Karte | keine | kein synthetischer Text: Die KI zieht Ortsangaben aus dem vorhandenen Text, die Herkunft steht als `source="ai"` („KI-Extraktion“) an der Verortung | nicht nach Art. 50 Abs. 2 erforderlich |
| KI-Prüfung der Verortung (Insight) | `insight_core/services/verortung_pruefung.py` (#1222, im Tagesbatch der Zusammenfassungen) | öffentlich nur in der Wirkung: Orte, die Claude als „passt nicht“ beurteilt, verschwinden von der Karte; ergänzte Orte stehen mit Herkunft `ai_check` („KI-Prüfung“) auf der Karte | im Admin: Filter „KI-Prüfung“, ausgeblendete Orte lassen sich mit „Bestätigen“ zurückholen | kein synthetischer Text: Urteil und Begründung bleiben intern, neue Orte geokodiert die bestehende Pipeline, nie die KI | nicht nach Art. 50 Abs. 2 erforderlich; die Herkunft steht an der Verortung |
| KI-Protokollentwurf (Session) | `apps/minutes/models.py` (`ProtocolDraft`) | noch keine Ausgabe: nur Datenmodell, keine Oberfläche, keine API | vorgesehen: Ein Entwurf wird erst durch Übernahme der Protokollführung Teil der Niederschrift (`accepted_at`) | Anbieter (Abs. 2) | folgt mit der Oberfläche; Entwurfsansicht und API brauchen dann dasselbe Merkmal |
| Texterkennung (OCR) | `hub/ris/erkennung.py`, `shared/mandari_dokumente` | öffentlich als Dokumenttext | – | kein synthetischer Text: OCR gibt vorhandenen Text wieder | nicht erforderlich |

Session (Verwaltungs-RIS) erzeugt heute keinen Text mit KI.

### Maschinenlesbare Merkmale

- **HTML:** `data-ai-generated="true"` steht am Element, das genau den KI-Text enthält, nicht an Überschrift,
  Hinweis oder Quellenliste. Ein eigenes Microdata- oder Schema.org-Merkmal für KI-Text gibt es derzeit nicht. Legt
  ein Verhaltenskodex nach Art. 50 Abs. 7 ein Format fest, ergänzen wir es hier.
- **Description und JSON-LD der Vorgangsseite:** Suchergebnisse und Vorschaukarten zeigen den Text ohne die Seite.
  Die Kennzeichnung steht deshalb im Text selbst: „KI-Zusammenfassung: …“ (`insight_core.seo.KI_ZUSAMMENFASSUNG_PRAEFIX`).
- **OParl-API:** `mandari:summaryAiGenerated: true` neben `mandari:summary` (Abbildung `hub/ris/mapping/bestand.py`,
  Version 3). `mandari:summary` bleibt unverändert ein Text.
- **JSON-Schnittstellen:** `ai_generated: true` in den Antworten des KI-Assistenten und der Schreibhilfe. Das MCP-
  bzw. Chat-Werkzeug `vorgang` liefert `zusammenfassung_ki_erzeugt: true`, wenn eine Zusammenfassung vorhanden ist.

Alle Merkmale ergänzen die bestehenden Antworten; kein Feld ist entfallen oder hat seinen Typ geändert.

### Menschliche Prüfung als Arbeitsschritt

- **Insight-Zusammenfassungen:** Eine Freigabe vor der Anzeige würde Redaktion für tausende Vorgänge je Kommune
  verlangen. Die Zusammenfassungen bleiben deshalb ungeprüft und gekennzeichnet. Seit #1271 verwirft ein Programm vor
  dem Speichern Zusammenfassungen mit Zahlen, Daten oder Fraktionen, die nicht im Material stehen, mit längeren
  wörtlichen Übernahmen und mit Hausnummern; jede neue Fassung des Prompts beurteilt ein Mensch vorher an einer
  festen Stichprobe (`manage.py zusammenfassung_stichprobe`). Das ist keine Prüfung je Text im Sinne von Abs. 4, die
  Kennzeichnung bleibt.
- **Schreibhilfe:** Die Übernahme durch die schreibende Person ist die Prüfung. Das Dokument verantwortet danach
  die Fraktion.
- **KI-Protokoll:** Die Übernahme durch die Protokollführung ist im Datenmodell vorgesehen und bleibt Pflicht,
  wenn die Funktion eine Oberfläche bekommt.

## 7. Tests

- Assistent: `insight_ai/tests/test_chat_werkzeuge.py` (Werkzeuge mit festen Daten der Musterstadt, auch
  `vorgang`), `insight_ai/tests/test_chat_service.py` (Fragen von Anfang bis Ende mit Drehbuch-Anbieter, Endpunkt),
  `insight_ai/tests/test_anbieter_werkzeuge.py` (Werkzeugrunden im OpenAI-kompatiblen Anbieter,
  `httpx.MockTransport`), `insight_ai/tests/test_token_messung.py` (Verbrauch)
- MCP-Server: `insight_ai/tests/test_mcp.py` (Schalter, Initialisierung, Werkzeugliste, Kommunengrenze, nur lesend,
  Protokoll- und HTTP-Fehler, Ratenbegrenzung auch für Stapel und Gesamtgrenze, Hinweis vor den Ergebnissen)
- Lese-Fassade: `hub/ris/tests/test_selectors_oeffentlich.py` (öffentlicher Bestand, gelistete Kommunen)
- Kennzeichnung: `insight_core/tests/test_ki_kennzeichnung.py`, `insight_core/tests/test_worum_es_geht.py`
  (Vorschauzeilen), `insight_core/tests/test_seo_titel.py` (Description, JSON-LD),
  `insight_core/tests/test_ki_buergerportal.py` (KI-Assistent), `hub/ris/tests/test_mapping_bestand.py` (OParl),
  `apps/work/ris/tests/test_fragen.py`, `apps/work/ris/tests/test_neues_design.py` (Vorgangsseite in Work),
  `apps/work/motions/tests/test_ki_europa.py` (Schreibhilfe)
