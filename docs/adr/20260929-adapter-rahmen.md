# Adapter-Rahmen: übersetzen ohne Geschäftsregeln, Zustellprotokoll, dünne Webhooks mit HMAC

- Status: angenommen
- Datum: 2026-09-29
- Issue: #476
- Paket: Datendrehscheibe, A9
- Hängt ab von: [A2 Ereignistechnik](20260929-ereignistechnik-postgres.md),
  [A5 Verträge](20260929-ereignisvertraege.md), [A7 Kanonisches Modell](20260929-kanonisches-modell.md)

## Kontext

Kommunen erwarten Anbindungen an DMS und E-Akte, Finanzverfahren für Sitzungsgeld,
Workflow-Plattformen, Transparenz- und Open-Data-Portale sowie die Übernahme aus bestehenden
Ratsinformationssystemen. In der Branche entstehen solche Anbindungen meist Punkt zu Punkt, je
Zielsystem und je Produkt einzeln.

Eingehend gibt es bei mandari heute mehrere Einstiege in die Synchronisation (Dienst,
Management-Befehle, ein Thread aus dem Admin). Aggregatoren anderer Länder zeigen zwei typische
Fehler: Eine nicht erreichbare Quelle wird als „alles gelöscht“ gedeutet, und reine
Push-Mechanismen verlieren Zustellungen ohne Nachholweg.

## Entscheidung

**Grundsatz:** Ein Adapter übersetzt zwischen dem kanonischen Modell und einem Fremdsystem, ohne
Geschäftsregeln (Anti-Corruption Layer). Freigaben, Fristen und Prüfungen bleiben beim Eigentümer
der Daten.

**Eingang (Ingestor)**

- Je Batch `fetch → normalize (kanonisch) → upsert + publish` in einer Transaktion. Fremd-RIS
  erzeugen dieselben `ris.*`-Ereignisse wie Session.
- Ein Einstieg: der Ingestor-Dienst. Der Admin stößt eine Synchronisation per Auftrag an und
  bekommt eine Rückmeldung; weitere Einstiege entfallen.
- **„Nicht erreichbar ist nicht gelöscht.“** Löschungen entstehen nur aus einem Löschsignal der
  Quelle oder aus einem vollständig gelungenen Vollabgleich. Fehler, Zeitüberschreitungen, leere
  Antworten und Zugriffssperren führen zu einer Rückstandsmeldung, nie zu Löschungen.
- Ein regelmäßiger Vollabgleich ergänzt die Abfrage nach Änderungszeit, weil Zeitstempel-Abfragen
  Änderungen verlieren können. Die Aktualität je Quelle ist im Admin und in der offenen
  Schnittstelle sichtbar. Neu registrierte Quellen starten inaktiv.
- Für Quellen ohne OParl gelten die Regeln aus
  [20260917-kein-scraping-sternberg-regisafe-komuna](20260917-kein-scraping-sternberg-regisafe-komuna.md).

**Ausgang**

- Basisklasse `hub.adapters.OutboundAdapter` mit `types` (abonnierte Ereignismuster),
  `transform(event) -> Nachricht`, `deliver(nachricht)` und `idempotency_key`.
- Jeder Adapter ist ein Abonnement `adapter.<name>` ([A2](20260929-ereignistechnik-postgres.md)):
  Wiederholung, Parken, tote Ereignisse und Nachspielen sind damit eingebaut. Die Zustellung läuft
  in der Warteschlange `adapter`.
- Inhalte liest der Adapter über die Fassade der Drehscheibe mit Sichtbarkeitsprüfung.
- Konfiguration und Zugangsdaten je Mandant liegen verschlüsselt (Plattform-Verschlüsselung).
- Das Zustellprotokoll `hub_delivery` hält Status, Versuche und Antwortcode fest, keine Inhalte.

**Webhooks**

- Nach der Spezifikation „Standard Webhooks“: HMAC-Signatur mit Zeitstempel, `webhook-id` gleich
  `event_id`, Wiederholung mit wachsender Wartezeit bis 14 Tage, Rotation der Geheimnisse.
