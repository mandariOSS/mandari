# Sicherheitsrichtlinie

## Unterstützte Versionen

Sicherheitskorrekturen erscheinen für den aktuellen Stand. Wer selbst betreibt, sollte
Aktualisierungen zeitnah einspielen (`./update.sh` beziehungsweise `helm upgrade`).

| Version | Unterstützt |
|---------|-------------|
| `latest` (aktueller Release) | ja |
| `beta` | nur Vorschau, keine Zusage |
| ältere Releases | nein |

## Sicherheitslücke melden

**Bitte nicht über öffentliche Issues oder Diskussionen melden.**

Zwei Wege stehen offen:

1. **E-Mail** an **security@mandari.de**, Betreff `[SECURITY] <kurze Beschreibung>`.
   Auf Wunsch verschlüsselt — den öffentlichen Schlüssel finden Sie unter
   <https://mandari.de/.well-known/security.txt>.
2. **GitHub Security Advisory**: [privat melden](https://github.com/mandariOSS/mandari/security/advisories/new)

Hilfreich sind: betroffene Version oder Commit, Beschreibung, Schritte zur Reproduktion,
mögliche Auswirkung und — falls vorhanden — ein Vorschlag zur Behebung.

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

## Was mandari mitbringt

| Bereich | Maßnahme |
|---------|----------|
| Verschlüsselung | AES-256-GCM für vertrauliche Felder, Schlüsselhierarchie Hauptschlüssel → Mandantenschlüssel → Feld |
| Anmeldung | Zwei-Faktor per TOTP, vertrauenswürdige Geräte, Sitzungsübersicht, Ratenbegrenzung (5 Versuche je 15 Minuten), Mindestpasswortlänge 12 Zeichen nach BSI-Empfehlung |
| Berechtigungen | Rollenbasiert mit über 50 Einzelrechten, zusätzlich individuelle Erteilung und Entzug je Mitgliedschaft |
| Mandantentrennung | Jede Abfrage ist organisationsgebunden; eine automatisierte Matrix prüft über 160 Adressen des Arbeitsbereichs gegen jede Rolle |
| Transport | HSTS, sichere Cookies, `SECURE_PROXY_SSL_HEADER`, Referrer-Policy; Zertifikate automatisch |
| Content-Security-Policy | Aktiv im Berichtsmodus mit Nonce an allen eingebetteten Skripten; die Umstellung auf Erzwingen ist in Arbeit |
| Protokollierung | Vollständiges Audit-Log im Verwaltungs-RIS, strukturierte Logs mit Anfrage-Kennung, keine personenbezogenen Inhalte in Log-Zeilen |
| Lieferkette | Alle Abhängigkeiten in Lockfiles, `pip-audit` blockierend bei jedem Build, CycloneDX-Stückliste je Release, Dependabot, REUSE-Lizenzinventar |
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
