# Löschkonzept — mandari Session RIS

Stand: Juli 2026 · Bezug: Issue #43 (DSGVO-Paket)

Dieses Löschkonzept beschreibt, welche personenbezogenen Daten das Session
RIS speichert, welche Aufbewahrungsfristen gelten und wie die Löschung bzw.
Anonymisierung technisch und nachweisbar erfolgt. Verbindlich sind stets die
örtlichen Satzungen und Aufbewahrungsvorschriften der jeweiligen Kommune —
die hier genannten Fristen sind konfigurierbare Voreinstellungen.

## 1. Datenarten und Speicherorte

| Datenart | Speicherort | Schutz |
|---|---|---|
| Stammdaten Mandatsträger (Name, Funktion) | `SessionPerson` | Zugriff nur mit Rollenberechtigung |
| Kontaktdaten (Telefon, Adresse) | `SessionPerson.*_encrypted` | AES-256-GCM, Tenant-Schlüssel |
| Bankdaten (Kontoinhaber, IBAN, BIC) | `SessionPerson.bank_*_encrypted` | AES-256-GCM, Zugriff nur mit `manage_allowances` |
| Sitzungsgeld-Positionen | `SessionAllowance` | Beträge/Status, keine Bankdaten |
| Ladungsprotokoll (Empfänger, Zustellweg, Versand, Empfangsbestätigung) | `SessionInvitationRecipient` | Zugriff nur mit `edit_meetings` |
| Rückmeldung zur Sitzung (Zu-/Absage, Zeitpunkt, Herkunft, Vertretungswunsch) | `SessionAttendance` | Zugriff nur mit Rollenberechtigung |
| Grund einer Absage | `SessionAttendance.response_reason_encrypted` | AES-256-GCM, nur in der Rückmeldeübersicht des Sitzungsdienstes sichtbar, nicht in OParl, Nachweis oder Mails an Dritte |
| Nicht-öffentliche Protokollteile | `SessionProtocol.content_encrypted` | AES-256-GCM |
| Interne Sitzungsnotizen | `SessionMeeting.internal_notes_encrypted` | AES-256-GCM |
| Audit-Log (Änderungen, Lesezugriffe auf Nichtöffentliches, Anmeldungen) | `SessionAuditLog` | unveränderbar, Hash-Kette je Mandant, keine Inhalte und keine Klartext-Werte verschlüsselter Felder; Einsicht nur mit Kontrollrechten ([Protokollierungskonzept](PROTOKOLLIERUNG.md)) |
| Sicherheitsprotokoll (Anmeldungen von Konten ohne Session-Mandant, Fehlversuche mit unbekannter Kennung) | `SecurityAuditLog` | unveränderbar, eigene Hash-Kette, Kennungen nur als HMAC, kein Passwort; nur für den Plattformbetrieb |
| Anlagen und ihre früheren Fassungen | `SessionFile`, `SessionFileVersion`, Inhalte in `SessionFileBlob` | nur über zugriffsgeprüfte Downloads, Sichtbarkeit wie die Anlage (Ö/NÖ) |
| Fassungen von Vorlagen (Texte, Angaben, Anlagen-Stand) | `SessionPaperVersion`, `SessionPaperVersionFile` | unveränderbar; Sichtbarkeit wie die Vorlage heute und zum Zeitpunkt der Fassung |

## 2. Aufbewahrungsfristen

Die Fristen werden **je Mandant** in den Einstellungen gepflegt
(*Einstellungen → Datenschutz*), Angabe in Jahren, `0` = Frist deaktiviert:

| Datenart | Einstellung | Wirkung nach Fristablauf |
|---|---|---|
| Kontakt-/Bankdaten ausgeschiedener Mandatsträger | `persons_years` (ab Mandatsende) | E-Mail, Telefon, Adresse und Bankdaten werden entfernt, ebenso Absagegründe und die in Ladungsprotokollen mitgeschriebene E-Mail-Adresse. **Der Name bleibt erhalten**, damit historische Beschlüsse, Protokolle und Ladungsnachweise nachvollziehbar bleiben. |
| Nicht-öffentliche Inhalte | `np_content_years` (ab Sitzungsdatum) | NÖ-Protokollteil und interne Notizen werden geleert. Der öffentliche Protokollteil bleibt unberührt. |
| Audit-Log | `audit_years` (ab Eintragsdatum) | Einträge werden gelöscht – vorher entsteht ein geprüftes Archivpaket (JSON, CSV, Kettenanker, `SHA256SUMS`) im Archivspeicher; gelöscht wird nur ein intaktes Anfangsstück der Hash-Kette, die danach ab dem Anker prüfbar bleibt. |

Empfehlungswerte (unverbindlich): Kontakt-/Bankdaten 2 Jahre nach
Mandatsende; Audit-Log 5–10 Jahre; NÖ-Inhalte gemäß örtlicher
Archivsatzung (häufig dauerhafte Aufbewahrung — dann Frist deaktiviert
lassen und dem Kommunalarchiv anbieten).

Archivpakete des Audit-Logs löscht mandari nicht selbst; die Kommune bietet
sie ihrem Archiv an oder legt eine eigene Frist fest. Das plattformweite
Sicherheitsprotokoll wird nach `SECURITY_AUDIT_RETENTION_DAYS` (Standard
365 Tage) mit `purge_security_audit_log` nach demselben Verfahren
archiviert und gelöscht.

### Konten ohne Zuordnung (plattformweit)

Unabhängig von den Mandantenfristen räumt die Plattform Konten auf, die nirgends mehr
gebraucht werden (Art. 5 Abs. 1 lit. e DSGVO, Speicherbegrenzung). Ein Konto wird nur
gelöscht, wenn es **keine** Mitgliedschaft (Organisation oder Session-Mandant), keine
gültige Einladung, keine Gruppe und kein weiteres verknüpftes Objekt hat und zusätzlich
eine dieser Fristen gerissen ist:

