# Work: Registrierung, Einreichen bei der Verwaltung, neues Design

Implementierungsnotizen zu mandari Work (Arbeitsbereich für Fraktionen und Organisationen).

## 1. Registrierung und Zugangs-Mails

Implementierungsnotiz zu Einladungen, Gastzugängen, Selbstregistrierung und Freischaltung in
mandari work. Code: `apps/work/organization/emails.py` (Versand), `services.py` (Abläufe),
`selectors.py` (Anfragen, Freigebende), `apps/accounts/views.py` (`SelfRegisterView`,
`SelfRegisterConfirmView`). Vorlagen: `templates/work/organization/email/`.

### Versandweg der Zugangs-Mails

Alle Mails laufen über den Mail-Dienst `apps.common.mail` mit der Organisation, also über den Weg, den die
Organisation unter *Organisation → E-Mail-Einstellungen* gewählt hat:

- **mandari-Standardversand** (Voreinstellung; auch wenn „eigenes SMTP“ gewählt, aber kein Server
  eingetragen ist) oder
- **eigenes SMTP der Organisation** mit deren Absender.

Rückfall: Schlägt das eigene SMTP fehl und ist „Bei SMTP-Fehler auf mandari-Versand zurückfallen“
aktiv (Voreinstellung), geht die Mail über den mandari-Standardversand – SMTP-Konfiguration und
Absender aus den Site-Einstellungen, ersatzweise aus den Umgebungsvariablen. Ohne Rückfall wird der
Fehler protokolliert; Freischalten und Ablehnen zeigen ihn in der Oberfläche an.

Ausnahmen: Links, mit denen sich ein Passwort setzen oder eine Adresse bestätigen lässt (Einladung,
Bestätigungslink der Selbstregistrierung, Gastzugang für neue Konten, Passwort-Zurücksetzen), gehen
immer über mandari (`via_organization=False`). Wer sie einlöst, gilt als Inhaberin des Postfachs; den
SMTP-Server einer Organisation trägt aber deren Verwaltung selbst ein und könnte die Links mitlesen.
Antworten landen bei der Kontaktadresse der Organisation, sofern hinterlegt; Anfragen an Freigebende
antworten direkt an die anfragende Person.
Jede Mail geht an genau eine Adresse. Versandfehler brechen den Ablauf nie ab; Freischalten und
Ablehnen melden in der Oberfläche, wenn die Mail nicht rausging.

Alle Vorlagen erweitern `emails/base_email.html` (Inliner, Textfassung per html2text) und escapen
Freitext wie Einladungsnachricht oder Ablehnungsgrund.

### Selbstregistrierung

| Schritt | Ergebnis | Mail |
|---------|----------|------|
| Formular `/accounts/register/<org>/` | Konto ohne Anmeldung, keine Mitgliedschaft | Bestätigungslink (48 h gültig) an die Person |
| Link öffnen (GET) | nur Bestätigungsseite – Link-Scanner lösen nichts aus; ohne Anmeldung Hinweis und Knopf zur Anmeldung, die danach zurück zum Link führt | – |
| Bestätigen (POST), angemeldet mit dem registrierten Konto | Adresse bestätigt, Mitgliedschaft entsteht | siehe unten |
| … mit automatischer Freischaltung | Mitgliedschaft aktiv | „Willkommen“ an die Person, In-App-Hinweis „Neues Mitglied“ an Freigebende |
| … ohne automatische Freischaltung | offene Anfrage (`Membership.registration_requested_at`) | Eingangsbestätigung an die Person, Anfrage an alle mit `members.invite` (ersatzweise Eigentümer), In-App-Hinweis „Registrierungsanfrage“ |
| Freischalten | Mitgliedschaft aktiv | „Zugang freigeschaltet“ mit Link und Hinweis auf Zwei-Faktor-Pflicht |
| Ablehnen (optional mit Begründung) | Mitgliedschaft gelöscht, `User.registration_rejected_at` gesetzt; das Konto wird nach 30 Tagen ohne andere Zuordnung automatisch gelöscht (`cleanup_orphaned_accounts`, siehe Löschkonzept) | „Registrierungsanfrage“ mit Begründung und Hinweis auf die Löschfrist |

- Die erlaubten Domains gelten für die bestätigte Adresse; angemeldete Konten registrieren sich
  immer mit ihrer Kontoadresse. Konten mit bestätigter Adresse treten ohne erneuten Link bei.
