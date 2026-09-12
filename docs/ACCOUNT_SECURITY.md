# Anmeldesicherheit: Zwei-Faktor-Pflicht, Sicherheitsschlüssel, Admin-Netze

Implementierungsnotiz. Code: `apps/accounts/two_factor_policy.py`,
`apps/accounts/middleware.py`, `apps/accounts/views.py` (`TwoFactorEnrollView`),
`apps/accounts/webauthn_service.py`, `apps/accounts/views_webauthn.py`,
`frontend/js/webauthn.ts`. Tests: `apps/accounts/tests/test_two_factor_policy.py`,
`apps/accounts/tests/test_webauthn.py`.

## Wer einen zweiten Faktor verwenden muss

| Bereich | Pflicht für |
|---|---|
| Plattform | Superuser und Staff |
| Work | Mitglieder mit Administrator-Rolle oder einer Rolle mit „2FA erforderlich“; alle Mitglieder, wenn die Organisation „2FA für alle Mitglieder“ eingeschaltet hat |
| Session | Nutzer mit Administrator-, Benutzer- oder Einstellungsrechten; alle Nutzer, wenn der Mandant „2FA für alle Nutzer“ eingeschaltet hat |

Maßgeblich sind nur aktive Mitgliedschaften in aktiven Organisationen bzw.
Mandanten. Die Schalter liegen in den Organisationseinstellungen (Work,
Karte „Anmeldesicherheit“) und in den Session-Einstellungen (Karte
„Anmeldesicherheit“, Recht „Einstellungen verwalten“, mit Audit-Eintrag).

## Durchsetzung

- **Beim Login:** Nach geprüftem Passwort führt ein Konto mit Pflicht, aber
  ohne eingerichteten Faktor, direkt in die Einrichtung
  (`/accounts/zwei-faktor/einrichten/`): QR-Code scannen, Code bestätigen,
  Backup-Codes sichern. Angemeldet wird erst danach. Der Zwischenstand gilt
  15 Minuten; fünf Fehlversuche in 15 Minuten sperren.
- **Bestehende Sitzungen:** `TwoFactorEnforcementMiddleware` leitet angemeldete
  Konten mit offener Pflicht auf die Einrichtung um (HTMX: `HX-Redirect`,
  JSON/XHR: 403). Ausgenommen sind `/accounts/`, statische Dateien und der
  Health-Check. Das Ergebnis wird je Sitzung fünf Minuten zwischengespeichert;
  Änderungen an Rollen oder Schaltern wirken spätestens nach fünf Minuten.
- **Profil:** Wer verpflichtet ist, kann den zweiten Faktor nicht deaktivieren.

## Sicherheitsschlüssel und Passkeys (WebAuthn/FIDO2)

- **Verwaltung** unter `/accounts/sicherheitsschluessel/` (verlinkt aus der
  Profil-Sicherheit): Schlüssel mit Bezeichnung registrieren, auflisten,
  mit Passwort entfernen. Voraussetzung ist eine eingerichtete
  Authenticator-App – sie bleibt mit den Backup-Codes der Rückfall.
  Deaktiviert jemand die App, werden auch die Schlüssel entfernt.
- **Anmeldung:** Im zweiten Schritt erscheint „Mit Sicherheitsschlüssel
  anmelden“, sobald ein Schlüssel registriert ist. Es gelten dieselben
  Sperren und Protokolleinträge wie beim Code.
- **Prüfung:** Challenges sind einmalig und fünf Minuten gültig. Die
  Relying-Party-ID ist die Hauptdomain, damit Schlüssel auch auf
  Organisations-Subdomains funktionieren. Anfragen von fremden Domains werden
  abgewiesen. Der Signaturzähler erkennt geklonte Schlüssel. Gespeichert werden
  nur öffentliche Schlüsseldaten.
- **Pflicht für die Plattform-Administration:** Mit
  `TWO_FACTOR_REQUIRE_SECURITY_KEY_FOR_SUPERUSERS` müssen Superuser einen
  Schlüssel registrieren (Umleitung auf die Verwaltungsseite) und sich damit
  anmelden. App-Codes gelten dann nicht mehr, Backup-Codes bleiben der
  Notfallweg. Erst einschalten, wenn Schlüssel ausgegeben sind – je Person
  mindestens zwei, einer davon als Reserve.

## Konfiguration

| Variable | Standard | Bedeutung |
|---|---|---|
| `TWO_FACTOR_ENFORCEMENT` | `true` in Produktion, `false` bei `DEBUG` | Pflicht durchsetzen |
| `TWO_FACTOR_EXEMPT_EMAIL_DOMAINS` | `demo.mandari.de` | Ausgenommene Domains für gemeinsam genutzte Demo-Zugänge ohne echte Daten |
| `TWO_FACTOR_REQUIRE_SECURITY_KEY_FOR_SUPERUSERS` | `false` | Superuser nur mit Sicherheitsschlüssel |
| `WEBAUTHN_RP_ID` | `MAIN_DOMAIN` | Relying-Party-ID (Hauptdomain ohne Port) |
| `WEBAUTHN_RP_NAME` | `mandari` | Anzeigename im Browser-Dialog |
| `ADMIN_ALLOWED_NETWORKS` | leer (keine Beschränkung) | Kommagetrennte CIDR-Netze, aus denen `/admin/` erreichbar ist, z. B. VPN-Netz und feste Büroadressen |

`ADMIN_ALLOWED_NETWORKS` setzt voraus, dass der vorgelagerte Reverse-Proxy
`X-Forwarded-For` durch die echte Client-Adresse ersetzt (Caddy tut das für
nicht vertrauenswürdige Clients). Ohne Proxy wird `REMOTE_ADDR` verwendet.

## Einführung und Notfall

- Beim Einschalten werden betroffene Konten bei der nächsten Anfrage in die
  Einrichtung geführt. Vorher informieren: Authenticator-App bereithalten,
  Backup-Codes im Passwort-Manager ablegen.
- Verlust des Geräts: Anmeldung mit einem Backup-Code. Sind auch diese
  verloren, setzt die Plattform-Administration nach Identitätsprüfung den
  zweiten Faktor zurück – bewusst nicht im Django-Admin, sondern protokolliert
  per Befehl:

  ```bash
  python manage.py reset_two_factor person@example.org --reason "Ticket 123, Identität per Rückruf geprüft"
  ```

  Entfernt werden Authenticator-App, Backup-Codes, Sicherheitsschlüssel und
  vertrauenswürdige Geräte; beim nächsten Login folgt die erneute Einrichtung.

## Ausblick

- E-Mail-Benachrichtigung, wenn ein zweiter Faktor eingerichtet oder entfernt wird.
- Netzbeschränkung je Session-Mandant (Zugriff nur aus dem Verwaltungsnetz).
