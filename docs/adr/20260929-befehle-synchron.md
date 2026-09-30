# Befehle laufen synchron: In-Process- und HTTP-Client, Idempotenzschlüssel, RFC 9457

- Status: angenommen
- Datum: 2026-09-29
- Issue: #476
- Paket: Datendrehscheibe, A6
- Hängt ab von: [A1 Schichtenmodell](20260929-schichtenmodell.md),
  [A5 Verträge](20260929-ereignisvertraege.md)

## Kontext

Work reicht Anträge heute auf drei Wegen bei Session ein, jeweils mit eigener Validierung: über
einen direkten Aufruf des Session-Codes, über die alte Session-API und über die API v1. Für Zu- und
Absagen zu Sitzungen gibt es vier Wege. Work importiert dafür Session-Code. Sollen Work als
gehosteter Dienst und Session im Rechenzentrum einer Kommune getrennt laufen, fehlt ein netzfähiger
Weg mit derselben Bedeutung wie der interne.

Ereignisse ([A2](20260929-ereignistechnik-postgres.md)) passen hier nicht: Wer einen Antrag
einreicht, braucht sofort eine Eingangsnummer oder einen verständlichen Fehler.

## Entscheidung

- Wer Daten eines anderen Moduls ändern will, schickt dem Eigentümer einen **Befehl**. Befehle
  sind **synchron**: Der Aufrufer erhält eine Quittung oder einen Fehler. Folgen für weitere Module
  laufen danach asynchron über Ereignisse.
- **In-Process:** `hub.commands.dispatch(SubmitApplication(...)) -> Receipt`. Der Handler liegt
  beim Eigentümer (z. B. `apps/session/commands.py`), läuft in dessen Transaktion, schreibt die
  Fachdaten und veröffentlicht Ereignisse wie `submission.received`. `hub.commands` enthält nur
  Dispatcher, Verträge und Clients, keine Fachregeln.
- **HTTP (Profil Einreichung):** django-ninja mit OpenAPI, z. B.
  `POST /api/v1/einreichung/antraege`. Pflicht-Header `Idempotency-Key`. Fehler als
  `application/problem+json` nach RFC 9457. Erfolg mit `201` und Quittung: Eingangsnummer,
  Eingangszeit, Inhalts-Hash. Zugriff über Tokens mit Berechtigungsumfang.
- **Idempotenz:** Beide Wege tragen einen Idempotenzschlüssel. Der Eigentümer speichert Schlüssel,
  Hash der Anfrage und Quittung. Wiederholung mit gleichem Schlüssel und gleicher Anfrage liefert
  dieselbe Quittung; gleicher Schlüssel mit anderer Anfrage wird abgelehnt (`422`); fehlt der
  Schlüssel, antwortet die API mit `400`. Das folgt dem IETF-Entwurf zum Header
  `Idempotency-Key`.
- **Clients:** eine Schnittstelle mit zwei Implementierungen, `InProcessClient` (gleiche
  Installation) und `HttpClient` (getrennte Installationen). Beide bestehen dieselbe
  Vertragstestsuite.
- **Startumfang:** `submission.submit`, `submission.withdraw`, `attendance.respond`,
  `invitation.acknowledge`. Befehlsschemas liegen wie Ereignisschemas in `hub/contracts`
  ([A5](20260929-ereignisvertraege.md)).
- **Alte Wege** werden nach `docs/RELEASE_POLITIK.md` abgekündigt und entfernt. Die alte
  Session-API ist bereits abgekündigt (siehe CHANGELOG, Abschnitt „Abgekündigt“).
- **Keine asynchrone Ausnahme für Offline-Clients:** Eine App, die offline eine Zu- oder Absage
  erfasst, speichert sie lokal und wiederholt den Befehl mit demselben Idempotenzschlüssel, bis
  eine Quittung vorliegt.

## Alternativen

- **Befehle als asynchrone Nachrichten.** Keine sofortige Eingangsnummer, Validierungsfehler
  erst später, die Oberfläche müsste Zwischenzustände verwalten. Verworfen.
- **Direkter Aufruf von Session-Services aus Work (heutiger Weg).** Koppelt die Fachmodule,
  verhindert getrennte Installationen und hält drei Validierungen am Leben. Verworfen.
- **Immer HTTP, auch innerhalb einer Installation.** Netz- und Anmeldeaufwand ohne Nutzen, wenn
  beide Module im selben Prozess laufen. Verworfen als Standard; der HTTP-Client bleibt für
  getrennte Installationen.
- **Doppelklickschutz nur im Client.** Schützt nicht vor dem Fall, dass die Antwort nach
  erfolgreicher Verarbeitung verloren geht. Verworfen.
- **Eigenes Fehlerformat.** Widerspricht den Engineering-Standards, die RFC 9457 vorgeben.
  Verworfen.

## Folgen

**Positiv**