| Fall | Frist | Grundlage |
|---|---|---|
| E-Mail-Adresse nie bestätigt | 9 Tage nach Registrierung | Bestätigungslink 48 h + 7 Tage Kulanz |
| Registrierungsanfrage abgelehnt | 30 Tage nach Ablehnung | `User.registration_rejected_at`; die Ablehnungsmail nennt die Frist |
| Bestätigt, aber nie zugeordnet (Altbestand ohne Ablehnungsstempel) | 30 Tage ohne Anmeldung | Konto älter als 30 Tage und seit 30 Tagen kein Login |

Mitarbeiter- und Superuser-Konten sind ausgenommen. Der Lauf löscht mit dem Konto nur
dessen eigene Artefakte (2FA-Gerät, vertraute Geräte, Sitzungen, Tokens,
Sicherheitsbenachrichtigungen); jede andere Beziehung schützt das Konto. Die Ausgabe
nennt nur Zahlen, keine Adressen.

## 3. Durchführung des Löschlaufs

Zwei gleichwertige Wege:

1. **UI**: *Einstellungen → Datenschutz → Löschlauf ausführen* (mit
   Probelauf-Option, Berechtigung `manage_settings`).
2. **Kommandozeile** (z. B. Cron, monatlich):

   ```bash
   python manage.py session_privacy_purge              # alle aktiven Mandanten
   python manage.py session_privacy_purge --tenant stadt-musterstadt
   python manage.py session_privacy_purge --dry-run    # nur zählen
   ```

   Konten ohne Zuordnung (plattformweit, täglich per Cron, siehe DEPLOYMENT.md):

   ```bash
   python manage.py cleanup_orphaned_accounts           # löschen
   python manage.py cleanup_orphaned_accounts --dry-run # nur zählen
   ```

### Nachweisbarkeit

Jeder Lauf schreibt Audit-Einträge:

- je anonymisierter Person ein Eintrag mit den geleerten Datenarten
  (niemals die Werte selbst),
- je bereinigter Sitzung ein Eintrag,
- ein Abschluss-Eintrag mit Zählern und den angewandten Fristen,
- bei gelöschten Audit-Einträgen ein Eintrag „Protokoll archiviert“ mit
  Paketname, Prüfsumme, Nummernbereich und Kettenanker.

Damit kann die Verwaltung die Durchführung gegenüber der Aufsichtsbehörde
belegen (Rechenschaftspflicht, Art. 5 Abs. 2 DSGVO).

## 4. Betroffenenauskunft (Art. 15 DSGVO)

*Einstellungen → Datenschutz → Betroffenenauskunft* exportiert alle zu
einer Person gespeicherten Daten als JSON-Datei (Stammdaten,
Gremienmitgliedschaften, Anwesenheiten mit Rückmeldungen und Absagegründen,
Ladungen mit Zustellweg und Empfangsbestätigung, Sitzungsgelder, Vorlagen als
Verfasser/in). Bankdaten werden nur entschlüsselt, wenn die abrufende
Person zusätzlich `manage_allowances` besitzt. Jeder Export wird auditiert.

## 5. Anlagen und Fassungen (Issue #226)

Vorlagen und Anlagen haben Fassungen: Bei jedem Workflow-Schritt und jedem
Beratungsergebnis sichert mandari den Stand einer Vorlage, beim Ersetzen einer
Anlage bleibt die bisherige Datei abrufbar. Jeder Dateiinhalt liegt je Mandant
nur einmal im Speicher (SHA-256); Anlagen und Fassungen verweisen darauf.

| Vorgang | Wirkung |
|---|---|
| Anlage ersetzen | Neue Fassung der Anlage; die bisherige bleibt im Verlauf, sichtbar wie die Anlage. |
| Anlage löschen | Die Anlage und ihr Verlauf verschwinden. Inhalte, die in einer gesicherten Fassung der Vorlage stecken, bleiben dort erhalten – sichtbar nur noch mit dem Recht für nichtöffentliche Vorlagen. Alles andere wird nach dem Löschen aus dem Speicher entfernt. |
| Inhalt endgültig löschen (Datenschutz) | Mit dem Einstellungsrecht und einem Grund entfernt die Verwaltung einen Inhalt aus dem Speicher, auch aus allen Fassungen. Prüfsumme und Größe bleiben als Nachweis, die Fassung zeigt „Inhalt gelöscht“; der Grund steht im Audit-Log. |
| Beschlossene Fassung | Ihr Inhalt lässt sich nicht löschen: Sie ist der amtliche Stand, den das Gremium beschlossen hat (Aufbewahrung nach Archivrecht, Art. 17 Abs. 3 lit. b DSGVO). |
| Vorlage oder Mandant löschen | Alle Fassungen und nicht mehr benötigten Inhalte werden mitgelöscht. |

Der aktuelle Inhalt einer Anlage lässt sich nicht über die Datenschutz-Löschung
entfernen – dafür die Anlage ersetzen oder löschen.

## 6. Löschung ganzer Mandanten

Beim Löschen eines `SessionTenant` (Vertragsende) werden alle abhängigen
Daten kaskadiert gelöscht, einschließlich Audit-Log und tenant-spezifischem
Verschlüsselungsschlüssel (Crypto-Shredding: ohne Schlüssel sind etwaige
Backups der verschlüsselten Felder nicht mehr lesbar).

## 7. Zugehörige Dokumente

- [AVV-Muster](DSGVO_AVV_MUSTER.md)
- [Technische und organisatorische Maßnahmen (TOM)](DSGVO_TOM.md)
- [Protokollierungskonzept](PROTOKOLLIERUNG.md)
