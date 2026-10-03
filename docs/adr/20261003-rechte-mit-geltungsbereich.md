# Rechte mit Geltungsbereich in Session: Objektart und Aktion, Zuweisung für Bereich und Zeitraum

- Status: angenommen
- Datum: 2026-10-03
- Issue: #759 (Epic #753), Teile #772–#776
- Paket: Session, P3
- Hängt ab von: [Schichtenmodell](20260929-schichtenmodell.md),
  [Ereignisverträge](20260929-ereignisvertraege.md),
  [Körperschaften im Mandanten](20261002-koerperschaften-im-mandanten.md)

## Kontext

Eine Session-Rolle besteht heute aus 29 mandantenweiten Häkchen (27 Fachrechte, 2 Kontrollrechte aus #221)
und dem Merkmal „Administrator“. Wer „Vorlagen freigeben“ hat, gibt jede Vorlage des Mandanten frei; wer
nichtöffentliche Vorlagen sehen darf, sieht alle. Für eine Verwaltung mit mehreren Körperschaften und vielen
Ämtern reicht das nicht: Das Bauamt soll seine nichtöffentlichen Vorlagen sehen, aber nicht die
nichtöffentlichen Personalvorlagen des Hauptamts; der Sitzungsdienst einer Mitgliedsgemeinde nur deren
Gremien.

Dazu kommen drei Befunde:

- Die Regel „nichtöffentlich nur mit Recht“ ist vielfach neben `visibility.py` umgesetzt (rund 280
  Fundstellen in 47 Dateien), im Antragsbereich stehen mehrere Rechtelogiken nebeneinander.
- Work (`apps.tenants`: Role, Membership, Permission) und Session haben zwei getrennte Rechtesysteme.
- Mandatstragende sollen künftig nichtöffentliche Unterlagen ihrer Gremien sehen (#761). Diese Rechte
  entstehen aus der Besetzung, nicht von Hand, und andere Module erfahren davon über die Datendrehscheibe.
  Dafür braucht es einen festen Ereignisnamen.

## Entscheidung

**Ein Recht ist Objektart + Aktion und wird gegen einen Geltungsbereich ausgewertet. Rollen bündeln
Rechte; eine Zuweisung gibt eine Rolle einem Konto für einen Geltungsbereich und einen Zeitraum.** Gebaut
zunächst in Session, umgestellt ohne Verhaltensänderung.

**Ort**

- Paket `apps/session/rechte`. Der Kern (Zuweisungen auswerten, Bäume auflösen, Zugriffskontext,
  Rechteauskunft) ist fachfrei geschrieben und importiert keine Session-Modelle. Katalog, Bereichsbäume aus
  Körperschaften und Gremien sowie der Zuweisungsdienst sind Session-Fachlogik daneben.
- Kein drittes Rechtesystem in der Plattform: Eine Rollenzuweisung an `SessionUser` aus der Plattform
  verböte das Schichtenmodell, und ein weiteres System neben Work und Session verschärfte den Befund „zwei
  Rechtesysteme“. Der Kern wird erst in die Plattform gehoben, wenn Work dasselbe Modell nutzt; dann ersetzt
  er das Rechtesystem in `apps.tenants`. Das bekommt ein eigenes ADR.

**Rechtekatalog**

- Kennung `objektart.aktion` in Kleinbuchstaben (`vorlage.freigeben`, `sitzung.noe_sehen`), rund 70 Rechte
  für Sitzung, Tagesordnung, Vorlage, Antrag/Anfrage, Niederschrift, Beschluss, Umlaufverfahren, Person,
  Gremium, Körperschaft, Sitzungsgeld, Endgeräte, Abläufe, Rollen/Konten/Vertretungen, Einstellungen und
  Prüfprotokoll.
- Der Katalog steht im Code (wie die Ereignisverträge), nicht in der Datenbank: Neue Rechte kommen mit dem
  Code, der sie prüft. Je Recht: Bezeichnung, das heutige Häkchen, aus dem es folgt (Herkunft), und die
  Merkmale **Station** (braucht zusätzlich die Zuständigkeit für eine offene Station, #760) und
  **Kontrollrecht** (nicht in der Administrator-Vollmacht, #221).
- Jedes Häkchen hat ein **Leitrecht** (`can_approve_papers` → `vorlage.freigeben`); weitere Rechte mit
  derselben Herkunft bilden ab, was das Häkchen heute sonst noch erlaubt (`can_edit_meetings` → auch
  `sitzung.laden`, `umlauf.anlegen` …). Rechte ohne heutiges Häkchen (z. B. `tagesordnung.benehmen_erklaeren`,
  `konto.rechteauskunft`) hat bis zur Rollenmatrix nur die Administrator-Rolle; die zugehörigen Funktionen
  gibt es noch nicht.
- Die Herkunft folgt der heutigen Prüfstelle, nicht dem Namen des Häkchens: Die Rücknahme einer Vorlage
  (`vorlage.zurueckziehen`) folgt aus `can_edit_papers`, die Absage einer Sitzung (`sitzung.absagen`) aus
  `can_edit_meetings` – beides geht heute im Bearbeiten-Formular. `sitzung.loeschen` folgt aus
  `can_delete_meetings`. Ein Test ordnet jede Prüfstelle der Views (`permission_required`, `ACTION_PERMS`)
  Katalogrechten zu, deren Herkunft genau das geprüfte Häkchen ist. So verliert keine Rolle eine Funktion,
  wenn die Prüfstellen umgestellt werden.
- Bis zur Rollenmatrix (#775) folgen die Rechte einer Rolle aus ihren Häkchen. Danach speichert die Rolle
  Katalogrechte, und die Häkchen werden daraus abgeleitet, bis sie entfallen.

**Geltungsbereiche**

- Arten: Mandant (leer), Körperschaft, Gremium, Amt.
- Zwei Bäume je Mandant: **Körperschaft → Gremien** (eine Körperschaft umfasst ihre Gremien, nie eine andere
  Körperschaft, auch nicht die Mitgliedsgemeinden einer Samtgemeinde) und **Verwaltungsaufbau** (ein Amt
  umfasst die Ämter und Sachgebiete darunter; das vorhandene Feld `parent` der Gremien vom Typ
  „Amt/Fachbereich“ wird als Baum ausgewertet). Ein Gremium umfasst nur sich selbst.
- Ein Objekt hat einen oder mehrere Geltungsbereiche: Sitzung, TOP und Niederschrift das Gremium (bei
  gemeinsamer Sitzung alle beteiligten) und dessen Körperschaft; Vorlage die Körperschaft, das federführende
  Amt und – nur zum Lesen – die Gremien der Beratungsfolge; Antrag/Anfrage Körperschaft, Zielgremium und
  zugewiesenes Amt; Person die Körperschaften ihrer Mitgliedschaften; Sitzungsgeld die Körperschaft.
- Eine Zuweisung wirkt auf ein Objekt, wenn einer seiner Geltungsbereiche im Teilbaum der Zuweisung liegt.
  Unbekannte oder gelöschte Bereiche wirken nicht.

**Zuweisung**

- `SessionRoleAssignment`: Mandant, Konto (`SessionUser`), Rolle, Art und Kennung des Geltungsbereichs,
  gültig von/bis (einschließlich, leer = offen), Quelle (manuell, Mandat, Stelle, Vertretung, Migration),
  Vermerk, angelegt von/am, aufgehoben von/am.
- Zuweisungen sind bis auf die Aufhebung unveränderlich; eine Änderung ist Aufhebung plus neue Zuweisung.
  Aufgehobene Zuweisungen bleiben als Nachweis.
- Administrator-Rollen werden vorerst nur mandantenweit und unbefristet zugewiesen.
- Eine Rolle zu löschen löscht bis zur Rollenmatrix auch ihre Zuweisungen, aufgehobene eingeschlossen; die
  Rollenänderung selbst steht im Prüfprotokoll. Mit der Rollenmatrix (#775) werden Rollen mit Zuweisungen
  deaktiviert statt gelöscht, damit der Nachweis bleibt.

**Zugriffskontext**

- Einmal je Anfrage: für jedes Recht „mandantenweit“ oder die Mengen der Körperschaften, Gremien und Ämter, in
  denen es gilt (Teilbäume aufgelöst). Prüfung `darf(person, aktion, objekt)` und Listenfilter als
  Q-Ausdrücke (#773) lesen nur diesen Kontext; keine Abfrage je Objekt.

**Übergang ohne Verhaltensänderung (#772)**

- `SessionUser.roles` bleibt bis zum Entfernen der Häkchen die maßgebliche Quelle für mandantenweite,
  unbefristete Rollen. Zu jedem solchen Paar gibt es eine gespiegelte Zuweisung (Quelle Migration bzw.
  manuell): angelegt per Datenmigration, nachgeführt bei jeder Änderung der Rollen und bei jedem `migrate`
  (Änderungen eines älteren Images). Höchstens eine aktive Spiegelzuweisung je Konto und Rolle
  (Datenbankregel). Der Abgleich vergleicht Rollen und Spiegel in je einer Abfrage; in PostgreSQL warten
  Rollenänderungen, bis er fertig ist.
- Befristete Zuweisungen und Zuweisungen mit Geltungsbereich gehen nur in den Zugriffskontext ein, wenn der
  Mandant den Schalter „Rechte mit Geltungsbereich“ eingeschaltet hat (Standard aus). Ohne Schalter gilt genau
  das heutige Modell.
- Verträgliche Prüfschicht: `SessionPermissionChecker` und `visible_to()` arbeiten bis zur Umstellung (#773)
  mit den bisherigen Rechtenamen. Diese werden aus den Spiegelzuweisungen (also aus `SessionUser.roles`) über
  den Katalog abgeleitet: Ein Häkchen gilt, wenn sein Leitrecht mandantenweit gilt. Befristete Zuweisungen und
  Zuweisungen mit Geltungsbereich gewähren über die alten Namen nichts; sie wirken erst über die zentrale
  Prüfung. So bleibt die Abfragezahl je Seite gleich, und keine Prüfstelle sieht ein Recht, dessen
  Geltungsbereich sie nicht auswerten kann.
- Nachweis: Die Sicherheitsmatrix (`test_security_matrix.py`, `test_noe_sichtbarkeit.py`,
  `test_funktionstrennung.py`, `test_rechtevergabe.py`) läuft vor und nach der Umstellung unverändert grün;
  Äquivalenztests vergleichen alte und neue Auflösung für jede Standardrolle und jedes Häkchen.
- Abwärtskompatibel: neue Tabelle, Schalter mit Datenbank-Standardwert. Die Fremdschlüssel der Zuweisung
  löschen in PostgreSQL selbst mit (`ON DELETE CASCADE` bzw. `SET NULL`), damit ein älteres Image, das die
  Tabelle nicht kennt, Konten, Rollen und Mandanten weiter löschen kann. Djangos `DB_CASCADE` und
  `DB_SET_NULL` gehen hier nicht: Konto, Rolle und Mandant verweisen selbst mit Python-Löschregeln weiter, und
  gemischte Ketten verbietet Django (Systemprüfung `fields.E323`). Die Regeln setzt deshalb die Migration; ein
  PostgreSQL-Test in der CI prüft sie.
- Die Häkchen-Spalten entfallen zwei Releases nach der Umstellung der Prüfstellen und der Rollenmatrix
  (eigenes Issue); bis dahin ist die Migration umkehrbar.
- Vertretungen (`SessionDelegation`) und Amtszuordnungen der Mitzeichnung bleiben vorerst eigene Modelle. Die
  Amtszuordnungen werden mit der Rollenmatrix (#775) Zuweisungen mit Geltungsbereich Amt; Vertretungen
  bekommen Geltungsbereich und Stationsarten mit dem Ablauf-Regelwerk. Delegierbar bleiben nur
  Stationsrechte, nie Verwaltungs- und Kontrollrechte.

**Stufe „vertraulich“ (#774)**

- Drei Stufen je Vorlage, TOP und Anlage: öffentlich, nichtöffentlich, vertraulich. Vertraulich heißt Zugriff
  nur für ausdrücklich benannte Ämter, Rollen bzw. Gremienmitglieder; kein anderes Recht hebt das auf.
- Getrennt vom Merkmal „geheimhaltungspflichtig“ (#754), das allein die Beratung in Hybrid- und Videositzungen
  sperrt.

**Ereignisse**

- **`session.grant.changed`** (Eigentümer `apps.session`, Sichtbarkeit `personenbezogen`, nur Kennungen):
  Eine Zuweisung hat begonnen, ist abgelaufen oder wurde aufgehoben – manuell vergeben oder aus der Besetzung
  abgeleitet (Mandatsrechte, #761). Felder: Zuweisung, Mandant, Konto, Rolle, Art und Kennung des
  Geltungsbereichs, Quelle, Art der Änderung (`granted`, `expired`, `revoked`) und Ende der Gültigkeit. Ein
  Ereignis je Zuweisung, nicht je Recht. Vertrag und erste Veröffentlichung kommen mit #775; der tägliche Lauf
  der Mandatsrechte meldet abgelaufene Zuweisungen.
- **`core.membership.changed`** (besteht, Eigentümer `apps.tenants`) bleibt das Ereignis für Mitgliedschaften
  in Work.
- Die Projektion „Profil Mandat“ der Datendrehscheibe leitet den Entzug des Zugriffs (`accessRevoked`) aus
  beiden ab: aus `session.grant.changed` für Mandatsrechte, aus `core.membership.changed` für
  Work-Mitgliedschaften. Andere Namen (`session.mandate.*`, `core.grant.*`) verwenden wir nicht.

**Protokoll und Auskunft**

- Jede Zuweisung, jedes abgeleitete Recht (Beginn und Ende) und jede Vertretung steht mit Anlass in der
  Hash-Kette des Mandanten (#775). Die Spiegelung im Übergang schreibt keine eigenen Einträge; die
  Rollenänderung selbst protokolliert wie bisher die Oberfläche.
- Die Rechteauskunft (#776) liest denselben Zugriffskontext wie die Prüfung und nennt den Grund (Zuweisung,
  Mandat, Vertretung).

## Alternativen

- **Häkchen behalten, nur Geltungsbereich an der Zuweisung und Stufe „vertraulich“** (E3 B). Kleiner, aber
  Stationsrechte und Rechteauskunft bleiben grob: „Sitzungen bearbeiten“ deckt heute Laden, Umlaufverfahren,
  Beschlusskontrolle und Terminieren zugleich. Verworfen.
- **Gleich in der Plattform für Work und Session** (E3 C). Größer, und ohne Work als zweiten Nutzer entstünde
  ein drittes System neben den beiden heutigen. Verworfen; Hebung später.
- **Rechte als Datenbanktabelle.** Rechte ohne prüfenden Code wären wirkungslos; Katalog und Prüfung gehören
  zusammen in eine Änderung. Verworfen.
- **Zuweisungen sofort als einzige Quelle, `SessionUser.roles` abgelöst.** Ein älteres Image läse nach einem
  Rückfall veraltete Rollen; Rechte, die das neue Image entzogen hat, wären wieder da. Verworfen bis zum
  Entfernen der Häkchen.
- **Objektrechte je Datensatz** (Zeilen je Objekt und Person, etwa Django-Objektrechte). Keine Bäume,
  Abfragekosten wachsen mit den Objekten, keine Erklärung „warum“. Verworfen.
- **Django-Berechtigungen (`auth.Permission`).** Global statt je Mandant und ohne Geltungsbereich. Verworfen.

## Folgen

**Positiv**

- Rechte je Körperschaft, Gremium und Amt, befristet und mit Herkunft; Grundlage für Stationsrechte (#760),
  Mandatsrechte (#761) und Rechteauskunft (#776).
- Bestandsmandanten merken nichts; der Schalter macht Geltungsbereiche je Mandant zuschaltbar.
- Ein Ereignisname für Mandatsrechte in Session und Datendrehscheibe.

**Negativ**

- Bis zum Entfernen der Häkchen gibt es zwei Darstellungen derselben Rollenrechte (Häkchen und Katalog) und
  eine Spiegelung `SessionUser.roles` ↔ Zuweisung, die nachgeführt werden muss.
- Bis zur Umstellung der Prüfstellen (#773) wirken befristete Zuweisungen und Zuweisungen mit Geltungsbereich
  nur im Zugriffskontext, noch nicht in Oberfläche und API.
- Die Datenbankregeln für das Mitlöschen stehen außerhalb der Django-Felddefinition; ändert eine spätere
  Migration diese Fremdschlüssel, muss sie die Regeln neu setzen (der PostgreSQL-Test schlägt sonst fehl).

## Prüfung (Fitnessfunktion)

- Katalogtest: gültige Kennungen, jedes Häkchen hat genau ein Leitrecht, jedes Recht höchstens eine Herkunft,
  Kontrollrechte nicht in der Administrator-Vollmacht.
- Äquivalenztests alt gegen neu für jede Standardrolle, jedes einzelne Häkchen und zufällige Kombinationen.
- Zuordnungstest: Jede Prüfstelle der Views entspricht Katalogrechten, deren Herkunft genau das geprüfte
  Häkchen ist; jedes Recht mit Herkunft hat eine Prüfstelle oder einen benannten Grund, Rechte ohne Herkunft
  entsprechen keiner heutigen Stelle.
- Sicherheitsmatrix vor und nach jeder Stufe unverändert grün; Abfragezahl-Tests unverändert.
- Migrationstest: alter Stand → Konten mit Rollen → weitermigrieren → je Konto und Rolle genau eine
  Spiegelzuweisung; zweiter Abgleich ändert nichts; Rückmigration möglich.
- Ab #773: Prüfskript zählt direkte Ö/NÖ- und Rechteprüfungen außerhalb der zentralen Stelle; die Zahl sinkt
  nur.

## Bezug

- Issues #753 (Epic), #759 mit den Teilen #772 (Katalog, Zuweisungen, Migration), #773 (zentrale Prüfung),
  #774 („vertraulich“), #775 (Rollenmatrix, Ereignis), #776 (Rechteauskunft); #760 (Stationsrechte), #761
  (Mandatsrechte), #221 (Funktionstrennung), #222 (Vertretung), #754 (geheimhaltungspflichtig)
- [Ereignisverträge](20260929-ereignisvertraege.md): Namensregeln und Sichtbarkeitsklassen
- [Körperschaften im Mandanten](20261002-koerperschaften-im-mandanten.md): Geltungsbereich „Körperschaft“
