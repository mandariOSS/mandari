# Release- und Support-Politik

Kommunen planen Fachverfahren über Jahre. Diese Seite legt fest, wie mandari
versioniert wird, wie oft Releases erscheinen, wie lange eine Version unterstützt
wird, in welcher Frist Sicherheitslücken geschlossen werden und wie lange
Schnittstellen stabil bleiben. Sie ist die verbindliche Grundlage für Betreiber,
Selbst-Hoster und Vergabestellen (Issue #96).

> **Stand: Entwurf zur Entscheidung.** Werte, die mit „(Entscheidung)“ markiert
> sind, sind begründete Vorschläge des Entwicklungsteams; sie werden mit der
> Veröffentlichung dieser Seite auf docs.mandari.de verbindlich.

## 1. Versionierung

mandari folgt **Semantic Versioning 2.0** (`MAJOR.MINOR.PATCH`):

| Teil | Wann | Beispiel |
|---|---|---|
| **MAJOR** | Änderungen, die eine Migration mit Handarbeit, den Bruch einer veröffentlichten Schnittstelle oder den Wegfall einer Funktion bedeuten | 0.x → 1.0 |
| **MINOR** | Neue Funktionen, neue Einstellungen, neue Schnittstellenversionen; Aktualisierung ohne Handarbeit | 0.10 → 0.11 |
| **PATCH** | Fehlerbehebungen und Sicherheitskorrekturen ohne Funktionsänderung | 0.10.0 → 0.10.1 |

Die Version steht an genau einer Stelle je Sprache (`mandari/pyproject.toml`,
`mandari/package.json`) und wird von `scripts/check_version_consistency.py` gegen
den Git-Tag geprüft. Container-Images tragen die Version als Tag (`v0.10.0`),
zusätzlich `latest` für das jüngste Release und `dev-<commit>` für Entwicklungsstände.
Entwicklungsstände sind keine Releases und tragen keine Zusage.

**Vor 1.0** (heute): Wir halten uns bereits an die Regeln oben; MINOR-Versionen können
bis 1.0 in Ausnahmefällen brechende Änderungen enthalten, die dann im Changelog unter
„Brechend“ stehen und eine Migrationsanleitung bekommen.

## 2. Release-Kadenz (Entscheidung)

| Art | Rhythmus | Inhalt |
|---|---|---|
| **MINOR** | alle **zwei Monate**, jeweils in der ersten Woche | Funktionen, die auf `dev` fertig und in Produktion erprobt sind |
| **PATCH** | bei Bedarf, für Sicherheitskorrekturen innerhalb der Fristen aus Abschnitt 4 | nur Korrekturen, keine neuen Funktionen |
| **MAJOR** | angekündigt mindestens **sechs Monate** vorher | mit Migrationsleitfaden |

Begründung: Zwei Monate sind kurz genug, damit Korrekturen nicht liegen bleiben, und
lang genug, damit Kommunen mit Test- und Freigabeprozessen nachziehen können. Managed
Hosting läuft dazwischen auf erprobten Entwicklungsständen (`dev-<commit>`), die vor
jedem Release in Produktion waren; das Release ist damit nie der erste Kontakt mit
echtem Betrieb.

## 3. Supportzeitraum je Version (Entscheidung)

| Version | Erhält | Dauer |
|---|---|---|
| **Jüngste MINOR** | Funktionen, Fehlerbehebungen, Sicherheitskorrekturen | bis zur nächsten MINOR |
| **Vorherige MINOR** | Sicherheitskorrekturen | **sechs Monate** ab Erscheinen der Nachfolgerin |
| **LTS** | Sicherheitskorrekturen und kritische Fehlerbehebungen | **24 Monate**; erste LTS-Linie mit **1.0** |

Eine LTS-Linie gibt es ab 1.0 jeweils alle zwölf Monate (1.0, 1.6, …). Bis dahin
gilt: Selbst-Hoster bleiben innerhalb von sechs Monaten nach einem MINOR-Release auf
einem unterstützten Stand, wenn sie das jeweils jüngste oder vorherige MINOR fahren.

**Managed Hosting:** Wir aktualisieren alle gehosteten Instanzen spätestens **14 Tage**
nach einem Release; Sicherheitskorrekturen innerhalb der Fristen aus Abschnitt 4,
ohne gesonderte Zustimmung. Kunden erhalten die Ankündigung über die Statusseite
(status.mandari.de) und, bei Auswirkungen auf die Bedienung, per Mail an die
hinterlegte Kontaktadresse.

## 4. Sicherheitskorrekturen: Fristen (Entscheidung)

Schweregrad nach CVSS 3.1 (Bewertung siehe `docs/SICHERHEITSMELDUNGEN.md`):

| Schweregrad | CVSS | Korrektur in Produktion und als PATCH | Veröffentlichung des Advisories |
|---|---|---|---|
| **Kritisch** | ≥ 9,0 | **72 Stunden** | mit der Korrektur, CVE wird beantragt |
| **Hoch** | 7,0–8,9 | **7 Tage** | mit der Korrektur, CVE wird beantragt |
| **Mittel** | 4,0–6,9 | **30 Tage** | mit der Korrektur |
| **Niedrig** | < 4,0 | nächstes reguläres Release | im Changelog |

Die Frist beginnt mit der Bestätigung der Meldung (Eingangsbestätigung innerhalb von
drei Werktagen, siehe `SECURITY.md`). Abhängigkeiten werden von Dependabot überwacht;
`pip-audit` und `npm audit` blockieren Änderungen mit bekannten Lücken ab „hoch“.
Ist eine Korrektur in der Frist nicht möglich, veröffentlichen wir eine
Übergangsmaßnahme (Konfiguration, Abschaltung einer Funktion) und einen Termin.

## 5. Schnittstellen und Abkündigung (Entscheidung)

| Schnittstelle | Versionierung | Ankündigungsfrist für brechende Änderungen |
|---|---|---|
| **OParl-API** (`/oparl/v1/`) | folgt dem OParl-Standard (1.0, 1.1); Erweiterungen unter `mandari:`-Präfix | **12 Monate**, alte Version bleibt parallel erreichbar |
| **Session-API** (`/api/v1/session/`) | Pfadversion (`v1`, `v2`) | **6 Monate**, alte Version parallel |
| **Fraktions-API / Work** | Pfadversion | **6 Monate**, alte Version parallel |
| **Provisioning-API** (Kundenportal) | intern, nur für den Betreiber | keine Zusage |

Abkündigungen stehen im Changelog unter „Abgekündigt“ mit Datum des Wegfalls, in der
API-Antwort als Header `Deprecation` und `Sunset` (RFC 8594) und auf docs.mandari.de.
Felder werden nie umgedeutet; sie werden ergänzt oder nach Frist entfernt.

## 6. Changelog

`CHANGELOG.md` im Repository folgt **Keep a Changelog 1.1**: je Release die
Abschnitte *Hinzugefügt*, *Geändert*, *Abgekündigt*, *Entfernt*, *Behoben* und
**Sicherheit** (mit GHSA-/CVE-Kennung). Jedes Release ab 0.10.0 enthält einen
Changelog-Eintrag; ohne Eintrag wird kein Tag gesetzt (Prüfung in der
Release-Checkliste). Die Kurzfassung erscheint auf `/releases/`.

## 7. Sicherheitsmeldungen abonnieren

- **GitHub Security Advisories** des Repositories: „Watch → Custom → Security alerts“.
- **Feed:** `https://github.com/mandariOSS/mandari/security/advisories` und die
  Release-Feeds `https://github.com/mandariOSS/mandari/releases.atom`.
- **E-Mail-Verteiler** (Entscheidung): `security-announce@mandari.de`, nur Ankündigungen,
  Anmeldung über das Trust Center.

Meldungen an uns: `security@mandari.de`, Ablauf in `SECURITY.md`.

## 8. Entscheidungen auf einen Blick

| Punkt | Vorschlag |
|---|---|
| Kadenz MINOR | alle zwei Monate |
| Support vorherige MINOR | sechs Monate |
| LTS | ab 1.0, 24 Monate, jährlich |
| Managed-Hosting-Aktualisierung | binnen 14 Tagen, Sicherheit nach Fristen |
| Sicherheitsfristen | 72 h / 7 d / 30 d / nächstes Release |
| API-Abkündigung | OParl 12 Monate, übrige 6 Monate |
| Verteiler | security-announce@mandari.de |
