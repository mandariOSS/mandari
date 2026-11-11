# Release- und Support-Politik

Kommunen planen Fachverfahren über Jahre. Diese Seite legt fest, wie mandari
versioniert wird, wie oft Releases erscheinen, welche Versionen wie lange Korrekturen
erhalten, in welcher Frist Sicherheitslücken geschlossen werden und wie lange
Schnittstellen stabil bleiben. Sie ist die verbindliche Grundlage für Betreiber,
Selbst-Hoster und Vergabestellen (Issue #96).

Wie wir intern planen, ausliefern und zurückportieren, steht in
[Weiterentwicklung](WEITERENTWICKLUNG.md).

> **Stand:** 11.11.2026, gültig ab Release 0.13.0 (Issue #1341). Erste Fassung beschlossen am
> 17.09.2026, gültig ab 0.10.0. Änderungen stehen am Ende dieser Seite und auf docs.mandari.de.

## 1. Versionierung

mandari folgt **Semantic Versioning 2.0** (`MAJOR.MINOR.PATCH`):

| Teil | Bedeutung | Für Betreiber heißt das | Beispiel |
|---|---|---|---|
| **PATCH** | nur Fehlerbehebungen und Sicherheitskorrekturen, keine neuen Funktionen | ohne Prüfung einspielen | 0.16.0 → 0.16.1 |
| **MINOR** | neue Funktionen, Einstellungen, Schnittstellenversionen; alles Bisherige läuft weiter | einspielen ohne Handarbeit | 0.16 → 0.17 |
| **MAJOR** | eine Migration mit Handarbeit, der Bruch einer veröffentlichten Schnittstelle oder der Wegfall einer Funktion | Umstellung planen, lange vorher angekündigt | 1.x → 2.0 |

Die Version steht an genau einer Stelle je Sprache (`mandari/pyproject.toml`,
`mandari/package.json`) und wird von `scripts/check_version_consistency.py` gegen
den Git-Tag geprüft. `mandari/uv.lock` führt die Version des Projekts mit; nach dem
Anheben in `pyproject.toml` deshalb `uv lock` im Ordner `mandari` ausführen (das
Skript prüft auch das). Container-Images tragen die Version als Tag (`v0.16.0`),
zusätzlich `v0.16` für die jüngste Patch-Version einer Linie, `latest` für den jüngsten Stand von `main`
(die höchste veröffentlichte Version oder neuer; ein Patch-Release einer älteren Linie setzt es nie) und `dev-<commit>` bzw.
`main-<commit>` für Entwicklungs- und Produktionsstände. Diese Stände sind keine
Releases und tragen keine Zusage.

**Zweige:** Jede veröffentlichte Version bleibt über einen eigenen Zweig erreichbar,
`release/v0.16.0` für Version 0.16.0, auch Patch-Versionen (`release/v0.16.1`). Der Zweig
zeigt auf denselben Commit wie der Git-Tag und wird danach nicht mehr verändert; der
Release-Workflow legt ihn beim Veröffentlichen an. Für jede Pflegeversion (Abschnitt 3)
gibt es zusätzlich eine **Pflegelinie** `release/v0.16` ohne Patch-Nummer: Dort sammeln
sich die zurückportierten Korrekturen, aus ihr entstehen die Patch-Versionen. Entwickelt
wird auf `dev`; `main` ist der Stand der Produktion.

**Vor 1.0** (heute): Die Regeln oben gelten bereits. Brechende Änderungen sind vor 1.0
in einer MINOR-Version nur ausnahmsweise erlaubt: Sie stehen mindestens **vier Wochen**
vorher im Changelog unter „Abgekündigt“, bekommen eine Migrationsanleitung und kommen
nie in eine Patch-Version einer Pflegelinie.

## 2. Release-Takt

| Art | Rhythmus | Inhalt |
|---|---|---|
| **MINOR** | alle **zwei Wochen**, dienstags | was am Stichtag in `dev` fertig war und seitdem in Produktion läuft |
| **PATCH** | bei Bedarf, innerhalb der Fristen aus Abschnitt 4 | nur Korrekturen nach Abschnitt 5, keine neuen Funktionen |
| **MAJOR** | angekündigt mindestens **sechs Monate** vorher | mit Migrationsleitfaden |

- **Stichtag** ist der Donnerstag vor dem Release. Der an diesem Tag freigegebene Stand geht in
  Produktion und wird am Dienstag als Version veröffentlicht, wenn er bis dahin ohne Fehler läuft,
  die ein Release verhindern (Datenverlust, Sicherheitslücke, Ausfall, gescheiterte Migration).
  Das Release ist damit nie der erste Kontakt mit echtem Betrieb.
- **Der Termin steht, der Inhalt folgt ihm.** Was am Stichtag nicht fertig ist, kommt zwei
  Wochen später. Lässt sich ein Fehler nicht rechtzeitig beheben, entfällt das Release; das
  nächste erscheint planmäßig.
- **Feiertage:** Fällt der Dienstag zwischen den 24.12. und den 01.01. oder auf einen bundesweiten
  Feiertag, entfällt das Release. Fällt der Stichtag auf einen Feiertag, ist es der Werktag davor.
- Die Termine stehen als Fälligkeit an den [Meilensteinen](https://github.com/mandariOSS/mandari/milestones).

Warum zwei Wochen: Korrekturen und Funktionen erreichen alle Installationen schnell, jedes
Release bleibt klein und überschaubar. Wer seltener aktualisieren will, bleibt auf einer
Pflegeversion (Abschnitt 3).

## 3. Pflege und Supportzeitraum

Es gibt zwei Spuren: die **jüngste Version** für alle, die mit dem Takt gehen, und
**Pflegeversionen** für Betreiber mit eigenem Test- und Freigabeprozess.

| Version | Erhält | Dauer |
|---|---|---|
| **Jüngste MINOR** | Funktionen, Fehlerbehebungen, Sicherheitskorrekturen | bis zur nächsten MINOR, also rund zwei Wochen |
| **Pflegeversion** | Sicherheitskorrekturen ab „mittel“ und Korrekturen kritischer Fehler (Abschnitt 5) als PATCH | **sechs Monate** ab Erscheinen |
| **übrige MINOR** | nichts mehr nach Erscheinen der Nachfolgerin; Aktualisierung auf die jüngste Version ohne Handarbeit | – |
| **LTS** (ab 1.0) | Sicherheitskorrekturen und kritische Fehlerbehebungen | **24 Monate**, eine Linie je Jahr |

**Pflegeversion** ist die erste MINOR-Version, die in einem Kalenderquartal erscheint. So gibt
es alle drei Monate eine neue Pflegeversion; meist werden zwei gleichzeitig gepflegt. Welche
Versionen heute unterstützt werden und bis wann, steht in [SECURITY.md](../SECURITY.md#unterstützte-versionen).
Wie Pflegeversionen und LTS-Linien ab 1.0 zusammenspielen, legen wir spätestens mit der
Ankündigung von 1.0 fest.

**Übergang:** Für Versionen, die vor dem 11.11.2026 erschienen sind, gelten die bisher zugesagten
Zeiträume weiter: **0.11** erhält Sicherheitskorrekturen bis **03.05.2027**, **0.12** (zugleich
Pflegeversion für das vierte Quartal 2026) bis **17.05.2027**.

**Managed Hosting:** Unsere Cloud läuft auf dem jüngsten Stand. Weitere gehostete Instanzen
aktualisieren wir spätestens **14 Tage** nach einem Release; Sicherheitskorrekturen innerhalb
der Fristen aus Abschnitt 4, ohne gesonderte Zustimmung. Kunden erhalten die Ankündigung über
die Statusseite (status.mandari.de) und, bei Auswirkungen auf die Bedienung, per Mail an die
hinterlegte Kontaktadresse.

## 4. Sicherheitskorrekturen: Fristen

Schweregrad nach CVSS 3.1 (Bewertung siehe `docs/SICHERHEITSMELDUNGEN.md`):

| Schweregrad | CVSS | Korrektur in Produktion und als Release | Pflegeversionen | Veröffentlichung des Advisories |
|---|---|---|---|---|
| **Kritisch** | ≥ 9,0 | **72 Stunden** | PATCH in derselben Frist | mit der Korrektur, CVE wird beantragt |
| **Hoch** | 7,0–8,9 | **7 Tage** | PATCH in derselben Frist | mit der Korrektur, CVE wird beantragt |
| **Mittel** | 4,0–6,9 | **30 Tage** | PATCH in derselben Frist | mit der Korrektur |
| **Niedrig** | < 4,0 | spätestens mit der nächsten Pflegeversion | keine Rückportierung | im Changelog |

„Als Release“ heißt für die jüngste Version: als PATCH oder mit der nächsten MINOR, wenn sie
innerhalb der Frist erscheint. Die Frist beginnt mit der Bestätigung der Meldung
(Eingangsbestätigung innerhalb von drei Werktagen, siehe `SECURITY.md`). Abhängigkeiten werden
von Dependabot überwacht; `pip-audit` und `npm audit` blockieren Änderungen mit bekannten
Lücken ab „hoch“. Ist eine Korrektur in der Frist nicht möglich, veröffentlichen wir eine
Übergangsmaßnahme (Konfiguration, Abschaltung einer Funktion) und einen Termin.

## 5. Was in Pflegeversionen zurückportiert wird

Jede Korrektur entsteht zuerst in `dev`. In jede unterstützte Pflegelinie übernommen werden nur:

| Art | Rückportierung |
|---|---|
| Sicherheitskorrekturen ab Schweregrad **mittel** | ja |
| **Kritische Fehler:** Datenverlust oder Verfälschung gespeicherter Daten; Nichtöffentliches oder Personenbezogenes wird Unbefugten sichtbar; Ausfall einer Kernfunktion ohne Umgehung; Fehler mit Rechtswirkung, etwa falsch berechnete Fristen oder eine falsche Beschlusslage | ja |
| Aktualisierung einer Abhängigkeit | nur wenn sie eine Lücke ab „mittel“ schließt |
| neue Funktionen, Umbauten, Darstellung, Leistung, niedrige Sicherheitsbefunde, übrige Fehler | nein, sie kommen mit der nächsten MINOR bzw. Pflegeversion |

Zurückportierte Korrekturen enthalten **keine Datenbankmigration**. Lässt sich ein Fehler nur
mit einer Migration beheben, entscheidet die Projektleitung im Einzelfall; bis dahin gilt die
Übergangsmaßnahme aus Abschnitt 4.

## 6. Schnittstellen und Abkündigung

| Schnittstelle | Versionierung | Ankündigungsfrist für brechende Änderungen |
|---|---|---|
| **OParl-API** (`/oparl/v1/`) | folgt dem OParl-Standard (1.0, 1.1); Erweiterungen unter `mandari:`-Präfix | **12 Monate**, alte Version bleibt parallel erreichbar |
| **Session-API** (`/api/v1/session/`) | Pfadversion (`v1`, `v2`) | **6 Monate**, alte Version parallel |
| **Fraktions-API / Work** | Pfadversion | **6 Monate**, alte Version parallel |
| **Provisioning-API** (`/api/provisioning/`, `/api/provisioning/v2/`) | Pfadversion (v1, v2); intern, nur für den Betreiber | keine Zusage |

Die Fristen hängen am Kalender, nicht am Release-Takt: Wer auf mandari aufbaut, braucht die
Zeit unabhängig davon, wie oft wir veröffentlichen. Abkündigungen stehen im Changelog unter
„Abgekündigt“ mit Datum des Wegfalls, in der API-Antwort als Header `Deprecation` (RFC 9745,
Zeitpunkt der Abkündigung) und `Sunset` (RFC 8594, Zeitpunkt des Wegfalls) und auf
docs.mandari.de. Felder werden nie umgedeutet; sie werden ergänzt oder nach Frist entfernt.

## 7. Changelog

`CHANGELOG.md` im Repository folgt **Keep a Changelog 1.1**: je Release die
Abschnitte *Hinzugefügt*, *Geändert*, *Abgekündigt*, *Entfernt*, *Behoben* und
**Sicherheit** (mit GHSA-/CVE-Kennung, sobald das Advisory veröffentlicht ist). Jedes
Release ab 0.10.0 enthält einen Changelog-Eintrag; ohne Eintrag wird kein Tag gesetzt.
Patch-Versionen einer Pflegelinie stehen im Changelog ihrer Linie und werden im Changelog
von `dev` nachgetragen. Die Kurzfassung erscheint auf `/releases/`.

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
| Pflegeversion | erste MINOR jedes Quartals, sechs Monate Sicherheitskorrekturen ab „mittel“ und kritische Fehler |
| übrige MINOR | Korrekturen nur bis zur nächsten MINOR |
| Übergang | 0.11 bis 03.05.2027, 0.12 bis 17.05.2027 |
| LTS | ab 1.0, 24 Monate, jährlich |
| Managed-Hosting-Aktualisierung | binnen 14 Tagen, Sicherheit nach Fristen |
| Sicherheitsfristen | 72 h / 7 Tage / 30 Tage / nächste Pflegeversion |
| Brechende Änderungen vor 1.0 | mindestens vier Wochen angekündigt, nie in Pflegelinien |
| MAJOR | mindestens sechs Monate angekündigt |
| API-Abkündigung | OParl 12 Monate, übrige 6 Monate |
| Verteiler | security-announce@mandari.de |

## 10. Änderungen dieser Seite

| Datum | Änderung |
|---|---|
| 11.11.2026 | Release alle zwei Wochen statt alle zwei Monate; Pflegeversionen je Quartal statt Pflege jeder vorherigen MINOR; Regeln zur Rückportierung; Frist für niedrige Befunde an die Pflegeversion gebunden; Übergang für 0.11 und 0.12 (Issue #1341) |
| 10.11.2026 | ein Zweig je Version |
| 17.09.2026 | erste Fassung, gültig ab 0.10.0 (Issue #96) |
