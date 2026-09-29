# Sicherheitsrichtlinie

## Unterstützte Versionen

Sicherheitskorrekturen erscheinen für den aktuellen Stand. Wer selbst betreibt, sollte
Aktualisierungen zeitnah einspielen (`./update.sh` beziehungsweise `helm upgrade`).

| Version | Unterstützt |
|---------|-------------|
| `latest` (aktueller Release) | ja |
| `beta` | nur Vorschau, keine Zusage |
| ältere Releases | nein |

Supportzeiträume je Version, Release-Kadenz und die Fristen für Sicherheitskorrekturen nach
Schweregrad stehen in der [Release- und Support-Politik](docs/RELEASE_POLITIK.md).

## Sicherheitslücke melden

**Bitte nicht über öffentliche Issues oder Diskussionen melden.**

Zwei Wege stehen offen:

1. **E-Mail** an **security@mandari.de**, Betreff `[SECURITY] <kurze Beschreibung>`.
   Auf Wunsch verschlüsselt — den öffentlichen Schlüssel finden Sie unter
   <https://mandari.de/.well-known/security.txt>.
2. **GitHub Security Advisory**: [privat melden](https://github.com/mandariOSS/mandari/security/advisories/new)

Hilfreich sind: betroffene Version oder Commit, Beschreibung, Schritte zur Reproduktion,
mögliche Auswirkung und — falls vorhanden — ein Vorschlag zur Behebung.

**Wird die Lücke bereits ausgenutzt?** Dann ergänzen Sie den Betreff um `[AKTIV AUSGENUTZT]` und
schreiben Sie, woran Sie das erkennen. Solche Meldungen bearbeiten wir vorrangig, denn für aktiv
ausgenutzte Schwachstellen gelten gesetzliche Meldefristen (siehe
[Meldepflichten nach dem Cyber Resilience Act](#meldepflichten-nach-dem-cyber-resilience-act)).

### Was Sie von uns erwarten können

| Zeitpunkt | Reaktion |
|-----------|----------|
| 48 Stunden | Eingangsbestätigung |
| 7 Tage | Erste Einschätzung mit Schweregrad (CVSS) und geplantem Vorgehen |
| laufend | Zwischenstände bis zur Behebung |
| nach der Behebung | Nennung in den Release Notes, sofern gewünscht |

Wir bemühen uns, kritische Lücken innerhalb von 14 Tagen zu schließen, schwere innerhalb von
30 Tagen. Nach der Veröffentlichung eines Fixes bleibt eine Frist von 90 Tagen bis zur
vollständigen Offenlegung — kürzer, wenn eine Lücke bereits ausgenutzt wird.

### Was wir von Ihnen erwarten

- Zeit zur Behebung, bevor Sie öffentlich darüber berichten
- Keine Zerstörung oder Veränderung fremder Daten, keine Beeinträchtigung des Betriebs
- Kein Zugriff auf Daten anderer Nutzerinnen und Nutzer über das zum Nachweis Nötige hinaus
- Kein Social Engineering, kein Spam, keine Überlastungsangriffe

Wer sich daran hält, muss keine rechtlichen Schritte befürchten. Ein Programm mit Geldprämien
gibt es nicht; wir nennen Meldende auf Wunsch in den Release Notes.

## Umgang mit gemeldeten Lücken

Wie wir eine Meldung behandeln — Bewertung nach CVSS v3.1, Behebung, CVE-Vergabe
und Fristen je Schweregrad — steht in
[docs/SICHERHEITSMELDUNGEN.md](docs/SICHERHEITSMELDUNGEN.md). Kurz: erst der Fix,
dann die Veröffentlichung. Wir bestätigen den Eingang innerhalb von drei
Werktagen und melden spätestens nach zwei Wochen einen Zwischenstand.

## Meldepflichten nach dem Cyber Resilience Act

Seit dem 11.09.2026 müssen Hersteller von Produkten mit digitalen Elementen aktiv ausgenutzte
Schwachstellen und schwerwiegende Sicherheitsvorfälle melden (Art. 14 der Verordnung (EU) 2024/2847,
„Cyber Resilience Act“). Gemeldet wird über die einheitliche Meldeplattform der ENISA, gleichzeitig an
das koordinierende CSIRT (in Deutschland das BSI) und an die ENISA:

| Frist ab Kenntnis | Meldung |
|-------------------|---------|
| 24 Stunden | Frühwarnung |
| 72 Stunden | Meldung mit ersten Angaben und Gegenmaßnahmen |
| 14 Tage nach verfügbarer Korrektur (Vorfälle: ein Monat nach der Meldung) | Abschlussbericht |

Betroffene Betreiber informieren wir über den Sicherheitshinweis (GitHub Security Advisory), die
Release Notes und bei gelieferten Installationen direkt, jeweils mit den Maßnahmen, die sie selbst
ergreifen können. Welche Rolle mandari nach dem CRA hat und wie wir vorgehen, beschreibt
[docs/SICHERHEITSMELDUNGEN.md](docs/SICHERHEITSMELDUNGEN.md#7-meldepflichten-nach-dem-cyber-resilience-act).
Wer mandari unverändert selbst betreibt, hat als Nutzer keine Meldepflicht nach dem CRA; wer mandari
verändert und unter eigenem Namen anbietet, wird selbst Hersteller.

## Was mandari mitbringt

| Bereich | Maßnahme |
|---------|----------|
| Verschlüsselung | AES-256-GCM für vertrauliche Felder, Schlüsselhierarchie Hauptschlüssel → Mandantenschlüssel → Feld; Verfahren und Parameter im [Kryptokonzept](docs/KRYPTOKONZEPT.md), abgeglichen mit BSI TR-02102 |
| Anmeldung | Zwei-Faktor per TOTP und Sicherheitsschlüssel, Sitzungsübersicht, Ratenbegrenzung (5 Versuche je 15 Minuten), Mindestpasswortlänge 12 Zeichen nach BSI-Empfehlung |
| Berechtigungen | Rollenbasiert mit über 50 Einzelrechten, zusätzlich individuelle Erteilung und Entzug je Mitgliedschaft |
| Mandantentrennung | Jede Abfrage ist organisationsgebunden; eine automatisierte Matrix prüft über 160 Adressen des Arbeitsbereichs gegen jede Rolle |
| Uploads | Eine gemeinsame Prüfung nach Dateityp und Größe für alle Upload-Pfade (`apps/common/uploads.py`, Profile und Limits in [docs/UPLOADS.md](docs/UPLOADS.md)); aktive Inhalte wie HTML, SVG oder Skripte werden nie angenommen; Nicht-Bild-Anhänge werden als Download ausgeliefert, nicht eingebettet; ein CI-Gate meldet neue Upload-Stellen ohne Prüfung |
| Transport | HSTS, sichere Cookies, `SECURE_PROXY_SSL_HEADER`, Referrer-Policy; Zertifikate automatisch |
| Content-Security-Policy | Aktiv im Berichtsmodus mit Nonce an allen eingebetteten Skripten; die Umstellung auf Erzwingen ist in Arbeit |
| Protokollierung | Vollständiges Audit-Log im Verwaltungs-RIS, strukturierte Logs mit Anfrage-Kennung, keine personenbezogenen Inhalte in Log-Zeilen; Container-Logs 90 Tage im systemd-Journal, Zugriffslogs 14 Tage, beides in der täglichen Sicherung ([docs/PROTOKOLLE.md](docs/PROTOKOLLE.md)) |
| Lieferkette | Stückliste (SBOM, CycloneDX) für Anwendung, Ingestor und Frontend als Anhang jedes Releases; Nachweis-Fahrplan in [docs/SICHERHEITSNACHWEISE.md](docs/SICHERHEITSNACHWEISE.md). Alle Abhängigkeiten in Lockfiles; `pip-audit` (Django-Anwendung **und** Ingestor) sowie `npm audit` blockieren jede Änderung, CycloneDX-Stückliste je Release, Dependabot, REUSE-Lizenzinventar |
| Prüfungen | Rund 1.500 automatisierte Tests je Änderung, darunter Sicherheitsmatrizen für Mandantentrennung, Gastzugänge und die Trennung öffentlicher von nicht-öffentlichen Daten |

## Für Betreiber

- Die Datei `.env` beziehungsweise das Kubernetes-Secret enthält den
  Verschlüsselungsschlüssel. **Sichern Sie sie getrennt vom Server.** Ohne diesen Schlüssel
  sind verschlüsselte Inhalte unwiederbringlich unlesbar.
- `DEBUG` bleibt in Produktion aus. `manage.py check --deploy` läuft in der CI blockierend.
- Die Provisioning-Schnittstelle ist ohne `PROVISIONING_API_KEY` vollständig abgeschaltet.
- Elasticsearch und PostgreSQL gehören nicht ins offene Netz; im Compose-Stack sind sie nur
  intern erreichbar, in Kubernetes hilft `networkPolicy.enabled=true`.
- Sicherheitsrelevante Einstellungen und Maßnahmen sind unter
  <https://docs.mandari.de/datenschutz/tom/> beschrieben.

## Dank

Wir danken allen, die Schwachstellen verantwortungsvoll melden.

*Bislang keine Einträge.*
