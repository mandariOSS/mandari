# Anmeldung für Anwendungen: mandari als OpenID-Connect-Anbieter

Stand: 2026-11-06 (Issues #1206, #1215, #1228 und #565, Teil von Epic #1203). Grundlage ist das ADR
[Eine Schnittstelle: API-first](../adr/20261105-eine-schnittstelle-api-first.md).

Anwendungen melden Personen über mandari an und erhalten ein begrenztes, widerrufbares Token. Die Anmeldung
selbst bleibt die von mandari, einschließlich des zweiten Faktors. Dienste ohne Person bekommen ihr Token über
Client Credentials, mit Client-Secret oder mit `private_key_jwt` (RFC 7523). Nichtöffentliches lesen Anwendungen nur
über die [nichtöffentliche Ausgabe](#nichtöffentliches-die-nichtöffentliche-ausgabe) nach dem Profil
Nichtöffentliches von kommParl. Umgesetzt ist das mit [django-oauth-toolkit](https://github.com/django-oauth/django-oauth-toolkit)
in der Plattform (`mandari/apps/accounts/oidc/`).

> **Standardmäßig aus.** Ohne `OIDC_PROVIDER_ENABLED=true` antworten alle Endpunkte mit 404, auch die Discovery,
> und kein Access-Token wird angenommen. Vor dem Einschalten in Produktion steht eine Sicherheitsprüfung an.
> Einschalten, Schlüssel, Proxy und das Anlegen von Anwendungen beschreibt [Teil B: Betrieb](#teil-b-betrieb).

## Teil A: Für Anwendungen

### Endpunkte

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

### Abläufe

| Ablauf | Für | Merkmale |
|---|---|---|
| Authorization Code mit PKCE | Anwendungen im Namen einer Person (App, Partnerdienst mit Oberfläche) | PKCE mit `S256` ist Pflicht, auch für Anwendungen mit Secret |
| Client Credentials | Dienste ohne Person | mit Client-Secret oder `private_key_jwt`, kein ID-Token, kein Refresh-Token |
| Refresh Token | beide Code-Anwendungen | Rotation bei jeder Nutzung |

Implicit, Password-Grant, Hybrid und Gerätefreigabe (RFC 8628) sind abgeschaltet. Access-Tokens werden nur im
Header `Authorization: Bearer …` angenommen, nie in der Adresse.

### Anwendung eintragen lassen

Anwendungen trägt die Plattform-Administration im Admin unter „OIDC-Anwendungen“ oder mit
`manage.py oidc_anwendung` ein ([Betrieb](#6-anwendungen-anlegen)). Dafür braucht sie:

- **Name**, wie er auf der Zustimmungsseite und unter „Verbundene Anwendungen“ erscheinen soll
- **Ablauf**: Code-Ablauf oder Client Credentials
- **Art**: öffentlich (App ohne Secret, etwa auf dem Telefon) oder vertraulich (mit Secret, Server)
- **Redirect-URIs**: nur `https`, sie müssen beim Aufruf exakt übereinstimmen (ohne Platzhalter, ohne Fragment).
  `http` gilt nur für `localhost` und nur in einer Entwicklungsinstallation (`DEBUG`).
- **Erlaubte Scopes**: mehr kann die Anwendung nicht anfordern

Die Anwendung erhält Client-ID und gegebenenfalls Client-Secret. Das Secret steht nur beim Anlegen im Klartext
im Formular bzw. in der Ausgabe des Befehls; gespeichert wird es gehasht. Eigene Anwendungen von mandari können als
vertrauenswürdig markiert werden und kommen ohne Zustimmungsseite aus. Fremde Anwendungen fragen die Person jedes Mal.

### Scopes

| Scope | Bedeutung | Code-Ablauf | Client Credentials |
|---|---|---|---|
| `openid` | Anmeldung, ID-Token mit `sub` | ja | nein |
| `profile` | Name (`name`, `given_name`, `family_name`) | ja | nein |
| `email` | E-Mail-Adresse (`email`, `email_verified`) | ja | nein |
| `ratsdaten:lesen` | Ratsdaten lesen, siehe unten | ja | ja |
| `einreichung:schreiben` | Profil Einreichung: einreichen, zurücknehmen, zur Ladung zurückmelden (nur im Namen einer Person wirksam) | ja | ja |
| `mandari:bereitstellung` | Bereitstellung von Organisationen und Mandanten (folgt) | nein | ja |
| `ratsdaten:nichtoeffentlich` | Nichtöffentliches lesen, soweit die Person es selbst sehen darf (nichtöffentliche Ausgabe); Anmeldung mit zweitem Faktor Pflicht, höchstens 7 Tage danach | ja | nein |
| `sitzung:lesen` | Alle Sitzungsdaten des gebundenen Mandanten einschließlich Nichtöffentlichem (nichtöffentliche Ausgabe); nur für Dienste mit Bindung an einen Mandanten | nein | ja |

Ein Token im Namen einer Person darf nie mehr als die Person selbst. Nichtöffentliches gibt es nur mit den beiden
letzten Scopes und nur über die [nichtöffentliche Ausgabe](#nichtöffentliches-die-nichtöffentliche-ausgabe); beide
Scopes muss die Plattform-Administration einer Anwendung ausdrücklich erlauben. Nimmt die Plattform-Administration einer
Anwendung einen Scope weg, gilt er auch für ihre bestehenden Tokens sofort nicht mehr, ebenso bei `userinfo` und
in neuen Tokens aus bestehenden Refresh-Tokens. Für gesperrte Konten gibt es keine neuen Tokens.

### Code-Ablauf Schritt für Schritt

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

### Tokens

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

### Client Credentials

```bash
curl -u '<client-id>:<client-secret>' https://mandari.example.org/oauth/token/ \
  -d grant_type=client_credentials -d scope=ratsdaten:lesen
```

Ohne `scope` bekommt der Dienst alle für ihn freigegebenen Scopes. Ein nicht freigegebener Scope ergibt
`invalid_scope`.

### Client Credentials mit `private_key_jwt`

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
| `aud` | der Aussteller (`issuer` der Discovery), `https://mandari.example.org`, als einzelne Zeichenkette. Die Adresse des Token-Endpunkts und Listen gelten nicht (draft-ietf-oauth-rfc7523bis, Schutz vor Audience Injection) |
| `exp` | höchstens fünf Minuten in der Zukunft |
| `jti` | eindeutig; ein zweites Mal wird die Assertion abgelehnt |
| `iat` | empfohlen |

Jede Abweichung ergibt `invalid_client` (401). Widerruf und Introspektion nehmen dieselbe Anmeldung an. Neue Schlüssel
trägt der Betrieb mit `manage.py oidc_anwendung schluessel` ein ([Betrieb](#6-anwendungen-anlegen)).

### Nichtöffentliches: die nichtöffentliche Ausgabe

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
  den Scope, sobald der zweite Faktor entfernt ist. Maßgeblich ist die Anmeldung selbst: War sie ohne zweiten Faktor
  (etwa weil er erst danach eingerichtet wurde), meldet sich die Person erneut an, diesmal mit Faktor.
- Liegt die Anmeldung in der Sitzung mehr als sieben Tage zurück, meldet sich die Person erneut an. Tokens mit diesem
  Scope gibt es höchstens sieben Tage nach der Anmeldung; danach endet der Refresh mit `invalid_grant`, und das letzte
  Access-Token läuft spätestens dann ab.
- Gelesen wird bei jedem Abruf genau, was die Person in Session sieht: dieselbe Regel wie die Oberfläche (Rechte
  „Nichtöffentliche Sitzungen und TOPs sehen“ und „Nichtöffentliche Vorlagen sehen“ samt Sichtrecht), bei
  eingeschalteten Rechten mit Geltungsbereich auch Zuweisungen für Körperschaften, Gremien und Ämter und befristete
  Zuweisungen etwa für Vertretungen, solange sie gelten. Ein festgestelltes Mitwirkungsverbot (in der Abstimmung als
  „befangen“ vermerkt) sperrt den nichtöffentlichen Tagesordnungspunkt, seine Anlagen, den nichtöffentlichen
  Beschlusstext und die dort beratenen Vorlagen mit ihren Anlagen, soweit sie nicht öffentlich sind. Als Person des
  Kontos gelten Personen des Mandanten mit derselben E-Mail-Adresse oder die mit dem Konto verknüpfte Person; ohne
  solche Person greift das Mitwirkungsverbot nicht (siehe [Betrieb](#6-anwendungen-anlegen)).
- Die Person braucht ein aktives Konto im Mandanten mit dem Recht „API-Zugang“, wie in der Session-API v1. Ohne Konto
  oder ohne dieses Recht gibt es 403, auch mit einer freigegebenen Anwendung: Der Mandant entscheidet, wer seine Daten
  über die Schnittstelle abrufen darf.

**Dienste je Mandant** (Scope `sitzung:lesen`, Client Credentials mit `private_key_jwt`): Die Anwendung ist an genau
einen Session-Mandanten gebunden und liest alles Nichtöffentliche dieses Mandanten, nie das eines anderen (403).

**Antworten** tragen `Cache-Control: private, no-store` und `Vary: Authorization`, keine CORS-Freigabe und keinen
`ETag`. Fehler kommen als `application/problem+json`:

| Status | Bedeutung |
|---|---|
| 401 | kein, ungültiges, abgelaufenes oder widerrufenes Token; `WWW-Authenticate: Bearer resource_metadata="…"`, bei mitgeschicktem Token mit `error="invalid_token"` |
| 403 | Scope fehlt (`error="insufficient_scope"`, `scope="…"`), kein Konto im Mandanten, kein „API-Zugang“ oder Dienst eines anderen Mandanten |
| 404 | gibt es nicht oder darf der Aufrufer nicht sehen |
| 503 | der Abruf konnte nicht protokolliert werden; ohne Eintrag gibt es keine Inhalte |

**Prüfprotokoll:** Jeder Abruf, dessen Antwort Nichtöffentliches enthält, steht im Protokoll des Mandanten (Session,
„Protokoll“) mit Person bzw. Dienst, Anwendung (Client-ID) und den Kennungen der nichtöffentlichen Objekte, ohne
Inhalte und ohne Zusammenfassung. Dazu zählen auch Beratungen einer veröffentlichten Vorlage, die Gremium, Rolle,
Sitzung oder Tagesordnungspunkt einer nichtöffentlichen Station nennen (sie tragen `kommparl:nonPublic` und
`kommparl:publicId`), und Verweise öffentlicher Objekte auf nichtöffentliche, etwa eines öffentlichen
Tagesordnungspunkts auf die Beratung einer nichtöffentlichen Vorlage. Ein Dienst steht dort immer als Dienst, auch wenn
seine Anfrage das Sitzungs-Cookie einer Person mitbringt.

### Ratsdaten lesen: Session-API v1

Die [Session-API v1](../SCHNITTSTELLE.md#session-api-v1) nimmt Access-Tokens mit dem Scope `ratsdaten:lesen` an:

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

### Einreichen: Profil Einreichung

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

### Fehler am Token-Endpunkt

| Fehler | Bedeutung |
|---|---|
| `invalid_client` (401) | Client-ID oder Secret falsch, Anwendung ausgeschaltet, Client-Assertion (`private_key_jwt`) ungültig, abgelaufen oder schon benutzt, Secret bei einer Anwendung mit Schlüsseln |
| `invalid_grant` | Code abgelaufen, schon benutzt oder `code_verifier` passt nicht; Refresh-Token widerrufen oder ersetzt; für `ratsdaten:nichtoeffentlich` Anmeldung älter als sieben Tage oder kein zweiter Faktor mehr am Konto |
| `invalid_scope` | Scope nicht für die Anwendung freigegeben |
| `unauthorized_client` | Ablauf für die Anwendung nicht freigegeben |
| `unsupported_grant_type` | Ablauf gibt es bei mandari nicht |
| `rate_limited` (429) | zu viele Anfragen von dieser Adresse; `Retry-After` beachten (gilt auch für Widerruf und Introspektion) |

Passt bei der Autorisierung die Redirect-URI oder Client-ID nicht, leitet mandari nicht weiter, sondern zeigt
der Person eine Fehlerseite.

## Teil B: Betrieb

Für Betreiber einer mandari-Installation (Issues #1215 und #565). mandari ist OpenID-Connect-Anbieter, aber standardmäßig ausgeschaltet. Diese Anleitung führt durch das Einschalten
auf einer neuen Installation, das Anlegen der ersten Anwendung, den Schlüsselwechsel und den Widerruf.

### Überblick

1. Signaturschlüssel erzeugen
2. Schlüssel einbinden und Umgebungsvariablen setzen
3. Reverse Proxy prüfen
4. Mit `manage.py oidc_pruefen` prüfen, solange der Anbieter noch aus ist
5. Einschalten, neu starten, erneut prüfen
6. Erste Anwendung mit `manage.py oidc_anwendung` anlegen

Die Befehle laufen im Container der Anwendung, bei Docker Compose etwa so:

```bash
docker compose exec mandari python manage.py oidc_pruefen
```

### 1. Signaturschlüssel erzeugen

mandari signiert ID-Tokens mit RS256. Verlangt ist ein unverschlüsselter RSA-Schlüssel mit mindestens 2048 Bit,
empfohlen sind 3072 Bit:

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out oidc.pem
chmod 400 oidc.pem
```

- Die Datei ist ein Geheimnis wie `SECRET_KEY`. Sie gehört nicht ins Repository und nicht in Sicherungen der
  Anwendungsdaten. Eine Kopie gehört dorthin, wo die übrigen Zugangsdaten der Installation liegen.
- Die Anwendung im Container muss die Datei lesen können. Die Nutzerkennung zeigt
  `docker compose exec mandari id -u`; die Datei bekommt diesen Eigentümer (`chown <kennung> oidc.pem`).
- Access- und Refresh-Tokens sind nicht signiert, sondern Zufallswerte. Der Schlüssel betrifft nur ID-Tokens.

### 2. Schlüssel einbinden und Umgebungsvariablen

| Variable | Bedeutung | Standard |
|---|---|---|
| `OIDC_PROVIDER_ENABLED` | Schalter. Aus: alle Endpunkte antworten mit 404, kein Access-Token gilt | `false` |
| `OIDC_RSA_PRIVATE_KEY_FILE` | Pfad zur PEM-Datei im Container (bevorzugt) | – |
| `OIDC_RSA_PRIVATE_KEY` | alternativ der PEM-Text selbst, Zeilenumbrüche als `\n` | – |
| `OIDC_RSA_PRIVATE_KEY_PREVIOUS_FILE` | nur beim Schlüsselwechsel: bisheriger Schlüssel, mehrere Pfade durch Komma | – |
| `OIDC_RSA_PRIVATE_KEY_PREVIOUS` | wie oben als PEM-Text | – |
| `OIDC_ACCESS_TOKEN_SECONDS` | Lebensdauer von Access- und ID-Tokens | `900` |
| `OIDC_REFRESH_TOKEN_DAYS` | Tage, die ein ungenutztes Refresh-Token nach Ablauf seines Access-Tokens gilt | `30` |
| `OIDC_TOKEN_RATE_LIMIT` | Anfragen je Minute und Adresse an Token-Endpunkt, Widerruf und Introspektion (IPv6 je /64), `0` = aus | `30` |
| `SITE_URL` | Aussteller (`iss`): `https://<host>` ohne Pfad. Bei Docker Compose aus `DOMAIN` gesetzt | – |
| `OPARL_NONPUBLIC_ENABLED` | Nichtöffentliche Ausgabe je Session-Mandant (Profil Nichtöffentliches, #565); wirkt nur mit eingeschaltetem Anbieter. Vorher externer Penetrationstest und Zuordnung der Konten zu Personen (siehe unten) | `false` |

**Ein Schlüssel für alle Dienste mit den Einstellungen der Anwendung.** Bei Docker Compose lesen die Anwendung und die
drei Worker (`worker`, `worker-heavy`, `worker-live`) dieselbe `.env`. Ist der Anbieter eingeschaltet, startet keiner
dieser Dienste ohne lesbaren Schlüssel. Die Datei wird deshalb in alle vier eingebunden, etwa mit einer
`docker-compose.override.yml` neben der `docker-compose.yml` (sie ist von Git ausgenommen):

```yaml
services:
  mandari:
    volumes:
      - ./secrets/oidc.pem:/run/secrets/oidc.pem:ro
  worker:
    volumes:
      - ./secrets/oidc.pem:/run/secrets/oidc.pem:ro
  worker-heavy:
    volumes:
      - ./secrets/oidc.pem:/run/secrets/oidc.pem:ro
  worker-live:
    volumes:
      - ./secrets/oidc.pem:/run/secrets/oidc.pem:ro
```

In der `.env`:

```bash
OIDC_RSA_PRIVATE_KEY_FILE=/run/secrets/oidc.pem
```

Der Schlüssel wird gelesen, sobald er eingestellt ist, auch bei ausgeschaltetem Anbieter. So lassen sich Prüfung und
Anwendungen vorbereiten. Ein untauglicher Schlüssel hält die Anwendung nur bei eingeschaltetem Anbieter vom Start ab;
vorher meldet ihn `oidc_pruefen`. In Kubernetes gilt dasselbe: den Schlüssel als Secret in alle Pods einbinden, die
mit den Einstellungen der Anwendung starten.

### 3. Reverse Proxy

Der Anbieter braucht drei Pfade an der Wurzel des Ausstellers:

- `/oauth/*`: Autorisierung, Token, Widerruf, Introspektion, Angaben zur Person, JWKS
- `/.well-known/openid-configuration`: Discovery
- `/.well-known/oauth-protected-resource/*`: Metadaten der nichtöffentlichen Ausgabe (RFC 9728, #565)

Der mitgelieferte `Caddyfile` leitet alle drei an die Anwendung weiter, ebenso `/accounts/*` mit Anmeldung und der Seite
„Verbundene Anwendungen“. Wer einen anderen Proxy nutzt, leitet dieselben Pfade weiter und reicht `Host` sowie
`X-Forwarded-Proto` durch. Der Proxy darf Antworten von `/oauth/token/`, `/oauth/userinfo/` und
`/oauth/introspect/` nicht zwischenspeichern. Ohne Weiterleitung landet `/.well-known/openid-configuration` bei der
Website und antwortet nicht als Discovery.

### 4. Prüfen

```bash
python manage.py oidc_pruefen
```

Der Befehl prüft:

- den Schalter
- den Signaturschlüssel: lesbar, unverschlüsselt, RSA, mindestens 2048 Bit, ebenso bisherige Schlüssel während eines
  Wechsels
- den Aussteller (`SITE_URL`): `https`, ohne Pfad, Host in `ALLOWED_HOSTS`, nicht `localhost` (außer bei `DEBUG`)
- Discovery und JWKS über den Test-Client mit dem Host des Ausstellers: Aussteller und Endpunkte in der Discovery,
  Schlüssel-ID des JWKS passend zum Schlüssel, keine privaten Schlüsselteile im JWKS
- ob es schon eine aktive Anwendung gibt

Jede Zeile beginnt mit `OK`, `HINWEIS` oder `FEHLER`. Bei mindestens einem Fehler endet der Befehl mit dem
Rückgabewert 1. Ist der Anbieter aus, fragt der Befehl Discovery und JWKS nur in seinem eigenen Prozess eingeschaltet
ab und meldet am Ende „Bereit zum Einschalten“ oder was fehlt.

Der Befehl sieht den Proxy nicht. Von außen lässt er sich so prüfen:

```bash
curl -s https://mandari.example.org/.well-known/openid-configuration
curl -s https://mandari.example.org/oauth/jwks/
```

`issuer` muss genau die Adresse aus `SITE_URL` sein, ohne Schrägstrich am Ende.

### 5. Einschalten

In der `.env` `OIDC_PROVIDER_ENABLED=true` setzen und Anwendung und Worker neu starten (bei Docker Compose
`docker compose up -d`). Danach `oidc_pruefen` wiederholen. Mit dem Schalter beginnt auch der tägliche Zeitplan
`cleartokens`, der abgelaufene Tokens und Codes löscht.

Ausschalten geht genauso mit `OIDC_PROVIDER_ENABLED=false`. Alle Endpunkte antworten dann wieder mit 404, kein
Access-Token gilt mehr. Anwendungen und Tokens bleiben gespeichert und gelten nach dem Wiedereinschalten weiter,
soweit sie nicht abgelaufen sind.

### 6. Anwendungen anlegen

Anwendungen lassen sich im Admin unter „OIDC-Anwendungen“ oder mit `manage.py oidc_anwendung` anlegen. Beide Wege
prüfen dieselben Regeln:

- nur der Code-Ablauf (`authorization-code`, PKCE mit S256 ist Pflicht) und Client Credentials (`client-credentials`)
- Redirect-URIs nur mit `https`, ohne Fragment und ohne Platzhalter; sie müssen beim Aufruf exakt passen. `http`
  gilt nur für `localhost` in einer Entwicklungsinstallation (`DEBUG`)
- Scopes nur aus dem Katalog und passend zum Ablauf (siehe [Scopes](#scopes))
- Dienste (Client Credentials) sind immer vertraulich
- vertrauliche Anwendungen brauchen ein Client-Secret mit mindestens 32 Zeichen; der Befehl erzeugt eines mit 128

**Eine App im Namen von Personen**, öffentlich, also ohne Secret:

```bash
python manage.py oidc_anwendung anlegen --name "Rats-App" --typ öffentlich --grant authorization-code \
    --redirect-uri https://app.example.org/rueckweg --scope openid --scope profile --scope ratsdaten:lesen
```

**Ein Server im Namen von Personen**, vertraulich:

```bash
python manage.py oidc_anwendung anlegen --name "Partnerportal" --typ vertraulich --grant authorization-code \
    --redirect-uri https://partner.example.org/oidc/callback --scope "openid email ratsdaten:lesen"
```

**Ein Dienst ohne Person**:

```bash
python manage.py oidc_anwendung anlegen --name "Datendienst" --typ vertraulich --grant client-credentials \
    --scope ratsdaten:lesen
```

Die Ausgabe nennt Client-ID und bei vertraulichen Anwendungen das Client-Secret. **Das Secret erscheint nur dieses
eine Mal**; gespeichert ist nur sein Hash. Es geht auf einem sicheren Weg an die Anwendung. Geht es verloren, bekommt
die Anwendung im Admin ein neues.

**Ein Dienst eines Mandanten mit Zugriff auf Nichtöffentliches** (#565): an genau einen Session-Mandanten gebunden,
Anmeldung mit `private_key_jwt`. Die Anwendung erzeugt ihr Schlüsselpaar selbst und gibt nur den öffentlichen Schlüssel
als JWK Set (oder einzelnen JWK) heraus; private Teile lehnt der Befehl ab:

```bash
python manage.py oidc_anwendung anlegen --name "Fachverfahren" --typ vertraulich --grant client-credentials \
    --mandant musterstadt --jwks dienst-jwks.json --scope sitzung:lesen
```

Es gibt kein Client-Secret. `--jwks` geht auch ohne `--mandant` für jeden vertraulichen Dienst. Neue Schlüssel
(Schlüsselwechsel des Dienstes) ersetzt `python manage.py oidc_anwendung schluessel <client-id> --jwks neue.json`;
während eines Wechsels enthält das Set beide Schlüssel mit eigener `kid`.

**Eine App für nichtöffentliche Unterlagen** im Namen von Personen: wie oben, mit `--scope ratsdaten:nichtoeffentlich`.
Den Scope erhält nur, wer ihn ausdrücklich bekommt; Personen melden sich mit zweitem Faktor an und brauchen im Mandanten
das Recht „API-Zugang“. Nimmt ein Mandant einer Rolle dieses Recht, lesen ihre Mitglieder auch über freigegebene
Anwendungen nichts mehr.

**Vor dem Einschalten der nichtöffentlichen Ausgabe:** Das Mitwirkungsverbot (in der Abstimmung als „befangen“
vermerkt) sperrt nur, wenn das Konto einer Person des Mandanten zugeordnet ist: dieselbe E-Mail-Adresse an Konto und
Person oder die verknüpfte Person am Session-Konto. Eine feste Verknüpfung von Konten und Personen folgt mit #761. Bis
dahin schaltet der Betrieb `OPARL_NONPUBLIC_ENABLED` erst ein, wenn jedes Konto mit Recht auf nichtöffentliche Inhalte,
das als Mitglied abstimmt, so einer Person zugeordnet ist (etwa die Ratsadresse an Konto und Person). Ohne Zuordnung
bleibt die Sperre wirkungslos, und die befangene Person liest den Tagesordnungspunkt samt Unterlagen.

`--skip-authorization` lässt die Zustimmungsseite weg. Das ist nur für eigene Anwendungen von mandari gedacht; fremde
Anwendungen fragen die Person jedes Mal.

Alle Anwendungen mit Ablauf, Typ, Scopes, Redirect-URIs und Stand (ohne Secret):

```bash
python manage.py oidc_anwendung liste
```

Anlegen geht auch bei ausgeschaltetem Anbieter, sobald der Schlüssel eingestellt ist. Die Anwendung wirkt dann ab dem
Einschalten.

### Schlüsselwechsel

Regelmäßig oder wenn eine Person mit Zugang zum Schlüssel ausscheidet:

1. Neuen Schlüssel erzeugen wie in Schritt 1, etwa `oidc-neu.pem`.
2. Den neuen Schlüssel als aktiven einbinden (`OIDC_RSA_PRIVATE_KEY_FILE`) und den bisherigen zusätzlich als
   `OIDC_RSA_PRIVATE_KEY_PREVIOUS_FILE`. Beide Dateien in alle Dienste einbinden, dann neu starten.
3. `oidc_pruefen` zeigt den Wechsel als Hinweis, das JWKS nennt beide Schlüssel. Signiert wird nur noch mit dem
   neuen. Anwendungen prüfen ID-Tokens, die vor dem Wechsel ausgestellt wurden, weiter mit dem bisherigen.
4. Nach der Lebensdauer der ID-Tokens (`OIDC_ACCESS_TOKEN_SECONDS`, Standard 15 Minuten) und dem Zwischenspeicher des
   JWKS (eine Stunde) den bisherigen Schlüssel entfernen, neu starten und erneut prüfen.

**Bei Verdacht, dass der Schlüssel in fremde Hände geraten ist,** sofort wechseln, aber ohne
`OIDC_RSA_PRIVATE_KEY_PREVIOUS_FILE`. Dann gilt keine Signatur des alten Schlüssels mehr, sobald die Anwendungen das
JWKS neu laden. Access- und Refresh-Tokens berührt das nicht. Wer auch sie entziehen will, sperrt die betroffenen
Anwendungen (siehe unten) und entsperrt sie danach.

### Widerruf

| Wer | Wie | Wirkung |
|---|---|---|
| Person | Seite „Verbundene Anwendungen“ (`/accounts/verbundene-anwendungen/`), erreichbar im Personenmenü von Work, Session und Insight und auf der Seite „Sicherheit“ in Work | alle Tokens dieser Anwendung für diese Person |
| Person | „Alle anderen Sitzungen beenden“, Passwort ändern oder über den Link aus der E-Mail neu setzen | alle Tokens und offenen Codes der Person bei allen Anwendungen |
| Anwendung | `/oauth/revoke/` (RFC 7009), etwa beim Abmelden | das Token und seine Kette |
| Betrieb | `python manage.py oidc_anwendung sperren <client-id> --grund "…"` | Anwendung aus, alle ihre Tokens widerrufen |
| Betrieb | Admin, „OIDC-Access-Tokens“ bzw. „OIDC-Refresh-Tokens“, Aktion „widerrufen“ | einzelne Tokens |
| Betrieb | Admin, Anwendung „Aktiv“ ausschalten | wie `sperren` |
| Betrieb | Konto sperren | alle Tokens der Person gelten sofort nicht mehr |

Eine gesperrte Anwendung bekommt keine Tokens mehr. `python manage.py oidc_anwendung entsperren <client-id>`
schaltet sie wieder ein; alte Tokens leben dabei nicht auf, Personen stimmen erneut zu.

### Prüfprotokoll, Ratenbegrenzung, Speicherung

- **Nichtöffentliche Ausgabe:** Jeder Abruf mit Nichtöffentlichem steht im Prüfprotokoll des Session-Mandanten
  (Person oder Dienst, Client-ID, Kennungen der Objekte). Die Ausgabe von Tokens nennt im Sicherheitsprotokoll auch
  `anmeldung: private_key_jwt` und die Bindung eines Dienstes. Dienste setzen in ihren Assertions `aud` auf den
  Aussteller (`SITE_URL`), nicht auf den Token-Endpunkt.
- **Prüfprotokoll:** Das Sicherheitsprotokoll (`SecurityAuditLog`) hält jede Ausgabe eines Tokens (`oidc_token`),
  jeden Widerruf (`oidc_widerruf`, durch Anwendung, Person, Passwortwechsel oder Plattform-Administration, beim
  Sperren mit Anzahl) und jede Änderung an Anwendungen fest (`oidc_anwendung`: im Admin mit Konto, mit dem Befehl
  als `manage.py oidc_anwendung`, beim Sperren mit Anlass). Es nennt Client-ID, Ablauf und Scopes, nie ein Token
  oder Secret.
- **Ratenbegrenzung:** `OIDC_TOKEN_RATE_LIMIT` Anfragen je Minute und Adresse, je Endpunkt gezählt (Token, Widerruf,
  Introspektion). Darüber antwortet mandari mit 429 und `Retry-After`. Abweisungen stehen wie gedrosselte Anmeldungen
  im Sicherheitsprotokoll.
- **Speicherung:** Access- und Refresh-Tokens stehen nur als SHA-256-Prüfsumme in der Datenbank, Client-Secrets nur
  als Passwort-Hash. Autorisierungscodes bleiben im Klartext: Sie gelten 60 Sekunden, nur einmal und sind ohne den nie
  gespeicherten `code_verifier` wertlos.
