# Anmeldung für Anwendungen: mandari als OpenID-Connect-Anbieter

Stand: 2026-11-05 (Issue #1206, Teil von Epic #1203). Grundlage ist das ADR
[Eine Schnittstelle: API-first](../adr/20261105-eine-schnittstelle-api-first.md).

Anwendungen melden Personen über mandari an und erhalten ein begrenztes, widerrufbares Token. Die Anmeldung
selbst bleibt die von mandari, einschließlich des zweiten Faktors. Dienste ohne Person bekommen ihr Token über
Client Credentials. Umgesetzt ist das mit [django-oauth-toolkit](https://github.com/django-oauth/django-oauth-toolkit)
in der Plattform (`mandari/apps/accounts/oidc/`).

> **Standardmäßig aus.** Ohne `OIDC_PROVIDER_ENABLED=true` antworten alle Endpunkte mit 404, auch die Discovery,
> und kein Access-Token wird angenommen. Vor dem Einschalten in Produktion steht eine Sicherheitsprüfung an.
> Einschalten, Schlüssel, Proxy und das Anlegen von Anwendungen beschreibt [Betrieb](anmeldung-betrieb.md).

## Endpunkte

Aussteller (`iss`) ist die Adresse der Installation (`SITE_URL`), etwa `https://mandari.example.org`.

| Zweck | Pfad | Norm |
|---|---|---|
| Discovery | `/.well-known/openid-configuration` | OpenID Connect Discovery 1.0 |
| Autorisierung | `/oauth/authorize/` | RFC 6749, RFC 7636 (PKCE), RFC 9207 (`iss`) |
| Token | `/oauth/token/` | RFC 6749 |
| Widerruf | `/oauth/revoke/` | RFC 7009 |
| Introspektion | `/oauth/introspect/` | RFC 7662 |
| Angaben zur Person | `/oauth/userinfo/` | OpenID Connect Core 1.0 |
| Schlüssel (JWKS) | `/oauth/jwks/` | RFC 7517 |

Eine dynamische Registrierung von Anwendungen gibt es nicht.

## Abläufe

| Ablauf | Für | Merkmale |
|---|---|---|
| Authorization Code mit PKCE | Anwendungen im Namen einer Person (App, Partnerdienst mit Oberfläche) | PKCE mit `S256` ist Pflicht, auch für Anwendungen mit Secret |
| Client Credentials | Dienste ohne Person | nur mit Client-Secret, kein ID-Token, kein Refresh-Token |
| Refresh Token | beide Code-Anwendungen | Rotation bei jeder Nutzung |

Implicit, Password-Grant, Hybrid und Gerätefreigabe (RFC 8628) sind abgeschaltet. Access-Tokens werden nur im
Header `Authorization: Bearer …` angenommen, nie in der Adresse.

## Anwendung eintragen lassen

Anwendungen trägt die Plattform-Administration im Admin unter „OIDC-Anwendungen“ oder mit
`manage.py oidc_anwendung` ein ([Betrieb](anmeldung-betrieb.md#6-anwendungen-anlegen)). Dafür braucht sie:

- **Name**, wie er auf der Zustimmungsseite und unter „Verbundene Anwendungen“ erscheinen soll
- **Ablauf**: Code-Ablauf oder Client Credentials
- **Art**: öffentlich (App ohne Secret, etwa auf dem Telefon) oder vertraulich (mit Secret, Server)
- **Redirect-URIs**: nur `https`, sie müssen beim Aufruf exakt übereinstimmen (ohne Platzhalter, ohne Fragment).
  `http` gilt nur für `localhost` und nur in einer Entwicklungsinstallation (`DEBUG`).
- **Erlaubte Scopes**: mehr kann die Anwendung nicht anfordern

Die Anwendung erhält Client-ID und gegebenenfalls Client-Secret. Das Secret steht nur beim Anlegen im Klartext
im Formular bzw. in der Ausgabe des Befehls; gespeichert wird es gehasht. Eigene Anwendungen von mandari können als
vertrauenswürdig markiert werden und kommen ohne Zustimmungsseite aus. Fremde Anwendungen fragen die Person jedes Mal.

## Scopes

| Scope | Bedeutung | Code-Ablauf | Client Credentials |
|---|---|---|---|
| `openid` | Anmeldung, ID-Token mit `sub` | ja | nein |
| `profile` | Name (`name`, `given_name`, `family_name`) | ja | nein |
| `email` | E-Mail-Adresse (`email`, `email_verified`) | ja | nein |
| `ratsdaten:lesen` | Ratsdaten lesen, siehe unten | ja | ja |
| `einreichung:schreiben` | Anträge einreichen (folgt mit dem Profil Einreichung) | ja | ja |
| `mandari:bereitstellung` | Bereitstellung von Organisationen und Mandanten (folgt) | nein | ja |

Ein Token im Namen einer Person darf nie mehr als die Person selbst. Nichtöffentliche Inhalte gibt es über Tokens
nicht, bis das Zugriffsmodell abgestimmt und geprüft ist (#565). Nimmt die Plattform-Administration einer
Anwendung einen Scope weg, gilt er auch für ihre bestehenden Tokens sofort nicht mehr, ebenso bei `userinfo` und
in neuen Tokens aus bestehenden Refresh-Tokens. Für gesperrte Konten gibt es keine neuen Tokens.

## Code-Ablauf Schritt für Schritt

**1. PKCE vorbereiten.** Die Anwendung erzeugt einen zufälligen `code_verifier` (43 bis 128 Zeichen) und daraus
`code_challenge = BASE64URL(SHA-256(code_verifier))` ohne Auffüllzeichen.

**2. Zur Autorisierung weiterleiten:**

```text
GET https://mandari.example.org/oauth/authorize/
    ?response_type=code
    &client_id=<client-id>
    &redirect_uri=https://app.example.org/rueckweg
    &scope=openid%20profile%20ratsdaten:lesen
    &state=<zufall>
    &nonce=<zufall>
    &code_challenge=<challenge>
    &code_challenge_method=S256
```

Ist die Person nicht angemeldet, meldet sie sich bei mandari an, mit zweitem Faktor nach den Regeln ihres Kontos.
Eine Sitzung, in der der zweite Faktor noch aussteht oder eine Pflicht zum zweiten Faktor offen ist, gilt nicht als
angemeldet. Danach stimmt die Person zu (bei fremden Anwendungen) und kommt zurück:

```text
https://app.example.org/rueckweg?code=<code>&state=<zufall>&iss=https%3A%2F%2Fmandari.example.org
```

Die Anwendung prüft `state` und `iss`. Lehnt die Person ab, kommt `error=access_denied` zurück.

Weitere Parameter:

- `prompt=none`: ohne jede Anzeige. Gelingt nur, wenn die Person angemeldet ist und die Anwendung
  vertrauenswürdig ist; sonst `error=login_required` bzw. `error=consent_required`.
- `prompt=login` oder `max_age=<sekunden>`: Die Person meldet sich erneut an (auch mit zweitem Faktor). Bis
  dahin gibt mandari für diese Anfrage (gleiche `client_id` und gleicher `state`) keinen Code aus. Der Zeitpunkt
  der Anmeldung steht im ID-Token als `auth_time`; wer eine frische Anmeldung braucht, prüft ihn. Gemeint ist die
  Anmeldung in der Browsersitzung, aus der der Code stammt, nicht die letzte Anmeldung auf irgendeinem Gerät.
  ID-Tokens aus einem Refresh tragen denselben Zeitpunkt. Ist er nicht bekannt (Sitzung von vor dieser Regel),
  fehlt `auth_time`, und `max_age` führt immer zur erneuten Anmeldung.
- `id_token_hint`: ein früheres ID-Token von mandari, auch abgelaufen. Gehört es zu einer anderen Person als der
  angemeldeten oder ist es nicht von mandari signiert, kommt `error=login_required` statt eines Codes.
- `approval_prompt` beachtet mandari nicht: Bei fremden Anwendungen stimmt die Person jedes Mal zu.

**3. Code einlösen** (der Code gilt 60 Sekunden und nur einmal):

```bash
curl -u '<client-id>:<client-secret>' https://mandari.example.org/oauth/token/ \
  -d grant_type=authorization_code \
  -d code=<code> \
  -d redirect_uri=https://app.example.org/rueckweg \
  -d code_verifier=<verifier>
```

Öffentliche Anwendungen ohne Secret schicken statt `-u` den Parameter `client_id`. Die Antwort:

```json
{
  "access_token": "mandari_at_…",
  "token_type": "Bearer",
  "expires_in": 900,
  "refresh_token": "mandari_rt_…",
  "scope": "openid profile ratsdaten:lesen",
  "id_token": "eyJ…"
}
```

**4. ID-Token prüfen:** Signatur RS256 mit dem Schlüssel aus `/oauth/jwks/` (passend zu `kid`), dazu `iss`,
`aud` (die eigene Client-ID), `exp` und `nonce`. `sub` ist die dauerhafte Kennung der Person; die E-Mail-Adresse
kann sich ändern und taugt nicht als Kennung.

## Tokens

- **Access-Token** gelten 15 Minuten (`OIDC_ACCESS_TOKEN_SECONDS`) und beginnen mit `mandari_at_`.
- **Refresh-Token** beginnen mit `mandari_rt_` und werden bei jeder Nutzung ersetzt (Rotation). Wird ein schon
  ersetztes Refresh-Token noch einmal benutzt, widerruft mandari die ganze Kette; die Person muss sich dann neu
  anmelden. Das gilt auch für zwei gleichzeitige Refreshes mit demselben Token: Anwendungen erneuern nacheinander,
  nie parallel (etwa aus mehreren Tabs). Ein Refresh-Token verfällt 30 Tage nach dem Ablauf seines letzten
  Access-Tokens (`OIDC_REFRESH_TOKEN_DAYS`).

```bash
curl -u '<client-id>:<client-secret>' https://mandari.example.org/oauth/token/ \
  -d grant_type=refresh_token -d refresh_token=mandari_rt_…
```

- **Widerruf** durch die Anwendung, etwa beim Abmelden in der App (Access- oder Refresh-Token, beide werden
  ungültig):

```bash
curl -u '<client-id>:<client-secret>' https://mandari.example.org/oauth/revoke/ -d token=mandari_rt_…
```

- **Introspektion** nur für die eigenen Tokens und nur mit Client-Secret. Die Antwort nennt `active`, `scope`,
  `client_id`, `exp`, `iat`, `iss` und bei Personen `sub`; fremde Tokens sind `{"active": false}`.

Personen sehen und widerrufen ihre Anwendungen im Konto unter **„Verbundene Anwendungen“**
(`/accounts/verbundene-anwendungen/`), erreichbar über das Personenmenü in Work, Session und Insight und über die
Seite „Sicherheit“ in Work. Gesperrte Konten und ausgeschaltete Anwendungen verlieren ihre Tokens sofort. Beendet
eine Person alle anderen Sitzungen oder ändert sie ihr Passwort, verlieren alle Anwendungen ihren Zugang und fragen
erneut.

## Client Credentials

```bash
curl -u '<client-id>:<client-secret>' https://mandari.example.org/oauth/token/ \
  -d grant_type=client_credentials -d scope=ratsdaten:lesen
```

Ohne `scope` bekommt der Dienst alle für ihn freigegebenen Scopes. Ein nicht freigegebener Scope ergibt
`invalid_scope`.

## Ratsdaten lesen: Session-API v1

Die [Session-API v1](../API_V1_SESSION.md) nimmt Access-Tokens mit dem Scope `ratsdaten:lesen` an:

```bash
curl -H 'Authorization: Bearer mandari_at_…' https://mandari.example.org/api/v1/session/<mandant>/meetings/
```

- **Im Namen einer Person** liest das Token höchstens, was die Person mit eigener Anmeldung über die Schnittstelle
  lesen dürfte: nur mit dem Recht „API-Zugang“ in diesem Mandanten, sonst wie ein anonymer Aufruf. Nichtöffentliche
  Sitzungen und Vorlagen, Texte, interne Notizen und Anträge gibt es nicht, auch wenn die Person sie sehen darf.
- **Ein Dienst** (Client Credentials) liest nur, was anonym lesbar ist.
- Einreichen geht über Access-Tokens noch nicht; dafür bleibt bis zum Profil Einreichung das API-Token des
  Mandanten.

Fehler kommen als `application/problem+json` (RFC 9457) mit `WWW-Authenticate` nach RFC 6750: 401 mit
`error="invalid_token"` für unbekannte, abgelaufene oder widerrufene Tokens, 403 mit
`error="insufficient_scope"` ohne `ratsdaten:lesen`. Die bisherigen API-Tokens der Session-API (64 Zeichen)
funktionieren unverändert.

## Fehler am Token-Endpunkt

| Fehler | Bedeutung |
|---|---|
| `invalid_client` (401) | Client-ID oder Secret falsch, Anwendung ausgeschaltet |
| `invalid_grant` | Code abgelaufen, schon benutzt oder `code_verifier` passt nicht; Refresh-Token widerrufen oder ersetzt |
| `invalid_scope` | Scope nicht für die Anwendung freigegeben |
| `unauthorized_client` | Ablauf für die Anwendung nicht freigegeben |
| `unsupported_grant_type` | Ablauf gibt es bei mandari nicht |
| `rate_limited` (429) | zu viele Anfragen von dieser Adresse; `Retry-After` beachten (gilt auch für Widerruf und Introspektion) |

Passt bei der Autorisierung die Redirect-URI oder Client-ID nicht, leitet mandari nicht weiter, sondern zeigt
der Person eine Fehlerseite.

## Betrieb

Schlüssel erzeugen, Umgebungsvariablen, Proxy-Routen, Prüfung mit `manage.py oidc_pruefen`, Einschalten, Anwendungen
mit `manage.py oidc_anwendung` anlegen, Schlüsselwechsel, Widerruf, Prüfprotokoll und Speicherung:
[Anmeldung für Anwendungen: Betrieb](anmeldung-betrieb.md).
