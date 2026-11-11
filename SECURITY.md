# Sicherheitsrichtlinie

## Unterstützte Versionen

mandari erscheint alle zwei Wochen. Sicherheitskorrekturen erhalten die jüngste Version und die
**Pflegeversionen**: die erste Version jedes Quartals, sechs Monate lang. Wer selbst betreibt,
geht entweder mit dem Takt (`./update.sh --tag v<Version>` beziehungsweise `helm upgrade`, ohne
Handarbeit) oder bleibt auf einer Pflegeversion und spielt deren Patch-Versionen ein. Den
automatischen Rückfall von `update.sh` gibt es nur mit einer festen Version; ein bewegliches Tag
wie `latest` zeigt nach dem Update schon auf das neue Image.

| Version | Unterstützt | bis |
|---------|-------------|-----|
| jüngstes Release (Image-Tag `v<Version>`; `latest` folgt dem Stand von `main`) | alle Korrekturen | Erscheinen der nächsten Version |
| 0.12 (Pflegeversion 4. Quartal 2026) | Sicherheitskorrekturen ab „mittel“, kritische Fehler | 17.05.2027 |
| 0.11 (Übergang, bisherige Regel) | Sicherheitskorrekturen ab „mittel“, kritische Fehler | 03.05.2027 |
| übrige und ältere Releases | nein | – |
| Entwicklungs- und Produktionsstände (Image-Tags `dev-<commit>`, `main-<commit>`) | keine Zusage | – |

Die nächste Pflegeversion erscheint am 12.01.2027 (0.16, unterstützt bis 12.07.2027). Diese Tabelle
ist die maßgebliche Liste der Pflegelinien; das Werkzeug zur Rückportierung liest sie
(`scripts/rueckportieren.py`). Release-Takt, Supportzeiträume und Fristen nach Schweregrad stehen
in der [Release- und Support-Politik](docs/RELEASE_POLITIK.md).

## Sicherheitslücke melden

**Bitte nicht über öffentliche Issues oder Diskussionen melden.**

Zwei Wege stehen offen:

1. **E-Mail** an **security@mandari.de**, Betreff `[SECURITY] <kurze Beschreibung>`.
   Auf Wunsch verschlüsselt; den öffentlichen Schlüssel finden Sie unter
   <https://mandari.de/.well-known/security.txt>.
