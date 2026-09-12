# Anmeldesicherheit: Zwei-Faktor-Pflicht und Admin-Netze

Implementierungsnotiz. Code: `apps/accounts/two_factor_policy.py`,
`apps/accounts/middleware.py`, `apps/accounts/views.py` (`TwoFactorEnrollView`),
Tests: `apps/accounts/tests/test_two_factor_policy.py`.

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
  Konten ohne erfüllte Pflicht auf dieselbe Seite um (HTMX: `HX-Redirect`,
  JSON/XHR: 403). Ausgenommen sind `/accounts/`, statische Dateien und der
  Health-Check. Das Ergebnis der Richtlinie wird je Sitzung fünf Minuten
  zwischengespeichert; Änderungen an Rollen oder Schaltern wirken also
  spätestens nach fünf Minuten.
- **Profil:** Wer verpflichtet ist, kann den zweiten Faktor nicht deaktivieren.

## Konfiguration

| Variable | Standard | Bedeutung |
|---|---|---|
| `TWO_FACTOR_ENFORCEMENT` | `true` in Produktion, `false` bei `DEBUG` | Pflicht durchsetzen |
| `TWO_FACTOR_EXEMPT_EMAIL_DOMAINS` | `demo.mandari.de` | Ausgenommene Domains für gemeinsam genutzte Demo-Zugänge ohne echte Daten |
| `ADMIN_ALLOWED_NETWORKS` | leer (keine Beschränkung) | Kommagetrennte CIDR-Netze, aus denen `/admin/` erreichbar ist, z. B. VPN-Netz und feste Büroadressen |

`ADMIN_ALLOWED_NETWORKS` setzt voraus, dass der vorgelagerte Reverse-Proxy
`X-Forwarded-For` durch die echte Client-Adresse ersetzt (Caddy tut das für
nicht vertrauenswürdige Clients). Ohne Proxy wird `REMOTE_ADDR` verwendet.

## Einführung und Notfall

- Beim Einschalten werden betroffene Konten bei der nächsten Anfrage in die
  Einrichtung geführt. Vorher informieren: Authenticator-App bereithalten,
  Backup-Codes im Passwort-Manager ablegen.
- Verlust des Geräts: Anmeldung mit einem Backup-Code. Sind auch diese
  verloren, löscht die Plattform-Administration nach Identitätsprüfung das
  2FA-Gerät im Django-Admin (Konten → 2FA-Geräte); beim nächsten Login folgt
  die erneute Einrichtung.

## Ausblick

- Sicherheitsschlüssel und Passkeys (WebAuthn/FIDO2) als phishing-resistenter
  zweiter Faktor; Pflicht für kritische Konten, sobald Schlüssel ausgegeben sind.
- E-Mail-Benachrichtigung, wenn ein zweiter Faktor eingerichtet oder entfernt wird.
- Netzbeschränkung je Session-Mandant (Zugriff nur aus dem Verwaltungsnetz).
