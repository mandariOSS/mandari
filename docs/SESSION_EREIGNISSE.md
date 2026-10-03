# Session meldet Ereignisse an die Datendrehscheibe

Implementierungsnotiz zu Etappe E4 der Datendrehscheibe (Epic #488): mandari Session meldet ihre
fachlichen Änderungen als Ereignisse `ris.*` im Journal der Ereignistechnik. Bürgerportal, Suche, Work
und der Änderungsfeed sollen daraus lesen statt aus Session-Tabellen und Signalketten.

Grundlagen: [Ereignistechnik](adr/20260929-ereignistechnik-postgres.md),
[Ereignisverträge](adr/20260929-ereignisvertraege.md),
[Kanonisches Modell](adr/20260929-kanonisches-modell.md),
[Körperschaften im Mandanten](adr/20261002-koerperschaften-im-mandanten.md).

## Umfang

| Stand | Bereich | Issue |
|---|---|---|
| umgesetzt | Sitzungen, Tagesordnung, Ladung | #533 |
| offen | Vorlagen, Beratungsfolge, Anlagen | #534 |
| offen | Abstimmung, Beschluss, Niederschrift, Rücknahme | #535 |

## Aufbau

- **`apps/session/hub_events.py`** (Session): Schalter und Erfassung. Eine Fachfunktion legt
  `hub_events.track(tenant)` um ihre Änderung und nennt die betroffenen Objekte, **bevor** sie sie ändert
  (`tracked.meeting(...)`, `tracked.agenda(...)`, `tracked.invited(...)`). Am Ende des Blocks liest
  `track` den Zustand erneut und schreibt die Ereignisse in derselben Transaktion.
- **`hub/ris/session_events.py`** (Drehscheibe): bildet aus Zustand vorher und nachher die Ereignisse und
  schreibt sie mit `publish()`. Die Verträge `ris.*` gehören der Drehscheibe (`x-owner: hub.ris`); dieselben
  Typen erzeugt der Ingestor für fremde RIS. Die Drehscheibe importiert Session nicht.
- **Keine Signale.** Ereignisse entstehen nur in Fachfunktionen. Änderungen im Django-Admin oder per
  Shell melden nichts.
- **Verschachtelung.** Ein `track` in einem anderen für denselben Mandanten schließt sich dem äußeren an;
  jedes Objekt wird einmal gemeldet (Sitzung anlegen mit Standard-TOPs: erst die Sitzung, dann die TOPs).

## Schalter

| Wert | Wirkung |
|---|---|
| `aus` (Standard) | nichts; keine zusätzliche Abfrage, keine zusätzliche Transaktion |
| `schatten` | Ereignisse werden geschrieben. Scheitert das Bilden oder Schreiben, bleibt die Änderung bestehen (eigener Sicherungspunkt); das Protokoll nennt Schritt und Fehlerklasse, keine Inhalte. Für den Parallelbetrieb neben den bisherigen Wegen |
| `aktiv` | Änderung und Ereignisse sind atomar: Scheitert ein Ereignis, bleibt auch die Änderung aus |

- Installation: `SESSION_EVENTS` (`.env`, Compose reicht es an Anwendung und Worker durch).
- Mandant: Admin → Session-Mandant → „Ereignisse an die Datendrehscheibe“ (leer = wie die Installation).
  So lässt sich ein einzelner Mandant im Schatten betreiben, bevor die Installation umschaltet.
- Die Ereignisse brauchen den Sequenzierer (Dienst `worker`). Ist `SESSION_EVENTS` nicht `aus`, meldet
  `/health/` ohne Worker `degraded`. Wer nur einzelne Mandanten einschaltet, setzt
  `EVENTS_WORKER_REQUIRED=true`.

## Ereignisse je Fachfunktion (#533)

| Fachfunktion | Ereignisse |
|---|---|
| Sitzung anlegen (Formular, Jahresplanung) | `ris.meeting.scheduled`, danach je Standard-TOP `ris.agendaitem.changed` (`added`) |
| Sitzung bearbeiten, absagen | `ris.meeting.changed` mit den geänderten Feldern (`name`, `start`, `end`, `meetingState`, `cancelled`, `organization`, `location`, `meetingFormat`), bei Absage `cancelled: true` |
| Öffentlichkeit der Sitzung ändern | siehe Sichtbarkeit; die Tagesordnung folgt mit |
| TOP anlegen, bearbeiten, Ö/NÖ wechseln, absetzen, löschen, verschieben, Reihenfolge | `ris.agendaitem.changed` (`added`, `changed`, `withdrawn`, `deleted`), Rücknahmen als `ris.object.depublished`; neu nummerierte Punkte mit `changed: [number, order]` |
| TOP aus der Beratungsfolge terminieren, Tagesordnungsvorlage anwenden | wie TOP anlegen; ggf. `ris.meeting.changed` (`meetingFormat`) |
| Sitzungscockpit: eröffnen, schließen | `ris.meeting.changed` (`meetingState`) |
| Ladung, Nachtrag, Vertretungsanfrage versenden | `ris.meeting.invited` (nur `nichtoeffentlich`, Versandart, Kennung des Versandvorgangs; keine Empfänger) mit dem Versandvorgang, vor dem Versand der Mails |
| Erstladung | zusätzlich `ris.meeting.changed` (`meetingState`): Mit der Ladung ist die Tagesordnung veröffentlicht |

Nicht gemeldet, weil das kanonische Modell es nicht kennt: interne Notizen, Zugangsweg der
Zugeschalteten, Einladungstext, tatsächliche Zeiten im Sitzungsverlauf, Unterpunkt-Zuordnung,
Geheimhaltungsmerkmal. Speichern ohne Änderung meldet nichts.

## Kennungen und Hülle

- `aggregate_id` und alle Kennungen der Nutzlast sind kanonisch: `uuid5` über die Adresse des Objekts in
  der Session-Schnittstelle auf der festgeschriebenen Basis (`SessionUris.canonical_id`). Es sind dieselben
  Kennungen, die der RIS-Bestand für das gespiegelte Objekt führt.
- Mandant `session:<uuid>`, `body_id` die kanonische Kennung des Body der Schnittstelle (bis #758 einer je
  Mandant). Damit nennt der Änderungsfeed der Session-Schnittstelle und des Aggregators die Ereignisse.
- Auslöser ist das angemeldete Konto (`user:<uuid>`), nie ein Name.
- Ausnahme: `dispatch` in `ris.meeting.invited` ist die Kennung des Versandvorgangs in Session (kein
  Objekt des kanonischen Modells).

## Sichtbarkeit

- `oeffentlich` nur, was die Session-Schnittstelle nach der Änderung ausliefert, und nur mit deren
  Feldern. Von einer nichtöffentlichen Sitzung mit veröffentlichtem Termin sind das Name, Zeit, Status,
  Absage und Gremien. Ändert sich nur, was die Öffentlichkeit nicht sieht (etwa der Ort einer solchen
  Sitzung), geht das Ereignis nur an `nichtoeffentlich`; der Feed bleibt unverändert.
- Wird ein Objekt öffentlich, erscheint es öffentlichen Empfängern als neu (`ris.meeting.scheduled`,
  `added`).
- Endet die Veröffentlichung, meldet `ris.object.depublished` (Operation `delete`) die Rücknahme mit
  Grund `nichtoeffentlich`; mit der Sitzung auch ihr Ort und ihre öffentlichen TOPs. Die Änderung selbst
  geht als `nichtoeffentlich` an die übrigen Empfänger.
- Gelöscht: Öffentliches als `ris.object.depublished` (`quelle_geloescht`), Nichtöffentliches als
  `ris.agendaitem.changed` (`deleted`, nur `nichtoeffentlich`) – wie beim Ingestor (`hub.ris.retraction`).
- Ein öffentliches Ereignis nennt keine Kennung eines nichtöffentlichen Objekts, etwa die Vorlage eines
  TOP nur, solange sie veröffentlicht ist.

## Übergang

Bis der RIS-Projektor den Spiegel ablöst (#488), gleicht der Ingestor die Session-Schnittstelle weiter ab
und meldet mit `INGESTOR_EVENTS_ENABLED` dieselben Objekte unter dem Mandanten `source:<uuid>`. Der
Änderungsfeed nennt ein Objekt dann zweimal (sofort aus Session, später aus dem Abgleich). Das ist für
Abnehmer unschädlich: Einträge sind Hinweise zum Nachlesen, `upsert` und `delete` sind idempotent.
Wer das vermeiden will, nimmt die Quelle des Mandanten mit `sync_config["events_enabled"] = false` von
den Ereignissen des Ingestors aus, sobald Session `aktiv` meldet.

## Reihenfolge

Innerhalb einer Änderung: Sitzungen, dann Tagesordnungspunkte in der Reihenfolge der Tagesordnung, dann
ausdrücklich gemeldete Vorgänge (Ladung). Der Sequenzierer übernimmt die Reihenfolge des Schreibens.

## Tests

- `apps/session/tests/test_drehscheibe_sitzungen.py`: je Fachfunktion, Schalter, Sichtbarkeit, Reihenfolge,
  Schattenbetrieb und Atomarität, Ankunft im Änderungsfeed der Session-Schnittstelle.
- `hub/ris/tests/test_session_events.py`: Übergänge der Sichtbarkeit ohne Datenbank.
- Jedes Ereignis prüft `publish()` in Tests gegen seinen Vertrag (`EVENTS_VALIDATE_CONTRACTS`).
