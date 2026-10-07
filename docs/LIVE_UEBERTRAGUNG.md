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

## TOP-Zuordnung

Eine gelesene TOP-Angabe (Nummer und Titel) wird einem öffentlichen Tagesordnungspunkt **der Sitzung der
Übertragung** zugeordnet; Punkte anderer Sitzungen kommen nie in Frage (`hub/live/zuordnung.py`, `top_zuordnen`).
Die Texterkennung verliert gern den Punkt der Nummer („TOP 1.1“ wird „11“), und dem gelesenen Titel fehlt
manchmal der erste Buchstabe. Deshalb:

1. **Lesarten der Nummer:** „11“ zählt auch als „1.1“, „111“ als „11.1“, „1.11“ und „1.1.1“; eine Nummer mit Punkt
   nur als sie selbst.
2. **Titelprüfung:** Der gelesene Titel wird mit dem Titel jedes Tagesordnungspunkts der Sitzung verglichen
   (Kleinschreibung, Umlaute auf den Grundbuchstaben, ohne Satzzeichen und „…“; ein fehlender erster Buchstabe und
   ein abgeschnittenes Ende kosten kaum etwas).
3. **Entscheidung:** zuerst eine Lesart der Nummer, deren Titel ab `titel_zur_nummer` (Standard 0,6) passt
   (Sicherheit `nummer_titel`); sonst der Punkt mit dem ähnlichsten Titel ab `titel_allein` (Standard 0,75), auch
   ohne passende Nummer, wenn der Titel lang genug ist und klar vor dem zweitbesten liegt (`titel`); sonst die
   gelesene Nummer selbst mit niedriger Sicherheit (`nummer`); ohne Treffer kein Tagesordnungspunkt (`keine`).
   Beide Schwellen stehen im Einblendungsprofil und lassen sich je Quelle anpassen (0,3 bis 1).
4. **Entprellung** über den so bestimmten Tagesordnungspunkt, nicht über die gelesene Nummer: „11“ mit dem Titel
   von TOP 1.1 und „1.1“ sind derselbe TOP und bestätigen einander.

Von Hand gesetzte TOPs (`abschnitt_von_hand`) gelten ohne Lesarten und Titelprüfung. Das TOP-Feld des Profils
liest nur Ziffern, Punkt und „TOP“ (`tessedit_char_whitelist`), solange das Profil das Standardmuster `top_muster`
nutzt; mit `"zeichen": ""` im Feld `top` lässt sich das abschalten.

Die Live-Seite und der Kinomodus verlinken den TOP auf den Tagesordnungspunkt in der Sitzungsseite
(`/insight/termine/<sitzung>/#top-<tagesordnungspunkt>`); eine Vorlage dazu steht nur als Nebenlink „Vorlage“ da.

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

   **Vorlage geändert:** Ändert sich eine Vorlage (z. B. „balken_unten_dreizeilig“: Titel ab 0,545 statt 0,555,
   damit der erste Buchstabe nicht fehlt), zieht die Migration `hub_live.0002_top_zuordnung` gespeicherte Profile
   nach, deren Ausschnitte noch genau der alten Vorlage entsprechen; angepasste Profile bleiben, wie sie sind.
   Sonst setzt `live_quelle_einrichten --gremium … --profil balken_unten_dreizeilig` das Profil neu auf die
   Vorlage. Das ersetzt das ganze Profil, also eigene Angaben wie `fraktionen` danach wieder eintragen (oder
   `--profil-datei` mit dem angepassten Profil nutzen).
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
- **TOP-Abschnitte:** `number_read` ist die gelesene Nummer, `number` die des zugeordneten Tagesordnungspunkts.
  `title_similarity` vergleicht den gelesenen Titel mit dem Titel dieses Punkts, `confidence` sagt, worauf die
  Zuordnung beruht (`nummer_titel`, `titel`, `nummer`, `keine`; leer bei Abschnitten von vor der Titelprüfung).
  Viele Abschnitte mit `nummer` oder niedriger Ähnlichkeit deuten auf einen schlecht sitzenden Ausschnitt im Profil
  oder eine abweichende Nummerierung.
- **TOP-Abschnitte neu zuordnen:** Wurden Abschnitte noch falsch zugeordnet (vor der Titelprüfung oder mit einem
  schlechten Profil), ordnet `python manage.py live_abschnitte_neu_zuordnen --uebertragung <uuid> --probelauf`
  sie anhand der protokollierten Lesungen (TOP und Titel je Lesung) neu zu und zeigt nur die Änderungen; ohne
  `--probelauf` wird gespeichert. Der Befehl korrigiert Zuordnungen, ergänzt gelesene Nummer und Sicherheit, führt
  Abschnitte desselben TOP zusammen (Wortmeldungen wandern mit) und hängt Wortmeldungen an den Abschnitt, der bei
  ihrem Beginn lief. Von Hand gesetzte Abschnitte bleiben. Der bisherige Stand landet vorher im Protokoll (Art
  `zustand`, Grund `neuzuordnung`). Er sendet **keine Ereignisse**: Abonnenten haben auf die ursprünglichen
  reagiert. Ein zweiter Lauf ändert nichts mehr. Grenze: Lesungen, die das Protokoll schon aufgeräumt hat
  (`LIVE_PROTOKOLL_TAGE`), fehlen; Abschnitte außerhalb des Zeitraums der Lesungen bleiben unverändert.
- **Wortmeldungen:** Zuordnung `eindeutig`, `unsicher` oder `keine`. Die Person lässt sich im Admin korrigieren;
  die Live-Seite verlinkt Personen nur bei `eindeutig`.
- **Fraktionen:** Aus Wortmeldungen mit Person und gelesener Fraktion leitet das Abonnement
  `insight.fraktionen_live` die Fraktion der Person ab (bestätigt bei eindeutiger Zuordnung und zwei gleichen
  Lesungen, sonst Vorschlag im Admin; [Fraktionen ohne OParl-Fraktion](INSIGHT_FRAKTIONEN.md)). Für eingespielte
  Protokolle: `python manage.py fraktionen_aus_wortmeldungen --meeting <uuid>`.
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
