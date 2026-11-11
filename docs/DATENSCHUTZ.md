# Datenschutz: AVV-Muster, TOM und Löschkonzept

Bezug: Art. 28 und Art. 32 DSGVO, Issue #43. Die öffentliche Fassung steht unter
<https://docs.mandari.de/datenschutz/>.

Diese Seite fasst drei zusammengehörige Unterlagen zusammen: das Muster für den
Auftragsverarbeitungsvertrag (Abschnitt 1), die technischen und organisatorischen Maßnahmen
als dessen Anlage 1 (Abschnitt 2) und das Löschkonzept als Anlage 2 (Abschnitt 3). Was
protokolliert wird und wie lange, steht im [Protokollierungskonzept](PROTOKOLLIERUNG.md).

> **Hinweis:** Die Unterlagen sind eine Arbeitsgrundlage für den Vertrag zwischen einer
> Kommune (Verantwortliche) und dem Betreiber der mandari-Plattform (Auftragsverarbeiter).
> Sie ersetzen keine Rechtsberatung und sind vor Verwendung durch die Vergabe- bzw.
> Rechtsstelle der Kommune zu prüfen und anzupassen. Verbindlich sind stets die örtlichen
> Satzungen und Aufbewahrungsvorschriften der jeweiligen Kommune; die genannten Fristen sind
> konfigurierbare Voreinstellungen.

## 1. Muster: Auftragsverarbeitungsvertrag (AVV) für mandari Session

### 1.1 Gegenstand und Dauer der Verarbeitung

Der Auftragsverarbeiter betreibt für die Verantwortliche das Ratsinformationssystem
**mandari Session** (Sitzungsdienst, Gremienverwaltung, Beschlussdokumentation,
Sitzungsgeld-Abrechnung, optionale Veröffentlichung im Bürgerportal). Die Dauer entspricht der
Laufzeit des Hauptvertrags.

### 1.2 Art und Zweck der Verarbeitung

Hosting, Speicherung, Bereitstellung und Pflege der von der Verantwortlichen eingegebenen
Daten; Versand von E-Mails im Auftrag (Einladungen, Benachrichtigungen); Erstellung von
Exporten (PDF, CSV, SEPA-Dateien) auf Veranlassung der Verantwortlichen.

### 1.3 Kategorien betroffener Personen

- Mandatsträgerinnen und Mandatsträger, sachkundige Bürger/innen
- Beschäftigte der Verwaltung (Sitzungsdienst)
- Verfasser/innen von Vorlagen und Anträgen

### 1.4 Kategorien personenbezogener Daten

- Stammdaten (Name, Titel, Funktion, Gremienzugehörigkeit, Wahlperiode)
- Kontaktdaten (E-Mail, Telefon, Adresse; verschlüsselt gespeichert)
- Bankdaten für Entschädigungen (Kontoinhaber, IBAN, BIC; verschlüsselt, zugriffsbeschränkt)
- Anwesenheits- und Abstimmungsdaten, Protokolle (ggf. nicht-öffentlich)
- Nutzungs- und Protokolldaten (Audit-Log, IP-Adressen)

### 1.5 Pflichten des Auftragsverarbeiters

1. Verarbeitung ausschließlich auf dokumentierte Weisung der Verantwortlichen (Konfiguration
   in der Anwendung gilt als Weisung).
2. Vertraulichkeitsverpflichtung aller mit der Verarbeitung befassten Personen.
3. Umsetzung der technischen und organisatorischen Maßnahmen gemäß **Anlage 1**
   (Abschnitt 2).
4. Unterstützung der Verantwortlichen bei Betroffenenrechten (die Anwendung stellt hierfür die
   Betroffenenauskunft und den Anonymisierungs- und Löschlauf bereit, Abschnitt 3).
5. Meldung von Verletzungen des Schutzes personenbezogener Daten ohne unangemessene
   Verzögerung.
6. Löschung oder Rückgabe aller Daten nach Vertragsende (Export über dokumentierte
   Schnittstellen; anschließend Löschung des Mandanten einschließlich Crypto-Shredding des
   Mandantenschlüssels).
7. Bereitstellung aller für den Nachweis der Pflichten erforderlichen Informationen;
   Ermöglichung und Duldung von Überprüfungen.

