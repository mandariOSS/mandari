# Muster: Auftragsverarbeitungsvertrag (AVV) — mandari Session RIS

Stand: Juli 2026 · Bezug: Art. 28 DSGVO, Issue #43

> **Hinweis:** Dieses Muster ist eine Arbeitsgrundlage für den Vertrag
> zwischen einer Kommune (Verantwortliche) und dem Betreiber der
> mandari-Plattform (Auftragsverarbeiter). Es ersetzt keine Rechtsberatung
> und ist vor Verwendung durch die Vergabe-/Rechtsstelle der Kommune zu
> prüfen und anzupassen.

## 1. Gegenstand und Dauer der Verarbeitung

Der Auftragsverarbeiter betreibt für die Verantwortliche das
Ratsinformationssystem **mandari Session RIS** (Sitzungsdienst,
Gremienverwaltung, Beschlussdokumentation, Sitzungsgeld-Abrechnung,
optionale Veröffentlichung im Bürgerportal). Die Dauer entspricht der
Laufzeit des Hauptvertrags.

## 2. Art und Zweck der Verarbeitung

Hosting, Speicherung, Bereitstellung und Pflege der von der
Verantwortlichen eingegebenen Daten; Versand von E-Mails im Auftrag
(Einladungen, Benachrichtigungen); Erstellung von Exporten (PDF, CSV,
SEPA-Dateien) auf Veranlassung der Verantwortlichen.

## 3. Kategorien betroffener Personen

- Mandatsträgerinnen und Mandatsträger, sachkundige Bürger/innen
- Beschäftigte der Verwaltung (Sitzungsdienst)
- Verfasser/innen von Vorlagen und Anträgen

## 4. Kategorien personenbezogener Daten

- Stammdaten (Name, Titel, Funktion, Gremienzugehörigkeit, Wahlperiode)
- Kontaktdaten (E-Mail, Telefon, Adresse — verschlüsselt gespeichert)
- Bankdaten für Entschädigungen (Kontoinhaber, IBAN, BIC — verschlüsselt,
  zugriffsbeschränkt)
- Anwesenheits- und Abstimmungsdaten, Protokolle (ggf. nicht-öffentlich)
- Nutzungs- und Protokolldaten (Audit-Log, IP-Adressen)

## 5. Pflichten des Auftragsverarbeiters

1. Verarbeitung ausschließlich auf dokumentierte Weisung der
   Verantwortlichen (Konfiguration in der Anwendung gilt als Weisung).
2. Vertraulichkeitsverpflichtung aller mit der Verarbeitung befassten
   Personen.
3. Umsetzung der technischen und organisatorischen Maßnahmen gemäß
   **Anlage TOM** ([DSGVO_TOM.md](DSGVO_TOM.md)).
4. Unterstützung der Verantwortlichen bei Betroffenenrechten (die
   Anwendung stellt hierfür die Betroffenenauskunft und den
   Anonymisierungs-/Löschlauf bereit, siehe
   [Löschkonzept](DSGVO_LOESCHKONZEPT.md)).
5. Meldung von Verletzungen des Schutzes personenbezogener Daten ohne
   unangemessene Verzögerung.
6. Löschung oder Rückgabe aller Daten nach Vertragsende (Export über
   dokumentierte Schnittstellen; anschließend Löschung des Mandanten
   einschließlich Crypto-Shredding des Tenant-Schlüssels).
7. Bereitstellung aller für den Nachweis der Pflichten erforderlichen
   Informationen; Ermöglichung und Duldung von Überprüfungen.

## 6. Unterauftragsverhältnisse

Eingesetzte Unterauftragnehmer (z. B. Rechenzentrum/Hosting) sind in einer
Anlage aufzuführen; Änderungen werden der Verantwortlichen vorab mitgeteilt
(Widerspruchsrecht). Eine Verarbeitung außerhalb der EU/des EWR findet
nicht statt.

## 7. Weisungsrecht der Verantwortlichen

Weisungen erfolgen in Textform oder unmittelbar durch Konfiguration in der
Anwendung (Rollen, Aufbewahrungsfristen, Veröffentlichungs-Schalter).

## 8. Ort der Verarbeitung

Rechenzentrum in der EU (Details in der Unterauftragnehmer-Anlage).

---

**Anlagen**

- Anlage 1: Technische und organisatorische Maßnahmen ([DSGVO_TOM.md](DSGVO_TOM.md))
- Anlage 2: Löschkonzept und Aufbewahrungsfristen ([DSGVO_LOESCHKONZEPT.md](DSGVO_LOESCHKONZEPT.md))
- Anlage 3: Unterauftragnehmer (je Installation zu pflegen)
