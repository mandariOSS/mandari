# Umgang mit Sicherheitslücken: Advisory, CVSS und CVE

Wie wir eine gemeldete oder selbst gefundene Schwachstelle behandeln — von der
Aufnahme bis zur Veröffentlichung. Gilt für mandari und die zugehörigen Repos.

Meldewege für Externe stehen in [`SECURITY.md`](../SECURITY.md). Dieses Dokument
beschreibt, was danach bei uns passiert.

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
