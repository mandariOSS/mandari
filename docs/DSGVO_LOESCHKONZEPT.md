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
| Nicht-öffentliche Protokollteile | `SessionProtocol.content_encrypted` | AES-256-GCM |
| Interne Sitzungsnotizen | `SessionMeeting.internal_notes_encrypted` | AES-256-GCM |
| Audit-Log | `SessionAuditLog` | unveränderbar (revisionssicher), keine Klartext-Werte verschlüsselter Felder |

## 2. Aufbewahrungsfristen

Die Fristen werden **je Mandant** in den Einstellungen gepflegt
(*Einstellungen → Datenschutz*), Angabe in Jahren, `0` = Frist deaktiviert:

| Datenart | Einstellung | Wirkung nach Fristablauf |
|---|---|---|
| Kontakt-/Bankdaten ausgeschiedener Mandatsträger | `persons_years` (ab Mandatsende) | E-Mail, Telefon, Adresse und Bankdaten werden entfernt. **Der Name bleibt erhalten**, damit historische Beschlüsse und Protokolle nachvollziehbar bleiben. |
| Nicht-öffentliche Inhalte | `np_content_years` (ab Sitzungsdatum) | NÖ-Protokollteil und interne Notizen werden geleert. Der öffentliche Protokollteil bleibt unberührt. |
| Audit-Log | `audit_years` (ab Eintragsdatum) | Einträge werden gelöscht. |

Empfehlungswerte (unverbindlich): Kontakt-/Bankdaten 2 Jahre nach
Mandatsende; Audit-Log 5–10 Jahre; NÖ-Inhalte gemäß örtlicher
Archivsatzung (häufig dauerhafte Aufbewahrung — dann Frist deaktiviert
lassen und dem Kommunalarchiv anbieten).

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

### Nachweisbarkeit

Jeder Lauf schreibt Audit-Einträge:

- je anonymisierter Person ein Eintrag mit den geleerten Datenarten
  (niemals die Werte selbst),
- je bereinigter Sitzung ein Eintrag,
- ein Abschluss-Eintrag mit Zählern und den angewandten Fristen.

Damit kann die Verwaltung die Durchführung gegenüber der Aufsichtsbehörde
belegen (Rechenschaftspflicht, Art. 5 Abs. 2 DSGVO).

## 4. Betroffenenauskunft (Art. 15 DSGVO)

*Einstellungen → Datenschutz → Betroffenenauskunft* exportiert alle zu
einer Person gespeicherten Daten als JSON-Datei (Stammdaten,
Gremienmitgliedschaften, Anwesenheiten, Sitzungsgelder, Vorlagen als
Verfasser/in). Bankdaten werden nur entschlüsselt, wenn die abrufende
Person zusätzlich `manage_allowances` besitzt. Jeder Export wird auditiert.

## 5. Löschung ganzer Mandanten

Beim Löschen eines `SessionTenant` (Vertragsende) werden alle abhängigen
Daten kaskadiert gelöscht, einschließlich Audit-Log und tenant-spezifischem
Verschlüsselungsschlüssel (Crypto-Shredding: ohne Schlüssel sind etwaige
Backups der verschlüsselten Felder nicht mehr lesbar).

## 6. Zugehörige Dokumente

- [AVV-Muster](DSGVO_AVV_MUSTER.md)
- [Technische und organisatorische Maßnahmen (TOM)](DSGVO_TOM.md)