2. **GitHub Security Advisory**: [privat melden](https://github.com/mandariOSS/mandari/security/advisories/new)

Hilfreich sind: betroffene Version oder Commit, Beschreibung, Schritte zur Reproduktion,
mögliche Auswirkung und, falls vorhanden, ein Vorschlag zur Behebung.

**Wird die Lücke bereits ausgenutzt?** Dann ergänzen Sie den Betreff um `[AKTIV AUSGENUTZT]` und
schreiben Sie, woran Sie das erkennen. Solche Meldungen bearbeiten wir vorrangig, denn für aktiv
ausgenutzte Schwachstellen gelten gesetzliche Meldefristen (siehe
[Meldepflichten nach dem Cyber Resilience Act](#meldepflichten-nach-dem-cyber-resilience-act)).

### Was Sie von uns erwarten können

| Zeitpunkt | Reaktion |
|-----------|----------|
| 3 Werktage | Eingangsbestätigung |
| 7 Tage | Erste Einschätzung mit Schweregrad (CVSS) und geplantem Vorgehen |
| spätestens alle zwei Wochen | Zwischenstand bis zur Behebung |
| nach der Behebung | Nennung in den Release Notes, sofern gewünscht |

Wie schnell wir eine Lücke schließen, richtet sich nach dem Schweregrad (siehe
[Fristen](#fristen)). Das Advisory erscheint mit der Korrektur. Technische Einzelheiten zum
Angriffsweg veröffentlichen wir spätestens 90 Tage danach; bei aktiv ausgenutzten Lücken erst nach
dem Abschlussbericht an die Behörden (siehe
[Meldepflichten](#meldepflichten-nach-dem-cyber-resilience-act)).

### Was wir von Ihnen erwarten

- Zeit zur Behebung, bevor Sie öffentlich darüber berichten
- Keine Zerstörung oder Veränderung fremder Daten, keine Beeinträchtigung des Betriebs
- Kein Zugriff auf Daten anderer Nutzerinnen und Nutzer über das zum Nachweis Nötige hinaus
- Kein Social Engineering, kein Spam, keine Überlastungsangriffe

Wer sich daran hält, muss keine rechtlichen Schritte befürchten. Ein Programm mit Geldprämien
gibt es nicht; wir nennen Meldende auf Wunsch in den Release Notes.

## Umgang mit gemeldeten Lücken

Was nach einer Meldung oder einem eigenen Fund bei uns passiert, von der Aufnahme bis zur
Veröffentlichung. Gilt für mandari und die zugehörigen Repositories.

### Reihenfolge: erst der Fix, dann die Veröffentlichung

Ein CVE ist eine öffentliche Bekanntgabe. Wird er vor dem Patch angefordert und das Advisory
veröffentlicht, steht eine ungepatchte Lücke in jeder Schwachstellendatenbank; mandari ist Open Source
und selbst betreibbar, es lesen also auch Dritte mit. Die Reihenfolge lautet deshalb immer:

1. Privates Advisory als Entwurf anlegen (nicht als öffentliches Issue)
2. Bewerten und CVSS-Vektor bestimmen
3. Beheben, Regressionstest schreiben
4. Ausliefern: erst `dev`, dann `main`, dann Produktion, Selbstbetreiber und die Pflegeversionen
   ([Rückportierung](docs/WEITERENTWICKLUNG.md#9-rückportierung-in-pflegelinien))
5. Advisory um die behobenen Versionen ergänzen
6. CVE anfordern (wo vorgesehen) und Advisory veröffentlichen

Schritt 6 erst, wenn Schritt 4 abgeschlossen ist.

### Wo was hingehört

Das Repository ist öffentlich. Daraus folgt eine klare Trennung:

| Ort | Inhalt |
|---|---|
| **Privates Advisory** (GitHub Security Advisory, Entwurf) | Angriffsweg, betroffene Stellen, Ausnutzbarkeit, CVSS, Rückportierung in die Pflegeversionen |
| **Pull Request und Commit** | Die Korrektur, neutral formuliert, ohne Anleitung und ohne Schweregrad |
| **Öffentliches Issue** | keines für die Lücke selbst |

Ein offenes Issue, das beschreibt, wie man eine noch bestehende Lücke ausnutzt, ist eine Anleitung.
Das vermeiden wir auch bei geringer Schwere.

### CVSS bestimmen

Wir bewerten nach **CVSS v3.1**, weil dieser Vektor in Vergabeunterlagen und
Schwachstellendatenbanken überall verstanden wird. Der Rechner des FIRST ist
maßgeblich: <https://www.first.org/cvss/calculator/3-1>

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

**Beispiel GHSA-6p5c-wv4v-8g24:** Anhänge ohne Dateityp-Prüfung, eingebettet ausgeliefert.

```
CVSS:3.1/AV:N/AC:L/PR:L/UI:R/S:C/C:L/I:L/A:N   →   5.4 (Medium)
```

- `AV:N`: über die Weboberfläche erreichbar
- `AC:L`: es braucht nur einen Upload, keine besonderen Umstände
- `PR:L`: ein gewöhnliches Konto mit Bearbeitungsrecht genügt, **kein** Administrationszugang
- `UI:R`: das Opfer muss die Datei öffnen
- `S:C`: der Angreifer handelt anschließend im Rechtebereich des Opfers
- `C:L` / `I:L`: Zugriff auf das, was das Opfer sieht und darf; keine vollständige Übernahme
- `A:N`: die Verfügbarkeit ist nicht betroffen

Ohne die Anmeldepflicht (`PR:N`) wären es 6.1 gewesen. Die Einordnung als *Medium* statt *High*
trägt also die Tatsache, dass ein gültiges Konto in derselben Organisation nötig ist, nicht eine
Verharmlosung.

### CVE anfordern

GitHub ist eine CVE Numbering Authority; die Anforderung läuft im Advisory über **Request CVE**.
Die Prüfung dauert bis zu drei Arbeitstage.

- **Immer** bei Schweregrad kritisch und hoch.
- **Bei mittel nach Einschätzung**, wenn Selbstbetreiber betroffen sind und handeln müssen.
- **Nie** bei Schwächen, die nur unseren eigenen Betrieb betreffen (Serverkonfiguration,
  Zugangsdaten, Infrastruktur), und bei Härtungen ohne konkrete Ausnutzbarkeit.

Im Advisory gehören immer ausgefüllt: betroffene Versionen, **behobene Versionen** (auch die
Patch-Versionen der Pflegeversionen), CVSS-Vektor, CWE und eine Beschreibung, aus der eine
betreibende Person ableiten kann, ob sie betroffen ist.

### Nach der Veröffentlichung

- Behobene Versionen in den Release Notes nennen
- Selbstbetreiber über den üblichen Weg informieren
- Wer die Lücke gemeldet hat, wird auf Wunsch genannt
- Prüfen, ob dieselbe Ursache anderswo im Code steckt; bei GHSA-6p5c-wv4v-8g24 war genau das der
  Fall: Die Regel stand an drei Stellen richtig und an vier gar nicht

## Fristen

Die Fristen je Schweregrad (kritisch 72 Stunden, hoch 7 Tage, mittel 30 Tage, niedrig spätestens
mit der nächsten Pflegeversion), auch für die Pflegeversionen, legt die
[Release- und Support-Politik, Abschnitt 4](docs/RELEASE_POLITIK.md#4-sicherheitskorrekturen-fristen)
fest. Sie gelten ab der Bestätigung der Meldung. Bei extern gemeldeten Lücken bestätigen wir den
Eingang innerhalb von drei Werktagen und melden spätestens nach zwei Wochen einen Zwischenstand.

## Meldepflichten nach dem Cyber Resilience Act

Der Cyber Resilience Act (Verordnung (EU) 2024/2847, CRA) verlangt seit dem **11.09.2026**
Meldungen über aktiv ausgenutzte Schwachstellen und schwerwiegende Sicherheitsvorfälle (Art. 14).
Die übrigen Pflichten gelten ab dem **11.12.2027**. Die Abschnitte oben regeln den Umgang mit der
Schwachstelle; dieser Abschnitt die Meldung an Behörden und Nutzer.

### Rolle: Hersteller oder Open-Source-Steward

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

Wir behandeln jede aktiv ausgenutzte Schwachstelle in mandari nach dem Ablauf unten, unabhängig
davon, welche Rolle im Einzelfall greift.

### Begriffe

- **Aktiv ausgenutzte Schwachstelle:** Es gibt verlässliche Belege, dass ein Angreifer sie
  ohne Erlaubnis in einem System ausgenutzt hat (Art. 3 Nr. 42). Eine gemeldete, aber nicht
  ausgenutzte Schwachstelle löst keine Meldepflicht aus.
- **Schwerwiegender Sicherheitsvorfall:** Er beeinträchtigt die Fähigkeit des Produkts,
  Verfügbarkeit, Authentizität, Integrität oder Vertraulichkeit sensibler Daten oder Funktionen
  zu schützen, oder er hat zur Ausführung von Schadcode im Produkt oder bei Nutzern geführt
  oder kann dazu führen (Art. 14 Abs. 5).

### Meldeweg und Fristen

Gemeldet wird über die einheitliche Meldeplattform der ENISA (Art. 16), gleichzeitig an das
koordinierende CSIRT des Mitgliedstaats der Hauptniederlassung (in Deutschland das BSI) und an die
ENISA. Die Fristen laufen ab Kenntnis, auch an Wochenenden und Feiertagen:

| | Frühwarnung | Meldung | Abschlussbericht |
|---|---|---|---|
| **Aktiv ausgenutzte Schwachstelle** (Art. 14 Abs. 2) | 24 Stunden | 72 Stunden | 14 Tage, nachdem eine Korrektur oder Abhilfe verfügbar ist |
| **Schwerwiegender Sicherheitsvorfall** (Art. 14 Abs. 4) | 24 Stunden | 72 Stunden | ein Monat nach der Meldung |

Die Frühwarnung nennt, soweit bekannt, die Mitgliedstaaten, in denen das Produkt bereitgestellt
ist. Die Meldung beschreibt die Art der Schwachstelle, die ergriffenen Maßnahmen und was Nutzer
selbst tun können. Der Abschlussbericht enthält Schweregrad und Auswirkung, Angaben zum Angreifer,
soweit bekannt, und die Korrektur.

### Ablauf

1. Eingang festhalten, Zeitpunkt der Kenntnis in UTC notieren.
2. Innerhalb weniger Stunden einstufen: aktiv ausgenutzt? schwerwiegender Vorfall? Welche
   Versionen und Editionen? Sind personenbezogene Daten betroffen?
3. Privates Advisory anlegen.
4. Meldeentscheidung mit Begründung festhalten.
5. Frühwarnung vor Ablauf von 24 Stunden, Meldung vor Ablauf von 72 Stunden absenden.
6. Betroffene Nutzer informieren (Art. 14 Abs. 8): mit Gegenmaßnahmen, ohne Angriffsweg; über
   den Sicherheitshinweis (GitHub Security Advisory), die Release Notes und bei gelieferten
   Installationen direkt.
7. Beheben und ausliefern nach den [Fristen](#fristen).
8. Abschlussbericht fristgerecht absenden, danach Advisory mit allen Einzelheiten veröffentlichen
   und CVE anfordern.
9. Liegt die Ursache in einer Fremdkomponente, deren Betreuer informieren.

Die Meldung an CSIRT und ENISA ist keine Veröffentlichung. Sie ist vertraulich und darf dem Fix
vorausgehen. Öffentlich gilt weiter: erst der Fix, dann die Veröffentlichung. Die Nutzerinformation
sagt, was zu tun ist, nicht, wie der Angriff funktioniert.

Betrifft ein Vorfall personenbezogene Daten in einer von uns betriebenen Instanz, gelten
zusätzlich die Meldepflichten der DSGVO (Art. 33) mit eigenen Fristen.

### Ab dem 11.12.2027

Dann gelten die übrigen Pflichten des CRA, darunter die Anforderungen an die
Schwachstellenbehandlung aus Anhang I Teil II: Stückliste der Komponenten, Richtlinie zur
koordinierten Offenlegung mit Kontaktadresse, Sicherheitsupdates getrennt von Funktionsupdates und
kostenlos, ein festgelegter Supportzeitraum sowie technische Dokumentation und
Konformitätsbewertung. Vorhanden sind die Stückliste je Release
([DEPENDENCIES.md](DEPENDENCIES.md#stückliste-sbom-je-release)), diese Richtlinie und die
[Release- und Support-Politik](docs/RELEASE_POLITIK.md).

## Befristete Ausnahmen in der Abhängigkeitsprüfung

Die CI blockiert jeden Merge, sobald `pip-audit` eine bekannte Schwachstelle in unseren
Python-Abhängigkeiten meldet (jede Meldung) oder `npm audit` eine ab „high“. Gibt es für eine Meldung
noch keine korrigierte Version und läuft der betroffene Teil bei uns nachweislich nicht, tragen wir eine
befristete Ausnahme mit Begründung ein:

- Python: `mandari/pip-audit-ausnahmen.json`, geprüft von `scripts/pip_audit_pruefen.py`
- npm (nur Entwicklungswerkzeuge): `mandari/npm-audit-ausnahmen.json`, geprüft von `scripts/npm_audit_pruefen.py`

Eine Ausnahme braucht Kennung (bei Python mit Aliasen), Paket, Grund und Ablaufdatum (höchstens
wenige Wochen). Die Prüfung scheitert, wenn eine Ausnahme abläuft, bei Python auch, sobald es für die
Meldung eine korrigierte Version gibt; dann aktualisieren wir die Abhängigkeit und entfernen die
Ausnahme.

## Was mandari mitbringt

| Bereich | Maßnahme |
|---------|----------|
| Verschlüsselung | AES-256-GCM für vertrauliche Felder, Schlüsselhierarchie Hauptschlüssel → Mandantenschlüssel → Feld; Verfahren und Parameter im [Kryptokonzept](docs/KRYPTOKONZEPT.md), abgeglichen mit BSI TR-02102 |
| Anmeldung | Zwei-Faktor-Pflicht für Konten mit Verwaltungsrechten, per TOTP oder Sicherheitsschlüssel (WebAuthn), Sitzungsübersicht, Ratenbegrenzung (5 Versuche je 15 Minuten), Mindestpasswortlänge 12 Zeichen nach BSI-Empfehlung ([docs/ACCOUNT_SECURITY.md](docs/ACCOUNT_SECURITY.md)) |
| Berechtigungen | Rollenbasiert mit 80 Einzelrechten in 15 Bereichen, zusätzlich individuelle Erteilung und Entzug je Mitgliedschaft |
| Mandantentrennung | Jede Abfrage ist organisationsgebunden; eine automatisierte Matrix prüft über 160 Adressen des Arbeitsbereichs gegen jede Rolle |
| Uploads | Eine gemeinsame Prüfung nach Dateityp und Größe für alle Upload-Pfade (`apps/common/uploads.py`, Profile und Grenzen in [docs/ENGINEERING_STANDARDS.md](docs/ENGINEERING_STANDARDS.md#41-uploads)); aktive Inhalte wie HTML, SVG oder Skripte werden nie angenommen; Nicht-Bild-Anhänge werden als Download ausgeliefert, nicht eingebettet; ein CI-Gate meldet neue Upload-Stellen ohne Prüfung |
| Transport | HSTS, sichere Cookies, `SECURE_PROXY_SSL_HEADER`, Referrer-Policy; Zertifikate automatisch |
| Content-Security-Policy | Aktiv im Berichtsmodus (`SECURE_CSP_REPORT_ONLY`) mit Nonce an allen eingebetteten Skripten |
| Protokollierung | Vollständiges Audit-Log im Verwaltungs-RIS mit Hash-Kette, strukturierte Logs mit Anfrage-Kennung, keine personenbezogenen Inhalte in Log-Zeilen; Container-Logs 90 Tage im systemd-Journal, Zugriffslogs 14 Tage, beides in der täglichen Sicherung ([docs/PROTOKOLLIERUNG.md](docs/PROTOKOLLIERUNG.md)) |
| Lieferkette | Stückliste (SBOM, CycloneDX) für Anwendung, Ingestor und Frontend als Anhang jedes Releases ([DEPENDENCIES.md](DEPENDENCIES.md#stückliste-sbom-je-release)). Alle Abhängigkeiten in Lockfiles; `pip-audit` (Django-Anwendung **und** Ingestor) blockiert jede Meldung, `npm audit` jede ab „high“; Dependabot, REUSE-Lizenzinventar |
| Prüfungen | Über 13.000 automatisierte Tests je Änderung, darunter Sicherheitsmatrizen für Mandantentrennung, Gastzugänge und die Trennung öffentlicher von nicht-öffentlichen Daten |

## Für Betreiber

- Die Datei `.env` beziehungsweise das Kubernetes-Secret enthält den
  Verschlüsselungsschlüssel. **Sichern Sie sie getrennt vom Server.** Ohne diesen Schlüssel
  sind verschlüsselte Inhalte unwiederbringlich unlesbar.
- `DEBUG` bleibt in Produktion aus. `manage.py check --deploy` läuft in der CI blockierend.
- Die Provisioning-Schnittstelle ist ohne `PROVISIONING_API_KEY` (v1) bzw. `PROVISIONING_V2_API_KEY`
  (v2) vollständig abgeschaltet. v2 lässt sich zusätzlich mit `PROVISIONING_V2_ALLOWED_NETWORKS` auf
  interne Netze beschränken; die Route gehört nicht ins offene Netz
  ([docs/SCHNITTSTELLE.md](docs/SCHNITTSTELLE.md#provisioning-api)).
- Elasticsearch und PostgreSQL gehören nicht ins offene Netz; im Compose-Stack sind sie nur
  intern erreichbar, in Kubernetes hilft `networkPolicy.enabled=true`.
- Sicherheitsrelevante Einstellungen und Maßnahmen sind unter
  <https://docs.mandari.de/datenschutz/tom/> beschrieben.

## Dank

Wir danken allen, die Schwachstellen verantwortungsvoll melden.

*Bislang keine Einträge.*
