# Registrierung und Zugangs-Mails (Work)

Implementierungsnotiz zu Einladungen, Gastzugängen, Selbstregistrierung und Freischaltung in
mandari work. Code: `apps/work/organization/emails.py` (Versand), `services.py` (Abläufe),
`selectors.py` (Anfragen, Freigebende), `apps/accounts/views.py` (`SelfRegisterView`,
`SelfRegisterConfirmView`). Vorlagen: `templates/work/organization/email/`.

## Versandweg

Alle Mails laufen über `apps.common.org_email.send_org_email`, also über den Weg, den die
Organisation unter *Organisation → E-Mail-Einstellungen* gewählt hat:

- **mandari-Standardversand** (Voreinstellung; auch wenn „eigenes SMTP“ gewählt, aber kein Server
  eingetragen ist) oder
- **eigenes SMTP der Organisation** mit deren Absender.

Rückfall: Schlägt das eigene SMTP fehl und ist „Bei SMTP-Fehler auf mandari-Versand zurückfallen“
aktiv (Voreinstellung), geht die Mail über den mandari-Standardversand – SMTP-Konfiguration und
Absender aus den Site-Einstellungen, ersatzweise aus den Umgebungsvariablen. Ohne Rückfall wird der
Fehler protokolliert; Freischalten und Ablehnen zeigen ihn in der Oberfläche an.

Ausnahmen: Links, mit denen sich ein Passwort setzen lässt (Gastzugang für neue Konten,
Passwort-Zurücksetzen), gehen immer über mandari. Antworten landen bei der Kontaktadresse der
Organisation, sofern hinterlegt; Anfragen an Freigebende antworten direkt an die anfragende Person.
Jede Mail geht an genau eine Adresse. Versandfehler brechen den Ablauf nie ab; Freischalten und
Ablehnen melden in der Oberfläche, wenn die Mail nicht rausging.

Alle Vorlagen erweitern `emails/base_email.html` (Inliner, Textfassung per html2text) und escapen
Freitext wie Einladungsnachricht oder Ablehnungsgrund.

## Selbstregistrierung

| Schritt | Ergebnis | Mail |
|---------|----------|------|
| Formular `/accounts/register/<org>/` | Konto ohne Anmeldung, keine Mitgliedschaft | Bestätigungslink (48 h gültig) an die Person |
| Link öffnen (GET) | nur Bestätigungsseite – Link-Scanner lösen nichts aus | – |
| Bestätigen (POST) | Adresse bestätigt, Mitgliedschaft entsteht | siehe unten |
| … mit automatischer Freischaltung | Mitgliedschaft aktiv | „Willkommen“ an die Person, In-App-Hinweis „Neues Mitglied“ an Freigebende |
| … ohne automatische Freischaltung | offene Anfrage (`Membership.registration_requested_at`) | Eingangsbestätigung an die Person, Anfrage an alle mit `members.invite` (ersatzweise Eigentümer), In-App-Hinweis „Registrierungsanfrage“ |
| Freischalten | Mitgliedschaft aktiv | „Zugang freigeschaltet“ mit Link und Hinweis auf Zwei-Faktor-Pflicht |
| Ablehnen (optional mit Begründung) | Mitgliedschaft gelöscht, `User.registration_rejected_at` gesetzt; das Konto wird nach 30 Tagen ohne andere Zuordnung automatisch gelöscht (`cleanup_orphaned_accounts`, siehe DSGVO_LOESCHKONZEPT.md) | „Registrierungsanfrage“ mit Begründung und Hinweis auf die Löschfrist |

- Die erlaubten Domains gelten für die bestätigte Adresse; angemeldete Konten registrieren sich
  immer mit ihrer Kontoadresse. Konten mit bestätigter Adresse treten ohne erneuten Link bei.
- Konten aus einer Einladung gelten als bestätigt (der Einladungslink kam per Mail).
- Wer den Bestätigungslink nie nutzt, behält kein Konto: unbestätigte Konten werden nach 9 Tagen
  automatisch gelöscht, sofern sie nirgends zugeordnet sind (Issue #238).
- Bestätigungsmails sind je IP und Adresse pro Stunde gedrosselt
  (`SELF_REGISTER_MAILS_PER_IP_PER_HOUR`, `SELF_REGISTER_MAILS_PER_ADDRESS_PER_HOUR`).
- Der Link meldet nicht an; die Anmeldung läuft über Passwort und gegebenenfalls zweiten Faktor.

## Offene Anfragen und deaktivierte Mitglieder

Offene Anfragen erkennt `selectors.pending_registrations` ausschließlich am Feld
`registration_requested_at`. Deaktivierte Mitglieder erscheinen nur unter „Deaktivierte Mitglieder“;
Freischalten und Ablehnen liefern für sie 404. Die Migration `tenants/0021_registrierungsanfrage`
übernimmt Bestandsanfragen: inaktiv, keine Gäste, nicht eingeladen und seit dem Anlegen unverändert.

## Weitere Mails

- **Einladung** (`invitation.html`): beim Einladen und erneuten Senden.
- **Gastzugang** (`guest_access.html`): Passwort-Link über mandari, Dokumentenlink über den
  Versandweg der Organisation.
- **Reaktivierung** (`access_granted.html`, Variante `reactivated`): beim Reaktivieren und wenn eine
  Einladung eine inaktive Mitgliedschaft wieder aktiviert.

## Tests

`apps/work/organization/tests/test_registration_emails.py`, Selbstregistrierung ohne Kontoübernahme
in `apps/accounts/tests/test_login_2fa.py`.