- **Dünne Nutzlast:** nur Typ, Kennungen und Feed-Cursor. Der Empfänger holt Inhalte mit eigener
  Berechtigung über Feed oder API. Der Änderungsfeed ([A10](20260929-aenderungsfeed-format.md)) ist
  die Wahrheit; ein Webhook ist nur ein Weckruf.

**Erste Ausgangsadapter** (Reihenfolge offen): DMS/E-Akte über xdomea (Anbietung nur mit
Metadaten), Transparenz- und Open-Data-Portale über DCAT-AP.de, Finanzverfahren für Sitzungsgeld.

## Alternativen

- **Punkt-zu-Punkt-Anbindungen in den Fachmodulen.** Der Aufwand wächst mit dem Produkt aus
  Zielsystemen und Modulen, Fachcode vermischt sich mit Fremdformaten. Verworfen.
- **Integrationsplattform oder Service-Bus** als zusätzlicher Dienst. Mehr Betrieb in jeder
  Installation, Übersetzungslogik außerhalb unserer Tests. Verworfen; Betreiber können eine
  vorhandene Plattform an Feed und Webhooks anschließen.
- **Webhooks mit vollständigen Inhalten.** Datenschutz, Sichtbarkeit zum Sendezeitpunkt statt zum
  Abrufzeitpunkt, Wiederholungen verbreiten zurückgenommene Inhalte weiter. Verworfen.
- **Webhooks als einziger Synchronisationsweg.** Verlorene Zustellungen lassen sich nicht
  nachholen. Verworfen.
- **Adapter mit eigenen Regeln.** Verdoppelt Fachlogik und macht Adapter schwer austauschbar.
  Verworfen.
- **Löschung aus fehlenden Objekten ableiten.** Bei Ausfällen entstehen Löschungen für ganze
  Kommunen. Verworfen.

## Folgen

**Positiv**

- Ein Rahmen für alle Partner; der Aufwand wächst linear mit der Zahl der Zielsysteme.
- Nach Störungen lassen sich Zustellungen nachspielen; jeder Versuch ist protokolliert.
- Dünne Nutzlasten halten Inhalte und Rechteprüfung beim Eigentümer.
- Quellenausfälle führen nicht zu Datenverlust im RIS-Bestand.

**Negativ**

- Webhook-Empfänger müssen Inhalte nachladen; das kostet einen zweiten Aufruf.
- Jeder Ausgangsadapter braucht eine Test- oder Partnerumgebung.
- Dauerhaft nicht erreichbare Webhook-Ziele erzeugen bis zu 14 Tage Wiederholungen und werden
  danach deaktiviert.
- Vollabgleiche erzeugen Last bei den Quellen und müssen gedrosselt werden.

## Prüfung (Fitnessfunktion)

- Test: Quelle antwortet mit Fehler, leer, gesperrt oder zeitüberschreitend; es entstehen keine
  `delete`-Ereignisse, die Quelle wird als rückständig gemeldet.
- Vertragstest je Ausgangsadapter: `transform` verarbeitet alle Schema-Beispiele der abonnierten
  Typen; Nachspielen erzeugt dieselben Idempotenzschlüssel.
- import-linter: `hub.adapters` importiert keine Fachmodule.
- Webhook-Signaturen werden gegen die Referenzbeispiele von Standard Webhooks getestet; das Schema
  der Webhook-Nutzlast erlaubt nur Kennungen und Cursor.
- Monitoring: Zustellfehler und Rückstand je Adapter, Aktualität je Quelle.

## Nachtrag zur Umsetzung (#556)

- **Löschsignale** sind OParl `deleted: true` (Ingestor) und `404`/`410` einer Dokumentadresse
  (Löschabgleich der Dokumente in Django, per GET bestätigt, vor dem Löschen erneut geprüft). Der
  OParl-Abgleich schließt nie aus dem Fehlen eines Objekts.
