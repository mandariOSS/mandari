# API-first ohne Ausnahme: Jede Datenänderung ist ein Befehl, die Oberfläche ein Client

- Status: angenommen
- Datum: 2026-11-28
- Issue: #1588 (Teil von #489), Datendrehscheibe #476
- Paket: Drehscheibe und Plattform
- Erweitert: [Eine Schnittstelle: API-first](20261105-eine-schnittstelle-api-first.md)
- Hängt ab von: [A1 Schichtenmodell](20260929-schichtenmodell.md),
  [A5 Verträge](20260929-ereignisvertraege.md),
  [A6 Befehle synchron](20260929-befehle-synchron.md),
  [Rechte mit Geltungsbereich](20261003-rechte-mit-geltungsbereich.md),
  [Installationsprofile](20261128-installationsprofile.md)

## Kontext

Die Entscheidung vom 05.11.2026 macht mandari API-first: eine Schnittstelle, ein Vertrag, die Oberfläche nutzt
dieselben Dienste. Für das Lesen gilt das weitgehend, für das Schreiben nicht. Heute ändern die Views von Session und
Work ihre Daten an rund 200 Stellen direkt über das ORM (Kennzahl K9 in `docs/DATENDREHSCHEIBE.md`). Nur vier Vorgänge
laufen als Befehl nach A6: Einreichen, Zurücknehmen, Zu- oder Absage und Empfangsbestätigung.

Solange die Oberfläche selbst schreibt, gibt es zwei Wege mit zwei Prüfungen. Eine Funktion der Oberfläche fehlt in der
Schnittstelle, oder sie prüft dort anders. Kommunen, Partner und eigene Produkte können keine eigene Oberfläche auf
derselben Schnittstelle bauen, und ein Modul lässt sich nicht als eigener Dienst betreiben.

## Entscheidung

Die Projektleitung hat am 28.11.2026 entschieden: **mandari wird API-first ohne Ausnahme.**

1. **Jede Datenänderung ist ein Befehl beim Eigentümer.** Der Befehl hat einen Vertrag in `hub/contracts` (JSON Schema,
   A5) und einen Handler im Paket, das der Vertrag als `x-owner` nennt (A6). Der Handler prüft Mandant, Person und
   Rechte mit dem Rechtemodell seines Moduls, schreibt in der Transaktion des Befehls, hält die Änderung im
   Prüfprotokoll fest und veröffentlicht Ereignisse im selben Commit. Er liefert eine Quittung oder einen Fehler nach
   RFC 9457.
2. **Die eigene Oberfläche ruft dieselben Befehle wie jeder andere Client.** Eine View liest nur ihr Formular und ruft
   den Befehl in einer Zeile auf (`ausfuehren(request, name, inhalt)`). Sie schreibt nicht über das ORM. Im selben
   Prozess geht der Aufruf direkt an den Dispatcher (wie A6, kein HTTP im eigenen Prozess). JavaScript der Oberfläche
   nutzt die HTTP-Operation mit der Browser-Sitzung und CSRF-Token. Beide Wege laufen durch denselben Handler.
3. **Jeder freigegebene Befehl ist ohne weiteren Code eine Operation der gemeinsamen Schnittstelle:**
   `POST /api/mandari/v1/<bereich>/<mandant>/befehle/<befehl>/v<version>`, mit `Idempotency-Key`, Access-Token im
   Namen einer Person (Scope je Modul, für Session `sitzung:schreiben`) oder Browser-Sitzung, und mit Eintrag in
   `docs/schnittstelle/openapi.json`. Der Inhalt ist das Schema des Vertrags, die Antwort die Quittung.
4. **Serverseitiges Rendern bleibt erlaubt und ist der Normalfall** (Barrierefreiheit, Auffindbarkeit, wenig
   JavaScript), wie in der Entscheidung vom 05.11.2026. Die Oberfläche ist nur nicht mehr die einzige, die schreiben
   kann.
5. **Ein Scope öffnet nur den Weg.** Rechte prüft immer der Handler, so wie für die Oberfläche. Ein Token im Namen einer
   Person darf nie mehr als die Person. Dienste ohne Person schreiben nicht über diesen Weg. Ein Modul darf den Zugang
   über Anwendungen an ein eigenes Recht knüpfen (Session: „API-Zugang“), damit der Mandant entscheidet, ob seine Leute
   über Anwendungen arbeiten.
6. **Die CI setzt das durch.** Ein Ratchet zählt die ORM-Schreibzugriffe in Views je Datei; der Bestand darf nur
   sinken (`scripts/check_view_writes_ratchet.py`, Kennzahl K9). Die Befehlsparität im Job „Schnittstelle“ verlangt
   für jeden Befehl eine Operation; die Liste der Ausnahmen ist leer und darf nicht wachsen.

### Kriterien für eigene Dienste

Es gelten die Kriterien D1 bis D6 aus [Installationsprofile](20261128-installationsprofile.md), Abschnitt 4: Ein Teil
wird ein eigener Dienst, wenn alle erfüllt sind, insbesondere ein belegter Nutzen (D5). Diese Entscheidung schafft die
Voraussetzung für D3 und D4: Ändert jede Funktion ihre Daten nur über Befehle beim Eigentümer, nimmt ein abgetrennter
Dienst dieselben Befehle über den `HttpClient` aus A6 an. An Verträgen, Handlern und Oberfläche ändert sich dabei
nichts. Messbar ist das an den Kennzahlen K1, K5, K7 und K9 für den Teil, die alle bei null stehen müssen. Ein eigener
Dienst nur der Ordnung halber kostet Betrieb, Latenz und Fehlerfälle und wird nicht gebaut.

