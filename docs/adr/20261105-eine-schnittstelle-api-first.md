# Eine Schnittstelle: API-first auf OParl, kommParl und OpenAPI

- Status: angenommen
- Datum: 2026-11-05
- Issue: #1204 (Epic #1203), Teile #1205, #1206
- Paket: Drehscheibe und Plattform
- Hängt ab von: [A1 Schichtenmodell](20260929-schichtenmodell.md),
  [A6 Befehle](20260929-befehle-synchron.md),
  [A7 Kanonisches Modell](20260929-kanonisches-modell.md),
  [A10 Änderungsfeed](20260929-aenderungsfeed-format.md),
  [Rechte mit Geltungsbereich](20261003-rechte-mit-geltungsbereich.md)

## Kontext

mandari hat heute zehn Schnittstellen nach außen. Sie sind unabhängig voneinander entstanden:

| Schnittstelle | Pfad | Zugang | genutzt von |
|---|---|---|---|
| OParl-Aggregator der Drehscheibe | `/oparl/` | offen | OParl-Anwendungen, Open Data |
| OParl je Session-Mandant | `/session/<mandant>/api/oparl/` | offen nach Freischaltung | OParl-Anwendungen |
| alte Session-API | `/session/<mandant>/api/` | Token | abgekündigt, Sunset 31.05.2027 (#543) |
| Session-API v1 | `/api/v1/session/` | `SessionAPIToken` oder Sitzung | Work (Einreichung), Integrationen |
| Fraktions-API | `/api/public/v1/` | Token je Organisation | Websites von Fraktionen |
| Website-API | `/api/stats/`, `/api/contact/` | offen | Website |
| Provisioning v1 | `/api/provisioning/` | Betreiberschlüssel | früheres Kundenportal |
| Provisioning v2 | `/api/provisioning/v2/` | Betreiberschlüssel | Bereitstellung |
| DCAT-AP.de | `/data/dcat/`, `/session/<mandant>/api/dcat/` | offen | Datenportale |
| iCalendar | Work-Feeds, Einladungen | Feed-Token | Kalender-Anwendungen |

Hinzu kommen interne JSON-Endpunkte in 46 Dateien (Aufgaben-Board, Merkliste, Chat …), die nur die eigene
Oberfläche aufruft. Es gibt vier Arten von Zugangsschlüsseln.

Jede dieser Schnittstellen beschreibt Sitzungen und Vorlagen auf eigene Weise. In der Oberfläche ist es
nicht anders: In Session laden sieben Ansichten eine Sitzung jeweils mit einer eigenen Funktion, und die
Regel „nichtöffentlich nur mit Recht“ steht an rund 280 Stellen in 47 Dateien
([Rechte mit Geltungsbereich](20261003-rechte-mit-geltungsbereich.md)). Ändert sich eine Regel, muss sie
an vielen Stellen nachgezogen werden. Wird eine Stelle vergessen, zeigt sie mehr als die anderen.

Neue Anwendungen brauchen dieselben Daten über einen verlässlichen Weg: die App (#113), die
Open-Data-Plattform (#112), Partnerdienste und Installationen im Rechenzentrum einer Kommune.
Mandatstragende sollen über Anwendungen auch nichtöffentliche Unterlagen erhalten (#565).

## Entscheidung

**mandari ist API-first. Es gibt eine Schnittstelle mit einem Vertrag. Ratsdaten sprechen OParl,
offene Erweiterungen kommParl, alles Weitere folgt denselben Regeln. Die eigene Oberfläche nutzt dieselben
Dienste wie die Schnittstelle.**

### Ein Vertrag, drei Schichten

| Schicht | Inhalt | Kennzeichen |
|---|---|---|
| OParl 1.1 | die Typen des RIS-Kerns, unverändert | Typ-URL `https://schema.oparl.org/1.1/…` |
| kommParl | offene Erweiterungen, die jedes RIS umsetzen kann: Änderungsfeed, Snapshot, Nichtöffentliches, Einreichung, Abstimmungen | Präfix `kommparl:`, Namensraum `https://schema.kommparl.de/<version>/` |
| mandari | nur mandari-spezifisch: Work, Sitzungsgeld, Ladungsrückmeldung, Bereitstellung | Präfix `mandari:` |

1. **Ratsdaten werden immer als OParl-Objekte ausgegeben.** Eine Anwendung für OParl 1.1 liest jede
   mandari-Schnittstelle ohne Anpassung und übergeht unbekannte Felder (A7).
2. **Gibt es etwas auch in anderen RIS, wird es ein kommParl-Profil. Gibt es es nur bei mandari, bekommt es
   das Präfix `mandari:`.** Ein `mandari:`-Feld wechselt nach kommParl, sobald das Profil feststeht. Das
   bisherige Feld bleibt bis zum Ende der Abkündigungsfrist erhalten. Das betrifft heute zum Beispiel
   `mandari:changes` und `mandari:snapshot`.
3. **Fachfremdes wird nicht in OParl-Typen gepresst.** Interne Daten von Work gehören nicht zum RIS-Kern
   (A7). Sie sind eigene `mandari:`-Typen. Ein Parteitagsantrag ist zum Beispiel kein `Paper`.
4. **Andere Standards sind Darstellungen derselben Daten, keine eigenen Schnittstellen.** Dazu gehören
   DCAT-AP.de für Kataloge, iCalendar für Termine und OpenID Connect für die Anmeldung. Sie verweisen auf die
   Schnittstelle und nutzen dieselben Dienste.

### Ein Einstieg, eine Beschreibung, gemeinsame Regeln

- **Einstieg `/api/`:** liefert das OParl-`System`-Objekt der Installation mit den unterstützten Profilen
  (`kommparl:conformsTo`) und Verweisen auf alle Bereiche.
- **Adressen der OParl-Objekte bleiben unverändert:** `/oparl/` und `/session/<mandant>/api/oparl/`, denn die
  Adresse ist zugleich die Kennung (A7). Bereiche nur für mandari liegen unter `/api/mandari/v<n>/`.
- **Eine Beschreibung nach OpenAPI 3.1** unter `/api/openapi.json` umfasst alle Bereiche, auch die OParl-Pfade.
  Sie wird aus dem Code erzeugt (django-ninja, wie die Session-API v1 seit #163) und als Datei
  eingecheckt. Brechende Änderungen lassen die CI scheitern. Die Schemas der OParl- und kommParl-Typen
  stammen aus der kommParl-Spezifikation, und der OParl-Validator (#560) prüft die Ausgabe.
- **Gemeinsame Regeln für alle Bereiche:**
  - Objekte haben eine feste Adresse als `id` sowie `type`, `created`, `modified` und `deleted` wie in OParl.
  - Listen haben Seiten wie in OParl (`data`, `pagination`, `links`).
  - Fehler werden als `application/problem+json` nach RFC 9457 gemeldet.
  - Schreibende Aufrufe sind Befehle nach A6 mit `Idempotency-Key`.
  - Bedingte Abrufe laufen über `ETag` und `If-None-Match`.
- **Versionen:** Additive Änderungen bekommen keine neue Version (wie A5). Brechende Änderungen bekommen
  eine neue Hauptversion im Pfad. Die alte wird mit den Headern `Deprecation` (RFC 9745) und `Sunset`
  (RFC 8594) abgekündigt, mit den Fristen aus `docs/RELEASE_POLITIK.md`.
- **Ort nach A1:** Die Schnittstelle liegt in der Drehscheibe (`hub/api`), die Anmeldung in der Plattform.

### Ein Login für alle Zugänge

- **mandari wird OpenID-Connect-Anbieter** in der Plattform:
  - Authorization Code mit PKCE für Anwendungen, die im Namen einer Person handeln
  - Client Credentials für Dienste
  - Die Anmeldung selbst bleibt die von mandari, einschließlich des zweiten Faktors.
- **Scopes begrenzen den Zugriff.** Ein Token im Namen einer Person darf nie mehr als die Person selbst.
  Die Rechte werden genauso ausgewertet wie in der Oberfläche
  ([Rechte mit Geltungsbereich](20261003-rechte-mit-geltungsbereich.md)).
- **Nichtöffentliches gibt es nur für Tokens, die einer Person gehören.** Das gilt erst, wenn das
  Zugriffsmodell abgestimmt und ein Penetrationstest bestanden ist (#565). Ein Dienst liest Nichtöffentliches
  nur für einen Auftrag, den eine berechtigte Person gestartet hat. Jeder solche Zugriff landet im
  Prüfprotokoll.
- **Die eigene Oberfläche** nutzt die Browser-Sitzung mit CSRF-Schutz, alles andere ein Token.
- **Die bisherigen Schlüssel werden abgelöst:** `SessionAPIToken`, die Tokens der Fraktions-API und die
  Betreiberschlüssel der Bereitstellung. Feed-Tokens für Kalender bleiben, weil Kalender-Anwendungen kein
  OAuth beherrschen.
- **SSO mit kommunalen Identitätsdiensten (#95)** ist die Gegenrichtung (mandari als Client) und hängt
  hiervon nicht ab.

### Die Oberfläche nutzt dieselben Dienste

- **Lesen und Schreiben:** Gelesen wird über die Abfragen des Eigentümers, also über Selektoren, für den
  RIS-Bestand `hub/ris/selectors.py` (A7). Geschrieben wird über Befehle (A6). Rechte und Nichtöffentliches
  prüfen nur diese Dienste. Ansichten und Schnittstelle sind zwei Ausgaben desselben Dienstes.
- **Keine Single-Page-Anwendung.** Die Oberfläche bleibt serverseitig gerendert, weil das barrierefrei,
  auffindbar und mit wenig JavaScript auskommt.
- **JavaScript in der Oberfläche** ruft die Schnittstelle unter `/api/` mit der Browser-Sitzung auf. Neue
  interne JSON-Endpunkte entstehen nicht mehr. Die bestehenden ziehen bereichsweise um.
- **API-Parität:**
  - Jede Aktion der Oberfläche gibt es als Befehl in der Schnittstelle.
  - Jeder Objekttyp der Oberfläche ist über die Schnittstelle lesbar, soweit die Rechte es erlauben.
  - Neue Funktionen kommen immer mit Endpunkt und Schema.
  - Eine Prüfung in der CI verlangt für jeden registrierten Befehl einen Endpunkt in der Beschreibung.
    Ausnahmen stehen in einer Liste, die nur kürzer werden darf (Ratchet wie in
    [Qualitätsgates](20260909-qualitaetsgates-ci.md)).

### Umzug der bestehenden Schnittstellen

| Schnittstelle | Weg |
|---|---|
| OParl-Aggregator, OParl je Mandant | Adressen und Kennungen bleiben; Teil der gemeinsamen Beschreibung |
| alte Session-API | entfällt zum Sunset (#543) |
| Session-API v1 | Lesen geht in OParl und kommParl auf, die Einreichung in das kommParl-Profil Einreichung (A6); danach Abkündigung |
| Fraktions-API | wird ein `mandari:`-Bereich; alte Pfade bis zum Ende der Frist |
| Website-API | wird ein öffentlicher `mandari:`-Bereich |
| Provisioning v1 | entfällt, nachdem die Zugriffe geprüft sind |
| Provisioning v2 | wird ein `mandari:`-Bereich mit Dienstzugang statt Betreiberschlüssel |
| DCAT-AP.de, iCalendar | bleiben als Formate |
| interne JSON-Endpunkte | ziehen um, sobald die Oberfläche ihres Bereichs umgestellt wird |

## Alternativen

- **Eigene Schnittstelle je Produkt und Zweck (heutiger Stand):** doppelte Rechteprüfungen, vier
  Zugangsarten, jede neue Anwendung braucht eigene Anbindung. Verworfen.
- **Eigenes Datenformat statt OParl:** bricht bestehende OParl-Anwendungen und widerspricht A7 und dem
  Ziel eines offenen Standards. Verworfen.
- **Single-Page-Anwendung, die nur die Schnittstelle nutzt:** Barrierefreiheit, Auffindbarkeit und
  Aufwand sprechen dagegen. Serverseitiges Rendering über dieselben Dienste bietet dieselbe Garantie, dass
  Oberfläche und Schnittstelle nicht auseinanderlaufen. Verworfen.
- **Oberfläche ruft die eigene HTTP-Schnittstelle auf:** kostet Netz- und Serialisierungsaufwand im selben
  Prozess, ohne etwas zu gewinnen (wie „immer HTTP“ in A6). Verworfen.
- **GraphQL:** OParl ist REST mit Verweisen. Zwei Abfragesprachen nebeneinander und eine Rechteprüfung
  je Feld wären der Preis. Verworfen.
- **Eigener Identitätsdienst wie Keycloak als Anbieter:** bedeutet einen weiteren Dienst in jeder
  kommunalen Installation und eine zweite Kontenverwaltung, obwohl mandari Konten und zweiten Faktor schon
  verwaltet. Für den Start verworfen. Weil Anwendungen nur den Standard sehen, lässt sich das später
  austauschen.

## Folgen

**Positiv**

- Je Vorgang gibt es nur eine Rechteprüfung. Oberfläche und Schnittstelle können nicht auseinanderlaufen.
- Neue Anwendungen bauen gegen einen dokumentierten Vertrag, ob App, Open-Data-Plattform, Partnerdienst
  oder Installation im Rechenzentrum.
- Bestehende OParl-Anwendungen funktionieren weiter. kommParl erhält eine vollständige Referenzumsetzung.
- Es gibt einen Login für alle Anwendungen, und jeder Zugang lässt sich einzeln widerrufen.

**Negativ**

- Jede neue Funktion braucht Schema und Endpunkt und kostet dadurch etwas mehr.
- Der Umzug bestehender Ansichten auf gemeinsame Dienste dauert und läuft bereichsweise.
- Ein OpenID-Connect-Anbieter ist sicherheitskritisch: Er braucht Härtung, Schlüsselwechsel und eine
  Prüfung vor dem Einschalten.
- kommParl ist ein Entwurf. mandari setzt Profile vor der Spezifikation um und muss Produktspezifisches
  konsequent unter `mandari:` halten.
- Bestehende Integrationen müssen innerhalb der Fristen umstellen.

## Umsetzung

Epic #1203 in Etappen:

1. Fundament: Einstieg, Beschreibung, gemeinsame Regeln (#1205)
2. OpenID-Connect-Anbieter und Scopes (#1206)
3. OParl-Ausgaben in die gemeinsame Beschreibung
4. Profile Nichtöffentliches (#565) und Einreichung
5. `mandari:`-Bereiche für Fraktions-API, Website und Bereitstellung
6. Oberfläche bereichsweise

Unter-Issues entstehen, wenn die Arbeit an einer Etappe beginnt.
