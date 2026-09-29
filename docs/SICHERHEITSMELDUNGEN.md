# Umgang mit Sicherheitslücken: Advisory, CVSS und CVE

Wie wir eine gemeldete oder selbst gefundene Schwachstelle behandeln — von der
Aufnahme bis zur Veröffentlichung. Gilt für mandari und die zugehörigen Repos.

Meldewege für Externe stehen in [`SECURITY.md`](../SECURITY.md). Dieses Dokument
beschreibt, was danach bei uns passiert. Für aktiv ausgenutzte Schwachstellen und
schwerwiegende Sicherheitsvorfälle gelten zusätzlich die Meldepflichten aus Abschnitt 7.

---

## 1. Grundregel zur Reihenfolge

**Erst der Fix, dann die Veröffentlichung.** Ein CVE ist eine öffentliche
Bekanntgabe. Wird er vor dem Patch angefordert und das Advisory veröffentlicht,
steht eine ungepatchte Lücke in jeder Schwachstellendatenbank — und mandari ist
Open Source und selbst betreibbar, es lesen also auch Dritte mit.

Die Reihenfolge lautet deshalb immer:

1. Privates Advisory als Entwurf anlegen (nicht als öffentliches Issue)
2. Bewerten und CVSS-Vektor bestimmen
3. Beheben, Regressionstest schreiben
4. Ausliefern — erst `dev`, dann `main`, dann Produktion und Selbst-Hoster
5. Advisory um die behobene Version ergänzen
6. CVE anfordern und Advisory veröffentlichen

Schritt 6 erst, wenn Schritt 4 abgeschlossen ist.

## 2. Wo was hingehört

Das Repo ist öffentlich. Daraus folgt eine klare Trennung:

| Ort | Inhalt |
|---|---|
| **Privates Advisory** (GitHub Security Advisory, Entwurf) | Angriffsweg, betroffene Stellen, Ausnutzbarkeit, CVSS |
| **Öffentliches Issue** | Die Umsetzungsarbeit, neutral formuliert — ohne Anleitung |
| **Commit-Nachricht** | Nach dem Fix vollständig; dann ist die Lücke geschlossen |

Ein offenes Issue, das beschreibt, wie man eine noch bestehende Lücke ausnutzt,
ist eine Anleitung. Das vermeiden wir auch bei geringer Schwere.

## 3. CVSS bestimmen

Wir bewerten nach **CVSS v3.1**, weil dieser Vektor in Vergabeunterlagen und
Schwachstellendatenbanken überall verstanden wird. Der Rechner des FIRST ist
maßgeblich: <https://www.first.org/cvss/calculator/3-1>

Die Metriken in unserem Zusammenhang:

| Metrik | Leitfrage für mandari |
|---|---|
| **AV** Angriffsvektor | Über das Netz erreichbar? Bei einer Weboberfläche fast immer `N` |
| **AC** Komplexität | Braucht es besondere Umstände, Rennen oder Wissen? Sonst `L` |
| **PR** Rechte | `N` ohne Konto, `L` mit gewöhnlichem Konto, `H` nur als Administration |
| **UI** Mitwirkung | Muss jemand einen Link öffnen oder etwas anklicken? Dann `R` |
| **S** Wirkungsbereich | Wird die Grenze zwischen Rechtebereichen überschritten? Bei gespeichertem XSS gegen andere Konten `C` |
| **C/I/A** | Was sieht, ändert oder stört ein Angreifer tatsächlich? |

**Wichtig bei der Mandantentrennung:** Eine Lücke, über die Daten einer *anderen
Organisation* erreichbar werden, ist immer `S:C` und mindestens `C:H`. Das ist
für ein Mehrmandantenprodukt im Verwaltungsumfeld der schwerste denkbare Fall,
unabhängig davon, wie leicht er auszulösen ist.

### Gearbeitetes Beispiel: GHSA-6p5c-wv4v-8g24

Anhänge ohne Dateityp-Prüfung, eingebettet ausgeliefert.

```
CVSS:3.1/AV:N/AC:L/PR:L/UI:R/S:C/C:L/I:L/A:N   →   5.4 (Medium)
```

Die Begründung Metrik für Metrik:

- `AV:N` — über die Weboberfläche erreichbar
- `AC:L` — es braucht nur einen Upload, keine besonderen Umstände
- `PR:L` — ein gewöhnliches Konto mit Bearbeitungsrecht genügt, **kein** Administrationszugang
- `UI:R` — das Opfer muss die Datei öffnen
- `S:C` — der Angreifer handelt anschließend im Rechtebereich des Opfers
- `C:L` / `I:L` — Zugriff auf das, was das Opfer sieht und darf; keine vollständige Übernahme
- `A:N` — die Verfügbarkeit ist nicht betroffen

