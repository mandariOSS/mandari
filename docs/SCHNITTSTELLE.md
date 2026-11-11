# Schnittstelle: Einstieg, Bereiche, gemeinsame Regeln

Grundlage: ADR [Eine Schnittstelle: API-first auf OParl, kommParl und OpenAPI](adr/20261105-eine-schnittstelle-api-first.md)
(Epic #1203), umgesetzt in #1205. Diese Seite beschreibt alles unter `/api/`: Einstieg und Beschreibung, die
kommParl-Profile, die Session-API v1 und die Provisioning-API. Die OParl-Ausgaben beschreiben
[OPARL_API.md](OPARL_API.md) (Aggregator, gemeinsame OParl-Regeln) und [SESSION_OPARL_API.md](SESSION_OPARL_API.md)
(je Session-Mandant), die Anmeldung für Anwendungen [schnittstelle/anmeldung.md](schnittstelle/anmeldung.md).

## Adressen

| Adresse | Inhalt |
|---|---|
| `GET /api/` | Einstieg: Verweis auf das OParl-System der Installation, `kommparl:conformsTo`, Beschreibung, Dokumentation, Bereiche |
| `GET /api/openapi.json` | eine OpenAPI-3.1-Beschreibung der angemeldeten Bereiche (Abschnitt [Bereiche anmelden](#bereiche-anmelden)) |
| `GET /api/docs/` | Swagger UI mit den lokalen Dateien von django-ninja (kein CDN) |

Die übrigen Adressen unter `/api/` bleiben unverändert; `hub/api/tests/test_schnittstelle.py` prüft das. Nicht alle
stehen in der gemeinsamen Beschreibung:

| Adresse | Beschreibung |
|---|---|
| `/api/v1/session/` | [Session-API v1](#session-api-v1), auch in der gemeinsamen Beschreibung |
| `/api/public/v1/` | öffentliche Fraktions-API (Token je Organisation), eigene Beschreibung; öffentliche Doku: docs.mandari.de/work/fraktions-api/ |
| `/api/provisioning/`, `/api/provisioning/v2/` | [Provisioning-API](#provisioning-api), nur für den Betreiber |
| `/api/insight/ki-bewertungen/` | Bewertungen der KI-Zusammenfassungen für das System des Betreibers ([Provisioning-API](#weitere-abrufe-mit-dem-schlüssel-der-version-2)) |
| `/api/stats/`, `/api/contact/` | Zahlen und Kontaktformular für die Website |

Außerhalb von `/api/` liegen der Datenkatalog (`/data/dcat/`, [DCAT_KATALOG.md](DCAT_KATALOG.md)), der
OpenID-Connect-Anbieter (`/oauth/`, [Anmeldung](schnittstelle/anmeldung.md)) und der MCP-Server (`/insight/mcp`).
Auch sie stehen nicht in der gemeinsamen Beschreibung.

### Der Einstieg ist kein zweites OParl-System

In OParl ist die Adresse eines Objekts seine Kennung. Ein `System` unter `/api/` hätte entweder eine fremde
Kennung (zwei Adressen für ein Objekt, zwei Fassungen mit verschiedenem `ETag`) oder eine zweite Kennung. Beides
verwirrt Anwendungen, die Objekte an ihrer `id` erkennen. Der Einstieg ist deshalb ein eigenes Dokument und
verweist mit `system` auf das System des Aggregators (`/oparl/v1/system`).

`kommparl:conformsTo` nennt nur Kennungen, deren Anforderungen die Installation vollständig erfüllt (kommParl,
Kapitel 5): heute `https://schema.kommparl.de/0.1/`. Dieselbe Grundliste steht seit #1216 am `System` beider
OParl-Ausgaben (`hub.api.kommparl.conforms_to`). Session ergänzt sie um die Profile Einreichung (bei eingeschaltetem
OpenID-Connect-Anbieter) und Nichtöffentliches (bei `OPARL_NONPUBLIC_ENABLED`), siehe unten. Die Profile Änderungsfeed
und Snapshot kämen hinzu, sobald die Installation den Feed anbietet (`OPARL_CHANGES_ENABLED`) und sie vollständig
erfüllt sind (`PROFILES_FULFILLED`). Seit #1229 nennt der Feed auch das einbettende Objekt, wenn sich ein eingebettetes
ändert (Profil Änderungsfeed, Abschnitt „Vollständigkeit und Reihenfolge“; ADR A10, Nachtrag #1229). Erfüllt ist das
Profil damit noch nicht: Session meldet Körperschaft, Gremien, Personen, Mitgliedschaften und Wahlperioden nicht als
Ereignis, `modified` kann sich ohne öffentliches Ereignis ändern, der Aggregator nennt Änderungen gespiegelter
Session-Mandanten vor ihrer Übernahme, ein nichtöffentlich gewordener Tagesordnungspunkt des Bestands bleibt ohne
`deleted: true` abrufbar, und hängt eine Datei des Bestands an einer anderen Vorlage, bekommt die frühere keinen
Eintrag. Diese Punkte bleiben unter #1229 offen. Das Profil Snapshot setzt das Profil Änderungsfeed voraus.

Die Fassung verlangt unter anderem jeden Zeitpunkt als `yyyy-mm-ddThh:mm:ss±hh:mm` (Kapitel „JSON-Ausgabe“). Beide
Ausgaben geben Zeitpunkte deshalb auf die Sekunde aus (`hub.ris.canonical.iso`), und die Konformitätsprüfung der Tests
prüft die Formate mit den Mustern der Spezifikation, ohne Ausnahme.

## OParl-Ausgaben nach kommParl (#1216)

Beide OParl-Ausgaben stehen mit den Schemas der kommParl-Spezifikation in der Beschreibung.

- **Übernommene Schemas:** `mandari/hub/api/kommparl/0.1/` enthält `schema/*.json` (Objekttypen im Format der
  OParl-Spezifikation) und `profiles/<Profil>/*.json` (JSON Schema 2020-12) unverändert, dazu `HERKUNFT.json` mit
  Repository, Commit, Datum, Lizenz (CC BY-SA 4.0, `REUSE.toml`) und Prüfsummen. Erneuern:
  `python scripts/kommparl_schemas.py --commit <sha>`, prüfen: `--pruefen` (auch ein Test).
- **Umwandlung im Code:** `hub.api.kommparl` wandelt die Objekttypen beim Erzeugen der Beschreibung in JSON Schema
  2020-12 um (`url` → `format: uri`, eingebettete Objekte → `$ref`, `type` → `const`, Texte nicht leer) und
  übernimmt die Profilschemas als Komponenten `Kommparl…` (ohne `$id`, `$defs` aufgelöst). Die frühere Datei
  `hub/api/oparl_types.py` ist entfallen; Beschreibung und Konformitätsprüfung (`hub/api/tests/konformitaet.py`)
  nutzen dieselben Schemas.
- **Ergänzungen** (`hub/api/openapi/oparl.py`), weil die Spezifikation sie nur im Text nennt: die sieben Werte von
  `organizationType` und `license` als URL (kommParl Kapitel 6), dazu die abgekündigten Eigenschaften
  `mandari:changes` und `mandari:snapshot` am Body. Eigene Felder erlaubt die Beschreibung wie bisher mit jedem
  Namensraum; dass `kommparl:`-Eigenschaften im Schema stehen, prüft die Konformitätsprüfung.
- **Änderungsfeed und Snapshot nach den Profilen** unter `…/kommparl/changes` und `…/kommparl/snapshot` (am Body
  `kommparl:changes`, `kommparl:snapshot`). Die ältere Form konnte nicht additiv angepasst werden: Die Profilschemas
  lassen keine Felder ohne Namensraum zu (`freshness`, `links.snapshot`, Kopfzeile des Snapshots) und legen andere
  Gründe, `links.next` nur bis zur letzten Seite, eine Schlusszeile und ein Fehlerobjekt nach OParl 1.1 fest.
- **Abkündigung:** `mandari:changes` und `mandari:snapshot` bleiben mit ihren Adressen bis frühestens 01.12.2027
  (12 Monate, [RELEASE_POLITIK.md](RELEASE_POLITIK.md#6-schnittstellen-und-abkündigung)). Die Beschreibung
  kennzeichnet Endpunkte und Eigenschaften mit `deprecated` und `x-sunset`, die Antworten tragen `Deprecation`,
  `Sunset` und den Nachfolger im Header `Link`. Die Beschreibung bleibt ohne brechende Änderung (oasdiff).

## Profil Einreichung (#1228)

Anträge, Änderungsanträge und Anfragen einreichen, den Bearbeitungsstand abrufen, zurücknehmen und sich zur Ladung
zurückmelden: nach dem Profil Einreichung von kommParl (`https://schema.kommparl.de/0.1/profile/submission`, Kapitel
„Profil Einreichung“), je Session-Mandant. Code: `apps/session/api/einreichung.py`, Beschreibung
`apps/session/api/einreichung_beschreibung.py` (Bereich „Profil Einreichung“). Die Anleitung für Anwendungen steht in
der [Anmeldung](schnittstelle/anmeldung.md#einreichen-profil-einreichung).

| Adresse unter `/session/<mandant>/api/oparl/` | am Objekt | Befehl |
|---|---|---|
| `GET body/kommparl/submission-rules/` | Body `kommparl:submissionRules` | |
| `POST body/kommparl/uploads/`, `GET …/uploads/<id>/` | Body `kommparl:submissionUpload` | |
| `GET body/kommparl/submissions/` (Liste), `POST` (einreichen) | Body `kommparl:submission` | `submission.submit` |
| `GET body/kommparl/submissions/<id>/` | `id` der Einreichung | |
| `POST body/kommparl/submissions/<id>/withdraw/` | `withdraw` der Einreichung | `submission.withdraw` |
| `GET`/`POST meeting/<id>/kommparl/attendance/` | Sitzung `kommparl:attendanceResponse` | `attendance.respond` |
| `POST meeting/<id>/kommparl/invitation-receipt/` | Sitzung `kommparl:invitationReceipt` | `invitation.acknowledge` |

- **Nur Session:** Body und Sitzungen der OParl-Ausgabe je Mandant nennen die Adressen, und ihr `System` nennt das
  Profil in `kommparl:conformsTo`, sobald der OpenID-Connect-Anbieter eingeschaltet ist (`OIDC_PROVIDER_ENABLED`);
  sonst antworten die Endpunkte mit 404. Der Aggregator und der Einstieg `/api/` nennen das Profil nicht, weil der
  Aggregator keine Einreichungen annimmt. Die Endpunkte gelten wie die übrige Ausgabe erst nach der Freischaltung der
  OParl-Schnittstelle des Mandanten.
- **Anmeldung:** nur Access-Tokens im Namen einer Person mit `einreichung:schreiben`. Dienst-Tokens 403, API-Tokens
  der Session-API 401, Sitzungscookies zählen nicht, deshalb ohne CSRF.
- **Ein Weg:** Geschrieben wird nur über die Befehle mit dem Auslöser `user:<id>`. Ihre Handler in
  `apps/session/commands.py` prüfen die Rechte wie die bisherigen Wege: Einreichen und Zurücknehmen für Organisationen,
  in denen die Person „Anträge ans RIS übermitteln“ darf und die mit der Verwaltung verbunden sind (wie aus Work);
  Rückmelden und Empfang bestätigen nur für die eigene Person, die wie im Portal über die bestätigte E-Mail-Adresse
  zugeordnet wird. Lesen und zurücknehmen darf eine Person nur ihre eigenen Einreichungen
  (`SessionApplication.submitted_by_user`, gesetzt aus dem Auslöser `user:<id>` in Work und im Profil), solange sie
  für deren Organisation einreichen darf. Einreichungen anderer Mitglieder derselben Organisation ergeben 404: Sie
  enthalten Kontaktdaten und Texte, die Work nur nach seiner Sichtbarkeit zeigt.
- **Gemeinsame Bausteine:** Probleme nach RFC 9457 (`apps/common/api/problems.py`), `Idempotency-Key` Pflicht bei
  jedem `POST` außer dem Hochladen (Speicher und Prüfung im Dispatcher, derselbe Schlüssel mit anderem Inhalt 422),
  `ETag` mit `If-None-Match` bei jedem `GET`, Seiten wie in OParl für die Liste.
- **Eingangsbestätigung:** Eingangsnummer und Eingangszeit aus der Quittung des Befehls, `contentDigest` nach RFC 9530
  über den Inhalt der Anfrage, wie er angekommen ist (gespeichert am Antrag, `SessionApplication.content_digest`). Ein
  mitgesendeter Header `Content-Digest` wird nachgerechnet (sha-256, sha-512), Abweichung 400.
- **Anlagen:** hochladen nur mit Einreichungsrecht bei der Verwaltung (sonst 403, bevor der Inhalt gelesen wird), als
  `multipart/form-data`, geprüft wie Antrags-Anhänge (Dateityp, Größe, Virenprüfung) und mit `validate_upload`, bis
  zum Einreichen 24 Stunden in `SessionSubmissionUpload` unter `session/files/` (nie über `/media/`). Je Person über
  alle Mandanten höchstens 50 offene Dateien und 500 MB (429). Eine Datei lässt sich in genau einer Einreichung
  verwenden: Vor dem Befehl wird sie für den `Idempotency-Key` vorgemerkt, eine andere Einreichung bekommt 422. Beim
  Einreichen übernimmt der Befehl die Datei in die Ablage der Anlagen; der Inhalt des Uploads wird danach gelöscht,
  die Angaben bleiben 30 Tage für Wiederholungen. Nicht eingereichte Dateien löscht der Zeitplan
  `befehl:einreichung_aufraeumen` stündlich. Hochgeladene Inhalte liefert das Profil nie aus.
- **Rückmeldung zur Ladung:** Herkunft „portal“ wie im Portal; der Grund einer Absage wird verschlüsselt gespeichert
  und erscheint in keiner Antwort. Die Rückmeldung bestätigt zugleich den Empfang aller Versände der Sitzung.
- **Prüfprotokoll:** Jede schreibende Handlung und jeder Abruf von Einreichungen steht im Protokoll des Mandanten mit
  Person (`user:<id>`) und Client-ID der Anwendung, ohne Inhalte.
- **Konformität:** Die Tests prüfen jede Antwort gegen die Schemas des Profils (`hub/api/kommparl/0.1/profiles/submission/`)
  und Body und Sitzung gegen die Objekttypen aus kommParl.

## Nichtöffentliche Ausgabe nach kommParl (#565)

Umsetzung des Profils Nichtöffentliches (kommParl, Kapitel 7, `https://schema.kommparl.de/0.1/profile/nonpublic`) je
Session-Mandant. Anleitung für Anwendungen: [Anmeldung](schnittstelle/anmeldung.md#nichtöffentliches-die-nichtöffentliche-ausgabe).

- **Eigene Ausgabe, eigene Kennungen:** `/session/<mandant>/api/oparl/nichtoeffentlich/` ist ein eigenes OParl-System.
  Die Abbildung (`hub/ris/mapping/session.py`) bildet mit einer `SessionSicht` ab, was der Aufrufer sieht; alle Verweise
  zeigen in diese Ausgabe, `kommparl:publicId` nennt die öffentliche Adresse. Die öffentliche Ausgabe liest kein Token
  und bleibt Byte für Byte gleich; nur ihr `System` nennt bei eingeschalteter Ausgabe das Profil und
  `kommparl:nonPublicSystem` (A7: Adressen öffentlicher Objekte bleiben ihre Kennungen).
  Die Adressen des Profils Einreichung (#1228) stehen nur in der öffentlichen Ausgabe; die nichtöffentliche nennt
  das Profil nicht.
- **Eine Regel für Sichtbarkeit:** `apps/session/visibility.py` (`Sicht`, `sicht_fuer_person`, `sicht_fuer_dienst`)
  baut auf denselben Bausteinen auf wie die Oberfläche und ergänzt Rechte mit Geltungsbereich, befristete Zuweisungen
  und das Mitwirkungsverbot. Die Oberfläche stellt mit #773 auf dieselbe Prüfung um.
- **Anmeldung in der Plattform:** Scopes `ratsdaten:nichtoeffentlich` (Person, Anmeldung mit zweitem Faktor,
  höchstens sieben Tage danach; Session verlangt dazu „API-Zugang“ im Mandanten) und `sitzung:lesen` (gebundener
  Dienst, liest alles Nichtöffentliche seines Mandanten), `private_key_jwt` mit dem Aussteller als `aud` und die
  Bindung einer Anwendung an einen Bereich (`apps/accounts/oidc/bindings.py`, fachfreie Kennung
  `session-mandant:<Kennung>`, ausgewertet von Session).
- **Endpunkte:** `apps/session/api/nichtoeffentlich.py`, Metadaten nach RFC 9728 unter
  `/.well-known/oauth-protected-resource/session/<mandant>/api/oparl/nichtoeffentlich/`, in der Beschreibung als Bereich
  „OParl je Mandant, nichtöffentlich“ mit `securitySchemes`. Schalter `OPARL_NONPUBLIC_ENABLED` (Standard aus).

## Session-API v1

Die Session-API v1 (django-ninja, Issue #163) war der Pilot für die API-Basis aus `docs/ENGINEERING_STANDARDS.md` §6:
generiertes OpenAPI-Schema, Pydantic-Schemas, Fehler nach RFC 9457, versionierte Pfade. Öffentliche OParl-1.1-Daten
liefert unverändert `/session/<slug>/api/oparl/` ([SESSION_OPARL_API.md](SESSION_OPARL_API.md)).

### Endpunkte der Session-API v1

| Methode | Pfad | Zugriff |
|---------|------|---------|
| GET | `/api/v1/session/{tenant_slug}/` | Einstiegspunkt mit allen Links |
| GET | `/api/v1/session/{tenant_slug}/meetings/` | öffentlich; NÖ-Sitzungen (samt internen Notizen) nur für angemeldete Nutzer mit Recht `view_non_public_meetings` |
| GET | `/api/v1/session/{tenant_slug}/papers/` | veröffentlichte Vorlagen (öffentlich und freigegeben, wie OParl); NÖ-Vorlagen, Entwürfe und Vorlagen in Prüfung samt Texten nur für angemeldete Nutzer mit Recht `view_non_public_papers` |
| GET | `/api/v1/session/{tenant_slug}/applications/` | nur mit Recht `view_applications` |
| POST | `/api/v1/session/{tenant_slug}/applications/submit/` | API-Token mit `can_submit_applications` |
| GET | `/api/v1/session/{tenant_slug}/applications/{id}/feedback/` | nur das einreichende API-Token (bzw. ein Token derselben in Work verbundenen Organisation) |
| GET | `/api/v1/session/openapi.json` | OpenAPI-3-Dokument des Bereichs |
| GET | `/api/v1/session/docs` | Swagger UI (lokale Assets, kein CDN) |

Listen unterstützen `limit` (1–200, Standard 100) und `offset`; `meta.total` ist die Gesamtzahl.

### Rückmeldestand eines Antrags (Issue #316)

`GET …/applications/{id}/feedback/` liefert Eingangsnummer und Status, die Vorlage (`converted`,
`public`, `reference` nur bei veröffentlichter Vorlage), die Stationen der Beratungsfolge und den
Beschluss. Es gelten die Ö/NÖ-Regeln der OParl-Schnittstelle: Eine nicht-öffentliche Station trägt
nur `order`, `public: false` und `label: "nicht-öffentlich beraten"`; der Beschluss stammt nur aus
einer öffentlichen, entscheidenden Station. Bearbeitungsnotizen und andere Interna der Verwaltung
werden nie ausgeliefert. Anträge anderer Tokens bzw. Organisationen antworten mit 404. Die Antwort
auf `…/applications/submit/` enthält die URL unter `feedback`.

### Einreichen über die Session-API v1 (Issue #540)

`POST …/applications/submit/` sendet den Befehl `submission.submit`; geprüft wird wie bei Einreichungen aus
mandari Work (Ablauf aus Work: docs.mandari.de/work/antraege-einreichen/). Die Antwort (201) ist die Quittung: `id`,
`reference` (Eingangsnummer), `status`, `feedback`, `submitted_at` (Eingangszeit der Einreichung) und `content_hash`
(SHA-256 über das kanonische JSON des Befehlsinhalts nach RFC 8785). `submitted_at` ist nicht `received_at` im
Rückmeldestand: Dort steht der Eingang bei der Verwaltung, zunächst `null`. Der Header `Idempotency-Key` ist optional
und empfohlen: Eine Wiederholung mit demselben Schlüssel und denselben Angaben liefert dieselbe Quittung, ohne erneut
einzureichen; derselbe Schlüssel mit anderen Angaben ergibt 422 (`idempotenzschluessel-wiederverwendet`). Schlüssel
gelten je Token und 30 Tage. Ohne Schlüssel ist jede Anfrage eine neue Einreichung. Pflichttexte nur aus Leerraum
ergeben 422 mit `errors` (`type` `befehl`). `co_signers` bleibt Text bis 5000 Zeichen und wird unverändert
gespeichert. Ein Zielgremium, das es beim Mandanten nicht gibt, bleibt unbeachtet; ein inaktives Zielgremium wird
ebenfalls übergangen, der Antrag geht dann ohne Zielgremium ein.

Externe Work-Installationen reichen über diesen Endpunkt mit einem API-Token des Mandanten ein. Neue Anwendungen
nutzen das [Profil Einreichung](#profil-einreichung-1228) mit Anmeldung über den OpenID-Connect-Anbieter.

### Anmeldung an der Session-API v1

- **API-Token**: `Authorization: Bearer <token>` (64 Zeichen, angelegt im Sitzungsdienst unter Einstellungen →
  Einreichungs-Zugänge). Token gehören zu genau einem Mandanten; Rechte über die Flags `can_read_meetings`,
  `can_read_papers` („Öffentliche Sitzungen/Vorlagen lesen“) und `can_submit_applications`; optional IP-Beschränkung
  und Ratenlimit je Minute (429 mit `Retry-After`). Ein Token liest nur öffentliche Daten: Nichtöffentliche
  Sitzungen, Vorlagen, Texte und interne Notizen liefert die API nie an ein Token, unabhängig von den Flags.
- **Sitzung**: angemeldete Nutzer des Session-RIS mit dem Recht „API-Zugang“ (`access_api`) und ihren
  übrigen Rollenrechten (nur GET). Ohne „API-Zugang“ gilt eine angemeldete Person wie ein anonymer Aufruf;
  das gilt auch für die abgekündigten Pfade.
- **Access-Token des OpenID-Connect-Anbieters** (`Authorization: Bearer mandari_at_…`, Issue #1206), nur bei
  eingeschaltetem Anbieter und nur mit dem Scope `ratsdaten:lesen` (sonst 403 mit `WWW-Authenticate`). Im Namen einer
  Person liest das Token höchstens, was die Person mit eigener Anmeldung über die Schnittstelle lesen dürfte (also
  nur mit „API-Zugang“), aber nie Nichtöffentliches und keine Anträge, und es reicht nichts ein. Ein Token eines
  Dienstes (Client Credentials) liest nur, was anonym lesbar ist. Code: `apps/session/api/v1/auth.py`.
- Ohne einen dieser Wege: anonym, nur öffentliche Daten.

**Protokoll:** Erhält eine angemeldete Person nichtöffentliche Sitzungen oder Vorlagen, steht je Abruf
ein Lesezugriff im Prüfprotokoll des Mandanten, etwa „Vorlagen (Session-API v1)“ bzw. auf den abgekündigten
Pfaden „Vorlagen (Session-API, abgekündigter Pfad)“. Einzelheiten in [PROTOKOLLIERUNG.md](PROTOKOLLIERUNG.md)
(Issue #366).

Vor der Freischaltung der OParl-Schnittstelle (Einstellungen) antwortet der Mandant anonymen Aufrufern
mit 404. Sitzungen bzw. Vorlagen lesen dann nur API-Token mit `can_read_meetings` bzw. `can_read_papers`
und Personen mit „API-Zugang“ und dem Recht „Sitzungen anzeigen“ bzw. „Vorlagen anzeigen“; sonst 403.
Nach der Freischaltung sind öffentliche Daten für alle frei.

Das Häkchen „OParl-API-Zugang“ steht nicht mehr in der Rechte-Matrix: Die OParl-Schnittstelle ist
öffentlich und anonym, ein Recht darauf hatte keine Wirkung.

### Fehlerformat der Session-API v1 (RFC 9457)

Alle Fehler sind `application/problem+json`:

```json
{
  "type": "https://docs.mandari.de/api/probleme/keine-berechtigung",
  "title": "Keine Berechtigung",
  "status": 403,
  "detail": "Dieses Token darf keine Anträge einreichen.",
  "instance": "/api/v1/session/musterstadt/applications/submit/",
  "request_id": "3f2b…"
}
```

Validierungsfehler (422) tragen zusätzlich `errors` mit `loc`, `msg`, `type` je Feld. `request_id`
entspricht dem `X-Request-ID`-Header und findet sich in den Server-Logs.

Die Session-API v1 nutzt die gemeinsamen Bausteine, ihre Antworten bleiben aber wie seit #163: Sie behält ihre
Titel je Status (`TITLES` in `apps/session/api/v1/problems.py`), Status außerhalb davon heißen weiter „Fehler“,
auch 501 und 503 aus dem Befehl `submission.submit`. Neue Bereiche nutzen die gemeinsame Tabelle
(`apps/common/api/problems.py`).

### Ablösung der alten Pfade

`/session/<slug>/api/session/meetings/`, `…/papers/`, `…/applications/`, `…/applications/submit/`
sind abgekündigt und entfallen am **31.05.2027** (`Sunset: Mon, 31 May 2027 00:00:00 GMT`, CHANGELOG
unter „Abgekündigt“; bis Version 0.11.x nannte der Header den 31.03.2027). Bis dahin bleiben sie erreichbar und
antworten mit `Deprecation: @1790467200`, `Sunset` und `Link: <neuer Pfad>; rel="successor-version"`. Der Wert von
`Deprecation` ist nach [RFC 9745](https://www.rfc-editor.org/rfc/rfc9745) der Zeitpunkt der Abkündigung als
Structured-Field-Date („@“ und Unix-Sekunden): 27.09.2026 00:00 UTC, das Erscheinen von 0.11.0. Bis Version 0.11.x
stand dort `true`; wer den Header auswertet, prüft am besten nur auf sein Vorhandensein oder liest das Datum. Der
Einstiegspunkt `/session/<slug>/api/` bleibt und verweist unter `v1` auf die neue API. Unterschiede beim Umstieg:

| Alt | Neu |
|-----|-----|
| Fehler als `{"error": "…"}` mit 400/401/403 | `application/problem+json`; fehlende/ungültige Felder → 422 mit `errors` |
| feste 100 Einträge | `limit`/`offset`, `meta.total` |
| Token nur zum Einreichen | Token reicht ein und fragt den Rückmeldestand eigener Anträge ab (`…/feedback/`); lesend nur öffentliche Daten wie anonyme Aufrufer, nichtöffentliche Daten nie, unabhängig von den Flags |
| `application_type`-Aliase `proposal`, `urgent_motion` | weiterhin akzeptiert (auf `motion`/`urgent` abgebildet) |

### Neue Endpunkte der Session-API v1 anlegen

1. Schema in `apps/session/api/v1/schemas.py` (Ein- und Ausgabe, `Field(description=…)`).
2. Endpunkt in `apps/session/api/v1/endpoints.py` auf dem `router`; Mandant über `get_tenant()`,
   Aufrufer über `resolve_principal()` und `principal.require(<recht>, <detail>)`.
3. Fehler ausschließlich über `Problem(status, detail, kind=…)`; nie `JsonResponse` von Hand.
4. Test in `apps/session/tests/test_api_v1.py` (Sichtbarkeit, Auth-Wege, Fehlerformat).
5. Alte `JsonResponse`-Views, die ersetzt werden, bekommen `successor_url_name` (Deprecation-Header)
   und einen Eintrag in der Tabelle oben.

Neue Bereiche außerhalb von Session entstehen nach dem ADR „Eine Schnittstelle“ unter `/api/mandari/v<n>/`, nicht
unter `/api/v1/<bereich>/`.

## Provisioning-API

Über die Provisioning-API legt ein vertrauenswürdiges System des Betreibers Work-Organisationen an und
hält sie aktuell, etwa ein Abrechnungs- oder ERP-System. Sie ist keine öffentliche Schnittstelle und ohne
Schlüssel vollständig abgeschaltet; bei selbst betriebenen Installationen entsteht keine Angriffsfläche.

Es gibt zwei Versionen nebeneinander:

| Version | Pfad | Schlüssel | Arbeitsweise |
|---------|------|-----------|--------------|
| v2 | `/api/provisioning/v2/work/` | `PROVISIONING_V2_API_KEY` | deklarativ: Soll-Zustand setzen, wiederholbar (Issue #1132) |
| v1 | `/api/provisioning/organizations/` | `PROVISIONING_API_KEY` | Befehle: anlegen (`POST`), ändern (`PATCH`) |

v1 bleibt unverändert, bis alle Aufrufer auf v2 umgestellt sind. Nach dem ADR „Eine Schnittstelle“ sollen die
Betreiberschlüssel später durch Dienst-Tokens des OpenID-Connect-Anbieters abgelöst werden; bis dahin gelten sie.

### Provisioning v2: Einschalten und Zugang

| Variable | Vorgabe | Bedeutung |
|----------|---------|-----------|
| `PROVISIONING_V2_API_KEY` | leer | Schlüssel des aufrufenden Systems (lang und zufällig erzeugt). Leer = v2 abgeschaltet, alle Routen antworten mit **404** |
| `PROVISIONING_V2_ALLOWED_NETWORKS` | leer | Kommagetrennte CIDR-Netze, aus denen v2 erreichbar ist (z. B. das interne Netz des aufrufenden Systems). Andere Absender erhalten **404**. Leer = keine Beschränkung |

- Jede Anfrage trägt `Authorization: Bearer <PROVISIONING_V2_API_KEY>`. Fehlt der Schlüssel oder ist er
  falsch, antwortet v2 mit **401** `{"error": "unauthorized"}`. Verglichen wird in konstanter Zeit.
- v2 hat einen eigenen Schlüssel, getrennt von v1. Wer nur v2 nutzt, lässt `PROVISIONING_API_KEY` leer.
- Die Netzprüfung nutzt dieselbe Client-Adresse wie `ADMIN_ALLOWED_NETWORKS`: den ersten Eintrag aus
  `X-Forwarded-For`, sonst `REMOTE_ADDR`. Sie wirkt nur, wenn der vorgelagerte Proxy `X-Forwarded-For` durch
  die echte Client-Adresse ersetzt (Caddy tut das, siehe [ACCOUNT_SECURITY.md](ACCOUNT_SECURITY.md)). Bei
  einem anderen Proxy, der den Kopf nur ergänzt, ist die Prüfung umgehbar.
- Die Route gehört nicht ins offene Netz. Das mitgelieferte `Caddyfile` leitet `/api/*` vollständig weiter, also auch
  `/api/provisioning/` und `/api/insight/ki-bewertungen/`. Wer den Pfad am öffentlichen Proxy sperren will, ergänzt dort
  eine eigene Regel; sonst schützen Schlüssel und `PROVISIONING_V2_ALLOWED_NETWORKS`.
- CSRF-Prüfung entfällt (Server-zu-Server, Schlüssel statt Sitzung).

### Provisioning v2: Endpunkte

| Methode | Pfad | Zweck |
|---------|------|-------|
| `PUT` | `/api/provisioning/v2/work/<ref>` | Soll-Zustand setzen: anlegen, übernehmen oder ändern. Antwort 201 (angelegt) oder 200 |
| `GET` | `/api/provisioning/v2/work/<ref>` | Ist-Zustand lesen; unbekannte Referenz: 404 `{"error": "not_found"}` |
| `GET` | `/api/provisioning/v2/work/` | Alle Organisationen, auch solche ohne Referenz (`"ref": null`), für den vollständigen Abgleich |

`<ref>` ist die Kennung der Bereitstellung im aufrufenden System: 1 bis 64 Zeichen aus `A-Z`, `a-z`, `0-9`,
Punkt, Unterstrich und Bindestrich (nicht nur `.` oder `..`). mandari speichert sie an der Organisation
(`provisioning_ref`, eindeutig). Ein Schrägstrich am Ende ist erlaubt.

### Provisioning v2: Soll-Zustand (`PUT`)

Der Körper ist ein JSON-Objekt. **Nicht mitgeschickte Felder bleiben unverändert**; `null` ist ein Wert
(bei `member_limit` „unbegrenzt“). Unbekannte Felder und falsche Typen sind Fehler (400), nichts wird still
übergangen. `true`/`false` gelten nicht als Zahl.

| Feld | Typ | Beim Anlegen | Bedeutung |
|------|-----|--------------|-----------|
| `name` | Text, 1–200 Zeichen | Pflicht | Name der Organisation |
| `slug` | Text | Pflicht | Adresse im Arbeitsbereich. Neu: 3–40 Zeichen `a-z`, `0-9`, Bindestrich. Danach unveränderlich |
| `admin_email` | E-Mail | Pflicht (außer `adopt`) | Wer beim Anlegen als Administrator eingeladen wird. Wirkt nur beim Anlegen, siehe unten |
| `body_ags` | Text oder `null` | bei `aktiv` Pflicht¹ | Heimatkommune über den Amtlichen Gemeindeschlüssel: 8 Ziffern, für Kreise 5, Bezirke 3, Länder 2. `null` entfernt die Kommune |
| `body_id` | UUID oder `null` | – | Heimatkommune über die Kennung, wenn der Schlüssel mehrdeutig ist oder fehlt (etwa bei Verbandsgemeinden). Mit `body_ags` zusammen müssen beide passen |
| `party_group` | Text oder `null` | bei `aktiv` Pflicht¹ | Partei über ihre Kennung (UUID), ihren Namen oder Slug (Groß- und Kleinschreibung egal). Einen unbekannten Namen legt mandari wie in v1 als neue Partei an. Höchstens 100 Zeichen |
| `status` | `aktiv`, `gesperrt`, `beendet` | nur `aktiv` (Vorgabe) | siehe Status; Anlegen mit einem anderen Status gibt 409 `create_requires_active` |
| `member_limit` | ganze Zahl ≥ 1 oder `null` | Vorgabe `null` | Höchstzahl aktiver regulärer Mitglieder, `null` = unbegrenzt. Wird in Work durchgesetzt |
| `guest_limit` | ganze Zahl ≥ 0 | Vorgabe 25 | Höchstzahl aktiver Gast-Zugänge |
| `billing_reference` | Text, höchstens 100 Zeichen | Vorgabe leer | Vertrags- oder Abrechnungsnummer des aufrufenden Systems |
| `plan` | Kennung, 1–50 Zeichen `a-z`, `0-9`, `.`, `_`, `-` | Vorgabe `hosted` | Plan als technische Kennung. Welche Pläne es gibt, entscheidet das aufrufende System |
| `adopt` | `true`/`false` | – | Bestehende Organisation mit diesem Slug übernehmen (siehe unten) |

¹ Aktive Organisationen brauchen Kommune und Partei (`Organization.clean()`, dieselbe Regel wie im Admin).
Fehlt eines, antwortet v2 mit 400 und dem Feld `body_ags` bzw. `party_group`.

**Ablauf je `PUT`:**

1. **Referenz bekannt:** mitgeschickte Felder vergleichen und nur Abweichungen schreiben. Ein anderer `slug`
   gibt 409 `slug_immutable`. Ohne Abweichung: 200 mit `"changed": false`, nichts wird geschrieben.
2. **Referenz unbekannt, Slug frei:** Organisation anlegen wie in v1 (Standard-Rollen, Einladung an
   `admin_email` mit Administrator-Rolle, 14 Tage gültig). Antwort 201 mit `invitation_sent`. Anlegen geht
   nur mit Status `aktiv`: Mit `gesperrt` oder `beendet` antwortet v2 mit 409 `create_requires_active`
   (mit `message`), legt nichts an und lädt niemanden ein. Wer eine gesperrte Organisation braucht, legt
   sie aktiv an und sperrt sie mit dem nächsten `PUT`.
3. **Referenz unbekannt, Slug belegt:** 409 `slug_taken`.
4. **Übernahme (`"adopt": true`):** Gibt es eine Organisation mit diesem Slug **ohne** Referenz, erhält sie
   die Referenz, und die mitgeschickten Felder werden angewendet. Keine Einladung, `admin_email` ist nicht
   nötig. Antwort 200 mit `"adopted": true`. Hat sie schon eine andere Referenz: 409 `ref_conflict`. Gibt es
   keine Organisation mit dem Slug: 409 `adopt_not_found`. Dabei wird nichts angelegt, damit ein Tippfehler
   keine zweite Organisation erzeugt und niemanden einlädt. Nach der Übernahme gilt Schritt 1; `adopt` darf
   in Wiederholungen stehen bleiben.

Eine Wiederholung desselben `PUT` verschickt **nie** eine zweite Einladung. Ging die Einladungs-Mail beim
Anlegen nicht hinaus (`"invitation_sent": false`), bleibt die Einladung bestehen und lässt sich in Work unter
Mitglieder → offene Einladungen erneut senden. Den Einladungslink enthält die Antwort nie.

`admin_email` wirkt nur beim Anlegen. Eine spätere Änderung lädt niemanden ein; den Wechsel der
Administration regelt die Organisation in Work selbst, damit eine Änderung im aufrufenden System keine
Administratorrechte vergibt.

Alles läuft in einer Transaktion: Schlägt die Prüfung fehl, bleibt auch eine dabei angelegte Partei nicht
zurück. Treffen zwei Anfragen mit derselben Referenz gleichzeitig ein, gleicht die zweite die von der ersten
angelegte oder übernommene Organisation normal ab (kein `ref_conflict`, keine zweite Einladung). Stoßen
beide beim Anlegen auf die Eindeutigkeit von Slug oder Referenz, erhält die zweite 409 `conflict`; ihre
Wiederholung trifft dann den Bestand.

Ein Vertrag endet mit `beendet`; die Daten bleiben erhalten. Export und Löschen einer Organisation über die API
gibt es nicht.

### Provisioning v2: Status

| Soll | Wirkung | Ist (`status` in der Antwort) |
|------|---------|-------------------------------|
| `aktiv` | `is_active = true` | `aktiv`, solange die Organisation aktiv ist |
| `gesperrt` | `is_active = false` | `gesperrt` |
| `beendet` | `is_active = false`, Status `beendet` mit Zeitpunkt gespeichert | `beendet` |

Gespeichert werden `provisioning_status` und `provisioning_status_changed_at` (in der Antwort
`status_changed_at`). Der Zeitpunkt ändert sich nur, wenn sich der Status tatsächlich ändert.
**Sperren und Beenden löschen und verändern keine Nutzerdaten**: Mitgliedschaften, Gäste, Dokumente und
Einstellungen bleiben vollständig erhalten; `aktiv` schaltet die Organisation wieder ein.

Der Ist-Status folgt dem tatsächlichen Zustand. Wird eine beendete Organisation von Hand (etwa im
Django-Admin) wieder aktiv geschaltet, meldet v2 `aktiv`, und das nächste `PUT` mit `beendet` stellt den
Soll-Zustand wieder her.

### Provisioning v2: Antwort

```json
{
  "ref": "vertrag-0815",
  "slug": "fraktion-beispiel",
  "name": "Fraktion Beispiel",
  "status": "aktiv",
  "is_active": true,
  "status_changed_at": "2026-10-27T10:00:00+00:00",
  "member_limit": 10,
  "guest_limit": 25,
  "member_count": 4,
  "guest_count": 1,
  "work_url": "https://mandari.example.org/work/fraktion-beispiel/",
  "billing_reference": "V-2026-001",
  "plan": "hosted",
  "body": {"id": "…", "ags": "12345678", "name": "Beispielstadt"},
  "party_group": {"id": "…", "name": "Beispielpartei"},
  "created_at": "2026-10-27T10:00:00+00:00",
  "updated_at": "2026-10-27T10:00:00+00:00",
  "changed": true,
  "invitation_sent": true
}
```

- `work_url` ist absolut und beruht auf `SITE_URL`, nie auf dem Host der Anfrage.
- `member_count` zählt aktive reguläre Mitglieder (gegen `member_limit`), `guest_count` aktive Gäste.
  Beide können über dem Limit liegen, wenn das Limit nachträglich gesenkt wurde.
- `changed` und `invitation_sent` (nur beim Anlegen) bzw. `adopted` (nur bei der Übernahme) gibt es nur in
  der Antwort auf `PUT`.
- Die Liste (`GET …/work/`) antwortet mit `{"organizations": [...], "count": n}`, sortiert nach Name.

### Provisioning v2: Fehler

| Status | Körper | Bedeutung |
|--------|--------|-----------|
| 400 | `{"error": "validation", "fields": {"<feld>": "<Meldung>"}}` | ungültige Eingabe, auch falscher Typ, unbekanntes Feld oder ungültige Referenz (`ref`); Fehler des ganzen Körpers unter `__all__` |
| 400 | `{"error": "invalid_json"}` | Körper ist kein JSON |
| 401 | `{"error": "unauthorized"}` | Schlüssel fehlt oder ist falsch |
| 404 | – | v2 abgeschaltet oder Absender außerhalb von `PROVISIONING_V2_ALLOWED_NETWORKS` |
| 404 | `{"error": "not_found"}` | Referenz unbekannt (`GET`) |
| 405 | – | Methode gibt es nicht (etwa `DELETE`) |
| 409 | `{"error": "slug_taken"}` | Slug gehört einer anderen Organisation |
| 409 | `{"error": "slug_immutable"}` | Slug einer bestehenden Organisation lässt sich nicht ändern |
| 409 | `{"error": "ref_conflict"}` | Übernahme einer Organisation, die schon eine andere Referenz hat |
| 409 | `{"error": "adopt_not_found"}` | Übernahme ohne Organisation mit diesem Slug |
| 409 | `{"error": "create_requires_active", "message": "…"}` | Anlegen mit `gesperrt` oder `beendet`; nur `aktiv` legt an |
| 409 | `{"error": "conflict"}` | gleichzeitige Anfrage; wiederholen |
| 500 | `{"error": "no_system_user"}` | kein aktiver Superuser als Absender der Einladung |

### Mitglieder-Limit in Work

`member_limit` wird seit v2 durchgesetzt, wie das Gast-Limit:

- Keine neue Einladung, wenn aktive Mitglieder und offene Einladungen zusammen das Limit erreichen. Eine
  zurückgezogene Einladung gibt den Platz frei; eine abgelaufene belegt ihn erst wieder, wenn sie erneut
  gesendet wird.
- Eine Einladung lässt sich nicht annehmen, solange das Limit erreicht ist; sie bleibt gültig.
- Reaktivieren und Freischalten offener Registrierungsanfragen nur unterhalb des Limits.
- Selbstregistrierung mit sofortiger Freischaltung wird bei erreichtem Limit zur offenen Anfrage; offene
  Einladungen zählen dabei mit wie beim Einladen.
- Bestehende Mitglieder bleiben immer, auch über dem Limit. Gäste zählen nicht mit.
- `null` heißt unbegrenzt (Vorgabe, auch für selbst betriebene Installationen).
- Bei gesetztem Limit sperrt jede Prüfung die Zeile der Organisation (`SELECT … FOR UPDATE`) vor dem
  Zählen, damit gleichzeitige Beitritte das Limit nicht gemeinsam überschreiten. Ohne Limit entfallen Sperre
  und Abfrage.

Die Meldungen in Work nennen das Limit und den Weg (Einladung zurückziehen, Mitglied deaktivieren oder
Limit erhöhen).

### Provisioning v2: Beispiel

```bash
curl -X PUT "https://mandari.example.org/api/provisioning/v2/work/vertrag-0815" \
  -H "Authorization: Bearer $PROVISIONING_V2_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "Fraktion Beispiel", "slug": "fraktion-beispiel", "admin_email": "vorsitz@example.org",
       "body_ags": "12345678", "party_group": "Beispielpartei", "status": "aktiv",
       "member_limit": 10, "guest_limit": 25, "billing_reference": "V-2026-001", "plan": "hosted"}'
```

### Weitere Abrufe mit dem Schlüssel der Version 2

`GET /api/insight/ki-bewertungen/?seit=JJJJ-MM-TT&nach=<id>&anzahl=<n>` (Issue #1221) liefert dem System des
Betreibers die Bewertungen der KI-Zusammenfassungen als Einzelwerte ohne Personenbezug, seitenweise nach Kennung.
Zugang wie Provisioning v2: `PROVISIONING_V2_API_KEY` als `Authorization: Bearer`, Netze
`PROVISIONING_V2_ALLOWED_NETWORKS`; ohne Schlüssel 404, falscher Schlüssel 401, ungültige Parameter 400. Code:
`insight_core/views/ki_bewertung_api.py`, `insight_core/services/ki_bewertung.py`. Der Endpunkt nutzt bewusst den
vorhandenen Betreiberschlüssel, obwohl das ADR „Eine Schnittstelle“ diese Schlüssel ablösen will: Das aufrufende
System hat ihn schon.

### Provisioning v1

| Methode | Pfad | Zweck |
|---------|------|-------|
| `GET` | `/api/provisioning/organizations/` | Liste aller Organisationen mit `owner_email` |
| `POST` | `/api/provisioning/organizations/` | Anlegen mit Einladung; belegter Slug: 409 |
| `GET`, `PATCH` | `/api/provisioning/organizations/<slug>/` | Lesen bzw. `plan`, `is_active`, Limits, `billing_reference`, `parties` ändern |

Anlegen und Einladung laufen über denselben Code wie in v2 (`apps/provisioning/services.py`).

### Provisioning: Implementierung

| Teil | Datei |
|------|-------|
| Anlegen mit Einladung, Mailversand (v1 und v2) | `mandari/apps/provisioning/services.py` |
| v2: Zugang und HTTP | `mandari/apps/provisioning/v2/views.py`, `urls.py` |
| v2: Prüfung und Anwenden des Soll-Zustands | `mandari/apps/provisioning/v2/services.py` |
| v2: Ist-Zustand und Liste | `mandari/apps/provisioning/v2/selectors.py` |
| Felder `provisioning_ref`, `provisioning_status`, `provisioning_status_changed_at` | `mandari/apps/tenants/models.py`, Migration `tenants/0029_provisioning_v2` |
| Mitglieder-Limit | `Organization.has_free_member_slot()`, `mandari/apps/work/organization/services.py` |
| Tests | `mandari/apps/provisioning/tests/test_v2_api.py`, `mandari/apps/work/organization/tests/test_mitglieder_limit.py` |

Die Plattform-Schicht (`apps.provisioning`) importiert `insight_core` nicht; die Kommune wird über den
Fremdschlüssel `Organization.body` gesucht.

## Bereiche anmelden

Die Drehscheibe kennt die Fachmodule nicht (A1). Jeder Bereich meldet sich deshalb beim Start an:

```python
from hub.api.openapi import Area, register_area

register_area(Area(key="session-v1", title="Session-API v1", description="…",
                   entry="/api/v1/session/{tenant_slug}/", openapi=beschreibung))
```

`openapi` liefert die Beschreibung des Bereichs (`paths`, `components`) und wird erst beim Abruf gebaut.
Heute angemeldet: der OParl-Aggregator (`hub/api/openapi/aggregator.py`), OParl je Mandant, OParl je Mandant
nichtöffentlich, das Profil Einreichung und die Session-API v1 (alle in `apps/session/api/schnittstelle.py`), dazu
der Einstieg selbst. Die OParl-Pfade stammen aus den Routen der Ausgaben, die Schemas der OParl-Typen aus der
kommParl-Spezifikation (`hub/api/kommparl/`), denselben Schemas wie die Typprüfung des OParl-Validators.

## Gemeinsame Regeln als Code

Fachfreie Bausteine liegen in der Plattform, `apps/common/api/`:

| Baustein | Modul | genutzt von |
|---|---|---|
| Fehler nach RFC 9457 (`Problem`, `ProblemError`, `problem_response`, Handler für django-ninja) | `problems.py`, `ninja.py` | Session-API v1, nichtöffentliche Ausgabe, Befehle über HTTP, Änderungsfeed (ältere Form), Datenkatalog |
| `ETag` und `If-None-Match` | `conditional.py` | OParl-Ausgaben, Einstieg |
| `Deprecation` (RFC 9745) und `Sunset` (RFC 8594) | `deprecation.py` | alte Session-API, abgekündigte Form des Änderungsfeeds (`hub/api/changes.py`) |
| `Idempotency-Key` lesen und prüfen | `idempotency.py` | Befehle (`hub.commands`), Session-API v1 |
| Seiten wie in OParl (`data`, `pagination`, `links`, `Link`-Header) | `pages.py` | OParl-Ausgaben, Beschreibung |

Die öffentlichen OParl-Ausgaben antworten bei Fehlern mit dem Fehlerobjekt nach OParl 1.1
(`{"error", "status"}`, `hub/api/http.py`), nicht mit `application/problem+json`. Die nichtöffentliche
Ausgabe (`apps/session/api/nichtoeffentlich.py`) antwortet bei Fehlern mit `application/problem+json`
und liefert kein `ETag`.

## Eingecheckte Beschreibung und CI

Die Beschreibung liegt als [`docs/schnittstelle/openapi.json`](schnittstelle/openapi.json) im Repository. Wer
einen Endpunkt ändert, schreibt sie neu und committet sie mit:

```bash
cd mandari && python manage.py schnittstelle_beschreibung        # neu schreiben
cd mandari && python manage.py schnittstelle_beschreibung --pruefen
```

- **Aktualität:** `hub/api/tests/test_schnittstelle.py` vergleicht die Datei mit der erzeugten Beschreibung.
- **Lizenz:** Die Datei enthält die umgewandelten Schemas aus kommParl und steht deshalb unter `CC0-1.0 AND CC-BY-SA-4.0`
  (`REUSE.toml`, mit den Urhebern der übernommenen Teile).
- **Brechende Änderungen:** Der CI-Job „Schnittstelle“ vergleicht die Datei mit dem Stand der Basis (oasdiff,
  Stufe `ERR`) und prüft sie gegen OpenAPI 3.1. Eine brechende Änderung braucht eine neue Hauptversion im Pfad
  und eine Abkündigung nach [RELEASE_POLITIK.md](RELEASE_POLITIK.md#6-schnittstellen-und-abkündigung).
- **Befehlsparität:** Jeder Befehl im Register (`hub/contracts/schemas`) braucht einen Endpunkt, der ihn in
  `x-mandari-command` nennt. Ausnahmen stehen in `mandari/hub/api/openapi/befehle_ohne_endpunkt.json`; die Liste
  darf nur kürzer werden (`scripts/check_schnittstelle.py --basis <stand>`). Die Session-API v1 setzt die
  Erweiterung erst für die gemeinsame Beschreibung (`SESSION_V1_COMMANDS` in `apps/session/api/schnittstelle.py`),
  ihre eigene unter `/api/v1/session/openapi.json` bleibt unverändert.
