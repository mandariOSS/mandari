# RIS-Software Feature-Analyse

Analyse der Features proprietärer Ratsinformationssysteme als Grundlage für Mandari Session und Work.

**Stand:** Januar 2026

---

## Konkurrenzanalyse

### 1. regisafe (REGISAFE)

**Website:** https://www.regisafe.de/produkt/ratsinformationssystem/

#### Kernfunktionen
- Zentrale Verwaltung von Ratssitzungsdaten und Dokumenten
- Automatische Veröffentlichung von Sitzungen und Tagesordnungen
- Integrierte Protokollerstellung
- Sitzungsgeldabrechnung

#### Dokumentenmanagement
- Gesamtdokument zu einzelnen TOPs oder gesamter Sitzung erzeugen
- Dokumentenfreigabe und kollaborative Bearbeitung
- Persönliche Notizen mit Freigabeoptionen

#### Benutzeroberfläche & Barrierefreiheit
- Anpassbares Design (Farben, Bilder)
- Responsive Layout für alle Geräte
- Kalender- und Listenansicht mit Filterung
- Favoritenfunktion
- Granulare Suche und Filterung
- BSI-zertifizierte Sicherheit
- BITV-Barrierefreiheitszertifizierung

#### Mobile Zugriff
- iOS und Android Apps mit Offline-Funktion
- Vereinfachte Anmeldeverfahren
- Tablet- und Smartphone-Unterstützung

#### Kommunikation & Kollaboration
- Persönlicher Posteingang im Portal
- Kontaktformulare für Anfragen
- Personenfilterung nach Gremium, Fraktion, Rolle
- Direktnachrichten zwischen Nutzern

#### Erweiterte Features
- Elektronische Abstimmungsverfahren
- KI-gestützte Protokollgenerierung (regisafe-Sitzungsassistent)
- Automatische Transkription von Audioaufnahmen
- Integration mit KommunalPLUS Sitzung

---

### 2. Sternberg (sitzungsdienst.net)

**Website:** https://www.sitzungsdienst.net/

#### Gremieninformationssystem
- Öffentliches Informationsportal
- Sitzungskalender mit Terminübersicht
- Dokumentenarchiv mit Volltextsuche
- Personenverzeichnis mit Fraktionszuordnung

#### Sitzungsmanagement
- Tagesordnungserstellung
- Vorlagenverwaltung
- Einladungsversand (digital/print)
- Protokollführung
- Beschlusskontrolle
- Fristenmanagement

---

### 3. ALLRIS 4 (CC e-gov)

**Website:** https://www.cc-egov.de/allris-4/

#### Planung & Organisation
- Automatisierte Tagesordnungs- und Sitzungspaket-Erstellung
- Stammdatenpflege und Workflow-Management
- Digitale Signatur und Genehmigungsprozesse

#### Dokumentenmanagement
- Vorlagenerstellung in standardisierten Formaten
- Trennung öffentlich/nicht-öffentlicher Inhalte
- Automatisierte Dokumentenintegration

#### Durchführung
- Digitale Protokollaufzeichnung
- Online-Abstimmungen (offen, geheim, namentlich)
- Ein-Klick-Veröffentlichung für Öffentlichkeit und Presse

#### Nachbereitung
- Automatische Beschlussauszugs-Generierung
- Beschlussverfolgung mit Workflows
- Direkte HKR-Integration für Sitzungsgelder

#### Zusatzfunktionen
- Native Apps für Android, iOS, Windows 10
- Zwei-Faktor-Authentifizierung (SMS mTAN, TOTP)
- HKR-Schnittstelle für Zahlungssysteme
- Workflow-Management mit E-Mail-Benachrichtigungen
- gpaNRW-zertifiziertes Abstimmungssystem
- Cloud-Hosting (SaaS)
- Mobile Device Management

---

### 4. kiC (KIC Software)

**Website:** https://www.kic-software.de/

#### Kernfunktionen
- Direkte Integration in kommunale Webseiten
- Datenpräsentation für Gremien, Fraktionen, Funktionen
- Personenprofile mit Fotos und Schwerpunkten

#### Zugriffssteuerung
- Öffentliche und geschützte Bereiche
- Passwortschutz für Mandatsträger
- Granulare Veröffentlichungseinstellungen

#### Inhalt & Suche
- Komplette Sitzungsdaten mit Anlagen
- Druckfunktion für Sitzungsdokumentation
- Volltextsuche über gesamte Datenbank

#### Organisation
- Sitzungskalender
- Archivierungssystem

#### Mobile Zugriff
- Native Apps für iOS, macOS, Android, Windows

---

### 5. Somacos (Session, SessionNet, Mandatos)

**Website:** https://somacos.de/

#### Session (Sitzungsmanagement)
- Kompletter Sitzungsworkflow (Planung bis Nachbereitung)
- Dokumentenvorlagen-Verarbeitung
- Entscheidungskontrolle und Projektverfolgung
- Aufwandsentschädigung
- Integration mit Textverarbeitungsanwendungen
- Persönliches "Session Today" Dashboard
- Workflow-Assistent
- iOS-App für Echtzeitzugriff

#### SessionNet (Bürgerportal)
- Jederzeit Zugriff auf Dokumente, Beschlüsse, Termine
- Such- und Filterfunktionen
- Multi-Plattform-Unterstützung
- Rollenbasierte Zugriffssteuerung (Intranet/Extranet/Internet)

