# Service Level Agreement (SLA) für mandari Managed Hosting

Entwurf der Vertragsanlage „Service Level“ für Kunden im Managed Hosting.
Grundlage sind das Verfügbarkeitskonzept und die seit September 2026 laufende
Messung über die Statusseite (Issue #93).

> **Stand: Entwurf zur Entscheidung.** Die Zusagen sind Vorschläge des
> Entwicklungsteams und werden erst mit Veröffentlichung auf mandari.de/sla/ und
> Aufnahme in die AGB verbindlich. Werte mit „(Entscheidung)“ sind Geschäftsentscheidungen
> des Betreibers. Preise stehen bewusst nicht hier.

## 1. Geltungsbereich

Gilt für die vom Betreiber gehosteten Instanzen von mandari work, mandari session
und dem Bürgerportal einschließlich OParl-API. Nicht erfasst sind Selbst-Hosting,
Testinstanzen, Vorschau-Funktionen („Beta“) und Drittsysteme (Ratsinformationssysteme
der Kommunen, Mailversand über kundeneigene Server, Videokonferenzdienste).

## 2. Verfügbarkeit (Entscheidung)

| Stufe | Zusage je Kalendermonat | Messung |
|---|---|---|
| **Standard** | **99,5 %** | Statusseite status.mandari.de, Prüfintervall 60 s |
| **Premium** | **99,7 %** | wie Standard, zusätzlich 24/7-Bereitschaft für Klasse 1 |
| **Enterprise** | **99,9 %** | wie Premium, mit zweitem Standort (Stufe 3 des Verfügbarkeitskonzepts) |

**Messregel:** Ein Dienst gilt als nicht verfügbar, wenn zwei aufeinanderfolgende
Prüfungen der Statusseite scheitern; die Ausfallzeit beginnt mit der ersten
gescheiterten Prüfung und endet mit der ersten erfolgreichen. Einzelne fehlgeschlagene
Prüfungen zählen als Prüfintervall, nicht als Ausfall. Der Monatswert ist
`(Minuten im Monat − Ausfallminuten − ausgenommene Minuten) / (Minuten im Monat −
ausgenommene Minuten)`. Gemessen werden die Anmeldeseite von mandari work, die
Sitzungsübersicht von mandari session, die Startseite des Bürgerportals und der
OParl-Systemendpunkt.

**Ausgenommen** sind angekündigte Wartungsfenster (Abschnitt 3), höhere Gewalt,
Ausfälle von Vorleistungen außerhalb des Einflusses des Betreibers (Netzanbindung des
Rechenzentrums, DNS-Registrare), Störungen durch den Kunden (Fehlkonfiguration,
Sperrung durch eigene Firewalls, Überlast durch kundeneigene Skripte) sowie Angriffe,
die den Dienst trotz angemessener Schutzmaßnahmen beeinträchtigen.

**Warum 99,5 % und nicht mehr für Standard:** 99,5 % entsprechen 3,6 Stunden je Monat.
Der heutige Betrieb (ein Standort, Sicherung an zwei Standorten, Deploy mit Rückfall)
erreicht das mit Messung belegbar; 99,9 % (44 Minuten je Monat) setzt die Stufen 2 und 3
des Verfügbarkeitskonzepts voraus und ist deshalb der Enterprise-Stufe vorbehalten.

## 3. Wartungsfenster (Entscheidung)

- **Regelfenster:** Dienstag und Donnerstag, 05:00–06:00 Uhr (Europe/Berlin).
- Wartungen werden mindestens **48 Stunden** vorher auf der Statusseite angekündigt;
  Wartungen mit Unterbrechung über zehn Minuten zusätzlich per Mail.
- Sicherheitskorrekturen der Klassen „kritisch“ und „hoch“ dürfen außerhalb des
  Fensters mit kürzerer Ankündigung eingespielt werden; sie zählen nicht als Ausfall,
  wenn die Unterbrechung unter fünf Minuten bleibt.
- Nicht in Wartungsfenster fallen Sitzungstage, die der Kunde bis 14 Tage vorher
  im Kundenportal hinterlegt hat.

## 4. Störungsklassen, Reaktion und Wiederherstellung (Entscheidung)

| Klasse | Beschreibung | Reaktion Standard | Reaktion Premium/Enterprise | Wiederherstellungsziel |
|---|---|---|---|---|
| **1 – Ausfall** | Dienst für alle Nutzer nicht erreichbar; Datenverlust droht; Sicherheitsvorfall | 1 Stunde (Servicezeit) | 30 Minuten (24/7) | 4 Stunden |
| **2 – Erhebliche Störung** | Kernfunktion (Sitzungsmappe, Abstimmung, Editor, Ladung) unbenutzbar, Umgehung nicht möglich | 4 Stunden | 2 Stunden | 1 Werktag |
| **3 – Störung** | Einschränkung mit Umgehung, Darstellungsfehler, einzelne Nutzer betroffen | 1 Werktag | 4 Stunden | nächstes Release |

Reaktion heißt: qualifizierte Rückmeldung mit Einschätzung und nächstem Schritt.
Wiederherstellung heißt: Dienst benutzbar, ggf. mit Umgehung; die endgültige
Korrektur folgt nach Release-Politik.

## 5. Datensicherung, RPO und RTO

| Kennzahl | Zusage | Grundlage |
|---|---|---|
| **Sicherung** | täglich, verschlüsselt, an zwei getrennten Standorten, 30 Tage Aufbewahrung | restic, wöchentliche Integritätsprüfung, monatlicher Wiederherstellungstest (`docs/BACKUP.md`) |
| **RPO** (höchstens verlorene Daten) | **24 Stunden** (Standard/Premium); **1 Stunde** Enterprise (Entscheidung) | Enterprise setzt die stündliche Replikation aus dem Verfügbarkeitskonzept voraus |
| **RTO** (Wiederanlauf nach Totalverlust des Standorts) | **8 Stunden** Standard, **4 Stunden** Premium/Enterprise | Datenbankwiederherstellung aus dem Offsite-Backup gemessen unter fünf Minuten; der vollständige Neuaufbau wird mit #229 gemessen und die Zahl danach geschärft |

## 6. Servicezeiten und Kanäle (Entscheidung)

| Stufe | Servicezeit | Kanäle |
|---|---|---|
| Standard | Mo–Fr 08:00–17:00 Uhr (Europe/Berlin), außer gesetzliche Feiertage NRW | Kundenportal (Ticket), E-Mail support@mandari.de |
| Premium | Mo–Fr 07:00–19:00 Uhr; Klasse 1 zusätzlich 24/7 | zusätzlich Bereitschaftsnummer |
| Enterprise | wie Premium; benannte Ansprechperson; Sitzungstag-Begleitung auf Anfrage | zusätzlich direkter Kanal zur Ansprechperson |

**Eskalation:** Stufe 1 Support → Stufe 2 Betrieb/Entwicklung (nach Ablauf der halben
Wiederherstellungszeit) → Stufe 3 Geschäftsführung (nach Ablauf der
Wiederherstellungszeit). Der Kunde kann jede Stufe über das Ticket anfordern.

## 7. Gutschriften (Entscheidung)

Unterschreitet die gemessene Monatsverfügbarkeit die Zusage, erhält der Kunde auf
Antrag (binnen 30 Tagen nach Monatsende) eine Gutschrift auf das Monatsentgelt der
betroffenen Instanz:

| Gemessene Verfügbarkeit | Gutschrift |
|---|---|
| unter Zusage, mindestens 99,0 % | 10 % |
| unter 99,0 %, mindestens 98,0 % | 25 % |
| unter 98,0 % | 50 % |

Gutschriften sind der einzige Anspruch aus einer Unterschreitung; weitergehende
gesetzliche Rechte bei grober Fahrlässigkeit oder Vorsatz bleiben unberührt. Die
Abwicklung erfolgt bis zur Automatisierung manuell über das Kundenportal (Ticket,
Vermerk auf der Folgerechnung).

## 8. Berichtswesen

- Die Statusseite zeigt die gemessene Verfügbarkeit der letzten 24 Stunden, 7 und
  30 Tage je Dienst öffentlich.
- Monatlich erstellt der Betreiber einen Verfügbarkeitsbericht je Dienst
  (`manage.py availability_report`, Issue #231) mit Gesamtverfügbarkeit, Störungen
  (Beginn, Ende, Klasse, Ursache, Maßnahme) und Wartungen; Premium- und
  Enterprise-Kunden erhalten ihn im Kundenportal.
- Störungen der Klasse 1 erhalten binnen fünf Werktagen einen Störungsbericht
  (Ursache, Auswirkung, Abhilfe, Vorbeugung).

## 9. Voraussetzungen und Mitwirkung

Der Kunde benennt eine Kontaktadresse für Störungs- und Wartungsmeldungen, hält
Sitzungstage im Kundenportal aktuell, nutzt unterstützte Browser (jeweils aktuelle
und vorherige Hauptversion von Firefox, Chrome, Edge, Safari) und meldet Störungen
über die genannten Kanäle mit Zeitpunkt, betroffener Funktion und Beispiel.

## 10. Offene Entscheidungen des Betreibers

| Punkt | Vorschlag | Zu klären |
|---|---|---|
| Verfügbarkeit je Stufe | 99,5 / 99,7 / 99,9 % | Enterprise erst nach Stufe 3 des Verfügbarkeitskonzepts anbieten? |
| Wartungsfenster | Di/Do 05:00–06:00 | |
| Reaktionszeiten | s. Abschnitt 4 | Premium-Bereitschaft personell abgesichert? |
| RPO/RTO | 24 h / 8 h Standard | RTO nach Messung mit #229 schärfen |
| Gutschriften | 10 / 25 / 50 % | Obergrenze je Jahr? |
| AGB | Ziffer „Verfügbarkeit“ ersetzen | juristisch gegenlesen lassen |
| Veröffentlichung | `/sla/` auf mandari.de, PDF mit Version/Datum, Trust Center und Preisseite verlinken | Website-Repo |
