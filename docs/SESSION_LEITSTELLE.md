# Leitstelle für Mandantengruppen und gemeinsame Sitzungen (Session)

Stand: 09/2026 · Issue #317 (Teil B)

Große Verwaltungen betreiben ihr Ratsinformationssystem für mehrere Körperschaften zugleich, etwa
die Bezirksversammlungen eines Stadtstaats. Jede Körperschaft ist in mandari ein eigener
Session-Mandant mit eigenen Gremien, eigenem Schlüssel und eigenem Nummernkreis. Eine
**Mandantengruppe** fasst diese Mandanten für eine zentrale **Leitstelle** zusammen. Die Leitstelle
sieht die Arbeitsvorräte aller Mandanten der Gruppe auf einer Seite, ohne in jedem Mandanten eine
Rolle zu brauchen. Schlüssel und Ö/NÖ-Rechte bleiben je Mandant getrennt.

Außerdem kann eine Sitzung von **mehreren Gremien desselben Mandanten** gemeinsam abgehalten werden,
etwa von Bau- und Umweltausschuss.

## Mandantengruppe einrichten

Superuser bzw. Staff mit den Modellrechten pflegen die Gruppe im Django-Admin unter
**Session → Mandantengruppen**:

| Feld | Bedeutung |
|---|---|
| Name, URL-Kürzel | Die Übersicht liegt unter `/session/leitstelle/<URL-Kürzel>/`. |
| Mandanten der Gruppe | Ein Mandant gehört **höchstens einer** Gruppe an. |
| Leitstelle | Konten mit Gruppenrolle, Vermerk und Schalter „Aktiv“. |

Gruppenrollen:

| Gruppenrolle | Sieht |
|---|---|
| **Leitstelle** | Kennzahlen, Vorlagen in Prüfung, offene Mitzeichnungen, Ladungs- und Vorlagenfristen, nächste Sitzungen und die Suche über alle Mandanten |
| **Kennzahlen** | nur die Zählwerte je Mandant, keine Listen und keine Suche |

Warum höchstens eine Gruppe je Mandant: So bleibt für jeden Mandanten eindeutig, welche Leitstelle
seine Übersicht sieht. Die Zahl der Personen mit mandantenübergreifender Sicht wächst nicht unbemerkt
über mehrere Gruppen. Braucht es später Überschneidungen, lässt sich die Zuordnungstabelle ohne
Umbau lockern.

Der Slug `leitstelle` ist für Mandanten reserviert (`/session/leitstelle/…`, siehe
`apps/session/middleware.py`, `RESERVED_SLUGS`).

## Die Leitstellen-Übersicht

Mitglieder einer Leitstelle finden in der Seitenleiste jedes Mandanten den Eintrag **Leitstelle …**.
Die Übersicht zeigt:

- **Kennzahlen je Mandant**: Vorlagen in Prüfung (davon nichtöffentlich), offene Mitzeichnungen,
  Ladungsfristen, Vorlagenfristen, Sitzungen der nächsten 28 Tage und im laufenden Jahr, Beschlüsse
  im Jahr und überfällige Beschlusskontrollen. Die Zählwerte schließen Nichtöffentliches ein.
- **Vorlagen in Prüfung**: die ältesten zuerst.
- **Offene Mitzeichnungen**: Stationen von Vorlagen in Prüfung.
- **Ladungsfristen**: Sitzungen ohne versandte Ladung, deren Frist verstrichen ist oder in 14 Tagen
  abläuft. Bei gemeinsamen Sitzungen gilt die längste Ladungsfrist der beteiligten Gremien.
- **Vorlagenfristen**: offene Vorlagen (Entwurf, in Prüfung) mit Frist, verstrichen oder in 14 Tagen.
- **Nächste Sitzungen** der kommenden 28 Tage.

Jede Liste zeigt höchstens 15 Einträge über alle Mandanten, dazu die Gesamtzahl.

Die **Suche** durchsucht Betreff und Nummer der Vorlagen, den Namen der Sitzungen sowie Betreff und
Beschlussnummer der Tagesordnungspunkte in allen Mandanten der Gruppe. Sie ist eine einfache
Datenbanksuche mit höchstens 25 Treffern je Art.

## Sichtbarkeit

Die Regeln gelten je Mandant und hängen nur an der **eigenen Mitgliedschaft** im Mandanten, nie an
der Gruppenrolle:

