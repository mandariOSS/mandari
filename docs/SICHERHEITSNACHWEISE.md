# Fahrplan Sicherheitsnachweise

Was mandari heute an Sicherheitsnachweisen hat, was in welcher Reihenfolge dazukommt
und wie wir mit Befunden umgehen. Grundlage für das Trust Center und für
Vergabeunterlagen (Issues #97 und #261, BSI IT-Grundschutz APP.3.1.A22).

> **Stand: Entwurf zur Entscheidung.** Termine und Turnus mit „(Entscheidung)“ sind
> Vorschläge; verbindlich werden sie mit Veröffentlichung im Trust Center.

## 1. Was heute belegbar ist

| Nachweis | Stand | Beleg |
|---|---|---|
| Statische Analyse (CodeQL) je Änderung | seit 2026, 0 offene Meldungen | GitHub Code Scanning |
| Abhängigkeiten geprüft (`pip-audit` für Anwendung und Ingestor, `npm audit`), blockierend ab „hoch“ | seit 09/2026 | CI-Job „Abhängigkeiten prüfen“ |
| Container-Images mit Trivy gescannt | je Build | Release-Workflow |
| Rund 1.700 automatisierte Tests je Änderung, Mandantentrennungs-Matrix über 160 Adressen | laufend | CI |
| Sicherheitsprozess mit CVSS, Advisories, CVE | seit 09/2026 | `SECURITY.md`, `docs/SICHERHEITSMELDUNGEN.md` |
| Kryptokonzept nach BSI TR-02102-1 | 09/2026 | `docs/KRYPTOKONZEPT.md` |
| Technisch-organisatorische Maßnahmen, Löschkonzept | laufend | `docs/DSGVO_TOM.md`, `docs/DSGVO_LOESCHKONZEPT.md` |
| Sicherung an zwei Standorten mit Wiederherstellungstest | seit 09/2026 | `docs/BACKUP.md` |
| Protokollierung mit Fristen | 09/2026 | `docs/PROTOKOLLE.md` |
| **SBOM (CycloneDX) je Release** | ab diesem Release | Release-Anhänge `sbom-*.cdx.json` (Abschnitt 4) |

Was fehlt: eine **unabhängige** Prüfung. Statische Analyse findet keine Fehler in
Berechtigungsketten und im Zusammenspiel der Portale.

## 2. Externer Penetrationstest

**Prüfumfang** (fest):

1. Bürgerportal einschließlich Suche und OParl-API (`/oparl/v1/`)
2. mandari work: Anmeldung mit zweitem Faktor (TOTP, WebAuthn), Einladungen und
   Selbstregistrierung, Editor und Kollaboration (WebSocket), Uploads, Freigaben
3. mandari session: Rollen und Rechte, Ö/NÖ-Trennung, Abstimmungen, Session-API v1
4. Mandantentrennung über alle drei Portale (horizontale Rechteausweitung)
5. Betrieb: Reverse Proxy, Header, TLS, Rate-Limits, Health- und Metrik-Endpunkte

Schwerpunkt ist das, was Automatisierung nicht abdeckt: Berechtigungslogik über
mehrere Schritte, Ö/NÖ-Trennung, Mandantentrennung.

**Form und Turnus (Entscheidung):** externer Dienstleister (Grey-Box mit Prüfkonten je
Rolle), **jährlich** sowie anlassbezogen vor 1.0 und vor größeren Änderungen an
Authentisierung oder Rechtemodell. Ein Bug-Bounty-Programm folgt frühestens nach dem
ersten Test.

**Vorbereitung:** eigener Testmandant auf der Staging-Instanz mit realitätsnahen Rollen
(Verwaltung, Gremium, Fraktion, Gast, Bürger), Prüfkonten je Rolle, Zurücksetzen auf
definierten Stand (`generate_load_data`, Issue #228, als Datenbasis), Freigabe des
Prüfzeitraums auf der Statusseite als Wartung.

**Umgang mit Befunden:** Bewertung nach CVSS 3.1 wie in `docs/SICHERHEITSMELDUNGEN.md`,
Fristen nach `docs/RELEASE_POLITIK.md` Abschnitt 4 (kritisch 72 h, hoch 7 Tage,
mittel 30 Tage). Jeder Befund wird ein Issue (nicht öffentlich, solange nicht
behoben; danach als Advisory oder im Changelog). Nachprüfung durch den Dienstleister
für kritische und hohe Befunde. Berichte liegen außerhalb des öffentlichen
Repositories beim Betreiber; das Trust Center erhält die Management-Summary mit
Datum, Umfang, Anzahl Befunde je Schweregrad und Status.

## 3. Selbstbewertung nach BSI IT-Grundschutz (Basis-Absicherung, BSI-Standard 200-2)

Zuordnung der bereits umgesetzten Maßnahmen zu Bausteinen; „offen“ heißt geplant,
nicht fehlend um jeden Preis.

| Baustein | Anforderung (Auswahl) | Stand | Beleg |
|---|---|---|---|
| APP.3.1 Webanwendungen | A4 Uploads begrenzt, A7 Schutz vor unberechtigtem Zugriff, A12 Schutz vor Injektion, A22 Sicherheitsprüfungen | A4/A7/A12 umgesetzt, A22 offen (Abschnitt 2) | `docs/UPLOADS.md`, Rechtematrix, CodeQL |
| CON.10 Webentwicklung | A5 Upload-Regeln, A17 Überlauf der Protokolle | umgesetzt | `docs/UPLOADS.md`, `docs/PROTOKOLLE.md` |
| CON.1 Kryptokonzept | Verfahren, Schlüsselhierarchie, Notfall | umgesetzt | `docs/KRYPTOKONZEPT.md` |
| CON.3 Datensicherung | täglich, verschlüsselt, getestet | umgesetzt | `docs/BACKUP.md` |
| OPS.1.1.5 Protokollierung | A6 zentral, A8 Fristen, A10 Zugriffsschutz | umgesetzt | `docs/PROTOKOLLE.md` |
| OPS.1.1.3 Patch- und Änderungsmanagement | Fristen, Prüfung vor Einspielen | umgesetzt (Release-Politik, CI-Gates) | `docs/RELEASE_POLITIK.md` |
| ORP.4 Identitäts- und Berechtigungsmanagement | Rollen, Vier-Augen, zweiter Faktor | umgesetzt (2FA-Pflicht, Rollen); Vier-Augen in Session (#222) offen | `SECURITY.md` |
| DER.4 Notfallmanagement | RTO/RPO, Wiederanlauf geübt | teilweise; Messung offen (#229) | `docs/HOCHVERFUEGBARKEIT.md`, `docs/SLA.md` |
| DER.2.1 Behandlung von Sicherheitsvorfällen | Meldeweg, Fristen | umgesetzt | `SECURITY.md` |
| ISMS.1 Sicherheitsmanagement | Rolle der Informationssicherheitsbeauftragten | **offen (Entscheidung):** Benennung durch den Betreiber | |

## 4. SBOM je Release

Der Release-Workflow erzeugt für jede veröffentlichte Version drei Stücklisten im
CycloneDX-Format und hängt sie an das GitHub-Release: `sbom-mandari-python.cdx.json`
(Django-Anwendung aus `mandari/requirements.lock`), `sbom-ingestor-python.cdx.json`
(aus `ingestor/uv.lock`) und `sbom-mandari-npm.cdx.json` (Frontend). Lokal:
`sh scripts/build_sbom.sh <ausgabeverzeichnis>`. Damit können Kunden ihre eigene
Schwachstellenüberwachung auf mandari anwenden.

## 5. ISO 27001 und BSI C5 (Entscheidung)

Eine Zertifizierung des Betreibers lohnt ab einem Auftragsvolumen, das die
jährlichen Kosten (Audit, Beratung, interner Aufwand) trägt. Vorschlag: Start der
Vorbereitung auf **ISO 27001**, sobald drei Verwaltungskunden im Managed Hosting
laufen oder eine Vergabe sie verlangt; BSI C5 nur auf ausdrückliche Anforderung.
Bis dahin: Grundschutz-Selbstbewertung (Abschnitt 3) jährlich fortschreiben.

## 6. Fahrplan (Entscheidung)

| Wann | Schritt | Nachweis im Trust Center |
|---|---|---|
| **Q4 2026** | SBOM je Release (erledigt mit diesem Fahrplan); Grundschutz-Selbstbewertung veröffentlicht; Informationssicherheitsbeauftragte benannt | Abschnitt „Nachweise“ mit Links auf TOM, Löschkonzept, SBOM, Fahrplan |
| **Q1 2027** | Erster externer Penetrationstest (Umfang Abschnitt 2), kritische und hohe Befunde geschlossen | Management-Summary mit Datum |
| **Q2 2027** | Notfallübung mit gemessener Wiederanlaufzeit (#229); SLA verbindlich | RTO/RPO als Zahl |
| **jährlich ab 2027** | Penetrationstest wiederholen, Selbstbewertung fortschreiben | Datum der letzten Prüfung |
| **bei Bedarf** | ISO 27001 (Abschnitt 5) | Zertifikat |
