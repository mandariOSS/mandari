# Technische und organisatorische Maßnahmen (TOM) — mandari

Stand: Juli 2026 · Bezug: Art. 32 DSGVO, Issue #43

Dieses Dokument beschreibt die technischen und organisatorischen Maßnahmen
der Plattform mandari (Session RIS, Work-Portal, Insight-Portal). Es dient
als Anlage zum Auftragsverarbeitungsvertrag ([AVV-Muster](DSGVO_AVV_MUSTER.md)).

## 1. Vertraulichkeit

### Zutritts-/Zugangskontrolle
- Hosting in einem europäischen Rechenzentrum; Zugriff auf Server nur per
  SSH mit Schlüssel-Authentifizierung.
- Anmeldung an der Anwendung mit E-Mail/Passwort, optional TOTP-basierte
  Zwei-Faktor-Authentifizierung; Rate-Limiting gegen Brute-Force
  (5 Versuche je Adresse bzw. IPv6-/64-Netz und 10 je Konto in 15 Minuten),
  Sitzungs- und Geräteverwaltung je Konto.

### Zugriffskontrolle (Berechtigungskonzept)
- Mandantenfähigkeit mit strikter Datenisolation je Kommune/Organisation
  (alle Abfragen tenant-gebunden).
- Feingranulares Rollen- und Rechtesystem (Session RIS: u. a.
  `manage_users`, `manage_settings`, `manage_allowances`,
  `view_non_public_meetings`; Work-Portal: 50+ Berechtigungen).
- Bankdaten sind zusätzlich auf die Berechtigung `manage_allowances`
  beschränkt; nicht-öffentliche Inhalte auf eigens berechtigte Rollen.

### Verschlüsselung
- Transport: ausschließlich TLS (HTTPS, HSTS).
- Speicherung sensibler Felder: AES-256-GCM mit **tenant-spezifischen
  Schlüsseln**, die ihrerseits mit einem Master-Key verschlüsselt sind
  (Schlüsselhierarchie; Crypto-Shredding beim Löschen eines Mandanten).
- Verschlüsselt gespeichert werden u. a. Kontaktdaten, Bankdaten,
  nicht-öffentliche Protokollteile und interne Notizen.

## 2. Integrität

- Revisionssicheres Audit-Log je Mandant: alle relevanten Änderungen
  (Erstellen/Ändern/Löschen, Freigaben, Exporte, Einladungsversand) mit
  Nutzer, Zeitstempel, IP; Einträge sind unveränderbar und nicht einzeln
  löschbar.
- Verschlüsselte Feldinhalte erscheinen im Audit-Log niemals im Klartext.
- Zusätzlich protokolliert: Lesezugriffe auf nichtöffentliche Inhalte (nur
  Objekt-Referenz, je Mandant abschaltbar), Anmeldungen, Abmeldungen und
  Fehlversuche (ohne Passwort), Stimmabgaben, Mitzeichnungen, Entschädigungen
  sowie Rollen- und Rechteänderungen.
- Manipulationsschutz durch eine Hash-Kette je Mandant (SHA-256) mit
  Prüfwerkzeug; Export für Prüfinstanzen mit Prüfsumme; Archivpaket vor der
  fristgerechten Löschung. Einsicht und Export nur mit eigenen Kontrollrechten
  (Revision, Datenschutz) und zweitem Faktor; jeder Zugriff auf das Protokoll
  wird selbst protokolliert ([Protokollierungskonzept](PROTOKOLLIERUNG.md)).
- Vier-Augen-Prinzip bei der Sitzungsgeld-Genehmigung (Standard), je Mandant
  zusätzlich für Vorlagenfreigabe, Niederschrift und Übergabe von
  Beschlussauszügen; Vertretungen nur im Zeitraum, ohne Rechteausweitung und
  mit Vermerk „in Vertretung für …“ im Audit-Log.

## 3. Verfügbarkeit und Belastbarkeit

- Tägliche automatisierte Sicherung aller Datenbanken, Dateien und der
  Konfiguration, vor der Übertragung verschlüsselt (AES-256), in zwei
  räumlich getrennte Rechenzentren; Aufbewahrung höchstens 30 Tage, danach
  automatische Löschung. Wöchentliche Integritätsprüfung, monatlicher
  automatisierter Wiederherstellungstest, Alarmierung bei Fehlern
  ([Backup-Konzept](BACKUP.md)).
- Infrastruktur als Container (reproduzierbare Deployments), getrennte
  Staging-/Produktionsumgebung.
- Monitoring des Sync-/Hintergrunddienstes (Watchdog).

## 4. Löschung und Datenminimierung

- Konfigurierbare Aufbewahrungsfristen je Datenart und Mandant; nachweisbar
  auditierter Anonymisierungs-/Löschlauf (UI + `manage.py
  session_privacy_purge`) — Details im [Löschkonzept](DSGVO_LOESCHKONZEPT.md).
- Betroffenenauskunft als strukturierter Export (Art. 15 DSGVO).
- Öffentliche Schnittstellen (OParl-API, öffentliche Fraktions-API,
  iCal-Feeds) liefern ausschließlich als öffentlich gekennzeichnete Inhalte;
  opake Zufalls-Tokens statt personenbezogener URLs.

## 5. Organisatorische Maßnahmen

- Prinzip der geringsten Rechte bei Rollenzuweisung (Standard-Rollen mit
  abgestuften Rechten).
- Einladungsbasierte Konten; Selbstregistrierung nur, wenn eine Organisation sie
  aktiviert – mit bestätigter E-Mail-Adresse, Domain-Allowlist und optionaler
  Freischaltung durch Berechtigte (keine Selbstregistrierung in Session-Mandanten).
- Quelloffener Code (AGPL-3.0-or-later) — überprüfbare Sicherheitsmaßnahmen.
- Regelmäßige automatisierte Testläufe (Smoke-Suiten) inkl. Permission- und
  Mandanten-Isolationstests vor jedem Release.

## 6. Weisungs- und Kontrollrechte des Auftraggebers

- Der Auftraggeber (Kommune) steuert Rollen, Fristen und Veröffentlichungen
  selbst in der Anwendung.
- Export aller Daten über dokumentierte Schnittstellen (OParl, CSV/JSON)
  — keine Anbieterbindung.