### 1.6 Unterauftragsverhältnisse

Eingesetzte Unterauftragnehmer (z. B. Rechenzentrum/Hosting) sind in einer Anlage aufzuführen;
Änderungen werden der Verantwortlichen vorab mitgeteilt (Widerspruchsrecht). Eine
Verarbeitung außerhalb der EU bzw. des EWR findet nicht statt.

### 1.7 Weisungsrecht der Verantwortlichen

Weisungen erfolgen in Textform oder unmittelbar durch Konfiguration in der Anwendung (Rollen,
Aufbewahrungsfristen, Veröffentlichungs-Schalter).

### 1.8 Ort der Verarbeitung

Rechenzentrum in der EU (Details in der Unterauftragnehmer-Anlage). Im Betrieb durch mandari:
Hosting bei der Hetzner Online GmbH, Rechenzentrum in Deutschland; verschlüsselte Sicherungen
in Falkenstein (Deutschland) und Helsinki (Finnland, EU). Für eine eigene Instanz der
Verantwortlichen sichern wir auf Wunsch ausschließlich in Deutschland. KI-Funktionen: Anbieter
und Verarbeitungsort siehe Unterauftragnehmer-Anlage.

**Anlagen:** Anlage 1 technische und organisatorische Maßnahmen (Abschnitt 2), Anlage 2
Löschkonzept und Aufbewahrungsfristen (Abschnitt 3), Anlage 3 Unterauftragnehmer (je
Installation zu pflegen).

## 2. Technische und organisatorische Maßnahmen (TOM)

Maßnahmen der Plattform mandari (Session, Work, Insight) als Anlage 1 zum
Auftragsverarbeitungsvertrag.

### 2.1 Vertraulichkeit

**Zutritts- und Zugangskontrolle**

- Hosting in einem europäischen Rechenzentrum (Standorte im Betrieb durch mandari: 1.8);
  Zugriff auf Server nur per SSH mit Schlüssel-Authentifizierung.
- Anmeldung an der Anwendung mit E-Mail und Passwort sowie einem zweiten Faktor
  (Authenticator-App oder Sicherheitsschlüssel/Passkey nach WebAuthn). Der zweite Faktor ist
  Pflicht für Konten mit Verwaltungs-, Benutzer-, Einstellungs- oder Kontrollrechten und lässt
  sich je Organisation bzw. Mandant für alle Mitglieder einschalten
  ([Anmeldesicherheit](ACCOUNT_SECURITY.md)). Ratenbegrenzung gegen Brute-Force (5 Versuche je
  Adresse bzw. IPv6-/64-Netz und 10 je Konto in 15 Minuten), Sitzungsverwaltung je Konto.

**Zugriffskontrolle (Berechtigungskonzept)**

- Mandantenfähigkeit mit strikter Datenisolation je Kommune bzw. Organisation (alle Abfragen
  mandantengebunden).
- Feingranulares Rollen- und Rechtesystem (Session: u. a. `manage_users`, `manage_settings`,
  `manage_allowances`, `view_non_public_meetings`; Work: rund 80 Berechtigungen in 15
  Bereichen).
- Bankdaten sind zusätzlich auf die Berechtigung `manage_allowances` beschränkt;
  nicht-öffentliche Inhalte auf eigens berechtigte Rollen.

**Verschlüsselung**

- Transport: ausschließlich TLS (HTTPS, HSTS).
- Speicherung sensibler Felder: AES-256-GCM mit **mandantenspezifischen Schlüsseln**, die
  ihrerseits mit einem Hauptschlüssel verschlüsselt sind (Schlüsselhierarchie;
  Crypto-Shredding beim Löschen eines Mandanten; [Kryptokonzept](KRYPTOKONZEPT.md)).
- Verschlüsselt gespeichert werden u. a. Kontaktdaten, Bankdaten, nicht-öffentliche
  Protokollteile und interne Notizen.

### 2.2 Integrität

- Revisionssicheres Audit-Log je Mandant: alle relevanten Änderungen (Erstellen, Ändern,
  Löschen, Freigaben, Exporte, Einladungsversand) mit Nutzer, Zeitstempel, IP; Einträge sind
  unveränderbar und nicht einzeln löschbar.
