# Implementierungsnotizen

Die **öffentliche Dokumentation** der mandari-Plattform lebt unter
<https://docs.mandari.de> (Quelltext: [mandariOSS/docs](https://github.com/mandariOSS/docs)).
Dort stehen die für Nutzer:innen, Betreiber und Integrator:innen aufbereiteten
Fassungen – Self-Hosting, Konfiguration, APIs, Datenschutz.

Die Dateien in diesem Verzeichnis sind **Implementierungsnotizen** für die
Entwicklung: Sie nennen Module, Dateipfade, Smoke-Tests und interne Details, die
in der öffentlichen Dokumentation bewusst fehlen. Wer ein Feature hier
dokumentiert, ergänzt bitte auch die passende Seite im Docs-Repository
(oder legt dort ein Issue an).

| Notiz | Öffentliche Seite |
|-------|-------------------|
| `OPARL_API.md` | [Insight → OParl-Aggregations-API](https://docs.mandari.de/insight/oparl-api/) |
| `SESSION_OPARL_API.md` | [Session → OParl-API je Kommune](https://docs.mandari.de/session/oparl-api/), [Veröffentlichung im Bürgerportal](https://docs.mandari.de/session/buergerportal/) |
| `FACTION_PUBLIC_API.md` | [Work → Öffentliche Fraktions-API](https://docs.mandari.de/work/fraktions-api/) |
| `WORK_SESSION_SUBMISSION.md` | [Work → Anträge digital einreichen](https://docs.mandari.de/work/antraege-einreichen/) |
| `SESSION_BESCHLUSSKONTROLLE.md` | [Session → Beschlusskontrolle](https://docs.mandari.de/session/beschlusskontrolle/) |
| `INSIGHT_DECISION_TRACKING.md` | [Insight → Beschlüsse verfolgen](https://docs.mandari.de/insight/beschluesse/) |
| `INSIGHT_GEO.md` | Insight → Nachbarschaft und Verortung (Seite folgt) |
| `INSIGHT_QUESTIONS.md` | [Insight → Ratsfragen](https://docs.mandari.de/insight/ratsfragen/) |
| `SESSION_REMINDERS.md` | [Session → Fristen-Erinnerungen](https://docs.mandari.de/session/fristen-erinnerungen/) |
| `PROTOKOLLIERUNG.md` | [Datenschutz → Protokollierungskonzept](https://docs.mandari.de/datenschutz/) (Seite folgt) |
| `SESSION_MANDANT_ANLEGEN.md` | Session → Mandanten anlegen und Bürgerportal je Körperschaft (Seite folgt) |
| `SESSION_VIER_AUGEN_VERTRETUNG.md` | Session → Vier-Augen-Prinzip und Vertretungen (Seite folgt) |
| `SESSION_LEITSTELLE.md` | Session → Leitstelle und gemeinsame Sitzungen (Seite folgt) |
| `SESSION_SITZUNGSFORMAT_LANDESRECHT.md` | Session → Sitzungsformate und Landesrecht (Seite folgt) |
| `FILE_CACHE.md` | [Betrieb → Dokument-Cache](https://docs.mandari.de/betrieb/dokument-cache/) |
| `MONITORING.md` | [Betrieb → Betriebsmonitor](https://docs.mandari.de/betrieb/monitoring/) |
| `BACKUP.md` | [Betrieb → Updates und Backups](https://docs.mandari.de/betrieb/updates-backups/) |
| `SCRAPER_SOURCES.md` | [Betrieb → Quellen anbinden](https://docs.mandari.de/betrieb/quellen-anbinden/) |
| `DEMO_ENVIRONMENT.md` | [Betrieb → Demo-Umgebung](https://docs.mandari.de/betrieb/demo-umgebung/) |
| `DEMO_PRAESENTATION.md` | – (Drehbuch für Produktvorstellungen, nur intern) |
| `ACCOUNT_SECURITY.md` | [Work → Konto und Sicherheit](https://docs.mandari.de/work/konto-und-sicherheit/); Betrieb → Anmeldesicherheit (Seite folgt) |
| `WORK_REGISTRATION.md` | Work → Registrierung und Zugangs-Mails (Seite folgt) |
| `DSGVO_TOM.md`, `DSGVO_LOESCHKONZEPT.md`, `DSGVO_AVV_MUSTER.md` | [Datenschutz](https://docs.mandari.de/datenschutz/) |
| `SBOM.md` | [Entwicklung → Abhängigkeiten](https://docs.mandari.de/entwicklung/sbom/) |
| `CSP.md` | – (Content-Security-Policy: Stand, Muster ohne Inline-Code, Bewertung Alpine-CSP-Build; Entwicklung) |
