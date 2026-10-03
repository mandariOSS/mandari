# Körperschaften im Mandanten: Der Mandant ist die Verwaltung, die Körperschaft die rechtliche Einheit

- Status: angenommen
- Datum: 2026-10-02
- Issue: #756 (Epic #753)
- Paket: Session, P2a
- Hängt ab von: [Schichtenmodell](20260929-schichtenmodell.md),
  [Kanonisches Modell](20260929-kanonisches-modell.md)

## Kontext

Ein Session-Mandant ist bisher genau eine Körperschaft: Art und Gemeindeschlüssel stehen am Mandanten
(`SessionTenant.body_type`, `ags`), die OParl-Schnittstelle kennt einen `Body` je Mandant.

In der Praxis führt eine Verwaltung den Sitzungsdienst oft für mehrere Körperschaften:

- Eine **Samtgemeinde** (Niedersachsen) ist selbst Körperschaft mit Rat, Ausschuss und Bürgermeisterin
  bzw. Bürgermeister. Ihre **Mitgliedsgemeinden** sind ebenfalls Körperschaften mit eigenem Rat und
  eigener Bürgermeisterin bzw. eigenem Bürgermeister, die zu deren Sitzungen lädt (§§ 98, 103–106
  NKomVG). In der Praxis bereitet die Verwaltung der Samtgemeinde auch diese Sitzungen vor und
  versendet die Ladungen; das Gesetz schreibt das nicht ausdrücklich vor, stützt es aber (Unterstützung
  der Mitgliedsgemeinden nach § 98 Abs. 4 und 5, Gemeindedirektorin bzw. Gemeindedirektor aus der
  Samtgemeinde nach § 106). Dieselben Mitarbeitenden arbeiten für alle; viele Mandatstragende sitzen in
  mehreren Körperschaften.
- Ein **Landkreis** führt den Sitzungsdienst für Zweckverbände oder Anstalten mit.
- Eine **Stadt mit Ortsräten** braucht Gremien mit eigener Identität.

Jede Körperschaft hat eigene Gremien, Vorlagen, Nummernkreise, Briefköpfe, Sitzungsgeldsätze und tritt
nach außen (Bekanntmachung, Bürgerportal, OParl) selbst auf. Konten, Rollen, Abläufe, Textbausteine und
die technische Verantwortung (Schlüssel, Prüfprotokoll) gehören dagegen der Verwaltung. Marktübliche
Ratsinformationssysteme führen solche Verwaltungen in einer Instanz mit Mandantenauswahl und
Gesamtansicht.

## Entscheidung

**Der Session-Mandant ist die Verwaltung, die den Sitzungsdienst führt. Er führt ihn für eine oder
mehrere Körperschaften (`SessionBody`).**