| Inhalt | ohne Mitgliedschaft im Mandanten | Mitglied ohne NÖ-Recht | Mitglied mit NÖ-Recht |
|---|---|---|---|
| öffentliche Vorlage, Sitzung, TOP | Titel ohne Link („kein Zugang“) | Titel mit Link | Titel mit Link |
| nichtöffentliche Vorlage, Sitzung, TOP | „Nichtöffentlich“ ohne Titel, Nummer und Gremium | wie links | Titel mit Link |
| nichtöffentliche Treffer der Suche | nicht gefunden, auch nicht gezählt | nicht gefunden | gefunden |
| verschlüsselte Felder | nie geladen, nie entschlüsselt | ebenso | ebenso |

Maßgeblich sind `view_non_public_papers` für Vorlagen und `view_non_public_meetings` für
Sitzungen und TOPs. Ein Link erscheint nur, wenn die Person im Mandanten Mitglied ist und das Recht
für die Zielseite hat (`view_papers`, `view_meetings`, `view_dashboard`). Die Suche fragt
nichtöffentliche Vorgänge ohne Recht gar nicht erst ab: Schon eine Trefferzahl verriete, dass ein
nichtöffentlicher Vorgang den Suchbegriff enthält.

**Entscheidung: Die Gruppenrolle gewährt keinen Lesezugriff auf Mandantenseiten.** Auch öffentliche
Vorgänge eines Mandanten öffnet die Leitstelle nur mit eigener Mitgliedschaft. Die Übersicht ist das
Werkzeug zum Steuern und Nachfassen; gearbeitet wird im Mandanten mit echter Rolle. So bleibt die
Rechteverwaltung je Mandant vollständig: Wer in einem Bezirk Vorgänge lesen oder bearbeiten soll,
steht in dessen Benutzerverwaltung, dessen Protokoll und dessen Zwei-Faktor-Pflicht. Die
Mandantenseiten prüfen weiterhin nur die Mitgliedschaft (`SessionMixin`); die Gruppe kommt dort nicht
vor.

Weitere Sicherungen:

- Ohne aktive Mitgliedschaft in einer aktiven Leitstelle antwortet die Übersicht mit **404**, auch
  für Superuser – ohne Auskunft, ob es die Gruppe gibt.
- Mitglieder einer Leitstelle brauchen einen **zweiten Faktor** (`two_factor_policy`).
- Erteilen, Ändern und Entziehen von Leitstellen-Zugängen sowie das Zuordnen und Entfernen von
  Mandanten stehen im Protokoll jedes betroffenen Mandanten („Rechte geändert“), mit Konto,
  Gruppenrolle und – bei Änderungen im Admin – dem ändernden Konto und der IP-Adresse.

## Protokoll

Jede Nutzung der Übersicht ist ein **Lesezugriff im Protokoll jedes Mandanten** der Gruppe
(`audit.log_read`, Objekt „Leitstellen-Übersicht <Gruppe>“):

- Je Mandant ein Eintrag, weil jeder Mandant seine eigene, manipulationsgeschützte Hash-Kette hat
  und seine Revision dort sehen muss, wer seine Daten über die Leitstelle gesehen hat. Ein einziger
  Eintrag „bei der Gruppe“ wäre für die Mandanten unsichtbar.
- Zusammengefasst: je Person, Mandant und Ansicht (Übersicht bzw. Suche) höchstens ein Eintrag in
  zehn Minuten.
- Unabhängig vom Schalter „Lesezugriffe protokollieren“ des Mandanten, weil der Eintrag einen
  mandantenübergreifenden Zugriff belegt.
- Inhalt: Gruppe, Ansicht, Gruppenrolle, ob die Person Mitglied im Mandanten ist, ob sie
  nichtöffentliche Titel sehen durfte, bei der Suche die Zahl der Treffer im Mandanten. Ohne
  Mitgliedschaft fehlt der Nutzerbezug im Eintrag; dann steht das Konto (E-Mail) in den Angaben.
- Suchbegriffe werden nicht gespeichert; sie können selbst personenbezogen sein.

## Leistung

