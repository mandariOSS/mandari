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
   die Aktion „Bei Verwaltung einreichen“ (Kopfzeile und Sidebar „Verwaltung“). Die Seite
   `documents/<id>/submit-ris/` zeigt die Dokumentvorschau und ein vorbelegtes Formular:
   Titel, Antragsart (aus Dokumenttyp/Titel geraten), Beschlussvorschlag, Begründung,
   finanzielle Auswirkungen (aus Überschriften wie „Beschlussvorschlag“, „Begründung“,
   „Finanzielle Auswirkungen“ herausgelöst), Zielgremium, Mitunterzeichnende, Dringlichkeit,
   Wunschtermin. Nach Bestätigung entsteht der `SessionApplication`-Datensatz mit
   Eingangsnummer `A/<Jahr>/<Nr>`; das Dokument wechselt auf „Eingereicht“ und ist über
   `Motion.session_application` verknüpft.
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

## Dateien

- Service: `apps/work/motions/ris_submission.py` (Einreichen), `apps/work/motions/administration_feedback.py`
  (Rückmeldung), `apps/session/services/application_feedback.py` (öffentlich zulässiger Rückmeldestand)
- Work-Views: `apps/work/motions/views/ris.py`, `apps/work/organization/views/ris_settings.py`
- Session-Views: `apps/session/views/api_tokens.py`
- Templates: `work/motions/submit_ris.html`, `work/organization/ris_settings.html`,
  `session/settings/api_tokens.html`
- Tests: `scripts/smoke_ris_submission.py` (Fraktion → Verwaltung → Rückmeldung, Rechte, Sperren),
  `apps/work/motions/tests/test_verwaltung_rueckmeldung.py` (Ende-zu-Ende, Ö/NÖ, Idempotenz, Admin,
  Organisationstrennung), `apps/session/tests/test_antrag_rueckmeldung.py` (Aufbereitung, API)

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
regelmäßiger Abgleich) ist noch nicht umgesetzt; innerhalb einer Installation arbeitet Work direkt
über den `ApplicationService`.
