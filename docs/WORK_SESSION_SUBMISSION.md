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
4. **Rückmeldung**: Statuswechsel der Verwaltung laufen per Signal ins Work-Dokument
   (`apps/work/motions/signals.py` → `ris_submission.sync_motion_from_application`):

   | Session-Status | Work-Status | Benachrichtigung |
   |---|---|---|
   | eingegangen / in Prüfung / angenommen / in Vorlage umgewandelt | Bei Verwaltung | ja, inkl. Bearbeitungsnotiz |
   | Beratung mit Sitzung angelegt (`SessionConsultation`) | Auf Tagesordnung | ja, mit Gremium und Termin |
   | abgelehnt | Abgelehnt | ja |
   | zurückgezogen | Freigegeben | ja |

   Benachrichtigt werden Autor:in und Federführung (In-App + E-Mail nach Präferenz).
   Die Statusseite in Work zeigt die Beratungsfolge der entstandenen Vorlage(n).

## Voraussetzungen für das Einreichen

- Verbindung vorhanden, Token gültig und nicht zurückgezogen, Verwaltung aktiv
- Dokumenttyp ist einreichbar (`MotionType.is_submittable`)
- Dokument im Status „Freigegeben“ – oder die einreichende Person besitzt `motions.approve`
- Noch keine Einreichung für dieses Dokument

## Dateien

- Service: `apps/work/motions/ris_submission.py`
- Work-Views: `apps/work/motions/views/ris.py`, `apps/work/organization/views/ris_settings.py`
- Session-Views: `apps/session/views/api_tokens.py`
- Templates: `work/motions/submit_ris.html`, `work/organization/ris_settings.html`,
  `session/settings/api_tokens.html`
- Test: `scripts/smoke_ris_submission.py` (Fraktion → Verwaltung → Rückmeldung, Rechte, Sperren)

## Externe Work-Installationen

Der bestehende HTTP-Endpunkt `<tenant>/api/session/applications/submit/` (Bearer-Token) bleibt
für getrennte Installationen bestehen; die hier beschriebene Integration nutzt denselben Token,
arbeitet aber innerhalb einer Installation direkt über den `ApplicationService`.
