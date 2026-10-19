# Anträge digital bei der Verwaltung einreichen (Work → Session)

Fraktionen im Work-Portal reichen Anträge direkt beim Session-RIS ihrer Verwaltung ein und
sehen ohne Rückfrage Eingangsnummer, Bearbeitungsstatus und Beratungstermine (Issue #40).

## Ablauf

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

## Voraussetzungen für das Einreichen

- Verbindung vorhanden, Token gültig und nicht zurückgezogen, Verwaltung aktiv
- Dokumenttyp ist einreichbar (`MotionType.is_submittable`)
- Dokument im Status „Freigegeben“ – oder die einreichende Person besitzt `motions.approve`
- Noch keine Einreichung für dieses Dokument

Work prüft das vor dem Senden. Der Session-Handler prüft unabhängig davon noch einmal Mitgliedschaft und
Recht `motions.submit_to_ris`, Verbindung und Token, Pflichttexte (nicht nur Leerraum), Zielgremium des
Mandanten und die Grenzen des Vertrags (Titel 500, Beschlussvorschlag und Begründung je 50.000, finanzielle
Auswirkungen 10.000 Zeichen, höchstens 100 Mitunterzeichnende mit je 200 Zeichen). Ablehnungen erscheinen im
Formular mit der Bezeichnung des Felds.

## Antragsfassung als PDF

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

## Dateien

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

## Externe Work-Installationen

Für getrennte Installationen (Fraktion in der Cloud, Verwaltung vor Ort) stellt die Session-API v1
Einreichen und Rückmeldestand bereit (`docs/API_V1_SESSION.md`):

- `POST /api/v1/session/<mandant>/applications/submit/` mit dem Einreichungs-Token; die Antwort
  enthält unter `feedback` die URL des Rückmeldestands.
- `GET /api/v1/session/<mandant>/applications/<id>/feedback/` liefert Eingang, Vorlagennummer,
  Beratungsfolge und Beschluss nach denselben Ö/NÖ-Regeln. Abrufbar nur mit dem Token, mit dem der
  Antrag eingereicht wurde (`SessionApplication.submitted_via_token`), oder einem Token, das dieselbe
  Organisation in Work verbunden hat; fremde Anträge sind nicht auffindbar (404). Ratenlimit und
  IP-Beschränkung des Tokens gelten.

Der Work-seitige Abruf aus einer anderen Installation (Verbindung per URL statt lokalem Mandanten,
regelmäßiger Abgleich, `HttpClient` mit Anlagen) ist noch nicht umgesetzt; innerhalb einer Installation
sendet Work den Befehl über den `InProcessClient`.
