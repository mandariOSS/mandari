# Session: Nummernkreise, Vier-Augen-Prinzip, Sitzungscockpit, Erinnerungen, Beschlusskontrolle

Implementierungsnotizen zu Funktionen des Sitzungsdienstes (mandari Session). Mandanten, Körperschaften und
Leitstelle stehen in [SESSION_MANDANTEN.md](SESSION_MANDANTEN.md), Umstieg und Stammdaten in
[SESSION_STAMMDATEN_IMPORT.md](SESSION_STAMMDATEN_IMPORT.md), Sitzungsformate in
[SESSION_SITZUNGSFORMAT_LANDESRECHT.md](SESSION_SITZUNGSFORMAT_LANDESRECHT.md).

## 1. Nummernkreise für Vorlagen und Drucksachen

Issue #150 (Teil Nummernkreise), Körperschaften: #756.

Jede Vorlage bekommt ihre Nummer automatisch aus einem **Nummernkreis** des Mandanten. Die
Nummer ist eindeutig, wird nach der Vergabe nicht mehr verändert und nie ein zweites Mal
vergeben. Einstellungen → **Nummernkreise** legt fest, wie sie aussieht und wann sie entsteht.

### Begriffe der Nummernkreise

| Begriff | Bedeutung |
|---|---|
| **Vorlagen- bzw. Drucksachennummer** | Öffentliche Nummer einer Vorlage (`reference`, OParl `reference`). Die Bezeichnung ist je Mandant einstellbar: „Drucksache“ (Bezirksversammlungen), „Vorlagen-Nr.“ (NRW-Kommunen). |
| **Eingangsnummer** | Interne Nummer eines Antrags beim Eingang (`A/<Jahr>/0001`), unabhängig von der Drucksachennummer. |
| **Unternummer** | Ergänzung, Neufassung, Änderungsantrag, Antwort oder Beschlussempfehlung zu einer Vorlage, z. B. `22-0593.1` oder `V/0599/2026/1`. |
| **Zählerbereich** | Wann der Zähler neu beginnt: jährlich, je Wahlperiode oder nie. |

### Muster

| Platzhalter | Wert |
|---|---|
| `{lfd}` / `{lfd:4}` | laufende Nummer, mit `:4` auf vier Stellen aufgefüllt |
| `{jahr}` / `{jj}` | Jahr der Vergabe, vier- bzw. zweistellig |
| `{wp}` | Nummer der Wahlperiode (Einstellungen → Wahlperioden, Feld „Nr.“) |
| `{prefix}` | Präfix des Kreises, z. B. `AN` |
| `{gremium}` | Kurzname des federführenden Gremiums (eigener Zähler je Gremium) |
| `{koerperschaft}` | Kurzname der Körperschaft der Vorlage (eigener Zähler je Körperschaft) |

Unternummern: `{parent}` (Nummer der Bezugsvorlage) und `{sub}` bzw. `{sub:2}`.

Das Muster muss den Zählerbereich enthalten – jährlich also `{jahr}` oder `{jj}`, je
Wahlperiode `{wp}`. Sonst entstünden in jedem Jahr dieselben Nummern; die Einstellungsseite
lehnt solche Muster ab.

### Mehrere Körperschaften in einem Mandanten

Ein Nummernkreis gilt für alle Körperschaften des Mandanten oder für eine (`SessionNumberRange.body`). Für eine
Vorlage gilt zuerst ein Kreis ihrer Körperschaft, dann der Kreis für alle; innerhalb davon der Kreis der
Vorlagenart vor dem für alle Arten. Kreise anderer Körperschaften gelten nie. Vorlagennummern bleiben je Mandant
eindeutig: Gleiche Muster in zwei Körperschaften brauchen `{koerperschaft}` oder ein eigenes Präfix, sonst
überspringt die Vergabe belegte Nummern. Presets verwalten nur die Kreise für alle Körperschaften. Hintergrund:
[ADR Körperschaften im Mandanten](adr/20261002-koerperschaften-im-mandanten.md).

### Presets für verbreitete Praxis

Recherchiert an öffentlichen Ratsinformationssystemen (Stand 09/2026):