- **Vollständig gelungener Vollabgleich** gilt nur für Scraper-Quellen ohne Löschsignal. Der Adapter
  hält je Objekttyp fest, ob er die Liste ganz gelesen hat. Nicht lesbare Seiten (Netzfehler,
  Zeitüberschreitung, 4xx, 5xx), Seiten ohne den Aufbau der Quelle (Sperr-, Prüf- oder Hinweisseite mit
  Status 200), ein erreichtes Detailseiten-Budget und nicht lesbare Kalendermonate machen den Typ für
  diesen Lauf unvollständig: Seine Zähler bleiben stehen, nichts wird markiert. Nennt eine Liste ein
  Objekt, dessen Detailseite nicht lesbar ist, gilt es als gesehen.
- **Aufbau der Quelle** heißt bei SessionNet: das Layout der Instanz (Klassen `smc…`), nicht der
  Produktname – Prüfseiten übernehmen die angefragte Adresse samt Installationspfad. Eine Liste muss
  zudem ihren eigenen Aufbau tragen (Kalender: die Tageszeilen des Monats, auch ohne Sitzung).
- **Bremse:** Fehlen in einem vollständigen Lauf mehr Objekte eines Typs als
  `SCRAPER_TOMBSTONE_MAX_MISSING` (Vorgabe 10), zählt der Lauf keines davon (`last_run.tombstone_braked`)
  – dieselbe Regel wie `FILE_RECONCILE_MAX_MISSING` beim Löschabgleich der Dokumente.
- **Eine Regel für beide Abgleiche:** `mandari_oparl.abgleich` (shared) hält Löschstatus, Bremsstatus,
  die Erkennung von HTML- und Sperrseiten und die Bremse für Ingestor und Django. Der Löschabgleich der
  Dokumente bleibt der einzige Löschweg für Dokumente; der Ingestor bekommt keinen zweiten.
- **Teilantworten im OParl-Abgleich:** Eine Folgeseite ohne Liste (Fehlerobjekt, anderes JSON, leer)
  bricht die Liste wie ein HTTP-Fehler ab (`ListFetchError`); auf der ersten Seite heißt ein
  Fehlerobjekt weiter „diese Liste gibt es hier nicht“ (OParl 1.0).
- **Aktualität:** `OParlSource.last_successful_sync` bzw. `last_successful_full_sync` – letzter Abgleich
  ohne Lücke (jede Kommune lesbar, jede Liste ganz, kein Sperr- oder Störungsbefund; Texterkennung und
  Suchindex zählen nicht). Bei Scraper-Quellen zählen tote Verweise auf Detailseiten (404/410 der Quelle)
  und einzelne nicht auswertbare Detailseiten nicht als Lücke; gehäuft fallen sie über die Parse-Quote
  auf, die den Lauf dann als fehlerhaft wertet. `last_sync` zählt weiter jeden durchgelaufenen Abgleich; ein abgebrochener
  Scraper-Lauf setzt weder `last_sync` noch den Fehlerstatus der Quelle zurück. Admin: Spalte
  „Vollständig abgeglichen“ mit dem Hinweis „seither mit Lücken“; Änderungsfeed: Feld `freshness`
  ([A10, Nachtrag #556](20260929-aenderungsfeed-format.md)).

## Bezug

- [A2 Ereignistechnik](20260929-ereignistechnik-postgres.md), [A5 Verträge](20260929-ereignisvertraege.md),
  [A10 Feed-Format](20260929-aenderungsfeed-format.md)
- [20260917-kein-scraping-sternberg-regisafe-komuna](20260917-kein-scraping-sternberg-regisafe-komuna.md),
  `docs/SCRAPER_SOURCES.md`
- Epic #125 (Adapter für RIS ohne OParl), Epic #112 (mandari Data)
- Standard Webhooks: https://www.standardwebhooks.com/
- FIT-Connect, Callbacks: https://docs.fitko.de/fit-connect/docs/details/callbacks/
- xdomea, FAQ zum Standard: https://www.xoev.de/xdomea/faq-zum-standard-xdomea-21614
- DCAT-AP.de: https://www.dcat-ap.de/
- Microsoft, Anti-Corruption Layer:
  https://learn.microsoft.com/en-us/azure/architecture/patterns/anti-corruption-layer