- Verschlüsselte Feldinhalte erscheinen im Audit-Log niemals im Klartext.
- Zusätzlich protokolliert: Lesezugriffe auf nichtöffentliche Inhalte (nur Objekt-Referenz, je
  Mandant abschaltbar), Anmeldungen, Abmeldungen und Fehlversuche (ohne Passwort),
  Stimmabgaben, Mitzeichnungen, Entschädigungen sowie Rollen- und Rechteänderungen.
- Manipulationsschutz durch eine Hash-Kette je Mandant (SHA-256) mit Prüfwerkzeug; Export für
  Prüfinstanzen mit Prüfsumme; Archivpaket vor der fristgerechten Löschung. Einsicht und
  Export nur mit eigenen Kontrollrechten (Revision, Datenschutz) und zweitem Faktor; jeder
  Zugriff auf das Protokoll wird selbst protokolliert ([Protokollierungskonzept](PROTOKOLLIERUNG.md)).
- Vier-Augen-Prinzip bei der Sitzungsgeld-Genehmigung (Standard), je Mandant zusätzlich für
  Vorlagenfreigabe, Niederschrift und Übergabe von Beschlussauszügen; Vertretungen nur im
  Zeitraum, ohne Rechteausweitung und mit Vermerk „in Vertretung für …“ im Audit-Log.

### 2.3 Verfügbarkeit und Belastbarkeit

- Tägliche automatisierte Sicherung aller Datenbanken, Dateien und der Konfiguration, vor der
  Übertragung verschlüsselt (AES-256), in zwei räumlich getrennte Rechenzentren (Standorte im
  Betrieb durch mandari: 1.8); Aufbewahrung höchstens 30 Tage, danach automatische Löschung.
  Wöchentliche Integritätsprüfung, monatlicher automatisierter Wiederherstellungstest,
  Alarmierung bei Fehlern ([Backup-Konzept](BACKUP.md)).
- Infrastruktur als Container (reproduzierbare Deployments) mit Prüfung nach jedem Deploy und
  automatischem Rückfall auf den vorherigen Stand.
- Betriebsmonitor und Gesundheitsprüfungen für Anwendung, Worker und Abrufdienst
  ([Betriebsmonitor](MONITORING.md)).

### 2.4 Löschung und Datenminimierung

- Konfigurierbare Aufbewahrungsfristen je Datenart und Mandant; nachweisbar auditierter
  Anonymisierungs- und Löschlauf (Oberfläche und `manage.py session_privacy_purge`), Einzelheiten
  in Abschnitt 3.
- Betroffenenauskunft als strukturierter Export (Art. 15 DSGVO).
- Work, Mitglied entfernen: Persönliche Daten (private Notizen, Benachrichtigungen,
  Einstellungen, Abwesenheiten) werden gelöscht, Inhalte der Organisation bleiben ohne
  Personenbezug („Ehemaliges Mitglied“). In Anwesenheitslisten, Protokolleinträgen
  (Redner:innen, Zuständige) und Teilnahmebestätigungen der Fraktionssitzungen bleibt der Name
  erhalten. Zweck: Nachweis der Beschlussfassung und der Teilnahme; die Organisation vermerkt
  dies in ihrem Verzeichnis von Verarbeitungstätigkeiten.
- Öffentliche Schnittstellen (OParl-API, öffentliche Fraktions-API, iCal-Feeds) liefern
  ausschließlich als öffentlich gekennzeichnete Inhalte; opake Zufalls-Tokens statt
  personenbezogener URLs.
- OParl-Ausgabe eines Session-Mandanten: erst nach Freischaltung durch die Verwaltung
  erreichbar (neue Mandanten starten gesperrt); E-Mail-Adressen von Personen nur mit
  Kennzeichen, Datum und Nachweis der Einwilligung ([Session-OParl-API](SESSION_OPARL_API.md),
  mit Baustein für das Verzeichnis der Verarbeitungstätigkeiten).

### 2.5 Organisatorische Maßnahmen

- Prinzip der geringsten Rechte bei Rollenzuweisung (Standardrollen mit abgestuften Rechten).
- Einladungsbasierte Konten; Selbstregistrierung nur, wenn eine Organisation sie aktiviert,
  mit bestätigter E-Mail-Adresse, Domain-Allowlist und optionaler Freischaltung durch
  Berechtigte (keine Selbstregistrierung in Session-Mandanten).