Ohne die Anmeldepflicht (`PR:N`) wären es 6.1 gewesen. Die Einordnung als
*Medium* statt *High* trägt also die Tatsache, dass ein gültiges Konto in
derselben Organisation nötig ist — nicht eine Verharmlosung.

## 4. CVE anfordern

GitHub ist eine CVE Numbering Authority; die Anforderung läuft im Advisory über
**Request CVE**. Die Prüfung dauert bis zu drei Arbeitstage.

Wann ein CVE sinnvoll ist:

- **Ja** bei allem, was Selbst-Hoster betrifft. mandari steht unter AGPL und wird
  von Dritten betrieben — die müssen erfahren, ob sie handeln müssen.
- **Nein** bei Schwächen, die nur unseren eigenen Betrieb betreffen
  (Serverkonfiguration, Zugangsdaten, Infrastruktur). Dafür gibt es kein
  „Produkt", das jemand aktualisieren könnte.
- **Nein** bei Härtungsmaßnahmen ohne konkrete Ausnutzbarkeit.

Im Advisory gehören immer ausgefüllt: betroffene Versionen, **behobene Version**,
CVSS-Vektor, CWE und eine Beschreibung, aus der eine betreibende Person ableiten
kann, ob sie betroffen ist.

## 5. Nach der Veröffentlichung

- Behobene Version in den Release Notes nennen
- Selbst-Hoster über den üblichen Weg informieren
- Wer die Lücke gemeldet hat, wird auf Wunsch genannt (siehe `SECURITY.md`)
- Prüfen, ob dieselbe Ursache anderswo im Code steckt — bei GHSA-6p5c-wv4v-8g24
  war genau das der Fall: Die Regel stand an drei Stellen richtig und an vier
  gar nicht

## 6. Fristen

| Schwere | Behebung | Auslieferung |
|---|---|---|
| Critical (9.0–10.0) | sofort | am selben Tag |
| High (7.0–8.9) | innerhalb einer Woche | mit dem nächsten Deploy |
| Medium (4.0–6.9) | innerhalb eines Monats | regulär |
| Low (0.1–3.9) | geplant | regulär |

Bei extern gemeldeten Lücken gilt zusätzlich: Eingangsbestätigung innerhalb von
drei Werktagen, Zwischenstand spätestens nach zwei Wochen.

## 7. Meldepflichten nach dem Cyber Resilience Act

Der Cyber Resilience Act (Verordnung (EU) 2024/2847, CRA) verlangt seit dem **11.09.2026**
Meldungen über aktiv ausgenutzte Schwachstellen und schwerwiegende Sicherheitsvorfälle
(Art. 14). Die übrigen Pflichten gelten ab dem **11.12.2027**. Die Abschnitte 1 bis 6
regeln den Umgang mit der Schwachstelle; dieser Abschnitt die Meldung an Behörden und Nutzer.

### 7.1 Rolle: Hersteller oder Open-Source-Steward

Der CRA knüpft die Pflichten an Rollen. Die Rolle hängt vom Vertriebsweg ab, nicht vom Quellcode:

| Rolle | Wer | Meldepflicht |
|---|---|---|
| **Hersteller** | wer ein Produkt im Rahmen einer Geschäftstätigkeit unter eigenem Namen auf dem Markt bereitstellt, etwa eine kommerziell gelieferte Edition | volle Pflichten nach Art. 14 |
| **Open-Source-Steward** | eine juristische Person, die freie Software für kommerzielle Verwendung dauerhaft unterstützt, ohne deren Hersteller zu sein (Art. 3 Nr. 14) | Meldungen, soweit sie an der Entwicklung beteiligt ist, und eine dokumentierte Cybersicherheitsrichtlinie (Art. 24) |
| **Nutzer** | wer mandari unverändert selbst betreibt | keine nach dem CRA |

Ein Anbieter kann für eine monetarisierte Edition Hersteller und zugleich für die freie Edition
Steward sein; die Rolle wird je Produkt bestimmt (Leitlinien der Kommission vom 27.07.2026).
Der reine Betrieb als Dienst (SaaS) ist kein Produkt im Sinne des CRA; dafür gelten Datenschutz-
und Vertragspflichten. Wer mandari verändert und unter eigenem Namen anbietet, wird selbst
Hersteller.

Wir behandeln jede aktiv ausgenutzte Schwachstelle in mandari nach dem Ablauf in 7.4,
unabhängig davon, welche Rolle im Einzelfall greift.