| Preset | Beispiel | Zählerbereich |
|---|---|---|
| Bezirksversammlung (Stadtstaat) | `22-0593`, Unternummer `22-0593.1`; ein Zähler je Bezirk über alle Drucksachenarten | Wahlperiode |
| Verwaltung und Politik getrennt (z. B. Köln) | `1344/2026`, `AN/1492/2026`, Fortschreibung `/1` | Jahr |
| Präfix je Vorlagenart (z. B. Münster) | `V/0599/2026`, `A/0012/2026`, `AF/0003/2026` | Jahr |
| VO mit zweistelligem Jahr (z. B. Wuppertal) | `VO/1044/26`, `VO/1044/26/1` | Jahr |
| Fünfstellig mit Jahr (z. B. Dortmund) | `22957-26`, `22957-26-E1` | Jahr |
| Kleine Gemeinde | `126/2026` | Jahr |
| Standard | `V/2026/0001` | Jahr |

Mehrere Bezirke einer Stadt können je ein eigener Mandant mit eigenem Zähler sein – so wie in
einem Stadtstaat jede Bezirksversammlung ihre Drucksachen unabhängig nummeriert. Liegen mehrere Körperschaften
in einem Mandanten, gelten die Regeln oben.

### Zeitpunkt der Vergabe

- **Beim Anlegen:** Die Nummer entsteht mit dem ersten Speichern (Anträge der Politik,
  Drucksachen der Bezirksversammlungen).
- **Bei der Freigabe:** Entwürfe bleiben ohne Nummer („Nummer folgt“) und erhalten sie erst mit
  der Freigabe. Verworfene Entwürfe hinterlassen keine Lücken.

Ein Antrag, den die Verwaltung in eine Vorlage umwandelt, gilt als angenommene Drucksache und
bekommt sofort seine Nummer aus dem Kreis seiner Art (z. B. `AN/…`).

### Nummern beim Umstieg aus einem Altsystem

1. Preset wählen und bei Mustern mit Wahlperiode (`{wp}`) die Wahlperiode mit Nummer pflegen.
2. Beim Kreis „Nächste laufende Nummer setzen“: zuletzt im Altsystem vergebene Nummer + 1
   (z. B. `2615`, wenn dort bis `22-2614` vergeben ist). Der Zähler lässt sich nur erhöhen.
3. Altbestände mit ihrer Originalnummer übernehmen: Nutzer mit Einstellungsrecht dürfen beim
   Anlegen eine Nummer von Hand setzen. Kollidiert eine automatisch gebildete Nummer mit dem
   Altbestand, wird sie übersprungen.

### Garantien und technische Umsetzung

- Eindeutig je Mandant (Datenbank-Constraint, auch für Unternummern je Bezugsvorlage).
- Atomar: Zeilensperre auf dem Zählerstand (`SessionNumberCounter`), Vergabe in derselben
  Transaktion wie das Speichern der Vorlage – scheitert das Speichern, verfällt keine Nummer.
- Jahreswechsel nach deutscher Ortszeit, nicht UTC.
- Ein Kreis, aus dem schon Nummern vergeben wurden, behält Muster und Zählerbereich; für ein
  neues Schema wird ein neuer Kreis angelegt und der alte deaktiviert (nie gelöscht).
- Code: `apps/session/services/numbering_service.py`, Tests: `apps/session/tests/test_numbering.py`.

## 2. Vier-Augen-Prinzip und Vertretungen

Issue #222 (Teil von #217).

Freigaben und Genehmigungen lassen sich organisatorisch trennen: Wer einen Vorgang erstellt oder
zuletzt inhaltlich bearbeitet hat, gibt ihn nicht selbst frei. Für Abwesenheiten tragen
Administratoren Vertretungen ein, die nur im Zeitraum wirken und nie mehr erlauben als die
vertretene Person darf.

### Vier-Augen-Prinzip

Einstellungen → **Vier-Augen-Prinzip & Vertretungen** (Recht `manage_settings`), je Mandant und
Vorgangsart:

| Vorgangsart | „Selbst“ (darf nicht freigeben) | Standard |
|---|---|---|
| Vorlagenfreigabe | Ersteller:in und letzte:r inhaltliche:r Bearbeiter:in (Betreff, Art, Sachverhalt, Beschlussvorschlag, vertraulicher Teil, finanzielle Auswirkungen, Anlagen) | aus; wahlweise „nur bei finanziellen Auswirkungen“ oder „immer“ |
| Genehmigung der Niederschrift | Ersteller:in und letzte:r inhaltliche:r Bearbeiter:in (allgemeiner Teil, Unterschriften, Protokoll der TOPs, Beschlussergebnisse) | aus |
| Sitzungsgeld und Pauschalen | wer den Abrechnungs- bzw. Monatslauf erzeugt hat | **an** |
| Übergabe von Beschlussauszügen | Ersteller:in und letzte:r inhaltliche:r Bearbeiter:in der Niederschrift der Sitzung | aus |

Berichtigungen einer genehmigten Niederschrift (Issue #318) folgen der Einstellung für die
Niederschrift: Ist sie an, wird eine Berichtigung erst wirksam, wenn eine zweite Person mit
`approve_protocols` sie bestätigt – wer sie beantragt hat, auch in Vertretung, bestätigt sie nicht
selbst. Ist sie aus, wird die Berichtigung sofort wirksam und protokolliert. Ohne
Genehmigungsschritt (Einstellung „Genehmigungsweg der Niederschrift“: direkte Veröffentlichung)
gilt das Vier-Augen-Prinzip für die Niederschrift beim Freigeben und Veröffentlichen.

Die Migration `session.0029_vier_augen_vertretung` setzt diese Standardwerte auch für bestehende
Mandanten; damit ändert sich nichts an bisherigen Abläufen (Sitzungsgeld galt schon im
Vier-Augen-Prinzip).

**Warum Ersteller:in und letzte:r Bearbeiter:in:** Wer erstellt, trägt die fachliche
Verantwortung; wer zuletzt inhaltlich geändert hat, hat genau den Stand geschaffen, der zur
Freigabe steht. Frühere Zwischenbearbeitungen sperren bewusst nicht dauerhaft – ihr Ergebnis
hat danach eine andere Person weiterbearbeitet, und sonst schlösse schon eine
Tippfehler-Korrektur die Freigabe durch diese Person für immer aus. Wer in der Prüfung selbst
ändert, gibt danach nicht mehr frei; der vorgesehene Weg ist die Zurückweisung mit Anmerkung.

Umsetzung:

- Prüfung auf Service-Ebene: `apps/session/services/four_eyes_service.py` (`evaluate`,
  `authorize`). Die Oberfläche zeigt statt des Freigabeknopfs einen Hinweis, der Server lehnt
  aber auch direkte Anfragen ab.
- Letzte inhaltliche Bearbeitung: `SessionPaper.content_edited_by`,
  `SessionProtocol.content_edited_by`, gesetzt über Signal-Hooks (`track_*`, angebunden in
  `apps/session/signals.py`) aus dem Altzustand des Audit-Logs. Speichern ohne inhaltliche
  Änderung zählt nicht. Bestehende Vorlagen haben das Feld zunächst leer; es füllt sich mit der
  nächsten Bearbeitung.
- Sitzungsgeld: `allowance_service.approve_allowances(..., four_eyes=...)` bzw.
  `approve_monthly_allowances`, Schalter aus `four_eyes_service.required`.

### Vertretungen

Benutzer → **Vertretungen** (Recht `manage_users`): vertretene Person, Vertretung (anderer
aktiver Nutzer desselben Mandanten), Zeitraum (erster bis einschließlich letzter Tag, höchstens
ein Jahr) und Umfang:

| Umfang | Wirkung |
|---|---|
| Freigaben und Genehmigungen | Vorlagen freigeben/zurückweisen, Niederschriften genehmigen/zurückweisen/veröffentlichen – soweit die vertretene Person es selbst darf (`approve_papers`, `approve_protocols`) |
| Arbeitsvorrat | Offene Mitzeichnungen der Ämter der vertretenen Person erscheinen unter „Mitzeichnungen“ und lassen sich in Vertretung erledigen |
| Benachrichtigungen | Freigabe-Aufforderungen und die Zurückweisung eigener Vorlagen gehen in Kopie an die Vertretung |

Regeln gegen Rechteausweitung (Tests: `apps/session/tests/test_vier_augen_vertretung.py`):

- Wirksam nur im Zeitraum; aufgehobene Vertretungen wirken sofort nicht mehr und bleiben als
  Nachweis in der Übersicht.
- Nur im eigenen Mandanten – auch ein unmittelbar in der Datenbank angelegter Datensatz mit
  fremder Person bleibt wirkungslos.
- Nie mehr als die vertretene Person: gerechnet aus deren eigenen Rollen
  (`permissions.role_permissions`). Sichtrechte werden nicht übertragen – eine nichtöffentliche
  Vorlage gibt in Vertretung nur frei, wer sie selbst sehen darf und wessen vertretene Person
  sie sehen darf. Bankdaten (`manage_allowances`) und Sitzungsbearbeitung (`edit_meetings`)
  sind nicht übertragbar.
- Keine Kettenvertretung: Rechte aus einer Vertretung werden nicht weitergereicht. Beim
  Eintragen weist die Oberfläche darauf hin, wenn die Vertretung im Zeitraum selbst abwesend ist.
- Das Vier-Augen-Prinzip gilt für Handelnde **und** vertretene Person.
- Wer ein Recht selbst hat, handelt im eigenen Namen.

Nachweis: `SessionAuditLog.on_behalf_of` („in Vertretung für …“ in der Audit-Log-Ansicht) für
jede Handlung aus einer Vertretung, dazu `approved_on_behalf_of` an Vorlage und Niederschrift.
Die Vertretung selbst (Eintragen, Aufheben) steht ebenfalls im Audit-Log.

Umsetzung: `apps/session/services/delegation_service.py`, Rechteprüfung in
`SessionPermissionChecker` (eine Abfrage je Anfrage, nur für Nutzer ohne die Freigaberechte),
Oberfläche `apps/session/views/approvals.py`.

### Abgrenzung

- Mitzeichnungsstationen (parallel, Fristen, Eskalation, automatische Weiterleitung bei
  Abwesenheit) gehören nicht dazu (#151).
- Fristen-Erinnerungen gehen weiterhin an alle mit dem jeweiligen Recht, nicht zusätzlich an
  Vertretungen.

## 3. Sitzungscockpit

Issue: #140. Grundlagen: #138 (Sitzungsformat, Landesprofile), #139 (Teilnahmeart, Störungen).
Nicht enthalten: Selbst-Abstimmung am eigenen Gerät (#141).

### Zweck

Sitzungsleitung und Protokollführung steuern die laufende Sitzung auf einer Seite; alle anderen mit
Sichtrecht verfolgen denselben Stand als **Mitlese-Ansicht**. Die Zeitmarken landen ohne Nacharbeit in der
Niederschrift.

Aufruf: Sitzungsseite → „Cockpit“ (mit Steuerungsrecht) bzw. „Live-Ansicht“,
`/session/<mandant>/meetings/<sitzung>/cockpit/`.

### Funktionen des Cockpits

| Bereich | Verhalten |
|---------|-----------|
| Sitzung | Eröffnen (tatsächlicher Beginn, Status „Laufend“), schließen (Ende, Status „Abgeschlossen“), fortsetzen. Beim Schließen enden der laufende TOP und andauernde Störungen mit der aktuellen Uhrzeit. |
| TOP | Aufrufen füllt `start_time`, ein neuer Aufruf beendet den laufenden TOP (`end_time`). „Weiter mit TOP …“ springt zum nächsten offenen, nicht abgesetzten TOP. Der erste Aufruf eröffnet die Sitzung. Wiederaufruf behält den Beginn. |
| Anwesenheit | Anwesend, kommt (verspätet, mit Uhrzeit), geht (vorzeitig, mit Uhrzeit), zurück, abwesend. „Zurück“ vermerkt die Unterbrechung als Zeitraum (`SessionAttendance.interruptions`), auch mehrfach; die Niederschrift nennt sie im Teilnahmevermerk („abwesend 18:30–18:50 Uhr“, bei Zugeschalteten „getrennt …“). Beschlussfähigkeit live, für den aufgerufenen TOP nach den Regeln des Landesprofils. |
| Störungsprotokoll | Störung Zugeschalteter „ab jetzt“ mit Ursache und internem Vermerk (z. B. „telefonisch gemeldet“ als zweiter Meldeweg), Ende „jetzt“. Während der Störung zählt die Person nicht zur Beschlussfähigkeit. |
| Abstimmung | Öffnen zum aufgerufenen TOP (Art, Wahl), schließen mit Ergebnis. Summen: bei „Nur Summen“ und „Geheim“ aus dem Formular (Prüfung gegen die stimmberechtigten Anwesenden; ohne eine einzige Stimme kein Ergebnis), bei „Offen“ und „Namentlich“ aus den Einzelstimmen der Abstimmungserfassung. Abbrechen ohne Ergebnis möglich. |

Während eine Abstimmung offen ist, lassen sich weder TOP wechseln noch die Sitzung schließen.
Abgesagte Sitzungen und Sitzungen mit genehmigter Niederschrift zeigt das Cockpit nur an.

### Rechte im Cockpit

- Steuern: Recht **„Sitzungen leiten (Cockpit)“** (`can_conduct_meetings`) plus Sichtrecht für Sitzungen.
  Standardrollen „Sachbearbeiter“ und „Protokollant“ haben es; Migration `session.0050_sitzungscockpit`
  gibt es allen Bestandsrollen, die Anwesenheit verwalten und Protokolle bearbeiten dürfen.
- Mitlesen: Sichtrecht für Sitzungen. Nichtöffentliche TOPs und Sitzungen nur mit dem NÖ-Recht; ohne es
  zeigt das Cockpit „Nichtöffentlicher Teil“. Interne Störungsvermerke sehen nur Steuernde.
- Steuern ohne NÖ-Recht greift nicht in den nichtöffentlichen Teil ein: Ist dort ein TOP aufgerufen, lassen
  sich weder ein anderer TOP aufrufen noch die Sitzung schließen (Meldung ohne Nummer des TOP); beenden kann
  ihn nur, wer ihn sieht.
- Der Service prüft das Steuerungsrecht selbst (`cockpit_service.perform`), die View zusätzlich.

### Echtzeit

- WebSocket `ws/session/<mandant>/cockpit/<sitzung>/` (`apps/session/consumers.py`), nur lesend. Nach jeder
  Änderung (nach dem Commit) geht an die Gruppe der Sitzung nur der Hinweis `{"type": "stand"}` – nie Inhalt.
  Auslöser sind die Modell-Signale von Sitzung, TOP, Anwesenheit, Störung und Niederschrift
  (`apps/session/signals.py`, `cockpit_service.notify_on_commit`): Änderungen über die Sitzungsseite, die
  Abstimmungserfassung oder den Admin erscheinen ebenso sofort wie Cockpit-Aktionen. Je Transaktion und
  Sitzung geht ein Hinweis hinaus.
- Jede Ansicht holt daraufhin ihren Stand per HTMX mit den eigenen Rechten
  (`…/cockpit/stand/?v=<Merkmal>`). Ist das Merkmal (`state_version`, eine Abfrage) unverändert, antwortet
  der Server mit 204.
- Rückfall ohne WebSocket: Polling alle zwei Sekunden; mit Verbindung zur Sicherheit alle 30 Sekunden.
  Alpine-Komponente `meetingCockpit` (`frontend/alpine/meeting-cockpit.ts`).
- Eingaben gehen beim Nachladen nicht verloren: Formulare im Stand tragen `data-cockpit-form`; nach der ersten
  Eingabe gelten sie als ungespeichert, und das neue Formular gleichen Schlüssels übernimmt Stimmenzahlen,
  Ergebnis, Vermerk und Auswahl. Nur das gerade abgeschickte Formular erscheint frisch. Eine abgewiesene
  Aktion tauscht den Stand nicht aus (`HX-Reswap: none`, Meldung per Toast), die Ansicht lädt danach selbst
  nach. Feste ids halten den Fokus. Während jemand in einem Textfeld tippt, wartet die Aktualisierung.

### Nebenläufigkeit

Cockpit, Abstimmungserfassung und Niederschrift schreiben dieselben TOPs, oft gleichzeitig (Leitung im
Cockpit, Protokollführung in der Erfassung bzw. Niederschrift):

- Alle drei nehmen dieselbe Zeilensperre auf der Sitzung (`cockpit_service.lock_meeting`) und laden den TOP
  erst darunter.
- Erfassung und Niederschrift speichern nur ihre Felder (`update_fields`), nie Zeiten oder
  Abstimmungszeitpunkte des Cockpits.
- Ergebnis, Art, Wahl und Summen übernehmen sie nur, wenn sie im Formular geändert wurden – verglichen mit dem
  Stand beim Laden (versteckte Felder `geladen_…`, `voting_service.form_value`). Ein im Cockpit inzwischen
  festgestelltes Ergebnis bleibt so stehen.

### Audit und Niederschrift

- Jede Aktion erzeugt Einträge über die Modell-Signale (Sitzung, TOP, Anwesenheit, Störung); das Schließen
  einer Abstimmung zusätzlich „Abstimmungsergebnis festgestellt“ mit Quelle „Sitzungscockpit“.
- Niederschrift (PDF, Ansicht, öffentliche Fassung): „Verlauf: eröffnet …, geschlossen …“ und je TOP
  „Behandelt 18:03–18:20 Uhr“, im Teilnehmerverzeichnis die Unterbrechungen. Nach der Genehmigung sind die
  TOP-Zeiten gesperrt (`protocol_lock`).
- Bestand: Verlauf und Behandlungszeiten erscheinen nur, wenn `SessionProtocol.show_timings` gesetzt ist –
  für neue Niederschriften und solche, die bei der Einführung im Entwurf oder in der Prüfung waren. Bereits
  genehmigte bzw. veröffentlichte behalten ihren genehmigten Inhalt (Migration `session.0050`, DB-Default
  `false`); der Schalter gehört nach der Genehmigung zum gesperrten Inhalt.

### Dateien des Cockpits

- Service: `apps/session/services/cockpit_service.py`
- Views: `apps/session/views/cockpit.py`, Templates `templates/session/cockpit/`
- Tests: `apps/session/tests/test_sitzungscockpit.py`, E2E `tests_e2e/test_sitzungscockpit.py`
  (zwei Browser, Wechsel binnen zwei Sekunden, Polling-Rückfall, Eingaben überstehen das Nachladen)
- Performance-Budget: `session_cockpit_stand` und `session_cockpit_stand_204` in
  `scripts/performance_budgets.json`; ein Test sichert, dass die Abfragen des Stands nicht mit Tagesordnung
  und Anwesenheitsliste wachsen.

## 4. Fristen-Erinnerungen und Erinnerung zur Ladung

Fristen-Erinnerungen (Issue #83): Der tägliche Lauf `send_session_reminders` (Zeitplan im Worker um 07:00,
`DEPLOYMENT.md`, „Geplante Aufgaben“) erinnert an Ladungs-, Vorlagen- und Erledigungsfristen der
Beschlusskontrolle und an fehlende Rückmeldungen zur Ladung. Er ist idempotent: Jede Erinnerung geht je Objekt
und Frist genau einmal hinaus (Protokoll in `session_reminder_logs`); `--dry-run` versendet nichts. Typen,
Vorlaufzeiten und An/Aus stellt jeder Mandant unter **Einstellungen → Fristen-Erinnerungen** ein; die
Beschreibung für die Verwaltung steht auf docs.mandari.de (Session → Fristen-Erinnerungen).

### Erinnerung zur Ladung (Issues #225, #619)

Pflicht ist nur die Ladung; die Anwesenheit wird vor Ort per Unterschrift nachgewiesen. Ob und
wie an Rückmeldungen erinnert wird, stellt deshalb jeder Mandant unter **Einstellungen →
Fristen-Erinnerungen → Erinnerung zur Ladung** ein:

| Einstellung | Möglichkeiten | Standard |
|-------------|---------------|----------|
| An/aus | Aus: weder täglicher Lauf noch Knopf; die Ladung selbst bleibt unberührt | an |
| Anlass | Zu- oder Absage fehlt · Empfangsbestätigung fehlt (und keine Rückmeldung) · eines von beiden fehlt | Zu- oder Absage fehlt |
| Empfängerkreis | alle Geladenen · Mitglieder der Gremien (ohne Gäste) · nur stimmberechtigte Mitglieder und ihre Vertretungen | alle Geladenen |
| Zeitpunkte | bis zu drei, z. B. „7, 2“ Tage vor der Sitzung | 5 |

**Eine Regel für beide Wege** (`apps/session/services/rsvp_reminders.py`): Der tägliche Lauf und
der Knopf „Jetzt erinnern“ in der Übersicht *Sitzung → Ladung → Rückmeldungen* erinnern dieselben
Personen. Der Knopf sendet sofort (ohne Zeitpunkte); die Übersicht nennt Anlass, Empfängerkreis
und die Zahl der zu Erinnernden. Die Mail nennt, was fehlt, und enthält den persönlichen
Rückmeldelink aus dem jüngsten Versand; Personen nur auf der Anwesenheitsliste (ohne Versand)
erhalten eine Textmail mit Verweis auf den Sitzungsdienst.

**Zeitpunkte im täglichen Lauf:** Je Person und Zeitpunkt wird höchstens einmal erinnert. Fällig
ist der kleinste Zeitpunkt, den die Sitzung erreicht hat – bei „7, 2“ ab sieben Tagen vorher die
erste, ab zwei Tagen die zweite Erinnerung; wird die Ladung erst spät versandt, geht nur die
jeweils fällige raus. Erinnerungen aus der Zeit vor den Zeitpunkten gelten für den ersten weiter.

Für die automatische Erinnerung genügt der tägliche Lauf oben; ein zusätzlicher Zeitplan ist
nicht nötig.

## 5. Beschlusskontrolle

Issue #37. Nach der Beschlussfassung dokumentiert die Verwaltung im Session-RIS je Beschluss
(`SessionAgendaItem` mit `vote_result`) den Umsetzungsstand: zuständige Stelle, Erledigungsfrist,
Status (offen / in Umsetzung / erledigt / zurückgestellt) und Erledigungsvermerk.

### Verwaltung (Session)

- Beschlussregister: `<tenant>/resolutions/` mit Ampel (offen, in Umsetzung, überfällig, erledigt),
  Filtern nach Gremium, Jahr, Ergebnis, Umsetzungsstand, CSV-Export
- Pflege je Beschluss: `<tenant>/agenda/<top>/tracking/` (Recht `edit_meetings`), Audit-Eintrag
- Wiedervorlage: Erinnerung bei nahender oder überschrittener Frist (Abschnitt 4)

### Mandatsträger (Work)

- Seite *Ratsinformation → Beschlüsse* (`/work/<org>/ris/decisions/`, Recht `ris.view`)
- Datenquelle: alle aktiven Session-Mandanten, deren `oparl_body` mit den Kommunen der
  Organisation verknüpft ist (`Organization.get_all_bodies()`)
- **Nur öffentliche Inhalte**: TOP und Sitzung müssen `is_public` sein, abgesetzte TOPs und TOPs
  ohne Ergebnis fehlen. Nicht-öffentliche Beschlüsse erscheinen weder in der Liste noch in der Suche.
- Ampel-Kacheln (offen / in Umsetzung / überfällig / erledigt), Filter nach Gremium, Jahr,
  Umsetzungsstand, „nur überfällig“ und Volltext (Betreff, Beschlusstext, Nummer, zuständiges Amt)
- Je Beschluss: Nummer, Ergebnis, Betreff, Beschlusstext, Gremium und Sitzungsdatum, Umsetzungsstand
  mit Erledigungsvermerk und Stand-Datum, zuständige Stelle, Frist (überfällig rot), Link zur
  Sitzung im RIS (wenn die Sitzung per OParl verknüpft ist)
- Ohne Session-Mandant zur Kommune zeigt die Seite einen Hinweis statt Daten.

Test: `scripts/smoke_work_decisions.py`.

### Veröffentlichung im Bürgerportal „Was wurde aus …?“

Issue #48. Bürgerinnen und Bürger sehen im Insight-Portal, was aus einem Beschluss geworden ist,
und können einzelne Beschlüsse abonnieren. Grundlage ist die Beschlusskontrolle oben.

#### Freigabe durch die Verwaltung (Opt-in)

1. **Mandant:** *Einstellungen → Bürgerportal → „Umsetzungsstand veröffentlichen“*
   (`SessionTenant.implementation_publish`, setzt `insight_publish` voraus). Audit-Eintrag.
2. **Je Beschluss:** Im Beschlussregister erhält das Tracking-Formular zwei zusätzliche Felder:
   *Öffentliche Statusmeldung* (`implementation_public_note`, Freitext ohne Interna) und
   *öffentlich zeigen* (`implementation_public`, Standard an). Der interne Erledigungsvermerk
   (`implementation_note`) wird **nie** veröffentlicht.

Sichtbar sind nur Beschlüsse, die alle Bedingungen erfüllen: Mandant aktiv und beide Schalter an,
Sitzung und TOP öffentlich, Ergebnis „angenommen“, nicht abgesetzt, Beschluss freigegeben
(`decision_tracking.is_publicly_visible`).

Hat der Mandant die Veröffentlichung im Bürgerportal beendet (Issue #618, siehe
`docs/SESSION_OPARL_API.md`), folgen die Beschlussseiten der gewählten Möglichkeit
(`decision_tracking.visibility`): als Archiv lesbar mit Hinweis, aber ohne neue Abos und ohne
E-Mails; vorübergehend abgeschaltet mit Hinweis (503); dauerhaft zurückgenommen „nicht mehr
verfügbar“ (410).

#### Seiten im Bürgerportal

- Reiter „Umsetzung“ der Beschlüsse, `/insight/beschluesse/umsetzung/`: Kacheln je Status (Umsetzung
  steht an / in Umsetzung / umgesetzt / zurückgestellt), Filter Gremium, Jahr, „nur offene“, Volltext
  (Betreff, Beschlusstext, Nummer, öffentliche Statusmeldung). Seit Issue #1270 ist „Beschlüsse“
  (`/insight/beschluesse/`, je Kommune `/insight/k/<slug>/beschluesse/`) die Liste der Entscheidungen aus
  dem RIS-Bestand und steht immer in der Navigation; den Reiter „Umsetzung“ zeigt sie erst, wenn
  mindestens ein Beschluss lesbar ist (`decision_tracking.has_public_decisions`), nicht schon mit
  eingeschaltetem Schalter. Alte Adressen der Liste mit `?status=`, `?offen=`, `?jahr=` oder `?q=` leiten
  auf den Reiter weiter. Ohne lesbare Beschlüsse zeigt der Reiter einen Hinweis, die zuletzt beratenen
  Punkte der Kommune und Wege zu Beschlüssen, Sitzungen und Vorgängen, mit `noindex, follow` (Issue #1261).
- Detail `/insight/beschluesse/<id>/`: Status-Zeitleiste beschlossen → in Umsetzung → umgesetzt
  (bzw. zurückgestellt), Beschlusstext, öffentliche Statusmeldung mit Stand-Datum, zuständige
  Stelle, geplante Umsetzung (überschritten rot), Links zu Sitzung und Vorlage (Beratungsverlauf)
  im Insight-Portal, Abo-Formular. Die aktive Kommune folgt dem Beschluss (Deep-Links aus E-Mails).

#### Abo und Benachrichtigung

- `DecisionSubscription` (E-Mail, Beschluss, Token, Double-Opt-In wie beim Insight-Digest).
  Bestätigung `/insight/beschluesse/abo/bestaetigen/<token>/`, Abmeldung `/insight/beschluesse/abo/abmelden/<token>/`.
- Signal auf `SessionAgendaItem` (`insight_core/signals.py`): ändert sich Umsetzungsstand **oder**
  öffentliche Statusmeldung eines sichtbaren Beschlusses, erhalten bestätigte Abonnent:innen eine
  E-Mail mit neuem Stand, Statusmeldung, Link zur Zeitleiste und Abmeldelink
  (`emails/decisions/status.html`). Nur interne Änderungen (Vermerk, Frist, Stelle) lösen keine Mail aus;
  ausgeblendete oder nicht mehr freigegebene Beschlüsse ebenfalls nicht.
- Absender: `INSIGHT_DIGEST_FROM_EMAIL` bzw. `DEFAULT_FROM_EMAIL`, Basis-URL `SITE_URL`.

#### Dateien des Beschluss-Trackings

- Service `insight_core/services/decision_tracking.py`, Views `insight_core/views/decisions.py`
- Templates `pages/decisions/{list,detail,subscription_status}.html`, `emails/decisions/{confirm,status}.html`
- Session: `views/settings.py` (`ImplementationPublishView`), `views/resolutions.py`, `resolutions/register.html`
- Migrationen `session/0024_beschluss_tracking_veroeffentlichung`, `insight_core/0026_decision_subscription`
- Test `scripts/smoke_insight_decisions.py`