- Quelloffener Code (AGPL-3.0-or-later): überprüfbare Sicherheitsmaßnahmen.
- Regelmäßige automatisierte Testläufe (Smoke-Suiten) inkl. Rechte- und
  Mandanten-Isolationstests vor jedem Release.

### 2.6 Weisungs- und Kontrollrechte des Auftraggebers

- Der Auftraggeber (Kommune) steuert Rollen, Fristen und Veröffentlichungen selbst in der
  Anwendung.
- Export aller Daten über dokumentierte Schnittstellen (OParl, CSV/JSON); keine
  Anbieterbindung.

## 3. Löschkonzept

Welche personenbezogenen Daten Session speichert, welche Aufbewahrungsfristen gelten und wie
die Löschung bzw. Anonymisierung technisch und nachweisbar erfolgt.

### 3.1 Datenarten und Speicherorte

| Datenart | Speicherort | Schutz |
|---|---|---|
| Stammdaten Mandatsträger (Name, Funktion) | `SessionPerson` | Zugriff nur mit Rollenberechtigung |
| Kontaktdaten (Telefon, Adresse) | `SessionPerson.*_encrypted` | AES-256-GCM, Mandantenschlüssel |
| Einwilligung zur Veröffentlichung der E-Mail-Adresse (Kennzeichen, Datum, Nachweis) | `SessionPerson.contact_publish`, `contact_consent_date`, `contact_consent_evidence` | Zugriff nur mit Rollenberechtigung; Datum und Nachweis nie in OParl oder im Bürgerportal, die E-Mail-Adresse dort nur mit Kennzeichen ([SESSION_OPARL_API.md](SESSION_OPARL_API.md)) |
| Bankdaten (Kontoinhaber, IBAN, BIC) | `SessionPerson.bank_*_encrypted` | AES-256-GCM, Zugriff nur mit `manage_allowances` |
| Sitzungsgeld-Positionen | `SessionAllowance` | Beträge/Status, keine Bankdaten |
| Ladungsprotokoll (Empfänger, Zustellweg, Versand, Empfangsbestätigung) | `SessionInvitationRecipient` | Zugriff nur mit `edit_meetings` |
| Rückmeldung zur Sitzung (Zu-/Absage, Zeitpunkt, Herkunft, Vertretungswunsch) | `SessionAttendance` | Zugriff nur mit Rollenberechtigung |
| Teilnahmeart und Störungen Zugeschalteter (Zeiten, Ursache, interner Vermerk) | `SessionAttendance.participation_mode`, `SessionAttendanceDisruption` | Zugriff nur mit Rollenberechtigung; Niederschrift nennt Zeiten und Ursache, nie den Vermerk; Anonymisierung leert den Vermerk |
| Grund einer Absage | `SessionAttendance.response_reason_encrypted` | AES-256-GCM, nur in der Rückmeldeübersicht des Sitzungsdienstes sichtbar, nicht in OParl, Nachweis oder Mails an Dritte |
| Nicht-öffentliche Protokollteile | `SessionProtocol.content_encrypted` | AES-256-GCM |
| Interne Sitzungsnotizen | `SessionMeeting.internal_notes_encrypted` | AES-256-GCM |
| Zugangsweg für zugeschaltete Mitglieder (hybride und digitale Sitzungen) | `SessionMeeting.remote_access_encrypted` | AES-256-GCM, nur in der Ladung an Gremienmitglieder, nie in öffentlichen Dokumenten oder der OParl-API |
| Audit-Log (Änderungen, Lesezugriffe auf Nichtöffentliches, Anmeldungen) | `SessionAuditLog` | unveränderbar, Hash-Kette je Mandant, keine Inhalte und keine Klartext-Werte verschlüsselter Felder; Einsicht nur mit Kontrollrechten ([Protokollierungskonzept](PROTOKOLLIERUNG.md)) |
| Sicherheitsprotokoll (Anmeldungen von Konten ohne Session-Mandant, Fehlversuche mit unbekannter Kennung) | `SecurityAuditLog` | unveränderbar, eigene Hash-Kette, Kennungen nur als HMAC, kein Passwort; nur für den Plattformbetrieb |
| Anmeldeversuche für die Drosselung (E-Mail-Adresse, IP-Adresse, Browserkennung, Ergebnis) | `LoginAttempt` | nur im Admin lesbar; gelöscht nach `LOGIN_ATTEMPT_RETENTION_DAYS` (Vorgabe 30 Tage); der Datenexport der Person enthält damit die Versuche der letzten 30 Tage |
| Anlagen und ihre früheren Fassungen | `SessionFile`, `SessionFileVersion`, Inhalte in `SessionFileBlob` | nur über zugriffsgeprüfte Downloads, Sichtbarkeit wie die Anlage (Ö/NÖ) |
| Fassungen von Vorlagen (Texte, Angaben, Anlagen-Stand) | `SessionPaperVersion`, `SessionPaperVersionFile` | unveränderbar; Sichtbarkeit wie die Vorlage heute und zum Zeitpunkt der Fassung |

