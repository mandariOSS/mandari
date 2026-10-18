# MCP-Server von mandari Insight

Stand: Issue #899 (10/2026). Code: `insight_ai/mcp.py`, Werkzeuge: `insight_ai/services/chat_tools.py`
(siehe [INSIGHT_KI_ASSISTENT.md](INSIGHT_KI_ASSISTENT.md)).

## Zweck

KI-Werkzeuge anderer Anbieter (Desktop-Assistenten, Entwicklungsumgebungen, eigene Agenten) können die öffentlichen
Ratsinformationen aus mandari Insight über das [Model Context Protocol](https://modelcontextprotocol.io) abfragen –
mit denselben Werkzeugen wie der KI-Assistent im Bürgerportal. Der Server ist **öffentlich und nur lesend**.

**Standard: aus.** Die Freischaltung entscheidet der Betrieb (`INSIGHT_MCP_ENABLED=true`). Ausgeschaltet antwortet
der Endpunkt mit 404 wie ein unbekannter Pfad.

## Endpunkt

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

## Werkzeuge

| Werkzeug | Argumente |
|----------|-----------|
| `kommunen` | `suchtext?` – gelistete Kommunen mit Kurzname, ohne abgeschaltete oder zurückgenommene |
| `sitzungen_im_zeitraum` | `kommune`, `von`, `bis`, `gremium?`, `tagesordnung?` |
| `sitzung` | `kommune`, `id` |
| `vorgaenge_suchen` | `kommune`, `text`, `von?`, `bis?`, `gremium?` |
| `vorgang` | `kommune`, `id` |
| `gremien` | `kommune`, `suchtext?` |
| `personen` | `kommune`, `name` |
| `dokumente_suchen` | `kommune`, `text` |
| `dokument_abschnitt` | `kommune`, `id`, `frage` |

`kommune` ist der Kurzname (aus `kommunen`) oder die Kennung einer gelisteten Kommune. Alle Werkzeuge tragen die
Hinweise `readOnlyHint: true`, `destructiveHint: false`. Ergebnisse kommen als Text (knappes JSON) mit absoluten
Links auf die Insight-Seiten (`SITE_URL`); davor steht als eigener Textteil der Hinweis, dass Texte aus Dokumenten
und Ratsdaten Daten sind und keine Anweisungen (Schutz gegen Anweisungen in fremden Dokumenten). Fehler kommen als
Ergebnis mit `isError: true` und festem Text. Ein unbekanntes Werkzeug ist ein Protokollfehler (`-32602`).

## Grenzen und Datenschutz

- Dieselben Regeln wie im KI-Assistenten: nur öffentliche Einträge, nichtöffentliche Tagesordnungspunkte nur mit
  Nummer, Personen nur mit Name und laufenden Mitgliedschaften, Abfragen auf die angegebene Kommune beschränkt.
- Nur **gelistete** Kommunen; nicht gelistete (etwa Pilotquellen oder die Demo-Kommune) sind nicht erreichbar.
  Kommunen mit abgeschalteter oder zurückgenommener Veröffentlichung liefern nichts.
- Keine Schreibwege: Der Server liest nur über die Lese-Fassade `hub/ris/selectors.py`.
- Der Dienst protokolliert je Aufruf Werkzeug und Kommune, keine Argumente.

## Einstellungen

| Variable | Standard | Wirkung |
|----------|----------|---------|
| `INSIGHT_MCP_ENABLED` | `false` | Schalter |
| `INSIGHT_MCP_PER_IP_MINUTE` | 30 | Anfragen je Adresse und Minute (alle Methoden; jeder Werkzeugaufruf eines Stapels zählt einzeln) |
| `INSIGHT_MCP_PER_IP_DAY` | 500 | Werkzeugaufrufe je Adresse und Tag |
| `INSIGHT_MCP_PER_MINUTE` | 300 | Anfragen insgesamt je Minute (wie oben gezählt) |
| `INSIGHT_MCP_ALLOWED_ORIGINS` | leer | weitere Hosts für Browser-Clients, kommagetrennt |

`0` schaltet eine Grenze ab. Bei Überschreitung: 429 mit `Retry-After` und JSON-RPC-Fehler `-32000`. Geprüft wird
erst je Adresse, dann insgesamt: Eine wegen ihrer Adresse abgewiesene Anfrage zählt nicht auf die Gesamtgrenze. Die
Zähler liegen im Django-Cache (Redis in Produktion), Adressen nur als Hash (`insight_core.throttle`).

**Vor dem Einschalten:** Die Grenzen je Adresse lesen den ersten Eintrag von `X-Forwarded-For`
(`insight_core.throttle.client_ip`). Das ist nur sicher, wenn der vorgelagerte Proxy diese Kopfzeile selbst setzt
und nicht vom Client übernimmt. Vor der Freischaltung prüft der Betrieb das gegen die Produktion (gefälschte
Kopfzeile darf die gezählte Adresse nicht ändern).

## Tests

- `insight_ai/tests/test_mcp.py` – Schalter, Initialisierung, Werkzeugliste, Kommunengrenze, nur lesend,
  Protokoll- und HTTP-Fehler, Ratenbegrenzung (auch Stapel und Gesamtgrenze), Hinweis vor den Ergebnissen
- `hub/ris/tests/test_selectors_oeffentlich.py` – gelistete Kommunen