Die Zahl der Datenbankabfragen hängt nicht von der Zahl der Mandanten ab: Die Kennzahlen sind je
eine gruppierte Abfrage über alle Mandanten, die Listen je eine Abfrage mit Obergrenze. Nur das
Protokoll schreibt je Mandant, und das höchstens einmal in zehn Minuten. Das Performance-Budget
`session_leitstelle` (`scripts/performance_budgets.json`) sichert die Zahl der Abfragen ab.

## Gemeinsame Sitzung mehrerer Gremien

Eine Sitzung hat ein **federführendes Gremium** und optional **weitere beteiligte Gremien** desselben
Mandanten (Sitzung anlegen oder bearbeiten → „Weitere beteiligte Gremien“).

| Bereich | Wirkung |
|---|---|
| Anzeige | Sitzungsdetail, Sitzungsliste, Kalender, Ladungs-PDF, Serienbrief, Ladungsnachweis, Niederschrift und Sitzungsmappe nennen alle Gremien („gemeinsame Sitzung“). |
| Ladung | Empfänger sind die Mitglieder aller beteiligten Gremien, jede Person genau einmal. Die vollständige Tagesordnung erhält, wer in einem der Gremien mehr als Gast ist. |
| Ladungsfrist | die längste Frist der beteiligten Gremien |
| Anwesenheit | eine Zeile je Person. Stimmberechtigt ist, wer in einem der Gremien Stimmrecht hat. Funktion und Vertretungshinweis kommen aus der Mitgliedschaft im federführenden Gremium, sonst aus der stimmberechtigten. |
| Abstimmung | Einzelstimmen sind je TOP und Person eindeutig; die Stimmrechtsprüfung (Issue #318) zählt die stimmberechtigten Anwesenden, also jede Person einmal. |
| Stellvertretung | Stellvertretungen aus allen beteiligten Gremien, jede Person einmal. |
| Gremienfilter | Sitzungsliste, Suche, Sitzungsplan, ICS-Feed des Gremiums und Gremienseite zeigen auch die gemeinsamen Sitzungen, an denen das Gremium beteiligt ist. |
| Beratungsfolge | Eine Beratungsstation eines beteiligten Gremiums kann auf die gemeinsame Sitzung terminiert werden. |
| OParl | `Meeting.organization` enthält alle Gremien, das federführende zuerst (OParl 1.1 erlaubt mehrere). |
| Session-API v1 | `joint_organizations` in der Sitzungsliste (leer bei einem Gremium). |

Schutz: Weitere Gremien müssen zum Mandanten der Sitzung gehören und dürfen nicht das
federführende Gremium sein. Das prüft nicht nur das Formular, sondern auch die Zuordnung selbst
(`m2m_changed`). Jede Änderung steht im Protokoll und setzt `updated_at` der Sitzung, damit
OParl-Clients sie beim inkrementellen Abgleich erhalten.

Nicht abgebildet: getrennte Beschlussfähigkeit und getrennte Abstimmung je Gremium. Die gemeinsame
Sitzung zählt jede Person einmal; Sitzungsgeld richtet sich nach den Sätzen des federführenden
Gremiums, und die Niederschrift genehmigt die Folgesitzung des federführenden Gremiums.

## Demo

`python manage.py setup_demo_praesentation --profil hamburg` legt die Gruppe „Bezirke Musterstadt
(Demo)“ mit beiden Demo-Bezirken und der Leitstelle an, dazu eine Vorlage in Prüfung in Bezirk B und
eine gemeinsame Sitzung von Haupt- und Bauausschuss in Bezirk A. Ein Mitglied sitzt in beiden
Ausschüssen und steht im Empfängerkreis nur einmal. Das Profil `nrw` und `--reset` entfernen Gruppe,
Vorlage und gemeinsame Sitzung wieder.

## Technik

| Baustein | Ort |
|---|---|
| Modelle | `SessionTenantGroup`, `SessionTenantGroupTenant`, `SessionTenantGroupMembership`, `SessionMeeting.joint_organizations` (Migration `0035`) |
| Übersicht, Suche, Protokoll | `apps/session/services/leitstelle_service.py`, `apps/session/views/leitstelle.py` |
| Gemeinsame Sitzung | `apps/session/services/joint_meeting_service.py` |
| URLs | `/session/leitstelle/<slug>/`, `/session/leitstelle/<slug>/suche/` |
| Tests | `apps/session/tests/test_leitstelle.py`, `apps/session/tests/test_gemeinsame_sitzung.py` |
