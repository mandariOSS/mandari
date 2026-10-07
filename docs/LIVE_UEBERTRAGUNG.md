# Live-Übertragungen von Gremiensitzungen

Insight erkennt, wann eine Kommune eine Sitzung live überträgt, liest während der Übertragung etwa alle
10 Sekunden ein Einzelbild per Texterkennung (TOP-Nummer, TOP-Titel, Name und Fraktion bzw. Funktion der
Person am Wort) und zeigt das auf einer Live-Seite je Sitzung: `/insight/termine/<uuid>/live/`. Die Seite ist
öffentlich, aber nirgends verlinkt und `noindex`. Entscheidung und Hintergründe:
[ADR Live-Übertragung](adr/20261007-live-uebertragung.md).

**Grundsätze:** Bild und Ton werden nie gespeichert, weder in der Datenbank noch in Dateien oder Logs. Einzelbilder
liegen nur im Arbeitsspeicher und werden nach der Lesung verworfen. Redezeiten werden weder gelesen noch erfasst;
eine Wortmeldung hat nur einen Beginn.

## Überblick

| Teil | Ort |
| --- | --- |
| Modelle, Ablauf, Ereignisse | `mandari/hub/live/` (Datendrehscheibe) |
| Anbieter-Adapter | `hub/live/anbieter/` (`3q`, `hls`) |
| Einblendungsprofil (Vorlage, Prüfung) | `hub/live/profil.py` |
| Live-Seite | `insight_core/views/live.py`, `templates/pages/meetings/live.html` |
| Zeitpläne | `live_status_abfragen` (jede Minute), `live_protokoll_aufraeumen` (täglich 04:20) |
| Worker | Dienst `worker-live` (Warteschlange `live`) |

Ablauf: Für jede aktive Quelle sucht der Zeitplan jede Minute die Sitzungen des Gremiums im Fenster
„Beginn − 45 Minuten bis Beginn + 10 Stunden“, legt je Sitzung eine Übertragung an und fragt beim Anbieter den
Status ab. Meldet er „live“, beginnt die Übertragung (Ereignis `ris.broadcast.started`), und ein Leseauftrag liest
rund 50 Sekunden lang im Takt der Quelle Einzelbilder. Ein Wechsel von TOP oder Person gilt nach zwei gleichen
Lesungen in Folge. Meldet der Anbieter das Ende (bei 3Q die Tafel `post`; das Standbild danach zählt nicht), kommt
15 Minuten lang kein Signal oder endet das Zeitfenster, ist die Übertragung beendet. Lief bis 3 Stunden nach Beginn
nichts, gilt die Sitzung als nicht übertragen.

## Einstellungen

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `LIVE_UEBERTRAGUNG_AKTIV` | `false` | Hauptschalter. Aus: keine Anfragen nach außen, kein Zeitplan, keine Aufträge. |
| `LIVE_PROTOKOLL_TAGE` | `90` | Aufbewahrung des Protokolls (Abfragen, Lesungen, Fehler) |
| `LIVE_TESSERACT_CMD` | `tesseract` | Tesseract-Programm |
| `LIVE_TESSDATA_DIR` | leer | Sprachdaten, leer = Vorgabe von Tesseract (`deu` muss vorhanden sein) |
| `LIVE_OCR_MEMORY_LIMIT_MB` | `256` | Adressraum je Tesseract-Aufruf (Linux) |
| `LIVE_OCR_TIMEOUT_SECONDS` | `20` | Zeitgrenze je Tesseract-Aufruf |

Mit Compose: `LIVE_UEBERTRAGUNG_AKTIV=true` in die `.env`, dann `docker compose up -d worker worker-live mandari`.
Der Dienst `worker-live` läuft auch bei ausgeschaltetem Schalter (er wartet dann nur); ohne ihn bleiben
Leseaufträge und Statusabfragen liegen.

## Kommune anschließen

1. **Anbieter ermitteln.** Auf der Seite der Kommune mit der Übertragung im Quelltext nach dem Player suchen.
   3Q: ein `iframe` auf `https://playout.3qsdn.com/embed/<embed-id>`; die Embed-ID ist die Kennung. Andere
   Anbieter mit erreichbarer HLS-Playlist (`.m3u8`): Adapter `hls` mit der Adresse der Playlist als Kennung.
   Für YouTube, Vimeo und andere gibt es noch keinen Adapter (siehe ADR, Erweiterung).
2. **Quelle einrichten** (zunächst inaktiv):

   ```bash
   python manage.py live_quelle_einrichten --gremium <uuid oder OParl-Adresse des Gremiums> \
       --anbieter 3q --kennung <embed-id> --seite https://www.musterstadt.example/live \
       --profil balken_unten_dreizeilig
   ```

   Je Gremium gibt es eine Quelle; ein erneuter Aufruf ändert sie. `--takt` setzt den Abstand der Einzelbilder
   (5 bis 60 Sekunden, Standard 10).
