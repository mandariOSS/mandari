# Abrufregeln: Wie mandari Ratsinformationssysteme abruft

Diese Seite beschreibt, wann und wie mandari Ratsinformationssysteme (RIS) abruft: mit welcher Kennung, in
welchem Takt, welche Inhalte und wie Sie den Abruf drosseln, ausschließen oder uns erreichen. Sie richtet sich an
Betreiber von Ratsinformationssystemen und an alle, die mandari selbst betreiben. Eine Kurzfassung für
IT-Verantwortliche der Kommunen steht unter <https://mandari.de/crawler/>.

Jede Aussage auf dieser Seite stammt aus dem Code oder der Konfiguration; die Fundstelle steht jeweils dabei.
Stand: Code auf `dev` mit den Änderungen aus [#1025](https://github.com/mandariOSS/mandari/issues/1025)
(Änderungsfenster, fehlende Listen, höchstens eine Anfrage je Sekunde), aus
[#1031](https://github.com/mandariOSS/mandari/pull/1031) (eine robots.txt-Regel für alle Abrufe, Weiterleitungen und
Werkzeuge im Takt je Server) und aus [#1036](https://github.com/mandariOSS/mandari/issues/1036) (eine robots.txt je
Hostname, Grenze gleichzeitiger Anfragen auch für Scraper und Vergleichsanfrage, eine Frist für die ganze Vorschau,
Kennung und Takt je Server auch für Live-Übertragung und Geodienste). Die von uns betriebene Instanz (mandari.de)
folgt diesen Regeln ab dem ersten Deploy, der #1031 und #1036 enthält; bis dahin läuft dort noch ein älterer Stand.

## Auf einen Blick

| Punkt | Regel |
|---|---|
| Kennung | `mandari-ingestor/<Version> (+https://mandari.de/crawler/; support@mandari.de)` |
| robots.txt | RFC 9309; für uns gilt die Gruppe `mandari-ingestor`, ohne sie `*`; je Hostname 24 Stunden zwischengespeichert |
| Takt | höchstens eine Anfrage je Sekunde und Server, über alle Abrufe von mandari zusammen; je Quelle nur langsamer |
| Gleichzeitig | höchstens zwei offene Anfragen je Server und Ingestor-Prozess, auch am Ziel einer Weiterleitung |
| Weiterleitungen | höchstens fünf je Abruf, jede erst nach der robots.txt ihres Ziels und mit eigenem Zeitpunkt im Takt |
| Abgleich | alle 10 Minuten; gefragt wird nur nach Änderungen (`modified_since`) |
| Vollabgleich | einmal täglich zu einer festen Stunde, je Quelle auch seltener oder nur in einem Zeitfenster |
| Dokumente | geladen, wenn sie fehlen oder die Quelle sie geändert hat; dazu Stichproben per `HEAD` |
| Abruf-Adresse | derzeit `144.76.153.200`, nur IPv4 (die von uns betriebene Instanz mandari.de); sie kann sich ändern, erkennen Sie uns am User-Agent |
| Andere Dienste | Live-Übertragung und Geodienste mit derselben Kennung und eigenem Takt je Server (siehe [Andere Abrufe](#andere-abrufe)) |
| Kontakt | support@mandari.de |

## Drosseln, ausschließen, Kontakt

- **Ausschließen:** per robots.txt, ganz oder nur für Dokumente (Beispiele im Abschnitt [robots.txt](#robotstxt)).
  Änderungen wirken spätestens nach 24 Stunden.
- **Langsamer oder zu anderen Zeiten:** Schreiben Sie uns. Wir können für Ihre Quelle den Abstand je Anfrage
  erhöhen (`request_interval`, bis 30 Sekunden), den Vollabgleich in ein Zeitfenster legen (`full_sync_window`),
  seltener ausführen (`full_sync_every_days`) oder den Dateiabruf abschalten (`file_downloads`). Liegen mehrere
  Quellen auf Ihrem Server, setzen wir den Abstand bei allen.
- **Kontakt:** support@mandari.de. Infoseite: <https://mandari.de/crawler/>.

## Abruf-Adresse

Die von uns betriebene Instanz (mandari.de) ruft derzeit von der Adresse `144.76.153.200` ab, ausschließlich über
IPv4. Erkennen Sie uns bitte vor allem am User-Agent: Die Adresse kann sich ändern, etwa bei einem Serverumzug.

Wer mandari selbst betreibt, ruft von der Adresse des eigenen Servers ab. Für solche Installationen gilt die
Adresse oben nicht.

Die folgenden Abschnitte sind die technische Referenz für den Betrieb und für Selbstbetreiber.

## Wer ruft ab

Zwei Programmteile rufen bei Ratsinformationssystemen ab: der Ingestor (eigener Container) und die
Django-Anwendung.

| Programmteil | Was | Wann |
|---|---|---|
| Ingestor: Abgleich | OParl-Schnittstelle: System, Kommunen und je Kommune bis zu neun Listen (Gremien, Personen, Mitgliedschaften, Sitzungen, Vorlagen, Orte, Tagesordnungspunkte, Dateien, Beratungen) | alle 10 Minuten, Vollabgleich täglich |
| Ingestor: Scraper | HTML-Seiten von RIS ohne OParl-Schnittstelle ([`SCRAPER_SOURCES.md`](SCRAPER_SOURCES.md)) | mit dem Abgleich |
| Ingestor: Texterkennung | Dateien (meist PDF), deren Text noch fehlt | mit dem Abgleich je Kommune bzw. im OCR-Worker `extract-daemon` |
| Anwendung: Dokument-Cache | Dateien, die noch nicht in der Ablage liegen | stündlich um :40 |
| Anwendung: Vorschau | ein Dokument, das jemand öffnet und das noch nicht in der Ablage liegt | auf Anforderung |
| Anwendung: Löschabgleich | geänderte Dokumente, Stichproben per `HEAD`, gesperrte Dokumente | stündlich um :15 |
| Anwendung: Personenfotos | Fotos von Ratsmitgliedern, nur für Kommunen mit hinterlegtem Fotomuster | montags um 03:00 |

Zeitpläne der Anwendung: `mandari/apps/common/schedules.py` (`cache_files --limit 400`, `loeschabgleich`,
`fetch_person_photos`), Zeiten in `TIME_ZONE` (`Europe/Berlin`). Mit `TEXT_EXTRACTION_RUNNER=worker` lädt statt des
Ingestors die Anwendung die Dateien für die Texterkennung, über denselben Weg wie der Dokument-Cache
(`mandari/hub/ris/abruf.py`).

Alle Abrufe der Tabelle nutzen dieselbe Kennung (abweichend nur je Quelle, siehe [Kennung](#kennung)), dieselbe
Auswertung der robots.txt und denselben Takt je Server.

### Werkzeuge

Von Hand kommen zwei Werkzeuge hinzu, ebenfalls nach der robots.txt und im selben Takt je Server:

- **Quelle anlegen** (`manage.py add_oparl_source`): fragt nach Prüfung der robots.txt System und Kommunenliste ab
  (höchstens 20 Seiten, `MAX_BODY_PAGES`), mit der Kennung der Anwendung. Ist die Quelle schon angelegt, gelten ihr
  Abstand (`request_interval`) und ihre Ausnahme von der robots.txt (`robots_override`), auch für den Abruf der
  robots.txt selbst. Hat eine registrierte Quelle auf demselben Server einen größeren Abstand, gilt dieser
  (`mandari/insight_core/management/commands/add_oparl_source.py`, `_config_for`). Eine neue Quelle legt das
  Werkzeug inaktiv an, neue Kommunen nicht gelistet: Regelmäßig abgefragt wird eine Quelle erst, wenn wir sie im
  Admin aktivieren. Eine schon angelegte Quelle behält ihren Status, eine abgeschaltete bleibt abgeschaltet. Dasselbe
  gilt für `python -m src.main add-source` und für Quellen, die ein Abgleich von Hand neu anlegt (Issue #555).
- **Sondierung** (`python -m src.main probe-ris`): erkennt vor dem Anbinden einer Kommune ihr System mit höchstens
  fünf Anfragen je Kommune (`ingestor/src/probe/ris_probe.py`, `MAX_REQUESTS`): robots.txt, Startseite, bei HTTP 403
  eine Vergleichsanfrage (siehe [Kennung](#kennung)), danach mögliche OParl-Adressen. Weiterleitungen und die
  robots.txt eines weiteren Servers zählen gegen die fünf. Gesperrte Pfade fragt sie nicht an; ist die robots.txt
  nicht erreichbar, endet die Prüfung nach dieser einen Anfrage. Abstand je Server wie im Abgleich
  (`INGESTOR_REQUEST_INTERVAL`); hat eine registrierte Quelle auf dem Server einen größeren Abstand, gilt dieser.
  Zwischen zwei Kommunen liegen zusätzlich 2 Sekunden Pause (`ingestor/src/main.py`, `probe_ris`).

## Kennung

| Abruf | User-Agent |
|---|---|
| Ingestor: Abgleich, Scraper, Texterkennung, Sondierung | `mandari-ingestor/<Version> (+https://mandari.de/crawler/; support@mandari.de)` |
| Anwendung: Dokument-Cache, Vorschau, Löschabgleich, Personenfotos, Texterkennung, Quellenanlage | `mandari-ingestor (+https://mandari.de/crawler/; support@mandari.de)` |
| Anwendung: Live-Übertragung und Geodienste ([Andere Abrufe](#andere-abrufe)) | `mandari-ingestor (+https://mandari.de/crawler/; support@mandari.de)` |

- Die Version ist die Paketversion des Ingestors (`ingestor/pyproject.toml`, derzeit `0.1.0`;
  `ingestor/src/config.py`, `DEFAULT_USER_AGENT`). Aufbau, Infoseite und Kontaktadresse:
  `shared/mandari_oparl/crawler.py`. Die Anwendung sendet dieselbe Kennung ohne Version
  (`mandari/insight_core/services/robots.py`, `USER_AGENT`); Dokument-Cache und Personenfotos übernehmen sie von
  dort.
- Anfragen an die Schnittstelle senden `Accept: application/json`, Abrufe der robots.txt `Accept: text/plain`.
- **Vergleichsanfrage nach HTTP 403:** Antwortet ein Server dem Abgleich mit 403, stellt der Ingestor je Server und
  Lauf einer Quelle genau eine Vergleichsanfrage mit der Standardkennung der HTTP-Bibliothek, `python-httpx/<Version>`
  (`ingestor/src/client/oparl_client.py`, `NEUTRAL_USER_AGENT`, `_diagnose_forbidden`). Sie geht im Takt und mit
  einem Platz in der Grenze gleichzeitiger Anfragen an die Adresse, die 403 geliefert hat, und folgt keiner
  Weiterleitung. Nur eine erfolgreiche Antwort (2xx) gilt als Sperre unseres User-Agents. Eine Weiterleitung (3xx)
  und eine Vergleichsanfrage ohne Antwort (Netzfehler, Zeitüberschreitung) lassen das offen („Sperre unklar“), jede
  andere Antwort heißt „Zugriff verweigert, auch für neutralen Client“. Das dient nur der
  Diagnose: Weiter abgerufen wird mit unserer Kennung, die Quelle wird geschont (siehe
  [Fehler und Schonung](#fehler-und-schonung)). Dieselbe Vergleichsanfrage stellt `probe-ris` nach HTTP 403 auf die
  Startseite. Beide schaltet `OPARL_UA_PROBE_ENABLED=false` ab.
- **Abweichende Kennung je Quelle:** Für Schnittstelle und Scraper gibt es das Feld „User-Agent“ der Quelle im Admin
  (`OParlSource.user_agent`), für Dateien `User-Agent` in `sync_config["download_headers"]` (Texterkennung,
  Dokument-Cache, Vorschau, Löschabgleich, Personenfotos). Das ist für Ausnahmen gedacht, etwa wenn ein Server Wörter
  im User-Agent filtert. Die robots.txt wird dann mit dieser Kennung abgerufen und für ihr Token ausgewertet; Regeln
  für `mandari-ingestor` gelten trotzdem (siehe nächster Abschnitt).

## robots.txt

Ausgewertet nach RFC 9309 mit eigenem Parser, für Ingestor und Anwendung gemeinsam
(`shared/mandari_oparl/robots.py`):

- **Gruppen:** Für ein Token gelten alle Gruppen, deren `User-agent` ihm entspricht (ohne Unterscheidung von Groß-
  und Kleinschreibung); gibt es keine, alle `*`-Gruppen; gibt es auch die nicht, ist alles erlaubt. Eine Gruppe für
  `mandari-ingestor` gilt also anstelle von `*`, nicht zusätzlich.
- **Regeln:** Die längste passende Regel entscheidet, bei gleicher Länge `Allow`. `*` steht für beliebig viele
  Zeichen, `$` verankert das Ende. Verglichen werden Pfad und Abfrage, Groß- und Kleinschreibung zählt.
- **Token:** Geprüft wird das Token der gesendeten Kennung und immer zusätzlich `mandari-ingestor`; beide müssen den
  Abruf erlauben. Gruppen für unsere früheren Kennungen `mandari-file-cache` und `mandari` gelten zusätzlich, aber nur,
  wenn es sie gibt, und sie können nur sperren (`shared/mandari_oparl/crawler.py`, `LEGACY_TOKENS`).
- **Geltung:** für Schnittstelle, HTML-Seiten und Dateien, auch für das Ziel jeder Weiterleitung (siehe
  [Weiterleitungen](#weiterleitungen)). Viele Systeme sperren nur Dokumente, etwa mit `Disallow: /*.pdf$`; dann rufen
  wir die Schnittstelle ab, die Dateien nicht. `/robots.txt` selbst ist immer erlaubt.
- **Abruf:** mit der Kennung des jeweiligen Abrufs und `Accept: text/plain`, im [Takt je Server](#takt-je-server).
  Weiterleitungen folgen wir bis zu fünfmal, jede mit eigenem Zeitpunkt im Takt. Ausgewertet werden höchstens
  500 KiB (`MAX_BYTES`). Zeitgrenze je Verbindungsaufbau und Lesevorgang im Ingestor 15 Sekunden
  (`ingestor/src/client/robots.py`, `ROBOTS_TIMEOUT`), in der Anwendung 5 Sekunden für den Verbindungsaufbau und
  10 Sekunden zum Lesen.
- **Zwischenspeicher:** 24 Stunden (`CACHE_SECONDS`) je Hostname und Kennung (`host_key`): Schema, Port und
  Schreibweise ergeben keinen eigenen Eintrag. `https://RIS.example.de:443/`, `http://ris.example.de/` und
  `https://ris.example.de:8443/` teilen sich eine robots.txt, so wie sie sich den [Takt](#takt-je-server) teilen.
  Geladen wird sie mit Schema und Port der ersten Anfrage, die sie braucht. RFC 9309 sieht eine robots.txt je Schema,
  Host und Port vor; bei Ratsinformationssystemen ist ein Hostname ein Server, deshalb fragen wir sie nur einmal an.
  Im Ingestor gilt der Zwischenspeicher je Prozess (`ingestor/src/client/robots.py`), in der Anwendung im
  gemeinsamen Cache aller Prozesse (`mandari/insight_core/services/robots.py`). Änderungen an Ihrer robots.txt wirken
  also spätestens nach 24 Stunden.
- **Nicht vorhanden:** 4xx (auch 401, 403, 406) gilt als „keine robots.txt“, also alles erlaubt.
- **Nicht erreichbar:** 5xx, 408, 429, ein Netzfehler oder mehr als fünf Weiterleitungen. Gab es vorher eine gültige
  Fassung, gilt sie weiter, bis ein Abruf wieder gelingt. Gab es keine, fragen wir nicht an, sondern stellen den Abruf
  zurück. Die robots.txt rufen wir frühestens nach 15 Minuten erneut ab (`RETRY_UNREACHABLE_SECONDS`). Der Abgleich
  wertet das als Störung, nicht als Sperre (Schonung mindestens 30 Minuten).
- **Gesperrt:** Wir stellen die Anfrage nicht. Lässt die robots.txt einer Quelle an einem Server keine einzige
  Anfrage zu, versucht der Abgleich es frühestens nach 24 Stunden wieder.
- **Nicht ausgewertet:** `Crawl-delay` und `Sitemap`. Für einen langsameren Takt schreiben Sie uns; wir setzen dann
  `request_interval` für Ihre Quellen.
- **Ausnahmen** gibt es nur mit Freigabe der Stelle und Vermerk in `sync_config["robots_override"]`
  (mindestens zehn Zeichen, Bereich `api`, `files` oder `all`, ohne Angabe `all`; `MIN_NOTE_LENGTH`,
  `OVERRIDE_SCOPES`, `robots_override`). Ohne gültigen Vermerk gilt die robots.txt.

Beispiele für Betreiber:

```text
# mandari ganz ausschließen
User-agent: mandari-ingestor
Disallow: /

# Schnittstelle erlauben, Dokumente sperren
User-agent: mandari-ingestor
Disallow: /*.pdf$
```

Gibt es eine Gruppe für `mandari-ingestor`, gelten die Regeln unter `User-agent: *` für uns nicht mehr. Wiederholen
Sie in unserer Gruppe, was auch für uns gelten soll.

## Takt je Server

Vor jeder Anfrage reserviert ein Abruf einen Zeitpunkt für den Server, auch vor jeder Weiterleitung und vor dem
Abruf einer robots.txt. Server heißt hier: der Hostname, ohne Port und ohne Unterschied in der Schreibweise
(`shared/mandari_oparl/pacing.py`, `host_of`); `https://RIS.example.de:443/` und `http://ris.example.de/` teilen
sich einen Takt. Der Zeitstempel liegt in Redis und gilt damit über alle Quellen, Abrufplätze und Prozesse hinweg,
für Ingestor, Anwendung und beide Werkzeuge gemeinsam (`RESERVE_SCRIPT`, mit der Uhr des Redis-Servers). Liegen
mehrere Kommunen auf demselben Server, teilen sie sich diesen Takt. Jede Reservierung schiebt den nächsten freien
Zeitpunkt um den Abstand des Abrufs weiter, der sie vornimmt; ein langsamerer Abstand einer Quelle gilt also für ihre
eigenen Abrufe (siehe „Je Quelle nur langsamer“).

| Abruf | Abstand je Server | Gleichzeitig | Fundstelle |
|---|---|---|---|
| Ingestor: Schnittstelle, Texterkennung | 1 s; je Quelle 1 bis 30 s | höchstens 2 offene Anfragen je Server und Prozess | `pacing.py` `DEFAULT_INTERVAL`; `ingestor/src/config.py` `request_interval`, `host_max_concurrent` |
| Ingestor: Scraper (RIS ohne OParl) | 2 s, nie unter 1 s | je Quelle streng nacheinander; zusammen mit Abgleich und Texterkennung höchstens 2 offene Anfragen je Server und Prozess | `ingestor/src/scrapers/base.py` `rate_limit_seconds`, `ingestor/src/scrapers/politeness.py` |
| Anwendung: Dokument-Cache, Löschabgleich, Personenfotos, Texterkennung, robots.txt | 1 s; je Quelle 1 bis 30 s | je Lauf nacheinander | `mandari/mandari/settings.py` `RIS_REQUEST_INTERVAL` |
| Anwendung: Vorschau | 1 s; je Quelle 1 bis 30 s | höchstens 4 Abrufe je Prozess (`FILE_PROXY_MAX_CONCURRENT`), 20 je Besucher-Adresse und Minute | `mandari/insight_core/throttle.py` |
| Löschabgleich zusätzlich | mindestens 2 s zwischen zwei Prüfungen je Server innerhalb eines Laufs; die `GET`-Nachprüfung nach einer `HEAD`-Stichprobe nur im gemeinsamen Takt | nacheinander | `loeschabgleich --interval` |
| Werkzeug `add_oparl_source` | 1 s; bei einer schon angelegten Quelle deren Abstand; ein größerer Abstand einer registrierten Quelle auf dem Server gilt | nacheinander | `add_oparl_source.py` (`RIS_REQUEST_INTERVAL`) |
| Werkzeug `probe-ris` | 1 s; ein größerer Abstand einer registrierten Quelle auf dem Server gilt; zwischen zwei Kommunen 2 s Pause | nacheinander | `ris_probe.py`, `ingestor/src/main.py` (`INGESTOR_REQUEST_INTERVAL`) |

- **Je Quelle nur langsamer:** `sync_config["request_interval"]` von 1 bis 30 Sekunden. Werte unter einer Sekunde,
  auch `0`, gelten als eine Sekunde, mit Warnung im Protokoll (`MIN_REQUEST_INTERVAL`, `at_least_minimum`). Werte
  über 30 Sekunden, negative Werte oder keine Zahl gelten als nicht gesetzt (`MAX_REQUEST_INTERVAL`,
  `request_interval`). Für Scraper gilt `scraper.rate_limit_seconds` mit derselben Untergrenze. Der Wert gilt für
  die Abrufe dieser Quelle. Andere Quellen auf demselben Server halten ihren eigenen Abstand; nur die Werkzeuge
  `add_oparl_source` und `probe-ris` übernehmen den größten. Für einen langsameren Takt des ganzen Servers setzen wir
  den Wert bei allen Quellen dort.
- **Drossel aus:** `0` in `INGESTOR_REQUEST_INTERVAL` bzw. `RIS_REQUEST_INTERVAL` schaltet die Drossel des jeweiligen
  Prozesses ab; ein `request_interval` der Quelle gilt weiter. Das ist nur für Tests und Notfälle gedacht.
- **Gleichzeitig im Ingestor:** Die Grenze von zwei offenen Anfragen je Server und Prozess
  (`INGESTOR_HOST_MAX_CONCURRENT`, `ingestor/src/client/host_pacing.py`) gilt für Abgleich, Vergleichsanfrage,
  Texterkennung und Scraper zusammen. Folgt ein Abruf einer Weiterleitung auf einen anderen Server, zieht sein Platz
  vorher dorthin mit (`HostSlot.move_to`), auch für den Abruf der robots.txt des Ziels; am Ziel gelten also Takt und
  Grenze. Nur der Abruf der robots.txt vor der ersten Anfrage an einen Server zählt allein im Takt (je Server und
  Kennung höchstens einmal in 24 Stunden, nach einer Störung alle 15 Minuten).
- **Ohne Redis** drosselt jeder Prozess für sich und versucht es nach 60 Sekunden wieder mit Redis
  (`REDIS_RETRY_SECONDS` in `ingestor/src/client/host_pacing.py` und `mandari/insight_core/services/host_pacing.py`).
  Mehrere Prozesse können dann zusammen schneller abrufen als eine Anfrage je Sekunde.
- **Wartezeit der Vorschau:** Eine Frist von höchstens 5 Sekunden für die ganze Anfrage
  (`FILE_PROXY_PACE_MAX_WAIT_SECONDS`): auf einen der vier Abrufplätze des Prozesses (davon höchstens 2 Sekunden,
  `_SLOT_MAX_WAIT_SECONDS`), auf robots.txt und freie Zeitpunkte, alle Weiterleitungen eingeschlossen. Jeder Schritt
  wartet nur die Restzeit. Ist kein Platz oder kein Zeitpunkt frei, bittet sie mit HTTP 503 und `Retry-After: 30` um
  einen neuen Versuch, statt den Takt zu überziehen (`mandari/insight_core/views/files.py`).
- **Zeitgrenzen** gelten je Verbindungsaufbau und je Lesevorgang (Warten auf das nächste Datenpaket), nicht für die
  ganze Anfrage: 60 Sekunden an der Schnittstelle (`OPARL_REQUEST_TIMEOUT`), 30 Sekunden bei den Scrapern,
  120 Sekunden bei Dateien der Texterkennung im Ingestor (`TEXT_EXTRACTION_TIMEOUT`); in der Anwendung im
  Dokument-Cache und im Löschabgleich 10 Sekunden für den Verbindungsaufbau und 30 Sekunden zum Lesen
  (`mandari/hub/ris/abruf.py`, `http_timeout`), bei Personenfotos 15 Sekunden, in der Vorschau 5 Sekunden für den
  Verbindungsaufbau und 15 Sekunden zum Lesen (`FILE_PROXY_TIMEOUT_SECONDS`). Eine Gesamtgrenze hat nur die Vorschau:
  60 Sekunden (`FILE_PROXY_TOTAL_SECONDS`).

## Weiterleitungen

Weiterleitungen folgen Ingestor, Anwendung und beide Werkzeuge selbst, höchstens fünfmal je Abruf
(`ingestor/src/client/redirects.py` und `mandari/insight_core/services/redirects.py`, `MAX_REDIRECTS`). Vor jeder
Weiterleitung:

1. In der Anwendung, außer beim Anlegen einer Quelle: Ist das Ziel eine öffentliche Adresse? Sonst endet der Abruf
   ohne weitere Anfrage (`safe_fetch.check_url`).
2. robots.txt des Ziel-Servers, mit derselben Kennung und Ausnahme der Quelle wie die erste Anfrage. Ist sie noch
   nicht bekannt, laden wir sie im Takt.
3. Ein eigener Zeitpunkt im Takt des Ziel-Servers.

Im Ingestor zieht der Platz des Abrufs in der Grenze gleichzeitiger Anfragen vorher zum Ziel-Server um (Abgleich,
Texterkennung, Scraper): Am Ziel gelten Takt und Grenze wie am Ausgangsserver.

Sperrt die robots.txt das Ziel, folgen wir nicht. Ist sie nicht erreichbar, stellen wir den Abruf zurück. Mehr als
fünf Weiterleitungen beenden den Abruf mit einem Fehler. Die robots.txt selbst folgt ihren Weiterleitungen ebenfalls
nur im Takt, ohne Prüfung, weil sie immer erlaubt ist.

| Abruf | Ziel gesperrt | robots.txt des Ziels nicht erreichbar |
|---|---|---|
| Ingestor: Abgleich | Befund „robots.txt sperrt“ für den Ziel-Server | Störung wie eine 5xx-Serie |
| Ingestor: Texterkennung | Datei übersprungen | Datei bleibt offen, Kommune 15 Minuten zurückgestellt |
| Ingestor: Scraper | wie eine gesperrte Seite (Schonung 24 Stunden) | Störung (Schonung 30 Minuten) |
| Anwendung: Dokument-Cache | verweigert, erst nach Freigabe erneut | Wiederholung nach dem Plan unten |
| Anwendung: Vorschau | Hinweis mit Link auf das Original | Hinweis mit Link, HTTP 503 mit `Retry-After: 900` |
| Anwendung: Löschabgleich | keine Aussage über das Dokument | ebenso |
| Anwendung: Personenfotos | Fehler vermerkt | nichts vermerkt, nächster Lauf |
| Werkzeuge | nicht gefolgt, Hinweis im Ergebnis | ebenso |

Bei `probe-ris` zählt jede Weiterleitung, auch die robots.txt eines neuen Ziel-Servers, gegen die fünf Anfragen je
Kommune.

## Abgleich und Änderungsfenster

### Rhythmus

- Der Ingestor-Daemon startet alle 10 Minuten einen inkrementellen Lauf über alle aktiven Quellen und einen sofort
  beim Start (`ingestor/src/main.py`, `daemon --interval`, Standard 10; `docker-compose.yml`: `INGESTOR_INTERVAL`,
  ohne Wert 10). Die Vorlage `.env.example`, `install.sh` und das Helm-Chart (`ingestor.syncInterval`) setzen
  15 Minuten. Der Abgleich läuft rund um die Uhr in diesem Rhythmus.
- Es läuft nie mehr als ein Lauf zur Zeit (`ingestor/src/scheduler/__init__.py`, `max_instances=1` und eigene
  Sperre). Ist ein Lauf beim nächsten Termin nicht fertig, entfällt dieser Termin; nur der Vollabgleich wird
  nachgeholt (siehe [Vollabgleich](#vollabgleich)). Je Datenbank arbeitet genau ein Daemon (PostgreSQL-Sperre,
  `ingestor/src/scheduler/singleton.py`).
- Gleichzeitig laufen höchstens 2 Quellen, je Quelle 2 Kommunen und je Kommune 2 Listen
  (`SYNC_SOURCE_CONCURRENCY`, `SYNC_BODY_CONCURRENCY`, `SYNC_ENTITY_CONCURRENCY`). Der Takt je Server gilt
  unabhängig davon.
- Je Lauf und Quelle fragt der Ingestor die eingetragene Adresse (System, Kommunenliste oder Kommune) und
  gegebenenfalls die Kommunenliste ab, danach je Kommune die Listen in fester Reihenfolge: Gremien und Personen,
  Mitgliedschaften, Sitzungen und Vorlagen, dann Orte, Tagesordnungspunkte, Dateien und Beratungen.
- Die Kommunenliste liest der Ingestor wie jede Liste über alle Seiten (`links.next`), Seite für Seite im Takt; ist
  die eingetragene Adresse selbst die Kommunenliste, fragt er ihre erste Seite nur einmal an (Issue #869,
  `ingestor/src/sync/orchestrator.py`, `_fetch_body_list`). Bricht eine Folgeseite ab (Fehler, Teilantwort), gleicht
  der Lauf die bis dahin gelesenen Kommunen ab, nennt die Seite im Sync-Log und gilt als nicht vollständig. Ebenso
  nach höchstens 20 Seiten (`MAX_BODY_PAGES`, wie beim Werkzeug „Quelle anlegen“) und wenn `links.next` auf eine schon
  gelesene Seite zeigt: Eine fehlerhafte Liste hält den Zyklus aller Quellen nicht auf. Eine Liste, die im Kreis
  läuft, bricht bei jeder Liste ab, nicht nur bei der Kommunenliste (`OParlClient.fetch_list`).
- Neu entdeckte Kommunen sind nicht gelistet (`is_listed`, `ingestor/src/storage/database.py`, `upsert_body`): Bis
  wir sie im Admin listen, erscheinen sie nicht in Kommunenauswahl, Übersichten, Sitemaps und öffentlichen Listen.
  Was das für die Ablage ihrer Dokumente heißt, steht in [`FILE_CACHE.md`](FILE_CACHE.md). Ausnahme ist die eigene
  OParl-Schnittstelle eines Session-Mandanten (`sync_config.session_tenant`, `lists_new_bodies`): Der Mandant hat die
  Veröffentlichung im Bürgerportal selbst eingeschaltet, seine Kommune ist gleich gelistet
  ([`SESSION_OPARL_API.md`](SESSION_OPARL_API.md)).
- Ein Lauf über alle Quellen lässt sich außerdem im Admin („Jetzt synchronisieren“, über Redis an den Daemon) oder
  von Hand (`python -m src.main sync --all`) auslösen. robots.txt, Takt, Zeitfenster und Schonung gelten auch dann.
- Die Aktionen „Inkrementeller Sync“ und „Vollständiger Sync“ an einer Quelle im Admin laufen nicht im Daemon,
  sondern als Auftrag im Worker der Anwendung, mit dem Code des Ingestors aus dem Image der Anwendung
  (`mandari/insight_core/admin.py`, `run_sync_in_thread`; `mandari/insight_sync/tasks.py`, `run_sync_with_logging`).
  Sie beachten robots.txt, Takt und Zeitfenster, aber nicht die [Schonung](#fehler-und-schonung), ebenso wie
  `python -m src.main sync --source` bzw. `--body`. Dabei gilt die Umgebung des Workers, nicht die des Ingestors
  (siehe [Für Selbstbetreiber](#für-selbstbetreiber)).

### Änderungsfenster

Inkrementelle Läufe fragen jede Liste mit `modified_since` ab (`ingestor/src/sync/orchestrator.py`):

- **Beginn des Fensters:** der Beginn des letzten vollständig erfolgreichen Abgleichs der Kommune, abzüglich eines
  Sicherheitsabstands von 120 Minuten (`modified_since_for`; `INGESTOR_MODIFIED_SINCE_MARGIN_MINUTES`, je Quelle
  `modified_since_margin_minutes`, 0 bis 10080). Der Abstand fängt abweichende Uhren und spät sichtbare Änderungen
  der Quelle ab.
- **Höchstens sieben Tage:** Das Fenster beginnt nie früher als sieben Tage zurück um Mitternacht
  (`MODIFIED_SINCE_FALLBACK_DAYS`, `fallback_modified_since`). Dieses Fenster gilt auch ohne einen vollständigen
  Abgleich (etwa bei einer neuen Kommune) und wenn der letzte vollständige Abgleich länger zurückliegt. Den Tag
  bestimmt die Ortszeit des Ingestors, gesendet wird die Uhrzeit aber als 00:00 UTC (`…T00:00:00+00:00`), in
  Deutschland also 01:00 bzw. 02:00 Uhr Ortszeit.
- **Kurzes Fenster nur mit Uhrzeit:** Das kurze Fenster bekommen nur Listen, deren `modified` nachweislich eine
  Uhrzeit trägt (`sync_config["timed_lists"]`, `TimedLists`). Alle übrigen Listen, etwa mit reinen Tagesstempeln,
  mit gemischten Stempeln oder ohne bisher gesehene Änderung, fragt der Lauf ab Tagesbeginn an: Der Fensterbeginn
  wird auf den früheren von 00:00 Uhr Ortszeit (`Europe/Berlin`) und 00:00 Uhr UTC abgerundet, nie früher als das
  Fenster von sieben Tagen (`day_start_window`). Sonst fände das kurze Fenster in einer Liste mit reinen
  Tagesstempeln ab etwa 02:00 Uhr nichts mehr vom selben Tag.
- **Vermerk „mit Uhrzeit“:** Der Ingestor vermerkt eine Liste, sobald ein Lauf dort Änderungen der letzten sieben
  Tage sieht und alle eine Uhrzeit tragen. Er löscht den Vermerk, sobald ein Lauf dort eine solche Änderung nur mit
  Datum sieht (00:00:00 Uhr, ohne Uhrzeit oder 00:00 Uhr Ortszeit in UTC wie `22:00:00Z`). Sieht ein Lauf dort keine
  Änderung, bleibt es, wie es ist (`_settle_timed_lists`).
- **Beginn festhalten:** Den Beginn eines Laufs halten wir vor der ersten Anfrage fest und speichern ihn in
  `sync_config["body_sync_started"]`, aber nur, wenn der Lauf jede Liste der Kommune ganz gelesen hat. Nach einem
  Lauf mit Lücke bleibt der alte Zeitpunkt stehen (`_advance_window`).
- **Nicht gespeicherte Objekte:** Konnte ein Lauf einzelne Objekte nicht speichern (Fehler beim Speichern, eine
  Mitgliedschaft ohne auflösbare Person oder Gremium, ein Tagesordnungspunkt ohne Sitzung), rückt das Fenster
  höchstens bis zu ihrem ältesten `modified` vor, ohne lesbares `modified` gar nicht (`BodyItemCheck.window_start`).
  Der nächste Lauf fragt diese Objekte also wieder ab. Ausgenommen sind Objekte, die die Quelle trotz Filter liefert
  (`modified` mehr als einen Tag vor dem Fenster); sie halten das Fenster nicht.
- **Format:** Parameter `modified_since` mit Zeitzone, auf die Sekunde, etwa `2026-10-15T06:00:00+00:00` (in der
  Adresse prozentkodiert; `ingestor/src/client/oparl_client.py`, `_append_modified_since`). Beispiel: Begann der
  letzte vollständige Abgleich am 15.10.2026 um 08:00:00 UTC, fragt der nächste Lauf eine Liste mit Uhrzeiten nach
  Änderungen ab `2026-10-15T06:00:00+00:00`, eine Liste ohne nachgewiesene Uhrzeit ab `2026-10-14T22:00:00+00:00`
  (00:00 Uhr Ortszeit).
- **Ohne Änderungen** kostet ein Lauf bei einer Quelle, die den Filter auswertet, eine Seite je Liste. Liefert eine
  Liste fünf Seiten in Folge ohne Änderung, bricht der inkrementelle Lauf sie ab (`stale_pages_to_stop`).

Wertet Ihre Schnittstelle `modified_since` aus, bleibt unsere Last am kleinsten.

### Quellen ohne Filter

- Lehnt eine Quelle den Filter ab (HTTP 400, 401 oder 403 auf die gefilterte erste Seite) oder ignoriert sie ihn
  (Parameter fehlt in den Links der Liste), liest der Ingestor die Liste ohne Filter. Er vergleicht selbst und
  bricht wie oben nach fünf Seiten ohne Änderung ab.
- Der Befund steht je Server in `sync_config["modified_since_unsupported_hosts"]`. Nach jedem Vollabgleich prüft
  der Ingestor ihn mit einer Anfrage neu (Fenster von sieben Tagen, `MODIFIED_SINCE_PROBE_DAYS`). Bringt die
  Prüfung keine Aussage, wiederholt er sie einmal nach 60 Sekunden (`MODIFIED_SINCE_RETRY_SECONDS`). Das Ergebnis
  steht in `sync_config["modified_since_check"]`.
- Antwortet eine Liste nur auf die gefilterte Anfrage mit HTTP 404 oder einem OParl-Fehlerobjekt, fragt der
  Ingestor einmal ohne Filter nach und liest diese Liste danach ohne Filter, solange der Prozess läuft.
- SD.NET RIM beantwortet eine gefilterte Liste ohne Treffer mit HTTP 404 und Fehlercode 802. Das gilt als leere,
  vollständige Liste, ohne Nachfrage ohne Filter (`sdnet_empty_list`).
- Filtert eine Quelle, verliert den Parameter aber in `links.next`, hängt der Ingestor ihn mit
  `carry_modified_since` an jede Folgeseite an.

### Fehlende Listen

- Antwortet schon die erste Seite einer optionalen Liste (Mitgliedschaften, Tagesordnungspunkte, Beratungen,
  Dateien, Orte; `OPTIONAL_LIST_TYPES`) ohne Filter mit HTTP 404 oder einem OParl-Fehlerobjekt, gibt es die Liste
  bei dieser Quelle nicht. Der Ingestor vermerkt sie am Ende des Laufs mit dem Zeitpunkt der Prüfung in
  `sync_config["missing_lists"]`, aber nur, wenn mindestens eine andere Liste der Kommune lesbar war. Sonst gilt das
  als Störung der Schnittstelle: Lücke im Abgleich, kein Vermerk (`_settle_missing_lists`).
- Pflichtlisten (Gremien, Personen, Sitzungen, Vorlagen) vermerkt er nie. Ein HTTP 404 bleibt dort eine Lücke, und
  der nächste Lauf fragt die Liste wieder an. Ein Fehlerobjekt gilt dort als leere Liste, solange eine andere Liste
  der Kommune lesbar war, sonst ebenfalls als Lücke.
- Vermerkte Listen fragt der inkrementelle Lauf einmal am Tag an (`MISSING_LIST_RECHECK`), dann mit dem Fenster von
  sieben Tagen; der Vollabgleich fragt sie jedes Mal an. Gibt es die Liste wieder, löscht der Ingestor den Vermerk.
- HTTP 404 zählt nie als Fehlversuch für den Schutzschalter je Server.

### Inhaltsvergleich

Liefert eine Quelle keine verlässlichen Zeitstempel (OParl 1.0: `modified` fehlt, oder `created` und `modified`
sind gleich und stehen auf Mitternacht), vergleicht der Ingestor den Inhalt. Die Reihenfolge eingebetteter Listen
zählt dabei nicht (`ingestor/src/storage/ris_events.py`, `content_fingerprint`). Das ändert nichts an den
Anfragen, verhindert aber, dass unveränderte Objekte in jedem Lauf neu geschrieben werden.

## Vollabgleich

- **Uhrzeit:** täglich zur vollen Stunde `--full-sync-hour`, Standard 03:00 (`ingestor/src/main.py`;
  `docker-compose.yml`: `INGESTOR_FULL_SYNC_HOUR`; `.env.example` und Helm `ingestor.fullSyncHour` ebenfalls 3),
  in der Ortszeit des Ingestor-Containers (`TZ`, in `docker-compose.yml` Standard `Europe/Berlin`). Die von uns
  betriebene Instanz startet ihn um 01:00 Uhr.
- **Umfang:** alle Seiten aller Listen ohne Filter, auch die als fehlend vermerkten. Löschmarkierungen der Quelle
  (`deleted: true`) übernehmen wir als Löschvermerk.
- **Später Beginn:** Läuft zur vollen Stunde noch ein inkrementeller Lauf, folgt der Vollabgleich direkt nach ihm
  (`ingestor/src/scheduler/__init__.py`, `_full_sync_pending`). Während ein Vollabgleich läuft, entfallen die
  inkrementellen Läufe.
- **Je Quelle seltener:** `full_sync_every_days` (ganze Zahl von 1 bis 365) verlangt so viele Tage Abstand zum
  letzten vollständig erfolgreichen Vollabgleich (`last_successful_full_sync`), mit einer Stunde Spielraum. Ist er
  nicht fällig, läuft die Quelle in dieser Nacht inkrementell. Ein Vollabgleich mit Lücke zählt nicht; er wird in der
  nächsten Nacht wiederholt (`ingestor/src/client/source_options.py`, `full_sync_plan`).
- **Je Quelle nur in einem Zeitfenster:** `full_sync_window`, etwa `{"start": "00:00", "end": "06:00"}` (Ortszeit,
  Standard `Europe/Berlin`, abweichend mit `"tz"`, über Mitternacht erlaubt, Beginn und Ende verschieden).
  Außerhalb des Fensters läuft ein Vollabgleich als inkrementeller Abgleich, ob geplant, aus dem Admin oder per
  `sync --full`. Das Fenster muss deshalb die Startstunde (`--full-sync-hour`) enthalten und lang genug für einen
  ganzen Vollabgleich der Quelle sein; sonst kommt für sie kein vollständiger Vollabgleich zustande.
- **Nach dem Ende des Fensters** holt ein laufender Vollabgleich keine weitere Seite; der Ingestor prüft das vor jeder
  Seite (`FetchWindowClosedError`). Offene Listen gelten als Lücke: Der Vollabgleich zählt nicht als vollständig,
  das Änderungsfenster rückt nicht vor, und die Listen kommen beim nächsten Vollabgleich wieder dran.
- Das Zeitfenster begrenzt nur die Listen des Vollabgleichs. Inkrementelle Läufe, Dateiabrufe der Texterkennung
  und die Abrufe der Anwendung richten sich nicht danach.

## Dokumente

Dateien (Vorlagen, Anlagen, Niederschriften, fast immer PDF) laden Ingestor und Anwendung unter folgenden Regeln.
Für alle gilt: robots.txt wie oben, Takt je Server wie oben, Weiterleitungen wie oben, und
`sync_config["file_downloads"] = false` schaltet den Dateiabruf einer Quelle ganz ab. Die Vorschau verweist dann auf
das Original.

### Texterkennung im Ingestor

- Der Ingestor lädt jede neu erfasste Datei, deren Text fehlt, einmal (Zustand `pending`). Geänderte Fassungen
  holt der Löschabgleich der Anwendung.
- Je Kommune und Lauf höchstens 500 Dateien (`TEXT_EXTRACTION_BATCH_SIZE`), 4 gleichzeitig
  (`TEXT_EXTRACTION_CONCURRENCY`), je Server aber im Takt von höchstens einer Anfrage je Sekunde mit höchstens
  zwei offenen Anfragen.
- Dateien über 50 MB lädt er nicht (`TEXT_EXTRACTION_MAX_SIZE_MB`); er prüft die angegebene Größe und bricht den
  Download darüber ab.
- Bricht die Bearbeitung einer Datei ab (etwa Speichermangel), versucht er es höchstens dreimal
  (`TEXT_EXTRACTION_MAX_ATTEMPTS`). Ein fehlgeschlagener Download wird hier nicht wiederholt.
- Sperrt die robots.txt die Datei, überspringt er sie. Ist die robots.txt nicht erreichbar, bleibt die Datei offen,
  und er stellt die Dateien der Kommune 15 Minuten zurück (`ingestor/src/extraction/extractor.py`).
- Mit `TEXT_EXTRACTION_RUNNER=worker` lädt der Ingestor keine Dateien mehr; dann holt sie nur die Anwendung
  ([`FILE_CACHE.md`](FILE_CACHE.md)).

### Dokument-Cache und Vorschau der Anwendung

- **Dokument-Cache:** stündlich um :40 höchstens 400 Dateien, je Quelle höchstens 200 (`DOCUMENT_FETCH_MAX_QUEUED`),
  fällige Wiederholungen und die neuesten Dateien zuerst (`mandari/hub/ris/abruf.py`, `nachladen`). Welche Kommunen
  ablegen, steht in [`FILE_CACHE.md`](FILE_CACHE.md).
- **Vorschau:** Öffnet jemand ein Dokument, das nicht in der Ablage liegt, lädt die Anwendung es bei der Quelle und
  legt es dabei ab, sofern die Kommune ablegt. Läuft für die Datei gerade ein Abruf des Dokument-Caches, fragt die
  Vorschau nicht ein zweites Mal an, sondern bittet um einen neuen Versuch.
- **Wiederholungen** nach Zeitüberschreitung, Verbindungsfehler, 429, 5xx, leerer Antwort oder nicht erreichbarer
  robots.txt: nach 15 Minuten, 1, 6, 24 und 72 Stunden (`BACKOFF`). Danach gilt die Datei als Fehler. Fehler, auch
  HTTP 403, versucht die Anwendung nach sieben Tagen erneut (`ERROR_RETRY`).
- **HTTP 404/410** in den ersten sieben Tagen nach unserer Erfassung gilt als vorläufig (`NOT_FOUND_GRACE`):
  Quellen veröffentlichen das Objekt oft vor der Datei. Danach übernimmt der Löschabgleich.
- **Gesperrt per robots.txt** oder **Bot-Schutz** (eine HTML-Seite statt der Datei): Diese Datei laden wir erst nach
  einer Freigabe wieder. Zugangsprüfungen umgehen wir nicht.
- **Größe:** höchstens 80 MB je Datei (`FILE_CACHE_MAX_MB`).
- **Abgeschaltet und Schonung:** Eine abgeschaltete Quelle (`is_active`) fragen Dokument-Cache und Personenfotos
  nicht an. Vorschau und Löschabgleich richten sich nur nach `file_downloads` und der Schonung, nicht nach
  `is_active`. Sollen gar keine Abrufe mehr stattfinden, gehört zur Abschaltung deshalb auch
  `file_downloads = false`; eine Sperre per robots.txt wirkt ohnehin bei allen Abrufen. Nach drei fehlgeschlagenen
  Läufen des Abgleichs in Folge fragen Dokument-Cache, Vorschau, Löschabgleich und Personenfotos die Quelle nicht
  an, bis ein Lauf sie wieder erreicht (`INSIGHT_SOURCE_BACKOFF_FAILURES`; was als fehlgeschlagen zählt, steht unter
  [Fehler und Schonung](#fehler-und-schonung)).

### Löschabgleich

Stündlich um :15 gleicht die Anwendung die Ablage mit den Quellen ab
(`mandari/insight_core/management/commands/loeschabgleich.py`, [`FILE_CACHE.md`](FILE_CACHE.md#löschabgleich)):

- Dokumente, die die Quelle nach unserer Kopie geändert hat (`modified`), lädt sie neu und vergleicht die Prüfsumme;
  höchstens 200 je Lauf (`--changed-limit`).
- Stichproben per `HEAD`, nur bei öffentlich gelisteten Kommunen, höchstens 30 je Kommune und Lauf
  (`--head-limit`, `sample_heads`).
- Wegen 404/410 gesperrte Dokumente prüft sie bei öffentlich gelisteten Kommunen nach 1, 7 und 25 Tagen erneut,
  zusammen mit den Stichproben (`mandari/insight_core/services/file_reconcile.py`, `RECHECK_DAYS`, `sample_heads`).
  Bevor sie Kopie und Text eines nicht mehr
  abrufbaren Dokuments nach Ablauf der Frist löscht (Standard 30 Tage, `FILE_PURGE_AFTER_DAYS`), fragt sie es noch
  einmal per `GET` ab.
- Mindestens 2 Sekunden zwischen zwei Prüfungen an demselben Server (`--interval`), zusätzlich im gemeinsamen
  Takt. Bringt eine `HEAD`-Stichprobe keine klare Antwort (etwa 404 oder 405, HTML statt Datei, andere Größe),
  folgt die Nachprüfung per `GET` nur im gemeinsamen Takt (Standard 1 s). Nach HTTP 429 oder 503 fragt der Lauf diesen
  Server nicht weiter an, nach 5 Fehlern in Folge ebenso (`MAX_ERRORS_PER_HOST`).
- Liefern in einem Lauf mehr als 10 Dokumente einer Quelle neu 404/410, sperrt sie keines und lässt die Quelle für
  diesen Lauf ruhen (`FILE_RECONCILE_MAX_MISSING`).
- Die robots.txt wertet der Löschabgleich wie alle anderen Abrufe aus (`mandari/insight_core/services/file_robots.py`).

### Personenfotos

- **Zeitplan:** montags um 03:00, nur für Kommunen mit hinterlegtem Fotomuster
  (`mandari/insight_core/management/commands/fetch_person_photos.py`).
- **Menge:** höchstens 500 Personen je Kommune und Lauf (`--limit`); geladen werden Fotos, die noch nie geprüft
  wurden oder deren letzte Prüfung mehr als 30 Tage zurückliegt (`--max-age-days`). Zwischen zwei Fotos liegen
  0,1 Sekunden Pause, dazu der Takt je Server.
- **Wiederholung:** Ein fehlendes Foto (404/410 oder kein Bild) und ein Fehler werden vermerkt und frühestens nach
  30 Tagen wieder angefragt. Ist die robots.txt nicht erreichbar, wird nichts vermerkt; der nächste Lauf versucht es
  erneut.
- **Ruhen:** Ist die Quelle abgeschaltet, steht `file_downloads` auf `false` oder wird die Quelle geschont, lädt die
  Anwendung keine Fotos und vermerkt nichts (`mandari/insight_core/services/person_photos.py`, `_source_blocked`).

## Fehler und Schonung

| Lage | Verhalten | Fundstelle |
|---|---|---|
| Netzfehler, Zeitüberschreitung, HTTP 5xx | höchstens 3 Versuche, Pausen 1 s und 2 s, jeder Versuch im Takt | `ingestor/src/config.py` `oparl_max_retries`, `oparl_retry_backoff` |
| HTTP 404 | keine Wiederholung, kein Fehlversuch für den Schutzschalter | `ingestor/src/client/oparl_client.py` |
| HTTP 403 | keine Wiederholung; eine Vergleichsanfrage je Server und Lauf einer Quelle | wie oben |
| andere 4xx, auch 429 | keine Wiederholung dieser Anfrage | wie oben |
| 5 Serverfehler in Folge an einen Server | Quelle gilt als gestört (5xx-Serie) | `config.py` `oparl_server_error_series_threshold` |
| 5 Fehlversuche an einen Server im Lauf einer Quelle (Netzfehler, Zeitüberschreitung, 4xx außer 404, 5xx) | Schutzschalter: 60 s keine Anfragen an diesen Server | `config.py` `circuit_breaker_failure_threshold`, `circuit_breaker_recovery_timeout` |
| 3 fehlgeschlagene Läufe einer Quelle in Folge | Pause 10 Minuten, je weiterem Fehlversuch doppelt so lang, höchstens 360 Minuten (10, 20, 40 … 360) | `ingestor/src/sync/orchestrator.py` `BACKOFF_*`, `source_backoff_until` |
| User-Agent gesperrt | Pause 60 Minuten; ist die Pause aus der Reihe darüber länger, gilt sie (ab dem 6. Fehlversuch in Folge 80, 160, 320, 360 Minuten) | `BACKOFF_MIN_MINUTES_BY_KIND` |
| 5xx-Serie oder robots.txt nicht erreichbar | Pause 30 Minuten; ab dem 5. Fehlversuch in Folge die längere Pause aus der Reihe (40, 80 … 360 Minuten) | wie oben |
| robots.txt sperrt | Pause 24 Stunden | wie oben |

Als fehlgeschlagen zählt ein Lauf, wenn die eingetragene Adresse oder die Kommunenliste nicht abrufbar ist (keine
Kommune gefunden; bricht erst eine Folgeseite der Kommunenliste ab, ist der Lauf nur unvollständig), wenn der Lauf
abbricht oder wenn ein Befund vorliegt: User-Agent-Sperre, 5xx-Serie oder ein Server, an den wegen seiner robots.txt
keine einzige Anfrage ging, weil sie sperrt oder nicht erreichbar ist
(`ingestor/src/sync/orchestrator.py`, `_apply_host_findings`; `ingestor/src/storage/database.py`,
`record_source_failure`). Andere Fehler in einzelnen Listen zählen nicht, auch nicht HTTP 429 oder
Zeitüberschreitungen: Der Lauf fragt die übrigen Listen weiter im Takt an, nur der Schutzschalter pausiert nach
fünf Fehlversuchen an einem Server 60 Sekunden. Einen `Retry-After`-Header werten wir nicht aus.

Bei OParl-Quellen setzt jeder Lauf, der Adresse und Kommunenliste erreicht und nicht abbricht, den Zähler der
Fehlversuche zurück (`update_source_sync_time`). Ein Befund in einem solchen Lauf zählt danach wieder als erster
Fehlversuch. Im Abgleich greifen die Pausen wegen User-Agent-Sperre, 5xx-Serie und robots.txt deshalb schon beim
ersten Befund; die längeren Pausen ab dem 5. bzw. 6. Fehlversuch treten nur ein, wenn Adresse oder Kommunenliste
mehrmals in Folge nicht abrufbar sind oder der Lauf mehrmals in Folge abbricht. Bei Quellen ohne OParl (Scraper)
setzt nur ein vollständiger Crawl den Zähler zurück (`ingestor/src/scrapers/runner.py`). Sperrt die robots.txt, ist
sie nicht erreichbar oder schlägt der Crawl aus einem anderen Grund fehl, bricht er ab und der Zähler steigt weiter;
dort werden die Pausen länger, und die Anwendung ruht nach drei solchen Läufen in Folge. Eine Sperre unseres
User-Agents umgehen wir nicht; wir versuchen es nur in den genannten Abständen wieder.

Die Pausen der Tabelle gelten für den Abgleich samt Texterkennung und für die Scraper. Die Abrufe der Anwendung
(Dokument-Cache, Vorschau, Löschabgleich, Personenfotos) ruhen erst bei drei fehlgeschlagenen Läufen in Folge im
obigen Sinn (`INSIGHT_SOURCE_BACKOFF_FAILURES`) und bis ein Lauf den Zähler wieder zurücksetzt. Bei OParl-Quellen
ruhen sie wegen einer User-Agent-Sperre oder 5xx-Serie also nur, wenn auch Adresse oder Kommunenliste betroffen sind
oder der Lauf abbricht. Eine Sperre per robots.txt beachten sie unabhängig davon bei jedem Abruf. Der getrennt betriebene OCR-Worker
`extract-daemon` richtet sich nur nach `file_downloads` und der robots.txt, nicht nach der Schonung und nicht danach,
ob die Quelle abgeschaltet ist.

## Einstellungen je Quelle

Jede Quelle hat im Admin ein JSON-Feld `sync_config`. Ungültige Werte gelten als nicht gesetzt. Der Ingestor liest
es zu Beginn jedes Laufs der Quelle, die Texterkennung spätestens nach 5 Minuten
(`ingestor/src/extraction/extractor.py`, `SOURCE_OPTIONS_TTL_SECONDS`), die Anwendung bei jedem Abruf.

| Schlüssel | Bedeutung | Beispiel |
|---|---|---|
| `request_interval` | Abstand der Abrufe dieser Quelle in Sekunden, 1 bis 30; nur langsamer als der Standard, kleinere Werte gelten als 1 | `2.0` |
| `full_sync_window` | Zeitfenster des Vollabgleichs (Ortszeit, optional `tz`) | `{"start": "00:00", "end": "06:00"}` |
| `full_sync_every_days` | Mindestabstand zwischen vollständigen Vollabgleichen in Tagen, ganze Zahl von 1 bis 365 | `7` |
| `modified_since_margin_minutes` | Sicherheitsabstand des Änderungsfensters in Minuten, ganze Zahl von 0 bis 10080 | `60` |
| `list_params` | Parameter für die erste Seite jeder Liste (nicht `modified_since`, Werte bis 100 Zeichen) | `{"size": 100}` |
| `carry_modified_since` | Quelle filtert, verliert den Filter aber in `links.next` | `true` |
| `file_downloads` | `false`: keine Dateien dieser Quelle abrufen; nur ein ausdrückliches `false` schaltet ab | `false` |
| `download_headers` | zusätzliche Header für Datei-Downloads (z. B. `Referer`, `Cookie`, `User-Agent`) | `{"Referer": "https://rat.example.de/bi/"}` |
| `robots_override` | Ausnahme von der robots.txt, nur mit Freigabe und Vermerk | `{"scope": "files", "note": "Freigabe per E-Mail vom …"}` |
| `source_type`, `scraper` | Quelle ohne OParl-Schnittstelle, mit `scraper.rate_limit_seconds` (Standard 2, mindestens 1) | siehe [`SCRAPER_SOURCES.md`](SCRAPER_SOURCES.md) |
| `document_since`, `document_backfill` | Stichtag und Altbestand der Ablage (nur mit `TEXT_EXTRACTION_RUNNER=worker`) | siehe [`FILE_CACHE.md`](FILE_CACHE.md) |

Bedeutung der Schlüssel im Code: `ingestor/src/client/source_options.py`, `shared/mandari_oparl/pacing.py`,
`shared/mandari_oparl/robots.py`. Beispiel einer Quelle, die langsamer und nur nachts vollständig abgerufen werden
soll:

```json
{
  "request_interval": 2.0,
  "full_sync_window": {"start": "22:00", "end": "05:00"},
  "full_sync_every_days": 7
}
```

Diese Schlüssel pflegt der Ingestor selbst (`ingestor/src/storage/database.py`); von Hand nur löschen, um einen
Befund zurückzusetzen:

| Schlüssel | Inhalt |
|---|---|
| `body_sync_started` | Beginn des letzten vollständig erfolgreichen Abgleichs je Kommune (UTC), Anfang des Änderungsfensters; nach nicht gespeicherten Objekten höchstens deren ältestes `modified`. Löschen setzt das Änderungsfenster auf sieben Tage zurück |
| `timed_lists` | Listen, deren `modified` nachweislich eine Uhrzeit trägt, mit dem Zeitpunkt des Nachweises (Adresse → Zeitpunkt UTC); nur sie bekommen das kurze Fenster. Löschen lässt die Listen ab Tagesbeginn laufen, bis ein Lauf die Uhrzeit wieder nachweist |
| `missing_lists` | Optionale Listen, die es bei der Quelle nicht gibt, mit dem Zeitpunkt der letzten Prüfung (Adresse → Zeitpunkt UTC). Löschen lässt den nächsten Lauf die Listen wieder anfragen |
| `modified_since_unsupported_hosts` | Server, die `modified_since` ablehnen oder ignorieren. Löschen wirkt erst nach einem Neustart des Ingestors und nur, wenn keine andere Quelle denselben Server vermerkt hat; sonst trägt die Prüfung nach dem nächsten Vollabgleich den Server aus, sobald er filtert |
| `modified_since_check` | Ergebnis der letzten Prüfung des Filters (`checked_at`, `host`, `result`) |
| `scraper_state` | Zustand der Scraper |
| `probe` | Ergebnis von `probe-ris --store` |

Weitere Schlüssel wie `events_enabled`, `id_address`, `id_base`, `id_rules`, `session_tenant` oder `portal_state`
betreffen nicht den Abruf.

## Einstellungen per Umgebungsvariable

Ingestor (`ingestor/src/config.py`; Groß- und Kleinschreibung egal):

| Variable | Standard | Wirkung |
|---|---|---|
| `INGESTOR_REQUEST_INTERVAL` | `1.0` | Abstand je Server in Sekunden; `0` = Drossel aus (nur Tests und Notfälle) |
| `INGESTOR_HOST_MAX_CONCURRENT` | `2` | offene Anfragen je Server und Prozess; `0` = ohne Grenze |
| `INGESTOR_MODIFIED_SINCE_MARGIN_MINUTES` | `120` | Sicherheitsabstand des Änderungsfensters |
| `INGESTOR_USER_AGENT` (älter: `SCRAPER_USER_AGENT`) | siehe [Kennung](#kennung) | User-Agent des Ingestors (Abgleich, Scraper, Texterkennung, Sondierung) |
| `OPARL_REQUEST_TIMEOUT` | `60` | Zeitgrenze in Sekunden je Verbindungsaufbau und Lesevorgang |
| `OPARL_MAX_RETRIES`, `OPARL_RETRY_BACKOFF` | `3`, `2.0` | Versuche bei Netzfehler und 5xx; Pausen `OPARL_RETRY_BACKOFF` hoch 0, 1, … Sekunden |
| `OPARL_UA_PROBE_ENABLED` | `true` | Vergleichsanfrage nach HTTP 403 (Abgleich und `probe-ris`) |
| `OPARL_SERVER_ERROR_SERIES_THRESHOLD` | `5` | Serverfehler in Folge bis zur 5xx-Serie |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD`, `CIRCUIT_BREAKER_RECOVERY_TIMEOUT` | `5`, `60` | Schutzschalter je Server |
| `SYNC_SOURCE_CONCURRENCY`, `SYNC_BODY_CONCURRENCY`, `SYNC_ENTITY_CONCURRENCY` | `2`, `2`, `2` | Quellen, Kommunen und Listen gleichzeitig |
| `TEXT_EXTRACTION_ENABLED` | `true` | Texterkennung im Abgleich |
| `TEXT_EXTRACTION_BATCH_SIZE`, `TEXT_EXTRACTION_CONCURRENCY`, `TEXT_EXTRACTION_MAX_SIZE_MB` | `500`, `4`, `50` | Dateien je Kommune und Lauf, gleichzeitig, Größe |
| `TEXT_EXTRACTION_RUNNER` | `ingestor` | `worker`: Ingestor lädt keine Dateien |
| `REDIS_URL` | `redis://localhost:6379` | gemeinsamer Takt je Server |

Daemon (Argumente von `python -m src.main daemon`, in `docker-compose.yml` aus der `.env`): `--interval`
(`INGESTOR_INTERVAL`), `--full-sync-hour` (`INGESTOR_FULL_SYNC_HOUR`), `--concurrent` (`INGESTOR_CONCURRENT`,
Abrufplätze je Quelle; je Server gilt trotzdem `INGESTOR_HOST_MAX_CONCURRENT`).

Anwendung (`mandari/mandari/settings.py`):

| Variable | Standard | Wirkung |
|---|---|---|
| `RIS_REQUEST_INTERVAL` | `1.0` | Abstand je Server in Sekunden; `0` = Drossel aus (nur Tests und Notfälle) |
| `FILE_PROXY_PACE_MAX_WAIT_SECONDS` | `5` | höchste Wartezeit der Vorschau insgesamt: auf Abrufplatz, robots.txt und Takt, Weiterleitungen eingeschlossen |
| `FILE_CACHE_MAX_MB` | `80` | größte Datei für die Ablage |
| `DOCUMENT_FETCH_MAX_QUEUED` | `200` | Abrufe je Quelle und Lauf des Dokument-Caches |
| `INSIGHT_SOURCE_BACKOFF_FAILURES` | `3` | ab so vielen fehlgeschlagenen Läufen in Folge ruhen Dokument-Cache, Vorschau, Löschabgleich und Personenfotos für die Quelle |
| `INSIGHT_FETCH_ALLOW_PRIVATE_NETWORKS` | `false` | Dokumentabrufe auch an private Adressen (RIS im eigenen Netz) |

Unter Docker Compose erreichen einen Dienst nur die Variablen, die `docker-compose.yml` in seinem Abschnitt
`environment` nennt. Die übrigen Variablen der Tabellen tragen Sie dort oder in einer zusätzlichen Compose-Datei
nach. Im Helm-Chart: `ingestor.extraEnv` bzw. `app.extraEnv`.

## Für Selbstbetreiber

- Ihre Abrufe kommen von Ihrem Server, mit Ihrer Verantwortung gegenüber den Betreibern der Quellen.
- Setzen Sie `INGESTOR_USER_AGENT` auf eine Kennung mit Ihrer Kontaktadresse, etwa
  `mandari-ingestor/0.1.0 (+https://ris.example.de/abruf/; it@example.de)`. Unter Docker Compose gehört die
  Variable in den Abschnitt `environment` des Dienstes `ingestor`, im Helm-Chart in `ingestor.extraEnv`; ein
  Eintrag nur in der `.env` wirkt nicht. Regeln der robots.txt für `mandari-ingestor` gelten weiter, weil mandari
  dieses Token immer zusätzlich prüft.
- Die Sync-Aktionen an einer Quelle im Admin laufen im Worker der Anwendung, ohne Schonung (siehe
  [Rhythmus](#rhythmus)). `INGESTOR_USER_AGENT` und die übrigen Variablen des Ingestors (`INGESTOR_*`, `OPARL_*`)
  gehören deshalb auch in dessen Umgebung: unter Docker Compose in `x-app-environment`, im Helm-Chart in
  `app.extraEnv`. Sonst ruft der Worker mit der Standardkennung samt support@mandari.de ab.
- Die Abrufe der Anwendung (Dokument-Cache, Vorschau, Löschabgleich, Personenfotos, Quellenanlage, Live-Übertragung
  und Geodienste) melden sich mit der festen Kennung
  `mandari-ingestor (+https://mandari.de/crawler/; support@mandari.de)`. Eine Umgebungsvariable dafür gibt es nicht;
  abweichend nur je Quelle über `sync_config["download_headers"]` (nicht für Quellenanlage, Live-Übertragung und
  Geodienste).
- Den gemeinsamen Takt über alle Prozesse gibt es nur mit Redis, das Ingestor und Anwendung beide erreichen.
- Liegt ein RIS im eigenen Netz, braucht die Anwendung `INSIGHT_FETCH_ALLOW_PRIVATE_NETWORKS=true`.

## Andere Abrufe

Diese Abrufe gehen nicht an Ratsinformationssysteme. Sie melden sich mit derselben Kennung,
`mandari-ingestor (+https://mandari.de/crawler/; support@mandari.de)`, und halten einen eigenen Takt je Server ein
(`mandari/insight_core/services/dienste.py`, `ABSTAND`), vor jeder Anfrage und vor jeder Weiterleitung. Der Takt
nutzt denselben Zeitstempel in Redis wie die Abrufe bei Ratsinformationssystemen: Liegt ein Dienst auf dem Server
eines RIS, etwa das Geoportal einer Kommune, teilen sich beide den Takt. Hat eine registrierte Quelle auf demselben
Hostnamen einen größeren Abstand (`request_interval`), gilt dieser für Live-Übertragung und Geoportale ebenfalls,
in beide Richtungen (`dienste.NEBEN_RIS`, `quellen_takte`; die Abstände der Quellen liegen fünf Minuten im Cache).
Ohne Redis drosselt jeder Prozess für sich.

Eine robots.txt werten diese Abrufe nicht aus, auch nicht bei Geoportalen von Kommunen: Sie durchsuchen keine
Seiten, sondern fragen Schnittstellen ab; es gelten die Nutzungsregeln des jeweiligen Dienstes. Eine Gruppe für
`mandari-ingestor` in der robots.txt eines Geoportals stoppt den täglichen Abruf der Umringe also nicht. Wer ihn
nicht möchte, schreibt uns; wir schalten die Quelle dann ab (Umring-Quelle im Admin, „Aktiv“,
`PlanBoundarySource.is_active`).

| Abruf | Ziel | Abstand je Server | Wann | Fundstelle |
|---|---|---|---|---|
| Live-Übertragung | Streaming-Anbieter (3Q, HLS-Server) | 1 s; ein größerer Abstand einer registrierten Quelle auf dem Server gilt | nur mit `LIVE_UEBERTRAGUNG_AKTIV`: Status einmal je Minute rund um Sitzungen; während einer Übertragung ein Einzelbild im Takt der Quelle (Standard 10 Sekunden, 5 bis 60), je Einzelbild 2 bis 4 Anfragen | `mandari/hub/live/anbieter/http.py`, `dienste.LIVE` |
| Umringe von Bebauungsplänen | Geoportale von Kommunen und Land (WFS, OGC API – Features) | 1 s; ein größerer Abstand einer registrierten Quelle auf dem Server gilt; dazu 1 s Pause zwischen zwei Seiten | täglich um 04:50 (`sync_plan_boundaries`) | `mandari/insight_core/services/plan_boundaries.py`, `dienste.GEOPORTAL` |
| Nominatim (OpenStreetMap) | `nominatim.openstreetmap.org` | 1 s (Nutzungsregeln: höchstens eine Anfrage je Sekunde) | Geodaten einer Kommune von Hand (`fetch_osm_geodata`, `resolve_body_geodata`); Ersatz in der Georeferenzierung von Vorgängen, wenn Photon nichts findet | `fetch_osm_geodata.py`, `georeferencing.py`, `dienste.NOMINATIM` |
| Overpass (OpenStreetMap) | `overpass-api.de`, Ersatz `overpass.kumi.systems` | 1 s | Straßen, Adressen und Grenzen von Hand (`import_streets`, `resolve_body_geodata`) | `mandari/insight_core/services/overpass.py`, `dienste.OVERPASS` |
| Photon | `photon.komoot.io` (`PHOTON_API_URL`) | 1 s; in der Adresssuche höchstens 2 s Wartezeit, sonst keine Vorschläge | Georeferenzierung von Vorgängen für Orte, die das eigene Straßenverzeichnis nicht kennt; Adresssuche nur ohne eigenes Straßenverzeichnis | `georeferencing.py`, `neighborhood.py`, `dienste.PHOTON` |
| Kartenkacheln | `tile.openstreetmap.org` | 0,05 s (bis zu 20 Kacheln je Sekunde); höchstens 5 s Wartezeit, sonst HTTP 503 mit `Retry-After: 5` | nur Kacheln, die nicht im eigenen Zwischenspeicher liegen; dazu je Besucher-Adresse höchstens 300 und insgesamt 1200 Abrufe je Minute | `mandari/insight_core/views/maps.py`, `dienste.KACHELN` |
| Kacheln vorab laden | `tile.openstreetmap.org` | 0,5 s | nur von Hand (`prefetch_tiles`) | `prefetch_tiles.py`, `dienste.KACHELN_VORAB` |

`DIENSTE_DROSSEL` schaltet den Takt dieser Abrufe ab; die Einstellung gibt es nur im Code für Tests
(`mandari/mandari/settings_test.py`), nicht als Umgebungsvariable.
