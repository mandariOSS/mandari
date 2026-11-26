# Release- und Support-Politik

Kommunen planen Fachverfahren über Jahre. Diese Seite legt fest, wie mandari
versioniert wird, wie oft Releases erscheinen, welche Versionen wie lange Korrekturen
erhalten, in welcher Frist Sicherheitslücken geschlossen werden und wie lange
Schnittstellen stabil bleiben. Sie ist die verbindliche Grundlage für Betreiber,
Selbst-Hoster und Vergabestellen (Issue #96).

Wie wir intern planen, ausliefern und zurückportieren, steht in
[Weiterentwicklung](WEITERENTWICKLUNG.md).

> **Stand:** 26.11.2026: keine Pflegeversionen und kein LTS, MAJOR mindestens drei Monate vorher
> angekündigt. Vorige Fassung vom 11.11.2026, gültig ab Release 0.13.0 (Issue #1341). Erste Fassung
> beschlossen am 17.09.2026, gültig ab 0.11.0 (eine Version 0.10.0 ist nicht erschienen). Änderungen
> stehen am Ende dieser Seite und auf docs.mandari.de.

## 1. Versionierung

mandari folgt **Semantic Versioning 2.0** (`MAJOR.MINOR.PATCH`):

| Teil | Bedeutung | Für Betreiber heißt das | Beispiel |
|---|---|---|---|
| **PATCH** | nur Fehlerbehebungen und Sicherheitskorrekturen, keine neuen Funktionen | ohne Prüfung einspielen | 0.16.0 → 0.16.1 |
| **MINOR** | neue Funktionen, Einstellungen, Schnittstellenversionen; alles Bisherige läuft weiter | einspielen ohne Handarbeit | 0.16 → 0.17 |
| **MAJOR** | eine Migration mit Handarbeit, der Bruch einer veröffentlichten Schnittstelle oder der Wegfall einer Funktion | Umstellung planen, mindestens drei Monate vorher angekündigt | 1.x → 2.0 |

Die Version steht an genau einer Stelle je Sprache (`mandari/pyproject.toml`,
`mandari/package.json`) und wird von `scripts/check_version_consistency.py` gegen
den Git-Tag geprüft. `mandari/uv.lock` führt die Version des Projekts mit; nach dem
Anheben in `pyproject.toml` deshalb `uv lock` im Ordner `mandari` ausführen (das
Skript prüft auch das). Container-Images tragen die Version als Tag (`v0.16.0`),
zusätzlich `v0.16` für die jüngste Patch-Version einer Linie, `latest` für den jüngsten Stand von `main`
(der Produktion; das ist keine Zusage einer Version, ein Release setzt `latest` nur, wenn sein Commit der Kopf
von `main` ist) und `dev-<commit>` bzw.
`main-<commit>` für Entwicklungs- und Produktionsstände. Diese Stände sind keine
Releases und tragen keine Zusage.

**Zweige:** Jede veröffentlichte Version bleibt über einen eigenen Zweig erreichbar,
`release/v0.16.0` für Version 0.16.0, auch Patch-Versionen (`release/v0.16.1`). Der Zweig
zeigt auf denselben Commit wie der Git-Tag und wird danach nicht mehr verändert; der
Release-Workflow legt ihn beim Veröffentlichen an. Für die Übergangslinien 0.11 und 0.12
(Abschnitt 3) gibt es bis zum 31.01.2027 zusätzlich je eine **Pflegelinie** ohne Patch-Nummer
(`release/v0.11`, `release/v0.12`): Dort sammeln sich die zurückportierten Korrekturen, aus ihr
entstehen die Patch-Versionen. Neue Pflegelinien entstehen nicht. Entwickelt wird auf `dev`;
`main` ist der Stand der Produktion.

**Vor 1.0** (heute): Die Regeln oben gelten bereits. Brechende Änderungen sind vor 1.0
in einer MINOR-Version nur ausnahmsweise erlaubt: Sie stehen mindestens **vier Wochen**
vorher im Changelog unter „Abgekündigt“, bekommen eine Migrationsanleitung und kommen
nie in eine PATCH-Version.

## 2. Release-Takt

| Art | Rhythmus | Inhalt |
|---|---|---|
| **MINOR** | alle **zwei Wochen**, dienstags | was am Stichtag in `dev` fertig war und seitdem in Produktion läuft |
| **PATCH** | bei Bedarf, innerhalb der Fristen aus Abschnitt 4 | nur Korrekturen nach Abschnitt 5, keine neuen Funktionen |
| **MAJOR** | angekündigt mindestens **drei Monate** vorher | mit Migrationsleitfaden |

- **Stichtag** ist der Donnerstag vor dem Release. Der an diesem Tag freigegebene Stand geht in
  Produktion und wird am Dienstag als Version veröffentlicht, wenn er bis dahin ohne Fehler läuft,
  die ein Release verhindern (Datenverlust, Sicherheitslücke, Ausfall, gescheiterte Migration).
  Das Release ist damit nie der erste Kontakt mit echtem Betrieb.
- **Der Termin steht, der Inhalt folgt ihm.** Was am Stichtag nicht fertig ist, kommt zwei
  Wochen später. Lässt sich ein Fehler nicht rechtzeitig beheben, entfällt das Release; das
  nächste erscheint planmäßig.
- **Ausnahme Korrektur:** Muss nach dem Stichtag ein Fehler behoben werden, der das Release
  verhindert, erscheint der korrigierte Stand aus `main`, wenn er bis Dienstag mindestens einen
  Werktag in Produktion läuft. Er enthält auch, was seit dem Stichtag fertig wurde; isolierte
  Korrekturen auf dem Stichtagsstand gibt es nicht.
- **Feiertage:** Fällt der Dienstag zwischen den 24.12. und den 01.01. oder auf einen bundesweiten
  Feiertag, entfällt das Release. Fällt der Stichtag auf einen Feiertag, ist es der Werktag davor.
- Die Termine stehen als Fälligkeit an den [Meilensteinen](https://github.com/mandariOSS/mandari/milestones).

Warum zwei Wochen: Korrekturen und Funktionen erreichen alle Installationen schnell, jedes
Release bleibt klein und überschaubar. Jede MINOR lässt sich ohne Handarbeit einspielen,
außer bei einer vor 1.0 angekündigten brechenden Änderung (mit Migrationsanleitung, Abschnitt 1)
und bei einer MAJOR-Version (mit Migrationsleitfaden); gepflegt wird deshalb nur die jüngste
(Abschnitt 3).

## 3. Pflege und Supportzeitraum

Unterstützt wird nur die **jüngste MINOR**. Pflegeversionen gibt es nicht, und es gibt **kein
LTS**, auch nicht ab 1.0. Dafür entwickeln wir zu schnell.

| Version | Erhält | Dauer |
|---|---|---|
| **Jüngste MINOR** | Sicherheitskorrekturen und Korrekturen kritischer Fehler (Abschnitt 5) als PATCH oder mit der nächsten MINOR, wenn diese innerhalb der Frist erscheint | bis zur nächsten MINOR, also rund zwei Wochen |
| **ältere MINOR** | nichts mehr nach Erscheinen der Nachfolgerin; Aktualisierung auf die jüngste Version ohne Handarbeit, außer bei angekündigten brechenden Änderungen und MAJOR-Versionen | – |
| **0.11 und 0.12** (Übergang) | Sicherheitskorrekturen ab „mittel“ und Korrekturen kritischer Fehler als PATCH | bis **31.01.2027** |

Welche Versionen heute unterstützt werden und bis wann, steht in
[SECURITY.md](../SECURITY.md#unterstützte-versionen).

**Übergang (Änderung vom 26.11.2026):** Bisher war die erste MINOR jedes Quartals eine
Pflegeversion mit sechs Monaten Pflege, ab 1.0 war LTS mit 24 Monaten angekündigt. Beides
entfällt. Zugesagt waren Korrekturen für **0.11** bis 03.05.2027 und für **0.12** bis 17.05.2027.
Beide erhalten Sicherheitskorrekturen ab „mittel“ und Korrekturen kritischer Fehler nur noch bis
**31.01.2027**; danach endet ihre Pflege. Wer 0.11 oder 0.12 betreibt, aktualisiert bis dahin auf
die jüngste Version. 0.16 und spätere Versionen werden keine Pflegeversionen.

**Managed Hosting:** Unsere Cloud läuft auf dem jüngsten Stand. Weitere gehostete Instanzen
aktualisieren wir spätestens **14 Tage** nach einem Release; Sicherheitskorrekturen innerhalb
der Fristen aus Abschnitt 4, ohne gesonderte Zustimmung. Kunden erhalten die Ankündigung über
die Statusseite (status.mandari.de) und, bei Auswirkungen auf die Bedienung, per Mail an die
hinterlegte Kontaktadresse.

## 4. Sicherheitskorrekturen: Fristen

Schweregrad nach CVSS 3.1 (Bewertung siehe [SECURITY.md](../SECURITY.md#cvss-bestimmen)):

| Schweregrad | CVSS | Korrektur in Produktion und als Release | 0.11 und 0.12 (bis 31.01.2027) | Veröffentlichung des Advisories |
|---|---|---|---|---|
| **Kritisch** | ≥ 9,0 | **72 Stunden** | PATCH in derselben Frist | mit der Korrektur, CVE wird beantragt |
| **Hoch** | 7,0–8,9 | **7 Tage** | PATCH in derselben Frist | mit der Korrektur, CVE wird beantragt |
| **Mittel** | 4,0–6,9 | **30 Tage** | PATCH in derselben Frist | mit der Korrektur, CVE nach Einschätzung |
| **Niedrig** | < 4,0 | spätestens mit der nächsten MINOR | keine Rückportierung | im Changelog |

„Als Release“ heißt für die jüngste Version: als PATCH oder mit der nächsten MINOR, wenn sie
innerhalb der Frist erscheint. Die Frist beginnt mit der Bestätigung der Meldung
(Eingangsbestätigung innerhalb von drei Werktagen, siehe `SECURITY.md`). Abhängigkeiten werden
von Dependabot überwacht; `pip-audit` blockiert Änderungen mit jeder bekannten Lücke in
Python-Abhängigkeiten, `npm audit` ab „high“. Technische Einzelheiten zum Angriffsweg folgen
spätestens 90 Tage nach dem Advisory (Ablauf in [SECURITY.md](../SECURITY.md#umgang-mit-gemeldeten-lücken)).
Ist eine Korrektur in der Frist nicht möglich, veröffentlichen wir eine
Übergangsmaßnahme (Konfiguration, Abschaltung einer Funktion) und einen Termin.

## 5. Was in PATCH-Versionen kommt

Jede Korrektur entsteht zuerst in `dev`. In eine PATCH-Version der jüngsten MINOR und, bis zum
31.01.2027, der Pflegelinien 0.11 und 0.12 kommen nur:

| Art | in einer PATCH-Version |
|---|---|
| Sicherheitskorrekturen ab Schweregrad **mittel** | ja |
| **Kritische Fehler:** Datenverlust oder Verfälschung gespeicherter Daten; Nichtöffentliches oder Personenbezogenes wird Unbefugten sichtbar; Ausfall einer Kernfunktion ohne Umgehung; Fehler mit Rechtswirkung, etwa falsch berechnete Fristen oder eine falsche Beschlusslage | ja |
| Aktualisierung einer Abhängigkeit | nur wenn sie eine Lücke ab „mittel“ schließt |
| neue Funktionen, Umbauten, Darstellung, Leistung, niedrige Sicherheitsbefunde, übrige Fehler | nein, sie kommen mit der nächsten MINOR |

Zurückportierte Korrekturen enthalten **keine Datenbankmigration**. Lässt sich ein Fehler nur
mit einer Migration beheben, entscheidet die Projektleitung im Einzelfall; bis dahin gilt die
Übergangsmaßnahme aus Abschnitt 4.

## 6. Schnittstellen und Abkündigung

| Schnittstelle | Versionierung | Ankündigungsfrist für brechende Änderungen |
|---|---|---|
| **OParl-API** (`/oparl/v1/`) | folgt dem OParl-Standard (1.0, 1.1); Erweiterungen unter den Präfixen `kommparl:` und `mandari:` | **12 Monate**, alte Version bleibt parallel erreichbar |
| **OParl je Mandant** (`/session/<mandant>/api/oparl/`) mit den kommParl-Profilen (Änderungsfeed, Einreichung, Nichtöffentliches) | wie die OParl-API | **12 Monate**, alte Version parallel |
| **Session-API** (`/api/v1/session/`) | Pfadversion (`v1`, `v2`) | **6 Monate**, alte Version parallel |
| **Fraktions-API / Work** (`/api/public/v1/`) | Pfadversion | **6 Monate**, alte Version parallel |
| **Einstieg und Beschreibung** (`/api/`, `/api/openapi.json`) | Bereiche wie oben; neue mandari-Bereiche unter `/api/mandari/v<n>/` | **6 Monate** |
| **Anmeldung für Anwendungen** (OpenID Connect, `/oauth/`) | Endpunkte und Scopes | **6 Monate** |
| **Datenkatalog** (`/data/dcat/`) | DCAT-AP.de | **6 Monate** |
| **Provisioning-API** (`/api/provisioning/`, `/api/provisioning/v2/`) | Pfadversion (v1, v2); intern, nur für den Betreiber | keine Zusage |
| **MCP-Server** (`/insight/mcp`) | Werkzeuge für KI-Assistenten | keine Zusage |

Die Fristen hängen am Kalender, nicht am Release-Takt: Wer auf mandari aufbaut, braucht die
Zeit unabhängig davon, wie oft wir veröffentlichen. Abkündigungen stehen im Changelog unter
„Abgekündigt“ mit Datum des Wegfalls, in der API-Antwort als Header `Deprecation` (RFC 9745,
Zeitpunkt der Abkündigung) und `Sunset` (RFC 8594, Zeitpunkt des Wegfalls) und auf
docs.mandari.de. Felder werden nie umgedeutet; sie werden ergänzt oder nach Frist entfernt.
Ausnahme: Eine Korrektur, die eine Ausgabe an den OParl-Standard angleicht (Normkonformität), gilt
nicht als brechende Änderung. Sie steht im Changelog unter „Geändert“ mit einem Hinweis für
Abnehmer (Beispiel: `organizationType` und `File.date`, Issue #560).

## 7. Changelog

`CHANGELOG.md` im Repository folgt **Keep a Changelog 1.1**: je Release die
Abschnitte *Hinzugefügt*, *Geändert*, *Abgekündigt*, *Entfernt*, *Behoben* und
**Sicherheit** (mit GHSA-/CVE-Kennung, sobald das Advisory veröffentlicht ist). Jedes
Release ab 0.11.0 enthält einen Changelog-Eintrag; ohne Eintrag wird kein Tag gesetzt.
Patch-Versionen stehen im Changelog ihrer Linie und werden im Changelog von `dev`
nachgetragen. Die Kurzfassung erscheint auf `/releases/`.

## 8. Sicherheitsmeldungen abonnieren

- **GitHub Security Advisories** des Repositories: „Watch → Custom → Security alerts“.
- **Feed:** `https://github.com/mandariOSS/mandari/security/advisories` und die
  Release-Feeds `https://github.com/mandariOSS/mandari/releases.atom`.
- **E-Mail-Verteiler:** `security-announce@mandari.de`, nur Ankündigungen,
  Anmeldung über das Trust Center.

Meldungen an uns: `security@mandari.de`, Ablauf in `SECURITY.md`.

## 9. Auf einen Blick

| Punkt | Regel |
|---|---|
| MINOR-Release | alle zwei Wochen, dienstags; Stichtag Donnerstag davor |
| Unterstützt | nur die jüngste MINOR: Sicherheitskorrekturen und kritische Fehler als PATCH oder mit der nächsten MINOR |
| ältere MINOR | keine Korrekturen mehr; Aktualisierung ohne Handarbeit (Ausnahmen: angekündigte brechende Änderungen, MAJOR) |
| Pflegeversionen, LTS | keine, auch nicht ab 1.0 |
| Übergang | 0.11 und 0.12 bis 31.01.2027, Sicherheitskorrekturen ab „mittel“ und kritische Fehler |
| Managed-Hosting-Aktualisierung | binnen 14 Tagen, Sicherheit nach Fristen |
| Sicherheitsfristen | 72 h / 7 Tage / 30 Tage / nächste MINOR |
| Brechende Änderungen vor 1.0 | mindestens vier Wochen angekündigt, nie in einer PATCH-Version |
| MAJOR | mindestens drei Monate angekündigt, mit Migrationsleitfaden |
| API-Abkündigung | OParl 12 Monate, übrige 6 Monate |
| Verteiler | security-announce@mandari.de |

## 10. Änderungen dieser Seite

| Datum | Änderung |
|---|---|
| 26.11.2026 | keine Pflegeversionen und kein LTS mehr, unterstützt wird nur die jüngste MINOR; 0.11 und 0.12 erhalten Korrekturen nur noch bis 31.01.2027 statt bis 03.05.2027 bzw. 17.05.2027; niedrige Sicherheitsbefunde mit der nächsten MINOR; MAJOR mindestens drei statt sechs Monate vorher angekündigt |
| 11.11.2026 | Schnittstellentabelle ergänzt (OParl je Mandant, Einstieg, Anmeldung, Datenkatalog, MCP), Ausnahme für Korrekturen zur Normkonformität; Release alle zwei Wochen statt alle zwei Monate; Pflegeversionen je Quartal statt Pflege jeder vorherigen MINOR; Regeln zur Rückportierung; Frist für niedrige Befunde an die Pflegeversion gebunden; Übergang für 0.11 und 0.12 (Issue #1341) |
| 10.11.2026 | ein Zweig je Version |
| 17.09.2026 | erste Fassung (Issue #96); erstes Release danach war 0.11.0, eine 0.10.0 ist nicht erschienen |