3. **Profil kalibrieren.** Ein Bildschirmfoto der laufenden Übertragung (oder eine Stelle in einer eigenen,
   rechtmäßig vorliegenden Aufnahme) lokal lesen lassen; der Befehl speichert nichts:

   ```bash
   python manage.py live_profil_testen bild.png --quelle <uuid der Quelle>
   python manage.py live_profil_testen aufnahme.mp4 --sekunde 1800 --profil-datei profil.json
   ```

   Die Ausgabe nennt, ob der Balken erkannt wurde, und die gelesenen Felder. Passen sie nicht, die Ausschnitte im
   Profil anpassen (relative Koordinaten links, oben, rechts, unten; Aufbau in `hub/live/profil.py`) und mit
   `--profil-datei` erneut prüfen, dann mit `live_quelle_einrichten --profil-datei` übernehmen. Das Profil lässt
   sich auch im Admin (Live-Übertragungen → Übertragungsquellen) bearbeiten; es wird beim Speichern geprüft.
   Bekannte Fraktionsbezeichnungen der Kommune können unter `fraktionen` stehen, Funktionsbezeichnungen, die
   nicht als Fraktion gelten sollen, unter `funktionen`.
4. **Einschalten:** `live_quelle_einrichten --gremium … --aktiv` und (einmal je Installation)
   `LIVE_UEBERTRAGUNG_AKTIV=true`.
5. **Prüfen:** Während der nächsten Sitzung im Admin unter „Übertragungen“ Status und Abschnitte ansehen, die
   Live-Seite aufrufen (Adresse aus der Sitzungs-UUID).

## Auswertung

- **Protokoll** (Admin → Live-Übertragungen → Protokoll, nur lesen): `abfrage` (Status des Anbieters mit Phase,
  Tafeltext, Zuschauerzahl, Rohdaten ohne Bild-Adressen), `lesung` (gelesene Felder, Millisekunden für Bild und
  Lesung, Bildgröße, Balken ja/nein), `zustand` (Wechsel mit Grund), `fehler` (Ort und fester Fehlertext).
- **Übertragung:** `frames_read` und `frames_without_overlay` zeigen, wie oft die Einblendung fehlte. Viele Bilder
  ohne Einblendung bei laufender Debatte deuten auf eine falsche Balkenerkennung im Profil.
- **TOP-Abschnitte:** `title_similarity` vergleicht den gelesenen Titel mit dem Titel im RIS. Werte deutlich
  unter 0,5 deuten auf eine falsch gelesene Nummer oder eine abweichende Nummerierung.
- **Wortmeldungen:** Zuordnung `eindeutig`, `unsicher` oder `keine`. Die Person lässt sich im Admin korrigieren;
  die Live-Seite verlinkt Personen nur bei `eindeutig`.
- **Prototyp-Protokoll einspielen:** Ein JSONL-Protokoll des lokalen Live-Wächters (Zeilen mit `zeit` und
  `art`) wird mit `python manage.py live_protokoll_einspielen <datei.jsonl> --meeting <uuid>` zur Übertragung
  der Sitzung, ohne Ereignisse der Datendrehscheibe. Mehrfaches Einspielen legt nichts doppelt an.
- **Ereignisse:** `ris.broadcast.started`, `ris.broadcast.agenda_item_started`, `ris.broadcast.speaker_changed`,
  `ris.broadcast.ended` (Katalog: [EREIGNISKATALOG.md](EREIGNISKATALOG.md)).

## Grenzen

- Texterkennung irrt: verlesene Namen, fehlende Umlaute, abgeschnittene Fraktionen. Die Entprellung (zwei gleiche
  Lesungen) und der unscharfe Abgleich der Fraktionen fangen vieles ab, nicht alles. Die Seite weist darauf hin;
  maßgeblich bleibt die Niederschrift.
- Ohne Einblendung (Pausen, Bild der Kamera ohne Balken) bleibt der letzte Stand stehen.
- Einen TOP von Hand setzen kann die Verwaltung über `hub.live.services.abschnitt_von_hand` (noch ohne
  Oberfläche).
- Den genauen Live-Wert von `playoutState` bei 3Q kennen wir noch nicht: „live“ heißt online und weder `pre` noch
  `post`. Das Protokoll hält die Rohdaten fest, damit sich das nach der ersten Sitzung prüfen lässt.
- Je Übertragung läuft höchstens ein Leseauftrag; `worker-live` hat zwei Plätze (Lesen und Statusabfrage).
  Laufen mehrere Quellen zugleich live (mehrere Gremien oder Kommunen), warten Statusabfrage und Leseaufträge
  aufeinander, die Abfrage kann sich dann um bis zu etwa 50 Sekunden verzögern. Die Parallelität muss deshalb mit
  der Zahl gleichzeitig laufender Quellen steigen: N = Quellen + 1 (`--concurrency live=N` beim Dienst
  `worker-live` in `docker-compose.yml`; ohne die Option gilt der Standardwert aus `settings.py`), dazu je Platz
  Speicher für einen Tesseract-Aufruf (`LIVE_OCR_MEMORY_LIMIT_MB`) mehr.
- Abrufe folgen Weiterleitungen nur auf https-Adressen mit öffentlichem Namen (`hub/live/anbieter/http.py`). Ein
  Name, der erst bei der Auflösung auf eine Adresse im eigenen Netz zeigt, wird noch nicht abgefangen.