| Gehört der Verwaltung (Mandant) | Gehört der Körperschaft |
|---|---|
| Personen, Konten, Rollen, Vertretungen | Gremien (Sitzungen über ihr Gremium) |
| Abläufe, Textbausteine, Einstellungen | Vorlagen |
| Schlüssel für verschlüsselte Felder | Nummernkreise (wahlweise, siehe unten) |
| Prüfprotokoll mit Hash-Kette | Briefkopf, Wappen, Unterzeichnende (#758) |
| Wahlperioden (landesweit gleich) | Sitzungsgeldsätze und Pauschalen (#758) |
| | OParl-`Body`, Änderungsfeed, Bürgerportal-Quelle (#758) |

**Datenmodell**

- `SessionBody`: Mandant, Name, Kurzname, Kurzkennung (`slug`, eindeutig je Mandant), Art, Amtlicher
  Gemeindeschlüssel (AGS), Regionalschlüssel (RGS), übergeordnete Körperschaft (Mitgliedsgemeinde →
  Samtgemeinde), aktiv, `is_default`.
- Die Arten der Körperschaft sind eine Liste für Mandant und Körperschaft. Die bisherigen Werte bleiben mit
  unveränderten Bezeichnungen (sie erscheinen als `classification` in OParl); neu sind Samtgemeinde,
  Mitgliedsgemeinde, Einheitsgemeinde, Selbständige Gemeinde, Große selbständige Stadt, Region und
  kommunale Gesellschaft.
- Fremdschlüssel `body` tragen nur **Gremium** (`SessionOrganization`), **Vorlage** (`SessionPaper`) und
  **Nummernkreis** (`SessionNumberRange`, leer = für alle Körperschaften). Sitzungen, Tagesordnungspunkte,
  Beratungen und Dateien leiten die Körperschaft über Gremium bzw. Vorlage ab; ein weiterer Fremdschlüssel
  dort wäre nur eine zweite Wahrheit.
- Ämter (Gremientyp „Amt/Fachbereich“) gehören zur Standardkörperschaft, also zu der Körperschaft, die die
  Verwaltung trägt; sie bleiben bei Vorlagen aller Körperschaften wählbar. Den Ämterbaum der Verwaltung
  baut #759.
- Löschen einer Körperschaft mit Gremien oder Vorlagen ist gesperrt (`RESTRICT`); beim Löschen des
  Mandanten gehen alle Körperschaften mit.

**Standardkörperschaft und Migration**

- Jeder Mandant hat genau eine Standardkörperschaft (Eindeutigkeit je Mandant per Datenbankregel). Sie
  entsteht beim Anlegen des Mandanten aus dessen Name, Kurzname, Art und AGS; Bestandsmandanten bekommen sie
  per Datenmigration, und alle Gremien und Vorlagen zeigen auf sie. Die Migration ist idempotent.
- **Abwärtskompatibel:** Die Spalten sind in der Datenbank nullbar. Pflicht ist die Körperschaft im Modell
  (`save()` ergänzt sie: Gremium → Standardkörperschaft, Vorlage → Körperschaft des federführenden Gremiums,
  sonst Standardkörperschaft), in Formularen und Diensten. Ein älteres Image läuft auf dem neuen Schema
  weiter; was es anlegt, hat keine Körperschaft und gilt beim Lesen als Teil der Standardkörperschaft.
  Jeder `migrate`-Lauf ordnet solche Nachzügler zu (`post_migrate`), also auch die Objekte, die zwischen
  Migration und Containerwechsel entstehen. `NOT NULL` folgt frühestens zwei Releases später.

**Nummernkreise**

- Ein Nummernkreis gilt für alle Körperschaften oder für eine. Vorrang hat der Kreis der Körperschaft der
  Vorlage vor dem allgemeinen, innerhalb davon der Kreis der Vorlagenart vor dem für alle Arten. Kreise
  anderer Körperschaften gelten nie.
- Platzhalter `{koerperschaft}` (Kurzname der Körperschaft) mit eigenem Zähler je Körperschaft über das
  vorhandene Feld `SessionNumberCounter.scope`, wie `{gremium}` je Gremium.
- Vorlagennummern bleiben **je Mandant** eindeutig: eine Verwaltung, eine Suche, keine Verwechslung im
  Schriftverkehr. Gleiche Muster in zwei Körperschaften brauchen `{koerperschaft}` oder ein eigenes Präfix;
  sonst überspringt die Vergabe belegte Nummern.
- Presets verwalten nur die Kreise für alle Körperschaften.

**Oberfläche**

- Mandanten mit einer Körperschaft sehen keine Änderung. Auswahl beim Anlegen, Filter und die Verwaltung
  der Körperschaften erscheinen erst ab der zweiten aktiven Körperschaft; Standard ist die Gesamtansicht
  „alle Körperschaften“. Weitere Körperschaften legt der Betrieb an (Admin) oder die Verwaltung selbst,
  sobald sie mehr als eine hat.
- Gemeinsame Sitzungen bleiben innerhalb einer Körperschaft (Prüfung beim Zuordnen, nicht nur im
  Formular). Beratungen über Körperschaftsgrenzen regelt #758.

**Abgrenzungen**

- **Mandantengruppe und Leitstelle (#317)** bleiben für Verbünde getrennter Verwaltungen mit eigenen
  Konten, Schlüsseln und Protokollen; die Leitstelle öffnet keinen Mandanten.
- **Rechte (#759):** Der Geltungsbereich „Körperschaft“ wirkt bis zur Umstellung wie „Mandant“
  (`body_service.bodies_in_scope` liefert alle Körperschaften des Mandanten).
- **Veröffentlichung (#758):** Bis dahin bleiben OParl-Schnittstelle, Bürgerportal-Quelle und Kennungen an
  den Mandanten gebunden; Name, Art und AGS am Mandanten bleiben dafür maßgeblich. Der bisherige Pfad
  `…/api/oparl/body/` zeigt danach die Standardkörperschaft; für Mandanten mit einer Körperschaft ändern
  sich Ausgabe und Kennungen nicht.
- **Verschlüsselung und Prüfprotokoll** bleiben je Mandant. Wer datenschutzrechtlich verantwortlich ist
  (die Verwaltung allein, gemeinsam mit den Körperschaften oder als Auftragsverarbeiterin), ist offen. Das
  Modell trägt jede Antwort, weil #758 Export, Löschung und Protokollauszug je Körperschaft liefert; der
  Auszug filtert die Kette, ohne sie zu teilen.

## Alternativen

- **Ein Mandant je Körperschaft, verbunden über die Mandantengruppe mit Verbundrollen.** Personen, Konten
  und Regeln müssten je Körperschaft gepflegt werden (bei einer Samtgemeinde mit n Mitgliedsgemeinden
  (n + 1)-fach), die Mitarbeitenden wechseln im Alltag den Mandanten, eine Beratung über
  Körperschaftsgrenzen ist nicht möglich. Verworfen für Verwaltungen, die mehrere Körperschaften führen;
  bleibt für getrennte Verwaltungen.
- **Mischform: Mandant je Körperschaft mit gemeinsamen Personen und Konten.** Braucht
  mandantenübergreifende Schlüssel und Fremdschlüssel und hebelt die Mandantentrennung aus, ohne die
  Doppelpflege der Regeln zu lösen. Verworfen.
- **Körperschaft nur als Merkmal am Gremium.** Keine Nummernkreise, Briefköpfe oder OParl-`Body` je
  Körperschaft möglich. Verworfen.
- **Fremdschlüssel an jedem Objekt** (Sitzung, Tagesordnungspunkt, Datei …). Redundant und fehleranfällig;
  die Körperschaft folgt aus Gremium bzw. Vorlage. Verworfen.
- **Spalten sofort `NOT NULL`.** Ein Rückfall per Image wäre nicht mehr möglich, weil das alte Image Gremien
  und Vorlagen ohne Körperschaft anlegt. Verworfen; frühestens zwei Releases später.
- **Vorlagennummern je Körperschaft eindeutig.** Gleiche Nummern in einer Verwaltung führen zu
  Verwechslungen in Suche und Schriftverkehr. Verworfen.

## Folgen

**Positiv**

- Eine Verwaltung arbeitet mit einem Konto, einem Kalender, einem Arbeitsvorrat und einem Regelwerk für
  alle Körperschaften; Filter je Körperschaft bei Bedarf.
- Dasselbe Modell trägt Samtgemeinden, Landkreise mit Zweckverbänden und Städte mit Ortsräten.
- Bestandsmandanten merken nichts; OParl-Ausgabe und Kennungen bleiben gleich.

**Negativ**

- Bis zur Pflicht in der Datenbank gibt es zwei Lesarten für „keine Körperschaft“ (Nachzügler = Standard);
  Filter müssen sie berücksichtigen (`body_service.body_q`).
- Die Trennung zwischen Körperschaften derselben Verwaltung beruht auf Rechten und Geltungsbereichen
  (#759), nicht auf getrennten Schlüsseln.
- Bis #758 stehen Name, Art und AGS doppelt (Mandant und Standardkörperschaft).

## Prüfung (Fitnessfunktion)

- Migrationstest: alter Stand → Mandanten, Gremien, Vorlagen anlegen → weitermigrieren → je Mandant genau
  eine Standardkörperschaft, alle Gremien und Vorlagen zugeordnet; zweiter Lauf ändert nichts.
- Datenbankregel: höchstens eine Standardkörperschaft je Mandant.
- OParl-Konformitäts- und Kennungstests sowie `check_ris_ids` laufen unverändert.
- Sicherheitsmatrix unverändert grün; Abfragezahl-Tests der Listen mit mehreren Körperschaften.
- Test: Gemeinsame Sitzungen über Körperschaftsgrenzen werden abgelehnt.

## Bezug

- Issues #753 (Epic), #756, #758 (Identität und Veröffentlichung je Körperschaft), #759 (Rechte mit
  Geltungsbereich), #317 (Mandantengruppe, Leitstelle), #150 (Nummernkreise)
- [Kanonisches Modell](20260929-kanonisches-modell.md): kanonische Kennungen tragen die Körperschaft ab #758
  als `body_id`
- Niedersächsisches Kommunalverfassungsgesetz (NKomVG), §§ 97–106: Samtgemeinde und Mitgliedsgemeinden