- Ein Validierungsweg je Vorgang beim Eigentümer statt drei oder vier.
- Sofortige Quittung für Nutzer; Wiederholungen sind gefahrlos.
- Work und Session lassen sich getrennt installieren, ohne dass sich die Bedeutung ändert.
- Die HTTP-Befehle sind zugleich die Grundlage des offenen Profils „Einreichung“.

**Negativ**

- Über HTTP hängt der Aufrufer an der Erreichbarkeit des Eigentümers; Work muss Ausfälle anzeigen
  und später wiederholen.
- Der Idempotenzspeicher braucht eine Aufbewahrungsfrist und Aufräumen.
- Zwei Client-Implementierungen sind zu pflegen.
- Bestehende Integrationen müssen innerhalb der Abkündigungsfrist umstellen.

## Prüfung (Fitnessfunktion)

- Die Vertragstestsuite läuft in der CI gegen `InProcessClient` und `HttpClient`.
- Tests: gleiche Anfrage mit gleichem Schlüssel ergibt dieselbe Quittung; abweichende Anfrage
  ergibt `422`; fehlender Schlüssel ergibt `400`; alle Fehler im Format RFC 9457.
- import-linter: Work importiert nichts aus Session (Vertrag `independence` aus
  [A1](20260929-schichtenmodell.md)).
- Kennzahl: je Vorgang genau ein Einreichungs- bzw. Rückmeldeweg nach Ablauf der Abkündigung.
- Änderungen am OpenAPI-Schema der Einreichungs-API folgen der Release-Politik.

## Nachtrag zur Umsetzung (#539)

Die Entscheidung bleibt unverändert; die Umsetzung in `hub/commands/` legt Folgendes fest.

- **Idempotenzspeicher in der Plattform.** Schlüssel, Hash der Anfrage und Quittung liegen fachfrei
  in `events_idempotency` (`apps.events.idempotency.run_once`). Der Dispatcher belegt den Schlüssel
  in derselben Transaktion, in der der Handler des Eigentümers die Fachdaten schreibt; scheitert der
  Handler, rollt beides zurück. Gleichzeitige Anfragen mit demselben Schlüssel warten am eindeutigen
  Index, der Handler läuft nur einmal. Bereich eines Schlüssels sind Mandant und Auslöser; die
  Aufbewahrung beträgt 30 Tage (`EVENTS_IDEMPOTENCY_RETENTION_DAYS`, Befehl
  `events_idempotency_purge`).
- **Reihenfolge der Prüfungen und Probleme** (RFC 9457, Typen unter
  `https://docs.mandari.de/api/probleme/`): fehlender oder ungültiger Schlüssel 400
  (`idempotenzschluessel-fehlt`), unbekannter Befehl 404 (`befehl-unbekannt`), kein Handler in dieser
  Installation 501 (`befehl-nicht-verfuegbar`), Inhalt passt nicht zum Schema 422 (`validierung`, mit
  `errors` als JSON-Pointer ohne Werte), Schlüssel für eine andere Anfrage 422
  (`idempotenzschluessel-wiederverwendet`). Fachliche Ablehnungen meldet der Handler selbst (z. B. 409),
  jede andere Ausnahme wird zu 500 mit festem Text. Über HTTP kommen 401, 403, 405, 413 und 415 dazu,
  im `HttpClient` 503 (Eigentümer nicht erreichbar) und 502 (unerwartete Antwort).
- **Handler beim Eigentümer:** Registriert wird nur für einen Befehl im Register, und nur aus dem
  Paket, das der Vertrag als `x-owner` nennt; sonst bricht der Start ab.
- **HTTP-Weg:** `POST …/<befehl>/v<version>` mit JSON-Objekt, `Idempotency-Key` (mit oder ohne
  Anführungszeichen) und Anmeldung über eine Funktion der Installation, die Mandant, Auslöser und
  erlaubte Befehle liefert; Erfolg 201 mit Quittung. Die Adressen werden erst eingebunden, wenn ein
  Eigentümer Befehle anbietet (Profil Einreichung, #540).
- **Quittung:** Eingangsnummer bzw. Kennung des Eigentümers, Eingangszeit und Inhalts-Hash (SHA-256
  über das kanonische JSON des Inhalts nach RFC 8785), dazu optional Kennung des Aggregats und weitere
  Kennungen. Eine Wiederholung erhält sie unverändert.

## Bezug

- [A5 Verträge](20260929-ereignisvertraege.md), [A2 Ereignistechnik](20260929-ereignistechnik-postgres.md)
- `docs/ENGINEERING_STANDARDS.md`, Abschnitt 6; `docs/API_V1_SESSION.md`;
  `docs/WORK_SESSION_SUBMISSION.md`; `docs/RELEASE_POLITIK.md`
- RFC 9457, Problem Details for HTTP APIs: https://www.rfc-editor.org/rfc/rfc9457
- IETF-Entwurf „The Idempotency-Key HTTP Header Field“:
  https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/
- K. Grzybek, Integration Styles:
  https://www.kamilgrzybek.com/blog/posts/modular-monolith-integration-styles