### 7.2 Begriffe

- **Aktiv ausgenutzte Schwachstelle:** Es gibt verlässliche Belege, dass ein Angreifer sie
  ohne Erlaubnis in einem System ausgenutzt hat (Art. 3 Nr. 42). Eine gemeldete, aber nicht
  ausgenutzte Schwachstelle löst keine Meldepflicht aus; für sie gelten die Abschnitte 1 bis 6.
- **Schwerwiegender Sicherheitsvorfall:** Er beeinträchtigt die Fähigkeit des Produkts,
  Verfügbarkeit, Authentizität, Integrität oder Vertraulichkeit sensibler Daten oder Funktionen
  zu schützen, oder er hat zur Ausführung von Schadcode im Produkt oder bei Nutzern geführt
  oder kann dazu führen (Art. 14 Abs. 5).

### 7.3 Meldeweg und Fristen

Gemeldet wird über die einheitliche Meldeplattform der ENISA (Art. 16), gleichzeitig an das
koordinierende CSIRT des Mitgliedstaats der Hauptniederlassung (für uns das BSI) und an die
ENISA. Die Fristen laufen ab Kenntnis, auch an Wochenenden und Feiertagen:

| | Frühwarnung | Meldung | Abschlussbericht |
|---|---|---|---|
| **Aktiv ausgenutzte Schwachstelle** (Art. 14 Abs. 2) | 24 Stunden | 72 Stunden | 14 Tage, nachdem eine Korrektur oder Abhilfe verfügbar ist |
| **Schwerwiegender Sicherheitsvorfall** (Art. 14 Abs. 4) | 24 Stunden | 72 Stunden | ein Monat nach der Meldung |

Die Frühwarnung nennt, soweit bekannt, die Mitgliedstaaten, in denen das Produkt bereitgestellt
ist. Die Meldung beschreibt die Art der Schwachstelle, die ergriffenen Maßnahmen und was Nutzer
selbst tun können. Der Abschlussbericht enthält Schweregrad und Auswirkung, Angaben zum Angreifer,
soweit bekannt, und die Korrektur.

### 7.4 Ablauf

1. Eingang festhalten, Zeitpunkt der Kenntnis in UTC notieren.
2. Innerhalb weniger Stunden einstufen: aktiv ausgenutzt? schwerwiegender Vorfall? Welche
   Versionen und Editionen? Sind personenbezogene Daten betroffen?
3. Privates Advisory anlegen (Abschnitt 1, Schritt 1).
4. Meldeentscheidung mit Begründung festhalten.
5. Frühwarnung vor Ablauf von 24 Stunden, Meldung vor Ablauf von 72 Stunden absenden.
6. Betroffene Nutzer informieren (Art. 14 Abs. 8): mit Gegenmaßnahmen, ohne Angriffsweg.
7. Beheben und ausliefern nach Abschnitt 6 und der
   [Release- und Support-Politik](RELEASE_POLITIK.md).
8. Abschlussbericht fristgerecht absenden, danach Advisory veröffentlichen und CVE anfordern
   (Abschnitt 4).
9. Liegt die Ursache in einer Fremdkomponente, deren Betreuer informieren.

**Verhältnis zur Grundregel aus Abschnitt 1:** Die Meldung an CSIRT und ENISA ist keine
Veröffentlichung. Sie ist vertraulich und darf dem Fix vorausgehen. Öffentlich gilt weiter:
erst der Fix, dann die Veröffentlichung. Die Nutzerinformation sagt, was zu tun ist, nicht, wie
der Angriff funktioniert.

Betrifft ein Vorfall personenbezogene Daten in einer von uns betriebenen Instanz, gelten
zusätzlich die Meldepflichten der DSGVO (Art. 33) mit eigenen Fristen. Zuständigkeit, Vorlagen
für die einzelnen Meldungen und eine Checkliste führen wir intern.

### 7.5 Ab dem 11.12.2027

Dann gelten die übrigen Pflichten des CRA, darunter die Anforderungen an die
Schwachstellenbehandlung aus Anhang I Teil II: Stückliste der Komponenten, Richtlinie zur
koordinierten Offenlegung mit Kontaktadresse, Sicherheitsupdates getrennt von
Funktionsupdates und kostenlos, ein festgelegter Supportzeitraum sowie technische
Dokumentation und Konformitätsbewertung. Vorhanden sind die Stückliste je Release
([SBOM.md](SBOM.md)), die Richtlinie in [`SECURITY.md`](../SECURITY.md) und die
[Release- und Support-Politik](RELEASE_POLITIK.md); den Rest bauen wir bis dahin auf.
