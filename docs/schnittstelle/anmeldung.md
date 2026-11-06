# Anmeldung für Anwendungen: mandari als OpenID-Connect-Anbieter

Stand: 2026-11-06 (Issues #1206, #1228 und #565, Teil von Epic #1203). Grundlage ist das ADR
[Eine Schnittstelle: API-first](../adr/20261105-eine-schnittstelle-api-first.md).

Anwendungen melden Personen über mandari an und erhalten ein begrenztes, widerrufbares Token. Die Anmeldung
selbst bleibt die von mandari, einschließlich des zweiten Faktors. Dienste ohne Person bekommen ihr Token über
Client Credentials, mit Client-Secret oder mit `private_key_jwt` (RFC 7523). Nichtöffentliches lesen Anwendungen nur
über die [nichtöffentliche Ausgabe](#nichtöffentliches-die-nichtöffentliche-ausgabe) nach dem Profil
Nichtöffentliches von kommParl. Umgesetzt ist das mit [django-oauth-toolkit](https://github.com/django-oauth/django-oauth-toolkit)
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
| Client Credentials | Dienste ohne Person | mit Client-Secret oder `private_key_jwt`, kein ID-Token, kein Refresh-Token |
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
| `einreichung:schreiben` | Profil Einreichung: einreichen, zurücknehmen, zur Ladung zurückmelden (nur im Namen einer Person wirksam) | ja | ja |
| `mandari:bereitstellung` | Bereitstellung von Organisationen und Mandanten (folgt) | nein | ja |
| `ratsdaten:nichtoeffentlich` | Nichtöffentliches lesen, soweit die Person es selbst sehen darf (nichtöffentliche Ausgabe); zweiter Faktor Pflicht, höchstens 7 Tage nach der Anmeldung | ja | nein |
| `sitzung:lesen` | Alle Sitzungsdaten des gebundenen Mandanten einschließlich Nichtöffentlichem (nichtöffentliche Ausgabe); nur für Dienste mit Bindung an einen Mandanten | nein | ja |

Ein Token im Namen einer Person darf nie mehr als die Person selbst. Nichtöffentliches gibt es nur mit den beiden
letzten Scopes und nur über die [nichtöffentliche Ausgabe](#nichtöffentliches-die-nichtöffentliche-ausgabe); beide
Scopes muss die Plattform-Administration einer Anwendung ausdrücklich erlauben. Nimmt die Plattform-Administration einer
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

## Client Credentials mit `private_key_jwt`

Dienste mit öffentlichen Schlüsseln an der Anwendung (JWK Set, nur RS256 mit RSA ab 2048 Bit oder ES256 mit P-256)
weisen sich statt mit Client-Secret mit einem selbst signierten JWT aus (RFC 7523, Abschnitt 2.2). Ein Client-Secret
gilt für solche Anwendungen nicht. Dienste mit Bindung an einen Mandanten melden sich nur so an.

```bash
curl https://mandari.example.org/oauth/token/ \
  -d grant_type=client_credentials -d scope=sitzung:lesen \
  -d client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer \
  -d client_assertion=eyJ…
```

Das JWT trägt im Kopf `alg` (`RS256` oder `ES256`) und `kid` (Schlüssel-ID aus dem JWK Set), im Inhalt:

| Angabe | Wert |
|---|---|
| `iss`, `sub` | die Client-ID |
| `aud` | die Adresse des Token-Endpunkts, `https://mandari.example.org/oauth/token/` |
| `exp` | höchstens fünf Minuten in der Zukunft |
| `jti` | eindeutig; ein zweites Mal wird die Assertion abgelehnt |
| `iat` | empfohlen |

Jede Abweichung ergibt `invalid_client` (401). Widerruf und Introspektion nehmen dieselbe Anmeldung an. Neue Schlüssel
trägt der Betrieb mit `manage.py oidc_anwendung schluessel` ein ([Betrieb](anmeldung-betrieb.md#6-anwendungen-anlegen)).

## Nichtöffentliches: die nichtöffentliche Ausgabe

Umsetzung des Profils Nichtöffentliches von kommParl (Issue #565). Standardmäßig aus: Es gibt sie nur mit
`OIDC_PROVIDER_ENABLED` und `OPARL_NONPUBLIC_ENABLED`, sonst antworten alle Adressen mit 404. **Vor dem Einschalten in
Produktion steht ein externer Penetrationstest an.**

**Adressen.** Je Session-Mandant eine eigene OParl-Ausgabe mit eigenem System-Objekt und eigenen Kennungen:

| Zweck | Pfad |
|---|---|
| System | `/session/<mandant>/api/oparl/nichtoeffentlich/` |
| Körperschaft, Liste der Körperschaften | `…/nichtoeffentlich/body/`, `…/nichtoeffentlich/bodies/` |
| Listen | `…/nichtoeffentlich/<liste>/` (`meetings`, `agendaitems`, `papers`, `files`, `consultations`, `organizations`, `people`, `memberships`, `legislativeterms`) |
| Objekte | `…/nichtoeffentlich/<art>/<kennung>/` |
| Dateien | `…/nichtoeffentlich/file/<kennung>/download/` |
| Metadaten (RFC 9728) | `/.well-known/oauth-protected-resource/session/<mandant>/api/oparl/nichtoeffentlich/` |

Die öffentliche Ausgabe bleibt unverändert, auch mit Token. Ist die nichtöffentliche Ausgabe eingeschaltet, nennt nur
das öffentliche `System` zusätzlich das Profil in `kommparl:conformsTo` und die Adresse in `kommparl:nonPublicSystem`.
Änderungsfeed und Snapshot enthalten nie Nichtöffentliches; die nichtöffentliche Ausgabe bietet keine an. Anwendungen
gleichen ihren Bestand regelmäßig vollständig ab und löschen, was nicht mehr enthalten ist.

**Inhalte.** Alles, was der Aufrufer sehen darf, öffentlich wie nichtöffentlich, mit Verweisen innerhalb der
Ausgabe. Nichtöffentliche Sitzungen, Tagesordnungspunkte, Vorlagen, Beratungen, Anlagen und Orte tragen
`kommparl:nonPublic: true`, nichtöffentliche Tagesordnungspunkte zusätzlich `public: false`. Objekte, die auch öffentlich
abrufbar sind, nennen ihre öffentliche Adresse in `kommparl:publicId`. Der nichtöffentliche Teil eines Beschlusstexts
steht in `kommparl:nonPublicResolutionText`. Vorlagen im Entwurf oder in der Prüfung gibt es nicht. Was der Aufrufer
nicht sehen darf, fehlt in Listen und Einbettungen und ergibt am Objekt 404.

**Im Namen einer Person** (Scope `ratsdaten:nichtoeffentlich`, Code-Ablauf mit PKCE):

- Die Person braucht einen zweiten Faktor; ohne ihn führt die Autorisierung zur Einrichtung, und ein Token verliert
  den Scope, sobald der zweite Faktor entfernt ist.
- Liegt die Anmeldung in der Sitzung mehr als sieben Tage zurück, meldet sich die Person erneut an. Tokens mit diesem
  Scope gibt es höchstens sieben Tage nach der Anmeldung; danach endet der Refresh mit `invalid_grant`, und das letzte
  Access-Token läuft spätestens dann ab.
- Gelesen wird bei jedem Abruf genau, was die Person in Session sieht: dieselbe Regel wie die Oberfläche (Rechte
  „Nichtöffentliche Sitzungen und TOPs sehen“ und „Nichtöffentliche Vorlagen sehen“ samt Sichtrecht), bei
  eingeschalteten Rechten mit Geltungsbereich auch Zuweisungen für Körperschaften, Gremien und Ämter und befristete
  Zuweisungen etwa für Vertretungen, solange sie gelten. Ein festgestelltes Mitwirkungsverbot (in der Abstimmung als
  „befangen“ vermerkt) sperrt den nichtöffentlichen Tagesordnungspunkt, seine Anlagen und den nichtöffentlichen
  Beschlusstext. Als Person des Kontos gelten Personen des Mandanten mit derselben E-Mail-Adresse.
- Ohne aktives Konto im Mandanten gibt es 403.

**Dienste je Mandant** (Scope `sitzung:lesen`, Client Credentials mit `private_key_jwt`): Die Anwendung ist an genau
einen Session-Mandanten gebunden und liest alles Nichtöffentliche dieses Mandanten, nie das eines anderen (403).

**Antworten** tragen `Cache-Control: private, no-store` und `Vary: Authorization`, keine CORS-Freigabe und keinen
`ETag`. Fehler kommen als `application/problem+json`:

| Status | Bedeutung |
|---|---|
| 401 | kein, ungültiges, abgelaufenes oder widerrufenes Token; `WWW-Authenticate: Bearer resource_metadata="…"`, bei mitgeschicktem Token mit `error="invalid_token"` |
| 403 | Scope fehlt (`error="insufficient_scope"`, `scope="…"`), kein Konto im Mandanten oder Dienst eines anderen Mandanten |
| 404 | gibt es nicht oder darf der Aufrufer nicht sehen |
| 503 | der Abruf konnte nicht protokolliert werden; ohne Eintrag gibt es keine Inhalte |

**Prüfprotokoll:** Jeder Abruf, dessen Antwort Nichtöffentliches enthält, steht im Protokoll des Mandanten (Session,
„Protokoll“) mit Person bzw. Dienst, Anwendung (Client-ID) und den Kennungen der nichtöffentlichen Objekte, ohne
Inhalte und ohne Zusammenfassung.

## Ratsdaten lesen: Session-API v1

Die [Session-API v1](../API_V1_SESSION.md) nimmt Access-Tokens mit dem Scope `ratsdaten:lesen` an:

```bash
curl -H 'Authorization: Bearer mandari_at_…' https://mandari.example.org/api/v1/session/<mandant>/meetings/
```

- **Im Namen einer Person** liest das Token höchstens, was die Person mit eigener Anmeldung über die Schnittstelle
  lesen dürfte: nur mit dem Recht „API-Zugang“ in diesem Mandanten, sonst wie ein anonymer Aufruf. Nichtöffentliche
  Sitzungen und Vorlagen, Texte, interne Notizen und Anträge gibt es hier nicht, auch wenn die Person sie sehen darf;
  Nichtöffentliches liefert die [nichtöffentliche Ausgabe](#nichtöffentliches-die-nichtöffentliche-ausgabe).
- **Ein Dienst** (Client Credentials) liest nur, was anonym lesbar ist.
- Einreichen geht in der Session-API v1 weiter nur mit dem API-Token des Mandanten. Mit Access-Tokens reichen
  Anwendungen über das Profil Einreichung ein (nächster Abschnitt).

Fehler kommen als `application/problem+json` (RFC 9457) mit `WWW-Authenticate` nach RFC 6750: 401 mit
`error="invalid_token"` für unbekannte, abgelaufene oder widerrufene Tokens, 403 mit
`error="insufficient_scope"` ohne `ratsdaten:lesen`. Die bisherigen API-Tokens der Session-API (64 Zeichen)
funktionieren unverändert.

## Einreichen: Profil Einreichung

Anträge und Anfragen einreichen, den Bearbeitungsstand abrufen, Einreichungen zurücknehmen und sich zur Ladung
zurückmelden: über das [Profil Einreichung von kommParl](../SCHNITTSTELLE.md#profil-einreichung-1228) je
Session-Mandant. Die Adressen stehen an Body und Sitzung der OParl-Ausgabe des Mandanten
(`kommparl:submission`, `kommparl:submissionUpload`, `kommparl:submissionRules`, `kommparl:attendanceResponse`,
`kommparl:invitationReceipt`).

```bash
curl -X POST -H 'Authorization: Bearer mandari_at_…' -H 'Idempotency-Key: 5b0f2c1e-8d3a-4f7e-9a61-2c7d0e4b9f10'   -H 'Content-Type: application/json'   -d '{"submissionType": "motion", "name": "…", "proposalText": "…", "reasoningText": "…"}'   https://mandari.example.org/session/<mandant>/api/oparl/body/kommparl/submissions/
```

- Nur **Access-Tokens im Namen einer Person** mit dem Scope `einreichung:schreiben`. Tokens von Diensten (Client
  Credentials) ergeben 403, API-Tokens der Session-API 401; eine angemeldete Browsersitzung zählt hier nicht.
- Die Person darf über das Token nur, was sie selbst darf: Einreichen für eine Organisation, in der sie das Recht
  „Anträge ans RIS übermitteln“ hat und die mit der Verwaltung verbunden ist (wie aus Work), zurückmelden zu
  Sitzungen, zu denen sie geladen ist (wie im Portal). Sie sieht und nimmt nur ihre eigenen Einreichungen zurück,
  nicht die anderer Mitglieder derselben Organisation. Anlagen hochladen kann nur, wer bei der Verwaltung einreichen
  darf.
- Jede Handlung steht im Prüfprotokoll des Mandanten mit Person und Client-ID der Anwendung.

## Fehler am Token-Endpunkt

| Fehler | Bedeutung |
|---|---|
| `invalid_client` (401) | Client-ID oder Secret falsch, Anwendung ausgeschaltet, Client-Assertion (`private_key_jwt`) ungültig, abgelaufen oder schon benutzt, Secret bei einer Anwendung mit Schlüsseln |
| `invalid_grant` | Code abgelaufen, schon benutzt oder `code_verifier` passt nicht; Refresh-Token widerrufen oder ersetzt; für `ratsdaten:nichtoeffentlich` Anmeldung älter als sieben Tage oder kein zweiter Faktor |
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