### 3.2 Aufbewahrungsfristen

Die Fristen werden **je Mandant** in den Einstellungen gepflegt (*Einstellungen →
Datenschutz*), Angabe in Jahren, `0` = Frist deaktiviert:

| Datenart | Einstellung | Wirkung nach Fristablauf |
|---|---|---|
| Kontakt-/Bankdaten ausgeschiedener Mandatsträger | `persons_years` (ab Mandatsende) | E-Mail, Telefon, Adresse und Bankdaten werden entfernt, ebenso die Einwilligung zur Veröffentlichung der E-Mail-Adresse (Kennzeichen, Datum, Nachweis), Absagegründe und die in Ladungsprotokollen mitgeschriebene E-Mail-Adresse. **Der Name bleibt erhalten**, damit historische Beschlüsse, Protokolle und Ladungsnachweise nachvollziehbar bleiben. |
| Nicht-öffentliche Inhalte | `np_content_years` (ab Sitzungsdatum) | NÖ-Protokollteil und interne Notizen werden geleert. Der öffentliche Protokollteil bleibt unberührt. |
| Audit-Log | `audit_years` (ab Eintragsdatum) | Einträge werden gelöscht; vorher entsteht ein geprüftes Archivpaket (JSON, CSV, Kettenanker, `SHA256SUMS`) im Archivspeicher; gelöscht wird nur ein intaktes Anfangsstück der Hash-Kette, die danach ab dem Anker prüfbar bleibt. |

Empfehlungswerte (unverbindlich): Kontakt-/Bankdaten 2 Jahre nach Mandatsende; Audit-Log 5–10
Jahre; NÖ-Inhalte gemäß örtlicher Archivsatzung (häufig dauerhafte Aufbewahrung; dann Frist
deaktiviert lassen und dem Kommunalarchiv anbieten).