#### Mandatos (Mandatsträger-App)
- Alle SessionNet-Funktionen plus Zusatzfunktionen
- Windows, iOS, Android
- Multi-Mandat-Unterstützung
- Automatische Synchronisation von Sitzungsmaterialien
- Versionskontrolle
- Offline-Zugriff
- Verschlüsselte Speicherung
- Elektronische Annotationen
- Volltextsuche
- Dashboard mit Schnellzugriff
- Integrierte Online-Abstimmung
- Anwesenheitsmanagement

---

## Feature-Kategorien für Mandari

### Mandari Insight (Bürger*innen-Portal)
*Bereits geplant:*
- Einblick in Anträge, Vorlagen, Beschlüsse
- KI-Zusammenfassungen und Chatbot
- Kontakt zu Politiker*innen
- Volltextsuche (Meilisearch)
- Geografische Einordnung (Kartenansicht)
- Abstimmungsverhalten
- Recherche-Tools

*Aus Konkurrenzanalyse:*
- [ ] Sitzungskalender mit Filterung
- [ ] Favoritenfunktion für Themen/Gremien
- [ ] RSS-Feeds für Neuigkeiten
- [ ] Barrierefreiheit (BITV-konform)
- [ ] Newsletter-Abonnement
- [ ] Social Media Sharing

---

### Mandari Work (Fraktionen/Teams)
*Bereits geplant:*
- Sitzungsvorbereitung
- Abstimmungen (intern)
- Antragsdatenbank
- Terminplanung
- Fraktionssitzungen
- Recherche
- Kommentare

*Aus Konkurrenzanalyse:*
- [ ] Persönliche Notizen mit Freigabeoptionen
- [ ] Kollaborative Dokumentenbearbeitung
- [ ] Persönlicher Posteingang/Messaging
- [ ] Offline-Zugriff (PWA/App)
- [ ] Automatische Dokumentensynchronisation
- [ ] Annotationen auf Dokumenten
- [ ] Multi-Mandat-Unterstützung
- [ ] Push-Benachrichtigungen
- [ ] Fristen-Tracking
- [ ] Aufgabenverwaltung
- [ ] Beschlusskontrolle (Was wurde aus Anträgen?)
- [ ] Vorlagen-Bibliothek für Anträge
- [ ] Team-Kalender
- [ ] Anwesenheitsübersicht

---

### Mandari Session (Verwaltung/RIS)
*Bereits geplant:*
- Sitzungsplanung
- Einladungen (digital)
- Datenpflege (Stammdaten)
- Protokollierung
- Sitzungsgeld und Aufwandsentschädigungen
- OParl-Export

*Aus Konkurrenzanalyse:*
- [ ] Tagesordnungserstellung (automatisiert)
- [ ] Vorlagenverwaltung mit Vorlagen
- [ ] Gesamtdokument-Generierung (Sitzungspaket)
- [ ] Digitale Signatur/Genehmigungsprozesse
- [ ] Trennung öffentlich/nicht-öffentlich
- [ ] Online-Abstimmungen (offen, geheim, namentlich)
- [ ] gpaNRW-Zertifizierung für Abstimmungen
- [ ] Automatische Beschlussauszüge
- [ ] Beschlussverfolgung/Workflows
- [ ] HKR-Schnittstelle (Finanzsystem)
- [ ] KI-Protokollassistent
- [ ] Audio-Transkription
- [ ] Zwei-Faktor-Authentifizierung
- [ ] Mobile Device Management
- [ ] Archivierung (langfristig)
- [ ] Druckvorlagen (DIN-konform)
- [ ] Fristenmanagement
- [ ] Workflow-Management mit E-Mail-Benachrichtigungen

---

## Alleinstellungsmerkmale Mandari

### 1. Offenes Ökosystem (kein Vendor Lock-in)
- **OParl-Standard:** Import UND Export
- **API-First:** Vollständige REST-API
- **Datenexport:** Alle Daten maschinenlesbar exportierbar
- **Open Source:** 100% quelloffen (AGPL-3.0)

### 2. Zwei Betriebsmodi für Fraktionen
**Modus A: Mandari Session wird genutzt**
- Volle Integration zwischen Session, Work und Insight
- Echtzeit-Synchronisation
- Nahtlose Workflows

**Modus B: Externes RIS mit OParl-Schnittstelle**
- Work nutzt OParl-Daten der Kommune
- Keine Abhängigkeit vom RIS-Anbieter
- Funktioniert mit regisafe, ALLRIS, Somacos, etc.

### 3. KI-Integration (optional, datenschutzkonform)
- Zusammenfassungen von Vorlagen
- Chatbot für Bürgeranfragen
- Protokollassistent (Session)
- DSGVO-konform, auf Wunsch lokal

### 4. Moderne Architektur
- Django + HTMX (keine SPA-Komplexität)
- PostgreSQL (relationale Datenbank)
- Meilisearch (Volltextsuche)
- Redis (Caching)
- Docker-basiertes Deployment

---

## Priorisierung

### Phase 1 (MVP)
1. Insight: Bürgerportal mit Suche, Karten, KI
2. Work: Basis-Kollaboration für Fraktionen
3. Session: Grundfunktionen für Verwaltung

### Phase 2 (Erweiterung)
1. Insight: Abstimmungsverhalten, Favoritenfunktion
2. Work: Offline-Zugriff, erweiterte Kollaboration
3. Session: Online-Abstimmungen, Sitzungsgelder

### Phase 3 (Enterprise)
1. Alle Module: Erweiterte Workflows
2. Session: gpaNRW-Zertifizierung
3. Integration: HKR-Schnittstellen

---

*Dokument erstellt: Januar 2026*
*Letzte Aktualisierung: Januar 2026*
