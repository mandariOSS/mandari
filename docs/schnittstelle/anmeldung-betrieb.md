# Anmeldung für Anwendungen: Betrieb

Stand: 2026-11-05 (Issue #1215, Teil von Epic #1203). Für Betreiber einer mandari-Installation. Wie Anwendungen den
Anbieter nutzen, steht in [Anmeldung für Anwendungen](anmeldung.md).

mandari ist OpenID-Connect-Anbieter, aber standardmäßig ausgeschaltet. Diese Anleitung führt durch das Einschalten
auf einer neuen Installation, das Anlegen der ersten Anwendung, den Schlüsselwechsel und den Widerruf.

## Überblick

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

## 1. Signaturschlüssel erzeugen

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

## 2. Schlüssel einbinden und Umgebungsvariablen

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

## 3. Reverse Proxy

Der Anbieter braucht zwei Pfade an der Wurzel des Ausstellers:

- `/oauth/*`: Autorisierung, Token, Widerruf, Introspektion, Angaben zur Person, JWKS
- `/.well-known/openid-configuration`: Discovery

Der mitgelieferte `Caddyfile` leitet beide an die Anwendung weiter, ebenso `/accounts/*` mit Anmeldung und der Seite
„Verbundene Anwendungen“. Wer einen anderen Proxy nutzt, leitet dieselben Pfade weiter und reicht `Host` sowie
`X-Forwarded-Proto` durch. Der Proxy darf Antworten von `/oauth/token/`, `/oauth/userinfo/` und
`/oauth/introspect/` nicht zwischenspeichern. Ohne Weiterleitung landet `/.well-known/openid-configuration` bei der
Website und antwortet nicht als Discovery.

## 4. Prüfen

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

## 5. Einschalten

In der `.env` `OIDC_PROVIDER_ENABLED=true` setzen und Anwendung und Worker neu starten (bei Docker Compose
`docker compose up -d`). Danach `oidc_pruefen` wiederholen. Mit dem Schalter beginnt auch der tägliche Zeitplan
`cleartokens`, der abgelaufene Tokens und Codes löscht.

Ausschalten geht genauso mit `OIDC_PROVIDER_ENABLED=false`. Alle Endpunkte antworten dann wieder mit 404, kein
Access-Token gilt mehr. Anwendungen und Tokens bleiben gespeichert und gelten nach dem Wiedereinschalten weiter,
soweit sie nicht abgelaufen sind.

## 6. Anwendungen anlegen

Anwendungen lassen sich im Admin unter „OIDC-Anwendungen“ oder mit `manage.py oidc_anwendung` anlegen. Beide Wege
prüfen dieselben Regeln:

- nur der Code-Ablauf (`authorization-code`, PKCE mit S256 ist Pflicht) und Client Credentials (`client-credentials`)
- Redirect-URIs nur mit `https`, ohne Fragment und ohne Platzhalter; sie müssen beim Aufruf exakt passen. `http`
  gilt nur für `localhost` in einer Entwicklungsinstallation (`DEBUG`)
- Scopes nur aus dem Katalog und passend zum Ablauf (siehe [Scopes](anmeldung.md#scopes))
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

`--skip-authorization` lässt die Zustimmungsseite weg. Das ist nur für eigene Anwendungen von mandari gedacht; fremde
Anwendungen fragen die Person jedes Mal.

Alle Anwendungen mit Ablauf, Typ, Scopes, Redirect-URIs und Stand (ohne Secret):

```bash
python manage.py oidc_anwendung liste
```

Anlegen geht auch bei ausgeschaltetem Anbieter, sobald der Schlüssel eingestellt ist. Die Anwendung wirkt dann ab dem
Einschalten.

## Schlüsselwechsel

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

## Widerruf

| Wer | Wie | Wirkung |
|---|---|---|
| Person | Seite „Verbundene Anwendungen“ (`/accounts/verbundene-anwendungen/`), erreichbar im Personenmenü von Work, Session und Insight und auf der Seite „Sicherheit“ in Work | alle Tokens dieser Anwendung für diese Person |
| Anwendung | `/oauth/revoke/` (RFC 7009), etwa beim Abmelden | das Token und seine Kette |
| Betrieb | `python manage.py oidc_anwendung sperren <client-id> --grund "…"` | Anwendung aus, alle ihre Tokens widerrufen |
| Betrieb | Admin, „OIDC-Access-Tokens“ bzw. „OIDC-Refresh-Tokens“, Aktion „widerrufen“ | einzelne Tokens |
| Betrieb | Admin, Anwendung „Aktiv“ ausschalten | wie `sperren` |
| Betrieb | Konto sperren | alle Tokens der Person gelten sofort nicht mehr |

Eine gesperrte Anwendung bekommt keine Tokens mehr. `python manage.py oidc_anwendung entsperren <client-id>`
schaltet sie wieder ein; alte Tokens leben dabei nicht auf, Personen stimmen erneut zu.

## Prüfprotokoll, Ratenbegrenzung, Speicherung

- **Prüfprotokoll:** Das Sicherheitsprotokoll (`SecurityAuditLog`) hält jede Ausgabe eines Tokens (`oidc_token`),
  jeden Widerruf (`oidc_widerruf`, durch Anwendung, Person oder Plattform-Administration, beim Sperren mit Anzahl)
  und jede Änderung an Anwendungen fest (`oidc_anwendung`: im Admin mit Konto, mit dem Befehl als
  `manage.py oidc_anwendung`, beim Sperren mit Anlass). Es nennt Client-ID, Ablauf und Scopes, nie ein Token oder
  Secret.
- **Ratenbegrenzung:** `OIDC_TOKEN_RATE_LIMIT` Anfragen je Minute und Adresse, je Endpunkt gezählt (Token, Widerruf,
  Introspektion). Darüber antwortet mandari mit 429 und `Retry-After`. Abweisungen stehen wie gedrosselte Anmeldungen
  im Sicherheitsprotokoll.
- **Speicherung:** Access- und Refresh-Tokens stehen nur als SHA-256-Prüfsumme in der Datenbank, Client-Secrets nur
  als Passwort-Hash. Autorisierungscodes bleiben im Klartext: Sie gelten 60 Sekunden, nur einmal und sind ohne den nie
  gespeicherten `code_verifier` wertlos.
