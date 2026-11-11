# Implementierungsnotizen

Die **öffentliche Dokumentation** der mandari-Plattform steht unter <https://docs.mandari.de>
(Quelltext: [mandariOSS/docs](https://github.com/mandariOSS/docs)): aufbereitet für Nutzer:innen,
Betreiber und Integrator:innen.

Die Dateien in diesem Verzeichnis sind **Implementierungsnotizen** für die Entwicklung und den Betrieb:
Sie nennen Module, Dateipfade, Befehle, Prüfungen und Grenzwerte. Regeln dafür stehen in
[WEITERENTWICKLUNG.md](WEITERENTWICKLUNG.md), Abschnitt 10: Eine Notiz beschreibt, was im Code existiert;
Pläne gehören in Issues oder ADRs; Überholtes wird entfernt. Wer eine Funktion hier beschreibt, ergänzt
auch die passende öffentliche Seite (oder legt im Docs-Repository ein Issue an).

## Arbeitsweise und Regeln

| Notiz | Inhalt | Öffentliche Seite |
|-------|--------|-------------------|
| `WEITERENTWICKLUNG.md` | verbindlich: Release-Zug alle zwei Wochen, Planung, Prioritäten, gleichzeitige Arbeit, Rückportierung in Pflegelinien | – |
| `RELEASE_POLITIK.md` | Zusagen nach außen: Versionierung, Takt, Pflegeversionen, Sicherheitsfristen, Abkündigung | [Release- und Support-Politik](https://docs.mandari.de/entwicklung/release-politik/) |
| `ENGINEERING_STANDARDS.md` | Regeln für Code: Schichten, Typisierung, Tests, Sicherheit mit Uploads und CSP, Frontend mit Barrierefreiheit, API, Betrieb | – |

## Schnittstellen

| Notiz | Inhalt | Öffentliche Seite |
|-------|--------|-------------------|
| `SCHNITTSTELLE.md` | alles unter `/api/`: Einstieg, OpenAPI, kommParl-Profile, Session-API v1, Provisioning-API, gemeinsame Regeln | [Session-API v1](https://docs.mandari.de/session/api-v1/) |
| `OPARL_API.md` | OParl-Aggregator und die gemeinsamen OParl-Regeln (Änderungsfeed, Kennungen, Konformität) | [OParl-Aggregations-API](https://docs.mandari.de/insight/oparl-api/) |
| `SESSION_OPARL_API.md` | OParl je Session-Mandant: Freischaltung, Sichtbarkeit, Veröffentlichung im Bürgerportal | [OParl-API je Kommune](https://docs.mandari.de/session/oparl-api/), [Bürgerportal](https://docs.mandari.de/session/buergerportal/) |
| `DCAT_KATALOG.md` | Datenkatalog nach DCAT-AP.de für Datenportale | – |
| `EREIGNISKATALOG.md` | aus dem Vertragsregister erzeugt: alle Ereignisse und Befehle der Datendrehscheibe | – |
| `DATENDREHSCHEIBE.md` | Zusagen und Nachweise der Ereignistechnik, Session als Erzeuger, Umstellung je Weg, Architektur-Kennzahlen | – |

## Bürgerportal und KI

| Notiz | Inhalt | Öffentliche Seite |
|-------|--------|-------------------|
| `INSIGHT.md` | Kommunenwechsel und Kommunenverzeichnis, Fraktionen ohne OParl-Fraktion, Ratsfragen, Personenfotos | [Ratsfragen](https://docs.mandari.de/insight/ratsfragen/) |
| `INSIGHT_GEO.md` | Verortung, Straßenverzeichnis, Umringe, Kartenanalyse in Anlagen | – |
| `KI.md` | KI-Assistent, MCP-Server, gemeinsame Werkzeuge, Kennzeichnung nach EU AI Act Art. 50 | – |
| `LIVE_UEBERTRAGUNG.md` | Live-Übertragungen von Gremiensitzungen | – |

## Session und Work

| Notiz | Inhalt | Öffentliche Seite |
|-------|--------|-------------------|
| `SESSION.md` | Nummernkreise, Vier-Augen-Prinzip und Vertretungen, Sitzungscockpit, Erinnerungen, Beschlusskontrolle bis ins Bürgerportal | [Fristen-Erinnerungen](https://docs.mandari.de/session/fristen-erinnerungen/), [Beschlusskontrolle](https://docs.mandari.de/session/beschlusskontrolle/), [Beschlüsse verfolgen](https://docs.mandari.de/insight/beschluesse/) |
| `SESSION_MANDANTEN.md` | Mandanten anlegen und deaktivieren, Körperschaften, Bürgerportal je Körperschaft, Mandantengruppen mit Leitstelle, gemeinsame Sitzungen | – |
| `SESSION_STAMMDATEN_IMPORT.md` | Umstieg aus SessionNet und Übernahme der Stammdaten | – |
| `SESSION_SITZUNGSFORMAT_LANDESRECHT.md` | Sitzungsformate und Landesrecht je Bundesland | – |
| `WORK.md` | Registrierung und Zugangs-Mails, Anträge digital einreichen, neues Design ausrollen | [Anträge einreichen](https://docs.mandari.de/work/antraege-einreichen/), [Fraktions-API](https://docs.mandari.de/work/fraktions-api/) |

## Abruf, Ablage und Betrieb

| Notiz | Inhalt | Öffentliche Seite |
|-------|--------|-------------------|
| `ABRUFREGELN.md` | wie mandari Ratsinformationssysteme abruft: Kennung, Takt, robots.txt, Schonung, Opt-out | [Infoseite Ingestor](https://mandari.de/ingestor/) |
| `SCRAPER_SOURCES.md` | Quellen ohne OParl: Scraper, Bridges, OParl 1.0, ALLRIS 3 | [Quellen anbinden](https://docs.mandari.de/betrieb/quellen-anbinden/) |
| `FILE_CACHE.md` | Dokumentablage, Objektspeicher, Abrufzustände, Texterkennung aus der Ablage, Löschabgleich | [Dokument-Cache](https://docs.mandari.de/betrieb/dokument-cache/) |
| `MONITORING.md` | Betriebsmonitor, Health-Endpunkte, Metriken, Alarme, Service-Level | [Betriebsmonitor](https://docs.mandari.de/betrieb/monitoring/) |
| `BACKUP.md` | Sicherungs-Stack mit restic, Zeitpunkt-Wiederherstellung, Journal nach dem Einspielen | [Updates und Backups](https://docs.mandari.de/betrieb/updates-backups/) |
| `LASTTESTS.md` | Mengengerüste, Lastszenarien, Budgets, Ergebnisse, Größenempfehlungen, Befehle | – |

## Sicherheit und Datenschutz

| Notiz | Inhalt | Öffentliche Seite |
|-------|--------|-------------------|
| `ACCOUNT_SECURITY.md` | Zwei-Faktor-Pflicht, Sicherheitsschlüssel, Admin-Netze, Sicherheitshinweise | [Konto und Sicherheit](https://docs.mandari.de/work/konto-und-sicherheit/) |
| `KRYPTOKONZEPT.md` | Kryptokonzept nach BSI CON.1 und TR-02102: Verfahren, Schlüssel, Schlüsselwechsel | – |
| `PROTOKOLLIERUNG.md` | alle Protokolle mit Fristen: Systemprotokolle und fachliches Audit-Log mit Hash-Kette | [Datenschutz](https://docs.mandari.de/datenschutz/) |
| `DATENSCHUTZ.md` | Muster-AVV, technische und organisatorische Maßnahmen, Löschkonzept | [Datenschutz](https://docs.mandari.de/datenschutz/) |

## Unterordner

| Ordner | Inhalt |
|--------|--------|
| [`adr/`](adr/README.md) | Architekturentscheidungen |
| [`schnittstelle/`](schnittstelle/anmeldung.md) | Anmeldung für Anwendungen per OpenID Connect (Teil A) und Betrieb des Anbieters (Teil B); `openapi.json` ist die eingecheckte Beschreibung |
| [`prozesse/`](prozesse/ANTRAGSLAUF.md) | fachliches Modell des Antragslaufs mit BPMN |
| [`cla/`](cla/README.md) | Beitragsvereinbarung |

Sicherheitsmeldungen und unterstützte Versionen stehen in [`SECURITY.md`](../SECURITY.md), Abhängigkeiten und
die Stückliste je Release in [`DEPENDENCIES.md`](../DEPENDENCIES.md), Installation und Betrieb mit Compose in
[`DEPLOYMENT.md`](../DEPLOYMENT.md).