### Verhältnis zu bestehenden Entscheidungen

- **A6 (Befehle synchron)** bleibt die Technik: Dispatcher, Quittung, Idempotenz, RFC 9457, In-Process- und
  HTTP-Client. Neu ist der Umfang. A6 galt für Befehle zwischen Modulen, jetzt gilt es für jede Änderung, auch innerhalb
  eines Moduls aus seiner eigenen Oberfläche. Den Weg `POST …/<befehl>/v<version>` zwischen Installationen ergänzt die
  gemeinsame Schnittstelle um Adressen je Mandantenbereich. Beide rufen denselben Dispatcher.
- **„Eine Schnittstelle“ (05.11.2026)** bleibt in Kraft. Ihr Abschnitt „Die Oberfläche nutzt dieselben Dienste“ wird
  verschärft: Geschrieben wird nur über Befehle, nicht über beliebige Dienste. Die API-Parität („jede Aktion der
  Oberfläche gibt es als Befehl“) wird mit dieser Entscheidung durchgesetzt statt angestrebt. Die Alternative
  „Oberfläche ruft die eigene HTTP-Schnittstelle auf“ bleibt verworfen; die Oberfläche ruft den Dispatcher im Prozess.
- **A5 (Verträge)** gilt unverändert. Befehle der internen Bereiche (`session.*`, `work.*`) haben den Eigentümer ihres
  Moduls und sind nie `oeffentlich`. Ihr Vertrag nennt je Feld einen `title`; die Oberfläche zeigt ihn in Meldungen.
- **Lesen** bleibt bei den Abfragen des Eigentümers (Selektoren, OParl-Ausgaben). Eine eigene Abfrage-Schicht mit
  Listen, Filtern und Seiten folgt in einem eigenen Schritt (#1588).

## Alternativen

- **Views schreiben weiter, die Schnittstelle bekommt eigene Endpunkte je Funktion.** Zwei Wege mit zwei Prüfungen,
  das heutige Problem. Verworfen.
- **Eine Single-Page-Anwendung als einzige Oberfläche.** Barrierefreiheit, Auffindbarkeit und Aufwand sprechen dagegen,
  wie am 05.11.2026. Verworfen.
- **Pydantic-Modelle als Eingabe-Schema statt JSON Schema in `hub/contracts`.** Zwei Quellen für denselben Vertrag
  liefen auseinander; die Verträge prüft die CI schon heute auf additive Änderungen und erzeugt daraus Katalog und
  Beschreibung. Das Schema des Vertrags ist deshalb auch der Inhalt der HTTP-Operation. Verworfen.
- **Die Oberfläche ruft die HTTP-Operation im eigenen Prozess.** Netz- und Serialisierungsaufwand ohne Gewinn
  (A6, „Immer HTTP“). Verworfen.

## Folgen

**Positiv**

- Eine Prüfung je Vorgang. Oberfläche und Schnittstelle können nicht auseinanderlaufen.
- Jede Funktion der Oberfläche steht ohne Zusatzaufwand in der Schnittstelle, mit Idempotenz, Quittung, Prüfprotokoll
  und OpenAPI.
- Doppelt abgeschickte Formulare wirken nur einmal (Idempotenzschlüssel im Formular).
- Module lassen sich später als eigene Dienste betreiben, wenn die Kriterien oben zutreffen.

**Negativ**

- Jede schreibende Funktion braucht einen Vertrag und eine Fachfunktion; die erste Umstellung einer View kostet Zeit.
- Rund 200 Schreibstellen in Views ziehen bereichsweise um. Bis dahin gelten zwei Wege, der Ratchet verhindert neue.
- Befehle laufen in einer eigenen Transaktion. Mehrere Änderungen, die gemeinsam gelingen müssen, sind ein Befehl,
  nicht mehrere.

## Umsetzung

- **Fundament (#1588):** `hub.commands` (Freigabe `Exposure`, Mandantenbereiche, `ausfuehren` und `Ergebnis`),
  `hub.api.befehle` (Operation, Anmeldung, Beschreibung), `apps.session.befehle` (Helfer `@befehl` mit Rechten nach dem
  Rechtemodell von Session, `Kontext`), Scope `sitzung:schreiben`, Ratchet K9, Beispiel Textbausteine. Anleitung:
  [DATENDREHSCHEIBE.md, Abschnitt 6](../DATENDREHSCHEIBE.md#6-einen-befehl-anlegen).
- **Session und Work bereichsweise:** Folge-Issues mit der Liste der Schreibstellen je Bereich. Work bekommt zuerst
  denselben Helfer (`apps/work/befehle`, Rechte über die Mitgliedschaft, eigener Scope).
- **Fertig**, wenn K9 bei null steht und K1, K5 und K7 bei null sind.

## Prüfung (Fitnessfunktion)

- `scripts/check_view_writes_ratchet.py` im Job „Qualität“: ORM-Schreibzugriffe in Views dürfen je Datei nur sinken.
- `scripts/check_schnittstelle.py` im Job „Schnittstelle“: Jeder Befehl im Register hat eine Operation.
- `hub/api/tests/test_befehle.py`: Für jeden freigegebenen Befehl gibt es die Operation mit `x-mandari-command`, und
  derselbe Befehl aus Oberfläche und Schnittstelle erreicht denselben Handler.
