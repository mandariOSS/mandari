# Schnittstelle: Einstieg, Beschreibung, gemeinsame Regeln

Grundlage: ADR [Eine Schnittstelle: API-first auf OParl, kommParl und OpenAPI](adr/20261105-eine-schnittstelle-api-first.md)
(Epic #1203), umgesetzt in #1205. Die Bereiche selbst beschreiben [OPARL_API.md](OPARL_API.md),
[SESSION_OPARL_API.md](SESSION_OPARL_API.md) und [API_V1_SESSION.md](API_V1_SESSION.md).

## Adressen

| Adresse | Inhalt |
|---|---|
| `GET /api/` | Einstieg: Verweis auf das OParl-System der Installation, `kommparl:conformsTo`, Beschreibung, Dokumentation, Bereiche |
| `GET /api/openapi.json` | eine OpenAPI-3.1-Beschreibung aller Bereiche |
| `GET /api/docs/` | Swagger UI mit den lokalen Dateien von django-ninja (kein CDN) |

Alle übrigen Adressen unter `/api/` (`stats/`, `contact/`, `v1/session/`, `public/v1/`, `provisioning/`,
`provisioning/v2/`) bleiben unverändert; `hub/api/tests/test_schnittstelle.py` prüft das.

### Der Einstieg ist kein zweites OParl-System

In OParl ist die Adresse eines Objekts seine Kennung. Ein `System` unter `/api/` hätte entweder eine fremde
Kennung (zwei Adressen für ein Objekt, zwei Fassungen mit verschiedenem `ETag`) oder eine zweite Kennung. Beides
verwirrt Anwendungen, die Objekte an ihrer `id` erkennen. Der Einstieg ist deshalb ein eigenes Dokument und
verweist mit `system` auf das System des Aggregators (`/oparl/v1/system`).

`kommparl:conformsTo` nennt nur Kennungen, deren Anforderungen die Installation vollständig erfüllt (kommParl,
Kapitel 5): heute `https://schema.kommparl.de/0.1/`. Dieselbe Liste steht seit #1216 am `System` beider OParl-Ausgaben
(`hub.api.kommparl.conforms_to`). Die Profile Änderungsfeed und Snapshot kämen hinzu, sobald die Installation den
Feed anbietet (`OPARL_CHANGES_ENABLED`) und sie vollständig erfüllt sind (`PROFILES_FULFILLED`). Seit #1229 nennt der
Feed auch das einbettende Objekt, wenn sich ein eingebettetes ändert (Profil Änderungsfeed, Abschnitt
„Vollständigkeit und Reihenfolge“; ADR A10, Nachtrag #1229). Erfüllt ist das Profil damit noch nicht: Session meldet
Gremien, Personen, Mitgliedschaften, Wahlperioden und die Körperschaft nicht als Ereignis, `modified` kann sich ohne
öffentliches Ereignis ändern, der Aggregator nennt Änderungen gespiegelter Session-Mandanten vor ihrer Übernahme,
ein nichtöffentlich gewordener Tagesordnungspunkt des Bestands bleibt ohne `deleted: true` abrufbar, und hängt eine
Datei des Bestands an einer anderen Vorlage, bekommt die frühere keinen Eintrag. Diese Punkte bleiben unter #1229
offen. Das Profil Snapshot setzt das Profil Änderungsfeed voraus.

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
  übernimmt die Profilschemas als Komponenten `Kommparl…` (ohne `$id`, `$defs` aufgelöst). `hub/api/oparl_types.py`
  entfällt; Beschreibung und Konformitätsprüfung (`hub/api/tests/konformitaet.py`) nutzen dieselben Schemas.
- **Ergänzungen** (`hub/api/openapi/oparl.py`), weil die Spezifikation sie nur im Text nennt: die sieben Werte von
  `organizationType` und `license` als URL (kommParl Kapitel 6), dazu die abgekündigten Eigenschaften
  `mandari:changes` und `mandari:snapshot` am Body. Eigene Felder erlaubt die Beschreibung wie bisher mit jedem
  Namensraum; dass `kommparl:`-Eigenschaften im Schema stehen, prüft die Konformitätsprüfung.
- **Änderungsfeed und Snapshot nach den Profilen** unter `…/kommparl/changes` und `…/kommparl/snapshot` (am Body
  `kommparl:changes`, `kommparl:snapshot`). Die ältere Form konnte nicht additiv angepasst werden: Die Profilschemas
  lassen keine Felder ohne Namensraum zu (`freshness`, `links.snapshot`, Kopfzeile des Snapshots) und legen andere
  Gründe, `links.next` nur bis zur letzten Seite, eine Schlusszeile und ein Fehlerobjekt nach OParl 1.1 fest.
- **Abkündigung:** `mandari:changes` und `mandari:snapshot` bleiben mit ihren Adressen bis frühestens 01.12.2027
  (12 Monate, [RELEASE_POLITIK.md](RELEASE_POLITIK.md), Abschnitt 5). Die Beschreibung kennzeichnet Endpunkte und
  Eigenschaften mit `deprecated` und `x-sunset`, die Antworten tragen `Deprecation`, `Sunset` und den Nachfolger im
  Header `Link`. Die Beschreibung bleibt ohne brechende Änderung (oasdiff).

## Profil Einreichung (#1228)

Anträge, Änderungsanträge und Anfragen einreichen, den Bearbeitungsstand abrufen, zurücknehmen und sich zur Ladung
zurückmelden: nach dem Profil Einreichung von kommParl (`https://schema.kommparl.de/0.1/profile/submission`, Kapitel
„Profil Einreichung“), je Session-Mandant. Code: `apps/session/api/einreichung.py`, Beschreibung
`apps/session/api/einreichung_beschreibung.py` (Bereich „Profil Einreichung“).

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
- **Anmeldung:** nur Access-Tokens im Namen einer Person mit `einreichung:schreiben`
  ([Anmeldung](schnittstelle/anmeldung.md#einreichen-profil-einreichung)). Dienst-Tokens 403, API-Tokens der
  Session-API 401, Sitzungscookies zählen nicht, deshalb ohne CSRF.
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
  `multipart/form-data`, geprüft wie Antrags-Anhänge (Dateityp, Größe, Virenprüfung) und mit `validate_upload`
  ([Uploads](UPLOADS.md)), bis zum Einreichen 24 Stunden in `SessionSubmissionUpload` unter `session/files/` (nie über
  `/media/`). Je Person über alle Mandanten höchstens 50 offene Dateien und 500 MB (429). Eine Datei lässt sich in
  genau einer Einreichung verwenden: Vor dem Befehl wird sie für den `Idempotency-Key` vorgemerkt, eine andere
  Einreichung bekommt 422. Beim Einreichen übernimmt der Befehl die Datei in die Ablage der Anlagen; der Inhalt des
  Uploads wird danach gelöscht, die Angaben bleiben 30 Tage für Wiederholungen. Nicht eingereichte Dateien löscht der
  Zeitplan `befehl:einreichung_aufraeumen` stündlich. Hochgeladene Inhalte liefert das Profil nie aus.
- **Rückmeldung zur Ladung:** Herkunft „portal“ wie im Portal; der Grund einer Absage wird verschlüsselt gespeichert
  und erscheint in keiner Antwort. Die Rückmeldung bestätigt zugleich den Empfang aller Versände der Sitzung.
- **Prüfprotokoll:** Jede schreibende Handlung und jeder Abruf von Einreichungen steht im Protokoll des Mandanten mit
  Person (`user:<id>`) und Client-ID der Anwendung, ohne Inhalte.
- **Konformität:** Die Tests prüfen jede Antwort gegen die Schemas des Profils (`hub/api/kommparl/0.1/profiles/submission/`)
  und Body und Sitzung gegen die Objekttypen aus kommParl.

## Bereiche anmelden

Die Drehscheibe kennt die Fachmodule nicht (A1). Jeder Bereich meldet sich deshalb beim Start an:

```python
from hub.api.openapi import Area, register_area

register_area(Area(key="session-v1", title="Session-API v1", description="…",
                   entry="/api/v1/session/{tenant_slug}/", openapi=beschreibung))
```

`openapi` liefert die Beschreibung des Bereichs (`paths`, `components`) und wird erst beim Abruf gebaut.
Heute angemeldet: der OParl-Aggregator (`hub/api/openapi/aggregator.py`), OParl je Mandant und die Session-API v1
(`apps/session/api/schnittstelle.py`) sowie das Profil Einreichung (`apps/session/api/einreichung_beschreibung.py`). Die OParl-Pfade stammen aus den Routen der Ausgaben, die Schemas der
OParl-Typen aus der kommParl-Spezifikation (`hub/api/kommparl/`), denselben Schemas wie die Typprüfung des
OParl-Validators.

## Gemeinsame Regeln als Code

Fachfreie Bausteine liegen in der Plattform, `apps/common/api/`:

| Baustein | Modul | genutzt von |
|---|---|---|
| Fehler nach RFC 9457 (`Problem`, `ProblemError`, `problem_response`, Handler für django-ninja) | `problems.py`, `ninja.py` | Session-API v1, Befehle über HTTP, Änderungsfeed, Datenkatalog |
| `ETag` und `If-None-Match` | `conditional.py` | OParl-Ausgaben, Einstieg |
| `Deprecation` (RFC 9745) und `Sunset` (RFC 8594) | `deprecation.py` | alte Session-API |
| `Idempotency-Key` lesen und prüfen | `idempotency.py` | Befehle (`hub.commands`), Session-API v1 |
| Seiten wie in OParl (`data`, `pagination`, `links`, `Link`-Header) | `pages.py` | OParl-Ausgaben, Beschreibung |

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
  und eine Abkündigung nach [RELEASE_POLITIK.md](RELEASE_POLITIK.md), Abschnitt 5.
- **Befehlsparität:** Jeder Befehl im Register (`hub/contracts/schemas`) braucht einen Endpunkt, der ihn in
  `x-mandari-command` nennt. Ausnahmen stehen in `mandari/hub/api/openapi/befehle_ohne_endpunkt.json`; die Liste
  darf nur kürzer werden (`scripts/check_schnittstelle.py --basis <stand>`). Die Session-API v1 setzt die
  Erweiterung erst für die gemeinsame Beschreibung (`SESSION_V1_COMMANDS` in `apps/session/api/schnittstelle.py`),
  ihre eigene unter `/api/v1/session/openapi.json` bleibt unverändert.

## Bestehende Bereiche bleiben unverändert

Die Session-API v1 nutzt die gemeinsamen Bausteine, ihre Antworten bleiben aber wie seit #163: Sie behält ihre
Titel je Status (`TITLES` in `apps/session/api/v1/problems.py`), Status außerhalb davon heißen weiter „Fehler“,
auch 501 und 503 aus dem Befehl `submission.submit`. Neue Bereiche nutzen die gemeinsame Tabelle
(`apps/common/api/problems.py`).
