# Eigene Anwendungen: Oberflächen und Dienste auf der Schnittstelle von mandari

Stand: 2026-11-28 (Issue #1591, Teil von Epic #493). Vorbereitet für docs.mandari.de unter
`schnittstelle/eigene-anwendungen/`. Grundlage sind das ADR
[Eine Schnittstelle: API-first](../adr/20261105-eine-schnittstelle-api-first.md) und die Anleitung
[Anmeldung für Anwendungen](anmeldung.md).

Mit einer eigenen Anwendung baut eine Kommune, eine Fraktion oder ein Partner eine eigene Oberfläche oder einen Dienst
auf mandari: etwa eine Rats-App, ein Dashboard für die Fraktion oder einen Abgleich mit einem Fachverfahren. Die
Anwendung meldet Personen über mandari an und ruft danach die Schnittstelle unter `/api/` auf. Sie sieht nie mehr, als
die angemeldete Person selbst sehen darf, und nur das, was ihre Scopes erlauben.

> **Voraussetzung:** Die Installation hat die Anmeldung über mandari eingeschaltet. Sonst antworten alle Endpunkte der
> Anmeldung mit 404, und die Seiten zum Registrieren fehlen. Ob sie an ist, zeigt
> `https://<installation>/.well-known/openid-configuration`.

## Auf einen Blick

| Schritt | Was | Wo |
|---|---|---|
| 1 | Anwendung registrieren, Client-ID und Client-Secret erhalten | Session, Work oder beim Betreiber |
| 2 | Person anmelden: Code-Ablauf mit PKCE | `/oauth/authorize/`, `/oauth/token/` |
| 3 | Schnittstelle mit dem Access-Token aufrufen | `/api/`, Beschreibung unter `/api/openapi.json` |
| 4 | Token erneuern, abmelden | `/oauth/token/` mit Refresh-Token, `/oauth/revoke/` |

## 1. Anwendung registrieren

| Wer | Wo | Gilt für |
|---|---|---|
| Kommune | Session › Einstellungen › Datenschutz und Zugänge › Anwendungen (Recht „Einstellungen verwalten“) | nur diese Kommune |
| Fraktion | Work › Einstellungen › Mitglieder und Rechte › Anwendungen (Recht „Organisation bearbeiten“) | nur diese Organisation in Work |
| Partner, Dienste ohne Person | beim Betreiber der Installation (Admin oder `manage.py oidc_anwendung`) | nach Absprache |

Beim Registrieren braucht es:

- **Name**: erscheint auf der Zustimmungsseite und bei den Personen unter „Verbundene Anwendungen“.
- **Art**: *vertraulich* für eine Anwendung auf einem Server (mit Client-Secret) oder *öffentlich* für eine App im
  Browser oder auf dem Telefon (ohne Secret, nur mit PKCE).
- **Redirect-URIs**: eine je Zeile, nur `https`, ohne Fragment. Sie müssen beim Aufruf genau übereinstimmen.
- **Scopes**: höchstens die, die der Bereich vergeben darf (siehe unten).

Nach dem Registrieren zeigt die Seite Client-ID und Client-Secret **genau einmal**. Gespeichert ist nur ein Hash des
Secrets. Geht es verloren, erzeugt „Neues Secret“ ein neues; das bisherige gilt dann sofort nicht mehr.

Anwendungen einer Kommune oder Fraktion handeln immer im Namen einer Person und fragen jede Person um Zustimmung. Die
Zustimmungsseite nennt, wer die Anwendung registriert hat. Dienste ohne Person, gebundene Dienste und Anwendungen ohne
Zustimmungsseite gibt es nur beim Betreiber.

## 2. Scopes

| Scope | Erlaubt | Kommune | Fraktion |
|---|---|---|---|
| `openid` | Anmeldung, ID-Token mit `sub` | ja | ja |
| `profile` | Name der Person | ja | ja |
| `email` | E-Mail-Adresse der Person | ja | ja |
| `ratsdaten:lesen` | Sitzungen und Vorlagen lesen (Session-API v1), nur Öffentliches | ja | ja |
| `session:lesen` | Session lesen, soweit die Person es darf, auch Anträge; nie Nichtöffentliches | ja | nein |
| `sitzung:schreiben` | Befehle von Session im Namen der Person (`/api/mandari/v1/session/<mandant>/befehle/…`); Anmeldung mit zweitem Faktor, höchstens 7 Tage danach | ja | nein |
| `einreichung:schreiben` | Profil Einreichung: einreichen, zurücknehmen, zur Ladung zurückmelden | ja | ja |
| `work:lesen` | Work lesen, soweit die Person es in ihren Organisationen darf | nein | ja |
| `work:schreiben` | Befehle von Work im Namen der Person | nein | ja |

Regeln:

- **Nie mehr als die Person.** Ein Token wirkt nur mit den Rechten der Person, die sich angemeldet hat. Für Session
  braucht die Person im Mandanten zusätzlich das Recht „API-Zugang“.
- **Nur im eigenen Bereich.** Eine Anwendung einer Kommune gilt nur bei dieser Kommune, eine Anwendung einer Fraktion
  nur bei dieser Organisation. Bei einem anderen Bereich derselben Art antwortet die Schnittstelle mit 403.
- **Nichtöffentliches gibt es über eigene Anwendungen nicht.** Die nichtöffentliche Ausgabe (Profil Nichtöffentliches)
  ist eigens abgesichert und in Produktion noch nicht eingeschaltet.
- **Welche Operation welchen Scope verlangt, steht an der Operation**: in `/api/openapi.json` im Feld `security`
  (Sicherheitsschema `mandariOidc`). Jeder dort genannte Scope genügt. Eine Operation, die einen Befehl annimmt
  (`x-mandari-command`), verlangt immer einen Scope zum Schreiben.
- `work:lesen` und `work:schreiben` gelten für den Bereich und die Befehle von Work, sobald sie in der Schnittstelle
  stehen. Eine Anwendung kann sie schon jetzt anfordern. `sitzung:lesen` ist etwas anderes: der Scope gebundener
  Dienste für Nichtöffentliches (#565), den es nur beim Betreiber gibt.

## 3. Person anmelden

Der Code-Ablauf mit PKCE ist Pflicht, auch für vertrauliche Anwendungen. Kurz:

```bash
# 1. code_verifier (43 bis 128 Zeichen) und code_challenge = BASE64URL(SHA-256(code_verifier)) erzeugen
# 2. Person weiterleiten
https://mandari.example.org/oauth/authorize/?response_type=code&client_id=<client-id>
    &redirect_uri=https://app.example.org/rueckweg&scope=openid%20profile%20session:lesen
    &state=<zufall>&nonce=<zufall>&code_challenge=<challenge>&code_challenge_method=S256

# 3. Code einlösen (gilt 60 Sekunden, nur einmal)
curl -u '<client-id>:<client-secret>' https://mandari.example.org/oauth/token/ \
  -d grant_type=authorization_code -d code=<code> \
  -d redirect_uri=https://app.example.org/rueckweg -d code_verifier=<verifier>
```

Öffentliche Anwendungen schicken statt `-u` den Parameter `client_id`. Die Antwort enthält `access_token`
(`mandari_at_…`, 15 Minuten gültig), `refresh_token` (`mandari_rt_…`, Rotation bei jeder Nutzung) und bei `openid` ein
`id_token` (RS256, Schlüssel unter `/oauth/jwks/`). Alle Einzelheiten, auch zu `prompt`, `max_age` und zur Prüfung des
ID-Tokens: [Anmeldung für Anwendungen](anmeldung.md#code-ablauf-schritt-für-schritt).

Ein kleines Beispiel in Python mit [Authlib](https://docs.authlib.org/):

```python
from authlib.integrations.requests_client import OAuth2Session
from authlib.common.security import generate_token

client = OAuth2Session(
    client_id="<client-id>",
    client_secret="<client-secret>",
    scope="openid profile session:lesen",
    redirect_uri="https://app.example.org/rueckweg",
    code_challenge_method="S256",
)
verifier = generate_token(48)
url, state = client.create_authorization_url(
    "https://mandari.example.org/oauth/authorize/", code_verifier=verifier
)
# Person zu url schicken; nach der Rückkehr:
token = client.fetch_token(
    "https://mandari.example.org/oauth/token/",
    authorization_response="https://app.example.org/rueckweg?code=…&state=…",
    code_verifier=verifier,
)
antwort = client.get("https://mandari.example.org/api/v1/session/<mandant>/applications/")
```

## 4. Schnittstelle aufrufen

Access-Tokens stehen immer im Header, nie in der Adresse:

```bash
curl -H 'Authorization: Bearer mandari_at_…' https://mandari.example.org/api/v1/session/<mandant>/meetings/
```

Der Einstieg `GET /api/` nennt alle Bereiche, `GET /api/openapi.json` beschreibt alle Operationen nach OpenAPI 3.1,
`/api/docs/` zeigt dieselbe Beschreibung zum Lesen.

| Bereich | Adressen | Scopes |
|---|---|---|
| OParl je Mandant (öffentlich) | `/session/<mandant>/api/oparl/` | keine (anonym, nach Freischaltung durch die Kommune) |
| OParl-Aggregator | `/oparl/` | keine (anonym) |
| Session-API v1: Sitzungen, Vorlagen | `/api/v1/session/<mandant>/meetings/`, `…/papers/` | `ratsdaten:lesen` oder `session:lesen` |
| Session-API v1: Anträge | `/api/v1/session/<mandant>/applications/` | `session:lesen` |
| Profil Einreichung | `/session/<mandant>/api/oparl/body/kommparl/submissions/` und weitere | `einreichung:schreiben` |
| Befehle von Session | `POST /api/mandari/v1/session/<mandant>/befehle/<befehl>/v<version>` | `sitzung:schreiben` |

### Befehle (schreibende Aufrufe)

Schreiben geht nur über Befehle. Jeder schreibende Aufruf trägt einen `Idempotency-Key` (UUID). Wiederholt die
Anwendung denselben Aufruf mit demselben Schlüssel, bekommt sie dieselbe Quittung, ohne dass etwas doppelt geschieht.

```bash
curl -X POST -H 'Authorization: Bearer mandari_at_…' \
  -H 'Idempotency-Key: 5b0f2c1e-8d3a-4f7e-9a61-2c7d0e4b9f10' -H 'Content-Type: application/json' \
  -d '{"submissionType": "motion", "name": "Radweg", "proposalText": "…", "reasoningText": "…"}' \
  https://mandari.example.org/session/<mandant>/api/oparl/body/kommparl/submissions/
```

### Änderungsfeed

Öffentliche Ratsdaten gleicht eine Anwendung über den Änderungsfeed und den Snapshot der OParl-Ausgabe ab
(`kommparl:changes` und `kommparl:snapshot` an der Körperschaft), ohne Anmeldung. Der Feed liefert
Änderungen seit einem Zeitpunkt oder einer Kennung; die Beschreibung steht in `/api/openapi.json`.

### Listen und Seiten

Listen kommen in Seiten wie in OParl (`data`, `pagination`, `links`) oder mit `limit` und `offset` (Session-API v1).
Eine Anwendung bekommt höchstens so viele Einträge je Seite, wie ihre Grenze erlaubt (Standard 100).

## 5. Fehler

Fehler kommen als `application/problem+json` nach RFC 9457, mit `WWW-Authenticate` nach RFC 6750:

| Status | Bedeutung | Was tun |
|---|---|---|
| 401 `invalid_token` | Token unbekannt, abgelaufen oder widerrufen | mit dem Refresh-Token erneuern, sonst neu anmelden |
| 403 `insufficient_scope` | Scope fehlt; `scope` im Header nennt die Scopes der Operation | Person mit dem nötigen Scope neu anmelden |
| 403 | Person ohne Recht, Anwendung eines anderen Bereichs | nichts erzwingen; die Person oder der Bereich entscheidet |
| 404 | gibt es nicht oder darf die Person nicht sehen | |
| 422 | Inhalt passt nicht (Befehle), Schlüssel mit anderem Inhalt schon verwendet | Meldung in `errors` lesen |
| 429 | Grenze der Anwendung oder der Person erreicht | `Retry-After` abwarten |

## 6. Grenzen

| Grenze | Standard | Wirkung |
|---|---|---|
| Anfragen je Minute und Anwendung, über alle ihre Tokens | 600 | darüber 429 mit `Retry-After` bis zur nächsten Minute |
| Einträge je Seite | 100 | größere Seiten werden gekürzt |
| Anfragen je Minute und Person im Profil Einreichung | 120 | darüber 429 |
| Anfragen je Minute und Adresse an Token-Endpunkt, Widerruf, Introspektion | 30 | darüber 429 |
| Lebensdauer Access-Token | 15 Minuten | danach Refresh |
| Refresh-Token ungenutzt | 30 Tage | danach neu anmelden |

Mehr Spielraum vergibt der Betreiber je Anwendung.

## 7. Widerruf und Prüfprotokoll

- **Die Person** sieht ihre Anwendungen unter „Verbundene Anwendungen“ (Personenmenü in Work, Session und Insight) und
  in Work auf ihrer Profilseite „Sicherheit“ und widerruft dort jede einzeln. „Alle anderen Sitzungen beenden“ und ein
  Passwortwechsel widerrufen alle Anwendungen.
- **Der Bereich** (Kommune oder Fraktion) sperrt eine Anwendung oder löscht sie; dabei verlieren alle ihre Tokens ihre
  Gültigkeit.
- **Die Anwendung** widerruft beim Abmelden selbst: `POST /oauth/revoke/` mit dem Refresh-Token.
- **Prüfprotokoll:** Ausgabe und Widerruf jedes Tokens, Anlegen, Sperren und Löschen einer Anwendung stehen im
  Sicherheitsprotokoll der Installation (mit Client-ID und Bereich, nie mit Token oder Secret). Einreichungen und
  Abrufe von Einreichungen stehen im Prüfprotokoll der Kommune mit Person und Client-ID.

## 8. Checkliste vor dem Start

- Redirect-URI genau so eintragen, wie die Anwendung sie schickt.
- PKCE mit `S256`, `state` und `nonce` prüfen, `iss` in der Rückleitung prüfen.
- Nur die Scopes anfordern, die die Anwendung braucht.
- Access-Tokens nur im Speicher halten, Refresh-Tokens verschlüsselt; nie parallel erneuern.
- Bei 429 `Retry-After` beachten, bei 401 erneuern, bei 403 nicht wiederholen.