- Konten aus einer Einladung gelten als bestätigt (der Einladungslink kam per Mail über mandari).
  Besteht zur eingeladenen Adresse schon ein Konto, auch in anderer Groß-/Kleinschreibung, führt der
  Link zur Anmeldung; ein zweites Konto entsteht nicht.
- Wer den Bestätigungslink nie nutzt, behält kein Konto: unbestätigte Konten werden nach 9 Tagen
  automatisch gelöscht, sofern sie nirgends zugeordnet sind (Issue #238).
- Bestätigungsmails sind je IP und Adresse pro Stunde gedrosselt
  (`SELF_REGISTER_MAILS_PER_IP_PER_HOUR`, `SELF_REGISTER_MAILS_PER_ADDRESS_PER_HOUR`).
- Der Link meldet nicht an und bestätigt allein nichts: Bestätigen kann nur, wer mit dem Konto des
  Links angemeldet ist (Passwort und gegebenenfalls zweiter Faktor). Der Link beweist die Kontrolle
  über das Postfach, die Anmeldung die über das Konto, dessen Passwort bei der Registrierung gesetzt
  wurde. Ohne Anmeldung leitet die Bestätigung zur Anmeldung und danach zurück zum Link; mit einem
  anderen Konto wird sie abgewiesen (403).
- Hat jemand anderes die Adresse registriert, übernimmt die Inhaberin das Konto über „Passwort
  vergessen“. Das ersetzt das fremde Passwort, beendet dessen Sitzungen und bestätigt die Adresse;
  danach kann sie den Link angemeldet einlösen.

### Offene Anfragen und deaktivierte Mitglieder

Offene Anfragen erkennt `selectors.pending_registrations` ausschließlich am Feld
`registration_requested_at`. Deaktivierte Mitglieder erscheinen nur unter „Deaktivierte Mitglieder“;
Freischalten und Ablehnen liefern für sie 404. Die Migration `tenants/0021_registrierungsanfrage`
übernimmt Bestandsanfragen: inaktiv, keine Gäste, nicht eingeladen und seit dem Anlegen unverändert.

### Weitere Mails

- **Einladung** (`invitation.html`): beim Einladen und erneuten Senden, immer über mandari.
- **Gastzugang** (`guest_access.html`): Passwort-Link über mandari, Dokumentenlink über den
  Versandweg der Organisation.
- **Reaktivierung** (`access_granted.html`, Variante `reactivated`): beim Reaktivieren und wenn eine
  Einladung eine inaktive Mitgliedschaft wieder aktiviert.

### Tests der Registrierung

`apps/work/organization/tests/test_registration_emails.py`, Selbstregistrierung ohne Kontoübernahme
in `apps/accounts/tests/test_login_2fa.py`.

## 2. Anträge digital bei der Verwaltung einreichen (Work → Session)

Fraktionen im Work-Portal reichen Anträge direkt beim Session-RIS ihrer Verwaltung ein und
sehen ohne Rückfrage Eingangsnummer, Bearbeitungsstatus und Beratungstermine (Issue #40).

### Ablauf der Einreichung

1. **Verwaltung** (Session): *Einstellungen → Einreichungs-Zugänge* → Token für die Fraktion
   erstellen. Der Token (64 Zeichen) wird genau einmal angezeigt und sicher übergeben.
   Technisch ist das ein `SessionAPIToken` mit `can_submit_applications`; gespeichert wird nur
   der SHA-256-Hash. Tokens lassen sich mit Ablaufdatum versehen und jederzeit zurückziehen.
2. **Fraktion** (Work): *Organisationseinstellungen → Verwaltung* → Token einfügen. Er wird
   sofort geprüft; es entsteht eine `AdministrationConnection` (Organisation ↔ Verwaltung,
   Token-Hash, Präfix). Recht: `faction.manage`.
3. **Einreichen**: Im Dokument-Editor erscheint für Mitglieder mit `motions.submit_to_ris`
   die Aktion „Bei Verwaltung einreichen“ (Kopfzeile und Sidebar „Verwaltung“; im neuen Erscheinungsbild
   Menü „Ablauf“ und Schritt „Einreichung“). Die Seite
   `documents/<id>/submit-ris/` zeigt die Dokumentvorschau und ein vorbelegtes Formular:
   Titel, Antragsart (aus Dokumenttyp/Titel geraten), Beschlussvorschlag, Begründung,
   finanzielle Auswirkungen (aus Überschriften wie „Beschlussvorschlag“, „Begründung“,
   „Finanzielle Auswirkungen“ herausgelöst), Zielgremium, Mitunterzeichnende, Dringlichkeit,
   Wunschtermin. Nach Bestätigung sendet Work den Befehl `submission.submit` über den Client
   (`hub.commands.InProcessClient`, Issue #540); geprüft wird im Session-Handler
   (`apps/session/commands.py`), derselbe wie für die APIs. Es entsteht der `SessionApplication`-Datensatz mit
   Eingangsnummer `A/<Jahr>/<Nr>`, die Quittung nennt Eingangsnummer, Eingangszeit und Inhalts-Hash. Erst
   danach hält Work seinen Stand fest: Das Dokument wechselt auf „Eingereicht“ und ist über
   `Motion.session_application` verknüpft. Der Idempotenzschlüssel kommt aus Dokument und Zeitpunkt seiner
   letzten Speicherung; scheitert das Festhalten in Work, liefert die Wiederholung dieselbe Quittung statt
   eines zweiten Eingangs.
4. **Einreichen setzt den Status über die Übergänge**: `Motion.advance_to("submitted")` geht die
   definierten Übergänge (`Motion.VALID_TRANSITIONS`); wer mit `motions.approve` einen Entwurf
   einreicht, gibt ihn damit zugleich frei. Die einreichende Person erhält eine
   **Eingangsbestätigung per E-Mail** (Eingangsnummer, Verwaltung, Link zur Statusseite) über den
   Mailweg der Organisation (eigenes SMTP oder mandari-Versand, `work/organization/email/submission_receipt.html`).
   Die Mail geht nach dem Commit raus; ein Versandfehler bricht die Einreichung nicht ab.
5. **Rückmeldung** (Issue #316): Jede Änderung in Session am Antrag, an der daraus entstandenen
   Vorlage, ihren Stationen der Beratungsfolge, den TOPs oder Sitzungen stößt per Signal
   (`apps/work/motions/signals.py`) nach dem Commit `administration_feedback.sync_application` an.
   Grundlage ist der öffentlich zulässige **Rückmeldestand**
   (`apps/session/services/application_feedback.py`) – dieselben Ö/NÖ-Regeln wie die OParl-Schnittstelle:

   - Vorlagen- bzw. Drucksachennummer nur bei veröffentlichter Vorlage (öffentlich, nicht Entwurf/Prüfung).
   - Je Station Gremium, Rolle, Sitzung (Datum, TOP) und Ergebnis nur, wenn Vorlage, Sitzung und TOP
     öffentlich sind; sonst nur „nicht-öffentlich beraten“ – ohne Gremium, Termin, Ergebnis, Beschlussnummer.
   - Beschluss (Ergebnis, Beschlussnummer) nur aus einer öffentlichen, entscheidenden Station
     (`authoritative` oder Rolle „Entscheidung“; ein TOP ohne Station zählt, wenn es keine entscheidende gibt).
   - Bearbeitungsnotizen der Verwaltung sind intern und erscheinen in Work nicht.

   | Stand in Session | Work-Status |
   |---|---|
   | eingereicht | Eingereicht |
   | eingegangen / in Prüfung / angenommen / in Vorlage umgewandelt | Bei Verwaltung |
   | öffentliche Station einer Sitzung zugeordnet (nicht abgesagt, TOP nicht abgesetzt) | Auf Tagesordnung |
   | Beschluss: angenommen | Beschlossen |
   | Beschluss: abgelehnt, oder Antrag abgelehnt | Abgelehnt |
   | Beschluss: zur Kenntnis genommen | Erledigt |
   | Beschluss: zurückgezogen, oder Antrag zurückgezogen | Zurückgezogen |

   Der Work-Status wechselt nur über die definierten Übergänge und nur, wenn sich der Stand der
   Verwaltung ändert (`Motion.administration_status`) – eine Hand-Korrektur der Fraktion bleibt
   stehen, bis in Session wieder etwas passiert. Ergebnisse vertagter oder nicht-öffentlicher
   Beratungen ändern den Status nicht.

   Benachrichtigt werden Autor:in und Federführung (In-App + E-Mail nach Präferenz), jedes Ereignis
   genau einmal (`MotionAdministrationEvent`, eindeutiger Schlüssel je Dokument): Antragsstatus,
   Vorlagennummer, **„Beratung terminiert“ einmal je Termin** (Sitzung; Speichern, Umsortieren,
   TOP-Anlage oder Ergebnis melden nichts Neues, eine Verschiebung auf eine andere Sitzung schon),
   Beratungsergebnis, Beschluss. Gespeichert wird nur der Schlüssel, kein Text. Für Dokumente, die vor
   Issue #316 eingereicht wurden, übernimmt der erste Abgleich den Stand ohne Benachrichtigungen.

   Anzeige: Statusseite `documents/<id>/submit-ris/` (Eingang, Vorlagennummer, Beschluss,
   Beratungsfolge), Seitenleiste „Verwaltung“ und Kopfzeile im Editor, Dokumentenliste (Nummer am Titel).

6. **Admin**: Sammelaktionen für Anträge und Sitzungen speichern jedes Objekt einzeln, „Vorlage
   erstellen“ nutzt denselben Dienst wie das Session-Portal (`application_service.convert_to_paper`).
   So lösen auch Admin-Änderungen Audit-Log und Rückmeldung aus.

### Voraussetzungen für das Einreichen

- Verbindung vorhanden, Token gültig und nicht zurückgezogen, Verwaltung aktiv
- Dokumenttyp ist einreichbar (`MotionType.is_submittable`)
- Dokument im Status „Freigegeben“ – oder die einreichende Person besitzt `motions.approve`
- Noch keine Einreichung für dieses Dokument

Work prüft das vor dem Senden. Der Session-Handler prüft unabhängig davon noch einmal Mitgliedschaft und
Recht `motions.submit_to_ris`, Verbindung und Token, Pflichttexte (nicht nur Leerraum), Zielgremium des
Mandanten und die Grenzen des Vertrags (Titel 500, Beschlussvorschlag und Begründung je 50.000, finanzielle
Auswirkungen 10.000 Zeichen, Mitunterzeichnende 5.000 Zeichen als Text). Ablehnungen erscheinen im
Formular mit der Bezeichnung des Felds.

### Antragsfassung als PDF

Beim Einreichen aus Work entsteht aus den übermittelten Angaben automatisch ein schlichtes A4-PDF des Antrags
und hängt am Eingang, z. B. `Antrag A-2026-0003.pdf` (`application_service.attach_application_pdf`, Template
`session/pdf/application.html`, gekennzeichnet über `SessionFile.is_application_pdf`). Es enthält Titel, Antragsart,
Fraktion, einreichende Person, Datum, Eingangsnummer, Zielgremium, Wunschtermin, Dringlichkeit, Beschlussvorschlag,
Begründung, finanzielle Auswirkungen und Mitunterzeichnende – keine E-Mail-Adresse und keine Telefonnummer. Die Texte
sind Klartext aus dem Formular und werden escaped, HTML wird nicht übernommen. Einen Virenscan gibt es dafür nicht:
Das PDF entsteht auf dem Server und enthält keine hochgeladenen Inhalte. Scheitert die Erzeugung, wird das geloggt
und ohne PDF eingereicht; die Erfolgsmeldung zählt die Antragsfassung nicht als Anhang.

Bis zur Umwandlung ist die Antragsfassung intern wie die Anhänge (nur mit `view_applications`). `convert_to_paper`
hängt sie **öffentlich** an die Vorlage, alle anderen Anhänge bleiben nichtöffentlich (#584). Über OParl und damit im
Bürgerportal erscheint sie erst, wenn die Vorlage freigegeben ist (`oparl_publication.visible_files`: nicht Entwurf
oder Prüfung); als älteste öffentliche Anlage ist sie dort die Hauptdatei (`mainFile`). Die Verwaltung kann sie vorher
an der Vorlage auf nichtöffentlich stellen. Einreichungen über die API v1 erhalten kein PDF; im Befehl entscheidet
das Feld `application_pdf`, das Work setzt.

Anhänge gehen nicht als Text im Befehl mit: Der Inhalt nennt je Anhang Name, Größe und SHA-256, die Dateien stehen in
`Command.attachments` und gehen nur innerhalb einer Installation mit. Der Handler prüft Größe und Hash, Dateityp,
Größe und Virenprüfung; nicht Angenommenes meldet die Quittung als Stelle und Code, Work nennt dann den Namen.

### Dateien der Einreichung

- Befehl: `apps/session/commands.py` (Handler `submission.submit`), Vertrag
  `hub/contracts/schemas/submission.submit/v1.json`
- Service: `apps/work/motions/ris_submission.py` (Einreichen), `apps/work/motions/administration_feedback.py`
  (Rückmeldung), `apps/session/services/application_feedback.py` (öffentlich zulässiger Rückmeldestand)
- Work-Views: `apps/work/motions/views/ris.py`, `apps/work/organization/views/ris_settings.py`
- Session-Views: `apps/session/views/api_tokens.py`
- Templates: `work/motions/submit_ris.html`, `work/organization/ris_settings.html`,
  `session/settings/api_tokens.html`
- Tests: `scripts/smoke_ris_submission.py` (Fraktion → Verwaltung → Rückmeldung, Rechte, Sperren),
  `apps/work/motions/tests/test_verwaltung_rueckmeldung.py` (Ende-zu-Ende, Ö/NÖ, Idempotenz, Admin,
  Organisationstrennung), `apps/session/tests/test_antrag_rueckmeldung.py` (Aufbereitung, API),
  `apps/work/motions/tests/test_dokument_anhaenge.py` (Anhänge und Antragsfassung als PDF),
  `apps/session/tests/test_befehl_einreichen.py` (Handler, API v1, abgekündigter Pfad),
  `apps/work/motions/tests/test_einreichung_befehl.py` (Weg aus Work, Wiederholung, Prüfung im Handler)

### Getrennte Installationen

Für getrennte Installationen (Fraktion in der Cloud, Verwaltung vor Ort) stellt die Session-API v1 Einreichen
und Rückmeldestand bereit; Endpunkte, Token und Grenzen beschreibt die Schnittstellen-Doku (`docs/SCHNITTSTELLE.md`,
Session-API v1). Innerhalb einer Installation sendet Work den Befehl über den `InProcessClient`; einen Work-seitigen
Abruf aus einer anderen Installation gibt es nicht.

## 3. Neues Design in Work ausrollen

Das neue Erscheinungsbild von Work (Rahmen wie Insight, Startseite, Sitzungsansicht, Vorbereitung und Editor; Epic
#850) steht hinter einem **Schalter je Organisation**. Das Feld steht für neu angelegte Organisationen auf „aus“. In
einer Installation legt der Betreiber fest, welche Organisationen es nutzen (Soll-Stand). Diese Anleitung beschreibt,
wie ein Update diesen Stand bestätigt, wie neue Organisationen dazukommen und wie der Rückweg geht.
Der Rückweg ist jederzeit derselbe Schalter.

| Was | Wo |
|---|---|
| Schalter | Feld `Organization.work_new_design` (#852), für neue Organisationen aus; Darstellung: `apps/work/rahmen.py` |
| Soll-Stand | vom Betreiber festgelegt; Abgleich mit `work_neues_design status --soll an` |
| Schalten und Stand | Verwaltungsbefehl `work_neues_design` (`apps/work/management/commands/work_neues_design.py`) |
| Prüfen | `scripts/pruefe_work_design.py` (Playwright): alle Bereiche, fünf Rollen, fünf Breiten, Schalter an und aus; zählt Seiten ohne neue Gestaltung |
| Stand je Seite | `data-gestaltung` am Hauptbereich im neuen Rahmen: `neu` setzt die Seite selbst (Block `gestaltung`), sonst `bisher` |
| Demo-Inhalte | `setup_demo_environment` ruft `setup_demo_work` auf (nächtlicher Neuaufbau der Demo) |

### Soll-Stand je Instanz

| Instanz | Soll | Anmerkung |
|---|---|---|
| Eigener Betrieb | vom Betreiber festgelegt, etwa alle Organisationen an | eine neu angelegte Organisation steht zunächst auf „aus“ und wird nach „Organisation einschalten“ eingeschaltet |
| Öffentliche Demo | Demo-Organisation an | setzt `setup_demo_environment` bei jedem Neuaufbau |
| Staging, Selbstbetrieb, lokal | Stand der Datenbank, aus der die Instanz aufgebaut ist | ohne eigenen Beschluss gilt das Feld: neue Organisationen aus, die Demo-Organisation an |

Der Soll-Stand ist eine Entscheidung des Betreibers. Bleibt eine Organisation bewusst beim bisherigen Rahmen,
wird das festgehalten und beim Abgleich mit `--org <slug> --soll aus` geprüft.

**Ein Abgleich schaltet nie um.** `work_neues_design status --soll an` nennt nur die Abweichungen und endet dann mit
einem Fehler. Wer daraufhin pauschal umschaltet, dreht womöglich den beschlossenen Stand zurück; vor allem
`aus --alle` ist ausschließlich der Notfall-Rückweg (Abschnitt „Rückweg“ unten).

### Was der Schalter ändert – und was nicht

- **Ändert:** nur die Darstellung von Work für die Mitglieder dieser Organisation (Rahmen, Navigation, neue
  Ansichten). Alle bisherigen Adressen bleiben gültig, das Ratsinformationssystem bleibt in Work.
- **Ändert nicht:** Inhalte, Einstellungen, Mitglieder, Rechte. Der Befehl schreibt ausschließlich diese eine
  Spalte der Organisation (ohne `save()`, ohne Signale, ohne neues `updated_at`). Der Test
  `apps/work/tests/test_neues_design_schalter.py` vergleicht dazu den **gesamten** Datenbestand vor und nach dem
  Ein- und Ausschalten.
- **Rechte:** Das neue Design zeigt nichts, was die Rolle vorher nicht sehen durfte. Das Prüfskript meldet jeden
  Link, den eine Rolle sieht, der aber auf 403/404 führt; die Tests der Bereiche prüfen die Rechte je Rolle.

### Verwaltungsbefehl `work_neues_design`

Im App-Container (`docker exec <app-container> python manage.py …`):

```bash
python manage.py work_neues_design status                        # Stand aller Organisationen
python manage.py work_neues_design status --org <slug> --json    # maschinenlesbar
python manage.py work_neues_design status --soll an              # Abgleich mit dem Soll-Stand, schaltet nichts um
python manage.py work_neues_design an  --org <slug> --probelauf  # zeigt nur, was sich ändern würde
python manage.py work_neues_design an  --org <slug>              # einschalten (mehrere: --org a --org b)
python manage.py work_neues_design aus --org <slug>              # Rückweg für eine Organisation
python manage.py work_neues_design aus --alle                    # Rückweg für alle Organisationen
```

Eingeschaltet wird bewusst nur je Organisation; `an --alle` gibt es nicht. Unbekannte Kurznamen brechen ab, ohne
etwas zu ändern. `status --soll an|aus` (auch mit `--org`) nennt jede Organisation, die abweicht, und endet dann mit
Exit-Code 1; stimmt alles, meldet er „Soll-Stand bestätigt (an)“ bzw. „(aus)“.

### Prüfskript

`scripts/pruefe_work_design.py` meldet sich je Rolle an (Vorsitz, Mitglied, Sachkundige, nicht vereidigt, Gast),
erkundet alle Work-Seiten über die Links, die diese Rolle sieht (nur Seitenaufrufe, keine Aktionen, Downloads oder
Schnittstellen; je Adressmuster höchstens zwei Seiten), und misst jede Seite in 390, 1280, 1440, 1920 und 2560 px.

**Wo es läuft:** lokal, in der CI, gegen die öffentliche Demo und gegen Staging – **nicht gegen Produktion**.
Produktion wird nach einem Deploy von Hand angesehen. Lokal folgt das Skript jedem Link außer denen der Sperrliste
(`AUSGESCHLOSSEN`: Aktionen, Teilstücke, Dateien, Schnittstellen), damit neue Seiten auffallen. Gegen entfernte
Instanzen ruft es zusätzlich nur Seiten der Erlaubnisliste `ERLAUBT_ENTFERNT` auf, auch mit `--nur`. Eine künftige
Adresse, deren Aufruf etwas ändert, wird dort also nie aufgerufen, selbst wenn sie auf der Sperrliste fehlt. Andere
sichtbare Links nennt der Bericht als „nicht geprüft“. Eine neue Seite kommt auf die Erlaubnisliste, wenn ihr Aufruf
nichts ändert. Der Test `apps/common/tests/test_pruefe_work_design.py` prüft, dass jeder Eintrag eine Seite von Work
mit GET ist und nicht auf der Sperrliste steht.

| Befund | Schwere |
|---|---|
| Anmeldung gescheitert oder Anmeldeseite nicht nutzbar (etwa Host nicht zugelassen); die Rolle wird übersprungen | Fehler |
| Serverfehler (Status ≥ 500, Fehlerseite) | Fehler |
| Toter Link (sichtbarer Link endet mit 403/404) | Fehler |
| Konsole: JavaScript-Ausnahme, Fehler in der Konsole (Alpine, CSP), eigene Ressource mit Status ≥ 400 | Fehler |
| Fremde Ressource lädt nicht (etwa Kartenkacheln bei gestörter Namensauflösung) | Hinweis |
| Überlauf: Seite breiter als das Fenster | Fehler |
| Leerfläche (Regel #841, für Work 1.280 bis 2.560 px): rechts mehr als ein Viertel frei; Text, Bedienelemente und Flächen mit Rahmen, Hintergrund oder Schatten zählen als Inhalt | Fehler im neuen Design (Schalter an bzw. Seite mit `data-rahmen="neu"`), sonst Hinweis |
| Design passt nicht zum Schalter: `data-rahmen="neu"` bei ausgeschaltetem Schalter | Fehler |
| Seite ohne neuen Rahmen bei eingeschaltetem Schalter (eigene Vorlage ohne `base_work.html`) | Hinweis |
| Seite im neuen Rahmen ohne neue Gestaltung (`data-gestaltung` nicht `neu`), gezählt je Adressmuster über alle Rollen | Hinweis in der Zusammenfassung, mit `--gestaltung-pflicht` Fehler |
| Sichtbarer Link nicht auf der Erlaubnisliste (nur gegen entfernte Instanzen; nicht aufgerufen), je Adressmuster | Hinweis in der Zusammenfassung |

Verbindungsversuche der Zusammenarbeit im Editor (WebSocket) zählen nicht. Exit-Code 1 bei mindestens einem Fehler.

Zugänge kommen nur aus der Umgebung: `MANDARI_PRUEF_PASSWORT` (alle Demo-Konten), je Rolle überschreibbar mit
`MANDARI_PRUEF_<ROLLE>_EMAIL` und `MANDARI_PRUEF_<ROLLE>_PASSWORT`. Rollen ohne Zugang werden übersprungen.

```bash
pip install playwright && playwright install chromium

# Lokal (Demo mit DEMO_INSTANCE=true und DEMO_PASSWORD aufgebaut, runserver auf 8000), Schalter an und aus:
MANDARI_PRUEF_PASSWORT=… python scripts/pruefe_work_design.py --basis http://localhost:8000 --schalter beide

# Öffentliche Demo (Schalter wie eingestellt), Bericht als JSON und Bildschirmfotos:
MANDARI_PRUEF_PASSWORT=… python scripts/pruefe_work_design.py --basis https://demo.mandari.de \
    --bericht demo.json --bilder demo-bilder/

# Staging mit eigenen Konten je Rolle (nur Seiten der Erlaubnisliste):
MANDARI_PRUEF_VORSITZ_EMAIL=… MANDARI_PRUEF_VORSITZ_PASSWORT=… \
    python scripts/pruefe_work_design.py --basis https://<staging> --org <slug> --rollen vorsitz
```

Gegen eine entfernte Instanz schaltet das Skript **nie von sich aus** um. `--schalter an|aus|beide` geht dort nur
mit `--schalt-befehl '<befehl mit {aktion} und {org}>'`; das Skript liest vorher den Stand und stellt ihn am Ende
wieder her. Die Datenbank der öffentlichen Demo ändert nur ihr nächtlicher Neuaufbau, nie das Prüfskript.

#### Seiten ohne neue Gestaltung zählen

Der neue Rahmen trägt jede Seite, aber nicht jede Seite ist schon inhaltlich umgestellt (Bestandsaufnahme in #951).
Darum kennzeichnet der Hauptbereich im neuen Rahmen den Stand: `<main data-gestaltung="bisher">`, solange die Seite
ihr bisheriges Markup zeigt. Eine Seite, die im neuen Erscheinungsbild steht (gemeinsame Bausteine, keine Kästen in
Kästen, Breiten 390 und 1.280 bis 2.560 px geprüft), setzt in ihrer Vorlage

```django
{% block gestaltung %}neu{% endblock %}
```

Welche Seiten das schon tun, zeigt `git grep -l "block gestaltung %}neu" mandari/templates/work`.
Das Prüfskript nennt am Ende die Zahl der Adressmuster ohne neue Gestaltung und listet sie auf, je Rolle steht
„x von y im neuen Rahmen ohne neue Gestaltung“. Ziel von #951 ist 0; dann läuft das Skript mit
`--gestaltung-pflicht` ohne Fehler. Im bisherigen Rahmen gibt es die Kennzeichnung nicht (Schalter aus zählt nicht).

Weitere Schalter: `--rollen`, `--breiten`, `--max-seiten` (Standard 80 je Rolle), `--neu-laden` (je Breite neu
laden statt Fenster verbreitern; genauer, dauert länger), `--sichtbar` (Browser sichtbar), `--nur-erlaubte` (lokal
wie gegen eine entfernte Instanz nur Seiten der Erlaubnisliste).

Im CI läuft derselbe Ablauf verkleinert als E2E-Test (`tests_e2e/test_work_design_pruefung.py`).

### Demo-Inhalte in Work

`setup_demo_environment` legt die Musterfraktion an und ruft am Ende `setup_demo_work` auf. Damit zeigt die Demo
nach jedem nächtlichen Neuaufbau:

- **Konten** (alle `@demo.mandari.de`): `demo-vorsitz` (Fraktionsvorsitz, vereidigt), `demo-mitglied`
  (Fraktionsmitglied, vereidigt), `demo-sachkundig` (Sachkundige Bürgerin, vereidigt), `demo-unvereidigt`
  (Fraktionsmitglied, noch nicht vereidigt), `demo-gast` (Gast mit Ordnerfreigabe)
- **Standard-Tagesordnung:** Beschlüsse, Politische Arbeit, Termine, Presse und Social Media, Sonstiges und ein
  nicht-öffentlicher Punkt
- **Vergangene Fraktionssitzung:** Anwesenheit vor Ort und online, Beschluss, Notiz und Aufgabe im Protokoll; das
  Protokoll wartet auf die Genehmigung
- **Kommende Fraktionssitzung** mit Ort und Videolink: Genehmigungs-TOP, Standard-Tagesordnung, Unterpunkte mit
  verknüpftem Antrag und verknüpfter Vorlage, zwei nicht-öffentliche TOPs (nur für Vereidigte) und ein
  TOP-Vorschlag der Sachkundigen
- **Sitzungsreihe** wöchentlich montags 18 Uhr (Zu- und Absagen und automatische Einladung aus) mit Ferienpause
  und Feiertagen als Ausnahme; die Termine erzeugt dieselbe Funktion wie im Betrieb
- **Beratungsverlauf:** Die Vorlage zum Feuerwehrbedarfsplan steht im Hauptausschuss und im Rat; die Position aus
  dem Hauptausschuss („Mit Änderungsantrag“, endgültig) erscheint in der Vorbereitung der Ratssitzung. Dazu
  vergangene Positionen mit Ergebnis
- **Antrag mit Kommentaren** an Textstellen (offen mit Antwort, erledigt) und einem allgemeinen Kommentar, dazu zwei
  **Änderungsanträge** (zum eigenen Antrag und zu einer Vorlage des RIS)
- **Ankündigung** „Was ist neu in Work“ im Hinweisband auf Start und in der Glocke (#857) für alle Mitglieder außer
  dem Gast
- **Neues Design an** für die Demo-Organisation (setzt `setup_demo_environment` bei jedem Aufbau)

### Ablauf bei einem Update

Ein Update ändert den Schalter nicht: Es bringt neue Seiten und Bausteine, der Stand der Organisationen bleibt, wie
er ist. Darum gilt bei jedem Update: bestätigen statt umschalten.

1. Vorher den Stand sichern: `work_neues_design status --json > work-design-vorher.json`.
2. Nach dem Update `work_neues_design status --soll an` (bzw. mit `--org <slug> --soll aus` für bewusste
   Ausnahmen). Meldet der Abgleich eine Abweichung, **nichts pauschal umschalten**, auf keinen Fall `aus --alle`,
   sondern mit dem gesicherten Stand vergleichen. Meist ist es eine neu angelegte Organisation (Feld „aus“).
3. Das Prüfskript gegen eine Demo- oder Testinstanz laufen lassen, nie gegen Produktion.

#### Organisation einschalten

In Absprache mit der Organisation (Ankündigung im Produkt, #857):

- `work_neues_design an --org <slug> --probelauf`, dann ohne `--probelauf`
- `work_neues_design status --org <slug> --soll an` bestätigt den Stand
- Sichtprüfung in Work durch die Administration der Organisation, Fehlerprotokoll der ersten Stunde ohne neue 5xx

### Rückweg

Jeder Rückweg weicht vom beschlossenen Stand ab und geschieht nur nach Entscheidung des Betreibers. Danach
festhalten, welche Organisationen aus sind, damit der nächste Abgleich sie nicht als Fehler meldet
und niemand sie unbeabsichtigt wieder einschaltet.

| Fall | Vorgehen |
|---|---|
| Eine Organisation hat Probleme mit dem neuen Design | `work_neues_design aus --org <slug>` – wirkt beim nächsten Seitenaufruf, Daten bleiben unberührt; danach `status --org <slug> --soll aus` |
| Das neue Design stört überall | Stand sichern (`status --json > work-design-vor-rueckweg.json`), dann `work_neues_design aus --alle`. Zurück zum beschlossenen Stand mit `an --org <slug>` je Organisation aus der Sicherung (`an --alle` gibt es bewusst nicht) |
| Das Image muss zurück | `deploy.sh rollback <tag>`; das Feld und der Stand der Organisationen bleiben (additive Migration, ältere Abbilder ignorieren es) |
| Ein Befund nur in einer Breite oder Rolle | Schalter an lassen, wenn die Arbeit nicht behindert ist; Issue mit Bild aus der Demo |

Weil der Schalter keine Daten verändert, braucht der Rückweg keine Datensicherung und keine Migration.