Archivpakete des Audit-Logs (`archive_years`), das plattformweite Sicherheitsprotokoll und die
Anmeldeversuche der Drosselung haben eigene Fristen; sie stehen in der Übersicht des
[Protokollierungskonzepts](PROTOKOLLIERUNG.md#übersicht-protokollarten-und-aufbewahrung) und in
dessen Abschnitt 10.

Einreichungen von Anträgen laufen über den Befehl `submission.submit` (Issue #540). Der
Idempotenzspeicher (`events_idempotency`) hält je Einreichung Idempotenzschlüssel, Hash der
Anfrage und Quittung (Eingangsnummer, Eingangszeit, Inhalts-Hash, Kennung des Antrags, Status,
Stelle und Code nicht angenommener Anlagen), keine Texte, Namen oder Adressen. Die Hashes sind
ungesalzene SHA-256-Werte über den Antrag; bei einem Antrag mit Titel, Beschlussvorschlag und
Begründung lässt sich der Inhalt daraus nicht erraten, sie bleiben aber wie der Inhalt
geschützt (nur in der Datenbank der Installation, kein Export). Der Zeitplan
`idempotenzschluessel_aufraeumen` löscht die Einträge täglich nach
`EVENTS_IDEMPOTENCY_RETENTION_DAYS` (Standard 30 Tage); von Hand
`manage.py events_idempotency_purge`. Danach gilt eine Wiederholung mit demselben Schlüssel als
neue Einreichung. Der Antrag selbst folgt den Fristen des Mandanten.

**Konten ohne Zuordnung (plattformweit)**

Unabhängig von den Mandantenfristen räumt die Plattform Konten auf, die nirgends mehr gebraucht
werden (Art. 5 Abs. 1 lit. e DSGVO, Speicherbegrenzung). Ein Konto wird nur gelöscht, wenn es
**keine** Mitgliedschaft (Organisation oder Session-Mandant), keine gültige Einladung, keine
Gruppe und kein weiteres verknüpftes Objekt hat und zusätzlich eine dieser Fristen gerissen ist:

| Fall | Frist | Grundlage |
|---|---|---|
| E-Mail-Adresse nie bestätigt | 9 Tage nach Registrierung | Bestätigungslink 48 h + 7 Tage Kulanz |
| Registrierungsanfrage abgelehnt | 30 Tage nach Ablehnung | `User.registration_rejected_at`; die Ablehnungsmail nennt die Frist |
| Bestätigt, aber nie zugeordnet (Altbestand ohne Ablehnungsstempel) | 30 Tage ohne Anmeldung | Konto älter als 30 Tage und seit 30 Tagen kein Login |

Mitarbeiter- und Superuser-Konten sind ausgenommen. Der Lauf löscht mit dem Konto nur dessen
eigene Artefakte (2FA-Gerät, Sitzungen, Tokens, Sicherheitsbenachrichtigungen); jede andere
Beziehung schützt das Konto. Die Ausgabe nennt nur Zahlen, keine Adressen.

### 3.3 Durchführung des Löschlaufs

Zwei gleichwertige Wege:

1. **Oberfläche**: *Einstellungen → Datenschutz → Löschlauf ausführen* (mit Probelauf-Option,
   Berechtigung `manage_settings`).
2. **Zeitplan im Worker** (monatlich, `befehl:session_privacy_purge`) oder von Hand:

   ```bash
   python manage.py session_privacy_purge              # alle aktiven Mandanten
   python manage.py session_privacy_purge --tenant stadt-musterstadt
   python manage.py session_privacy_purge --dry-run    # nur zählen
   ```

   Konten ohne Zuordnung (plattformweit, täglich als Zeitplan im Worker, siehe DEPLOYMENT.md,
   „Geplante Aufgaben“):

   ```bash
   python manage.py cleanup_orphaned_accounts           # löschen
   python manage.py cleanup_orphaned_accounts --dry-run # nur zählen
   ```

**Nachweisbarkeit.** Jeder Lauf schreibt Audit-Einträge:

- je anonymisierter Person ein Eintrag mit den geleerten Datenarten (niemals die Werte selbst),
- je bereinigter Sitzung ein Eintrag,
- ein Abschluss-Eintrag mit Zählern und den angewandten Fristen,
- bei gelöschten Audit-Einträgen ein Eintrag „Protokoll archiviert“ mit Paketname, Prüfsumme,
  Nummernbereich und Kettenanker.

Damit kann die Verwaltung die Durchführung gegenüber der Aufsichtsbehörde belegen
(Rechenschaftspflicht, Art. 5 Abs. 2 DSGVO).

### 3.4 Betroffenenauskunft (Art. 15 DSGVO)

*Einstellungen → Datenschutz → Betroffenenauskunft* exportiert alle zu einer Person
gespeicherten Daten als JSON-Datei (Stammdaten einschließlich der Einwilligung zur
Veröffentlichung der E-Mail-Adresse mit Datum und Nachweis, Gremienmitgliedschaften,
Anwesenheiten mit Rückmeldungen und Absagegründen, Ladungen mit Zustellweg und
Empfangsbestätigung, Sitzungsgelder, Vorlagen als Verfasser/in). Bankdaten werden nur
entschlüsselt, wenn die abrufende Person zusätzlich `manage_allowances` besitzt. Jeder Export
wird auditiert.

### 3.5 Anlagen und Fassungen (Issue #226)

Vorlagen und Anlagen haben Fassungen: Bei jedem Workflow-Schritt und jedem Beratungsergebnis
sichert mandari den Stand einer Vorlage, beim Ersetzen einer Anlage bleibt die bisherige Datei
abrufbar. Jeder Dateiinhalt liegt je Mandant nur einmal im Speicher (SHA-256); Anlagen und
Fassungen verweisen darauf.

| Vorgang | Wirkung |
|---|---|
| Anlage ersetzen | Neue Fassung der Anlage; die bisherige bleibt im Verlauf, sichtbar wie die Anlage. |
| Anlage löschen | Die Anlage und ihr Verlauf verschwinden. Inhalte, die in einer gesicherten Fassung der Vorlage stecken, bleiben dort erhalten, sichtbar nur noch mit dem Recht für nichtöffentliche Vorlagen. Alles andere wird nach dem Löschen aus dem Speicher entfernt. |
| Inhalt endgültig löschen (Datenschutz) | Mit dem Einstellungsrecht und einem Grund entfernt die Verwaltung einen Inhalt aus dem Speicher, auch aus allen Fassungen. Prüfsumme und Größe bleiben als Nachweis, die Fassung zeigt „Inhalt gelöscht“; der Grund steht im Audit-Log. |
| Beschlossene Fassung | Ihr Inhalt lässt sich nicht löschen: Sie ist der amtliche Stand, den das Gremium beschlossen hat (Aufbewahrung nach Archivrecht, Art. 17 Abs. 3 lit. b DSGVO). |
| Vorlage oder Mandant löschen | Alle Fassungen und nicht mehr benötigten Inhalte werden mitgelöscht. |

Der aktuelle Inhalt einer Anlage lässt sich nicht über die Datenschutz-Löschung entfernen;
dafür die Anlage ersetzen oder löschen.

### 3.6 Löschung ganzer Mandanten

Beim Löschen eines `SessionTenant` (Vertragsende) werden alle abhängigen Daten kaskadiert
gelöscht, einschließlich Audit-Log und mandantenspezifischem Verschlüsselungsschlüssel
(Crypto-Shredding: Ohne Schlüssel sind etwaige Sicherungen der verschlüsselten Felder nicht
mehr lesbar).

### 3.7 Dokumente aus fremden Ratsinformationssystemen (Insight)

Kopien und extrahierte Texte von Dokumenten, die wir aus den Ratsinformationssystemen der
Kommunen abrufen, folgen dem Löschabgleich ([FILE_CACHE.md, „Löschabgleich“](FILE_CACHE.md#löschabgleich)):

| Ereignis | Folge |
|---|---|
| Quelle meldet das Dokument als gelöscht (OParl `deleted`) oder seine Download-Adresse liefert `404`/`410` | sofort gesperrt: keine Auslieferung, kein Text auf der Vorgangsseite und in der OParl-Ausgabe, nicht im Suchindex, KI-Zusammenfassungen des Vorgangs verworfen und ohne seinen Text neu erstellt |
| Download-Adresse liefert das Dokument wieder (Prüfung nach 1, 7 und 25 Tagen und vor dem Löschen) | Sperre aufgehoben, nichts gelöscht |
| Quelle ändert das Dokument (anderer SHA-256, z. B. eine Schwärzung) | Kopie und Text werden durch die neue Fassung ersetzt, die alte Fassung gelöscht |
| 30 Tage gesperrt (`FILE_PURGE_AFTER_DAYS`) | lokale Kopie und extrahierter Text gelöscht; der Datensatz bleibt als Tombstone (Name, Adresse, Fingerabdruck). Lässt sich die Quelle vor dem Löschen nicht befragen, spätestens sieben Tage später (`FILE_PURGE_CONFIRM_GRACE_DAYS`) |
| Inhalt wird von keiner Datei mehr gebraucht (Ablage nach SHA-256: ersetzte Fassung, Löschen nach Frist) | Inhalt verwaist; der nächste Lauf von `loeschabgleich` bzw. `dokumentablage --aufraeumen` löscht ihn nach zehn Minuten lokal und im Objektspeicher. Ist der Objektspeicher ausgeschaltet (`OBJ_ENABLED=false`), bleibt eine dort schon liegende Kopie bis zum ersten Aufräumen nach dem Wiedereinschalten |
| Sicherungen | restic hält Stände 30 Tage; spätestens dann ist auch dort nichts mehr |
