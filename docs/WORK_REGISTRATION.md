# Registrierung und Zugangs-Mails (Work)

Implementierungsnotiz zu Einladungen, Gastzugängen, Selbstregistrierung und Freischaltung in
mandari work. Code: `apps/work/organization/emails.py` (Versand), `services.py` (Abläufe),
`selectors.py` (Anfragen, Freigebende), `apps/accounts/views.py` (`SelfRegisterView`,
`SelfRegisterConfirmView`). Vorlagen: `templates/work/organization/email/`.

## Versandweg

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

## Selbstregistrierung

| Schritt | Ergebnis | Mail |
|---------|----------|------|
| Formular `/accounts/register/<org>/` | Konto ohne Anmeldung, keine Mitgliedschaft | Bestätigungslink (48 h gültig) an die Person |
| Link öffnen (GET) | nur Bestätigungsseite – Link-Scanner lösen nichts aus; ohne Anmeldung Hinweis und Knopf zur Anmeldung, die danach zurück zum Link führt | – |
| Bestätigen (POST), angemeldet mit dem registrierten Konto | Adresse bestätigt, Mitgliedschaft entsteht | siehe unten |
| … mit automatischer Freischaltung | Mitgliedschaft aktiv | „Willkommen“ an die Person, In-App-Hinweis „Neues Mitglied“ an Freigebende |
| … ohne automatische Freischaltung | offene Anfrage (`Membership.registration_requested_at`) | Eingangsbestätigung an die Person, Anfrage an alle mit `members.invite` (ersatzweise Eigentümer), In-App-Hinweis „Registrierungsanfrage“ |
| Freischalten | Mitgliedschaft aktiv | „Zugang freigeschaltet“ mit Link und Hinweis auf Zwei-Faktor-Pflicht |
| Ablehnen (optional mit Begründung) | Mitgliedschaft gelöscht, `User.registration_rejected_at` gesetzt; das Konto wird nach 30 Tagen ohne andere Zuordnung automatisch gelöscht (`cleanup_orphaned_accounts`, siehe DSGVO_LOESCHKONZEPT.md) | „Registrierungsanfrage“ mit Begründung und Hinweis auf die Löschfrist |

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

## Offene Anfragen und deaktivierte Mitglieder

Offene Anfragen erkennt `selectors.pending_registrations` ausschließlich am Feld
`registration_requested_at`. Deaktivierte Mitglieder erscheinen nur unter „Deaktivierte Mitglieder“;
Freischalten und Ablehnen liefern für sie 404. Die Migration `tenants/0021_registrierungsanfrage`
übernimmt Bestandsanfragen: inaktiv, keine Gäste, nicht eingeladen und seit dem Anlegen unverändert.

## Weitere Mails

- **Einladung** (`invitation.html`): beim Einladen und erneuten Senden, immer über mandari.
- **Gastzugang** (`guest_access.html`): Passwort-Link über mandari, Dokumentenlink über den
  Versandweg der Organisation.
- **Reaktivierung** (`access_granted.html`, Variante `reactivated`): beim Reaktivieren und wenn eine
  Einladung eine inaktive Mitgliedschaft wieder aktiviert.

## Tests

`apps/work/organization/tests/test_registration_emails.py`, Selbstregistrierung ohne Kontoübernahme
in `apps/accounts/tests/test_login_2fa.py`.
