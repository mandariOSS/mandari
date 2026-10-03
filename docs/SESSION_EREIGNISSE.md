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
| umgesetzt | Vorlagen, Beratungsfolge, Anlagen | #534 |
| offen | Abstimmung, Beschluss, Niederschrift, Rücknahme | #535 |

## Aufbau

- **`apps/session/hub_events.py`** (Session): Schalter und Erfassung. Eine Fachfunktion legt
  `hub_events.track(tenant)` um ihre Änderung und nennt die betroffenen Objekte, **bevor** sie sie ändert
  (`tracked.meeting(...)`, `tracked.agenda(...)`, `tracked.paper(...)`, `tracked.file(...)`,
  `tracked.invited(...)`). Mit einer Sitzung beobachtet `track` auch ihre Anlagen und die Beratungen in ihr, mit
  einer Tagesordnung die Anlagen und Beratungen ihrer Punkte, mit einer Vorlage ihre Beratungsfolge, Anlagen und
  die TOPs, auf denen sie steht – deren Sichtbarkeit hängt daran. Am Ende des Blocks liest
  `track` den Zustand erneut und schreibt die Ereignisse in derselben Transaktion.
- **`hub/ris/session_events.py`** (Drehscheibe): bildet aus Zustand vorher und nachher die Ereignisse und
  schreibt sie mit `publish()`. Die Verträge `ris.*` gehören der Drehscheibe (`x-owner: hub.ris`); dieselben
  Typen erzeugt der Ingestor für fremde RIS. Die Drehscheibe importiert Session nicht.
- **Keine Signale.** Ereignisse entstehen nur in Fachfunktionen. Änderungen im Django-Admin oder per
  Shell melden nichts.
- **Verschachtelung.** Ein `track` in einem anderen für denselben Mandanten schließt sich dem äußeren an;
  jedes Objekt wird einmal gemeldet (Sitzung anlegen mit Standard-TOPs: erst die Sitzung, dann die TOPs).
  Die Erfassung endet noch in ihrer Transaktion. Ein Rückruf nach dem Commit (`transaction.on_commit`), der
  selbst `track` aufruft, öffnet deshalb eine eigene Erfassung mit eigener Transaktion.
- **Gleichzeitige Änderungen.** `track` liest den Zustand davor ohne Sperre (außer wo die Fachfunktion selbst
  sperrt, etwa Sitzungscockpit und Erfassung). Ändern zwei Anfragen dasselbe Objekt gleichzeitig, können beide
  dieselbe Änderung melden. Verloren geht dabei keine. „Genau ein Ereignis je Änderung“ gilt also nur ohne
  Gleichzeitigkeit; Abnehmer verarbeiten Ereignisse ohnehin idempotent (Ereignistechnik, ADR).

## Schalter

| Wert | Wirkung |
|---|---|
| `aus` (Standard) | nichts; keine zusätzliche Abfrage, keine zusätzliche Transaktion |
| `schatten` | Ereignisse werden geschrieben und erreichen wie im aktiven Betrieb den Änderungsfeed und alle Abonnenten; „Schatten“ heißt nicht „unsichtbar“. Anders ist nur der Fehlerfall: Scheitert das Bilden oder Schreiben, bleibt die Änderung bestehen (eigener Sicherungspunkt); das Protokoll nennt Schritt, Fehlerklasse und Aufrufstellen, keine Inhalte. Für den Parallelbetrieb neben den bisherigen Wegen |
| `aktiv` | Änderung und Ereignisse sind atomar: Scheitert ein Ereignis, bleibt auch die Änderung aus |

- Installation: `SESSION_EVENTS` (`.env`, Compose reicht es an Anwendung und Worker durch).
- Mandant: Admin → Session-Mandant → „Ereignisse an die Datendrehscheibe“ (leer = wie die Installation).
  So lässt sich ein einzelner Mandant im Schatten betreiben, bevor die Installation umschaltet.
- Die Ereignisse brauchen den Sequenzierer (Dienst `worker`). Ist `SESSION_EVENTS` nicht `aus`, meldet
  `/health/` ohne Worker `degraded`. Wer nur einzelne Mandanten einschaltet, setzt
  `EVENTS_WORKER_REQUIRED=true`; der Admin weist beim Speichern eines solchen Mandanten darauf hin.

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

## Ereignisse je Fachfunktion (#534)

| Fachfunktion | Ereignisse |
|---|---|
| Vorlage anlegen (Formular, Unternummer, aus einem Antrag) | `ris.paper.created` (nur `nichtoeffentlich`; aus einem Antrag mit `submission`, der Kennung der Einreichung) |
| Freigabelauf: vorlegen, zurückweisen, Mitzeichnung zurückgewiesen | `ris.paper.changed` (`status`, nur `nichtoeffentlich`) |
| Freigeben (öffentliche Vorlage) | `ris.paper.released` (öffentlich, ohne Einreichung), dazu ihre öffentlichen Anlagen und Stationen als `added` |
| Vorlage bearbeiten, Fassung wiederherstellen | `ris.paper.changed`: öffentlich die Felder der Schnittstelle (`name`, `reference`, `date`, `paperType`, `originatorPerson`, `originatorOrganization`, `underDirectionOf`), intern `status`, `public`, `mainText`, `resolutionText` |
| Zurückziehen | `ris.paper.changed` (`status`, intern; die Vorlage bleibt veröffentlicht) |
| Zurück in den Entwurf, nichtöffentlich stellen | `ris.object.depublished` für Vorlage, Stationen und Anlagen (`zurueckgenommen` bzw. `nichtoeffentlich`), die Änderung intern |
| Station anlegen, ändern, verschieben, entfernen | `ris.consultation.changed` (`added`, `changed` mit `organization`, `meeting`, `agendaItem`, `role`, `authoritative`, `order`; `result` nur intern), entfernte öffentliche Station als `ris.object.depublished` |
| Station terminieren, weiterleiten | `ris.paper.changed` (`status`, intern), `ris.agendaitem.changed` (`added`), `ris.consultation.changed` (`scheduled` mit dem TOP) |
| Anlage hochladen, umbenennen, ersetzen | `ris.file.changed` (`added`, `renamed`, `replaced`) mit Vorlage, Sitzung bzw. TOP; öffentlich nur an veröffentlichten Objekten |
| Anlage nichtöffentlich stellen, löschen | `ris.object.depublished` (öffentliche Anlage) bzw. `ris.file.changed` (`removed`, `nichtoeffentlich`, Operation `delete`) |

Die öffentliche Fassung der Niederschrift ist keine Anlage in diesem Sinn; sie meldet #535.

Nicht gemeldet, weil das kanonische Modell es nicht kennt: interne Notizen, Zugangsweg der
Zugeschalteten, Einladungstext, tatsächliche Zeiten im Sitzungsverlauf, Unterpunkt-Zuordnung,
Geheimhaltungsmerkmal, Frist, federführendes Amt, finanzielle Auswirkungen, Mitzeichnungen. Speichern ohne Änderung
meldet nichts, ebenso eine ersetzte Anlage mit gleichem Inhalt.

## Kennungen und Hülle

- `aggregate_id` und alle Kennungen der Nutzlast sind kanonisch: `uuid5` über die Adresse des Objekts in
  der Session-Schnittstelle auf der festgeschriebenen Basis (`SessionUris.canonical_id`). Es sind dieselben
  Kennungen, die der RIS-Bestand für das gespiegelte Objekt führt.
- Mandant `session:<uuid>`, `body_id` die kanonische Kennung des Body der Schnittstelle (bis #758 einer je
  Mandant). Damit nennt der Änderungsfeed der Session-Schnittstelle und des Aggregators die Ereignisse.
- Ausnahme: `submission` in `ris.paper.created` ist die Kennung der Einreichung in Session (wie in `submission.*`).
- Auslöser ist das angemeldete Konto (`user:<uuid>`), nie ein Name.
- Ausnahme: `dispatch` in `ris.meeting.invited` ist die Kennung des Versandvorgangs in Session (kein
  Objekt des kanonischen Modells).

## Sichtbarkeit

- `oeffentlich` nur, was die Session-Schnittstelle nach der Änderung ausliefert, und nur mit deren
  Feldern. Von einer nichtöffentlichen Sitzung mit veröffentlichtem Termin sind das Name, Zeit, Status,
  Absage und Gremien. Ändert sich nur, was die Öffentlichkeit nicht sieht (etwa der Ort einer solchen
  Sitzung), geht das Ereignis nur an `nichtoeffentlich`; der Feed bleibt unverändert.
- Vor der Freischaltung der Schnittstelle (`SessionTenant.oparl_public_since`, #319) und bei deaktiviertem
  Mandanten liefert sie nichts aus: Alle Ereignisse sind dann `nichtoeffentlich`, auch die zu öffentlichen
  Sitzungen (Einführung, Testdaten, Schulung, Umstieg).
- Wird ein Objekt öffentlich, erscheint es öffentlichen Empfängern als neu (`ris.meeting.scheduled`,
  `added`).
- Endet die Veröffentlichung, meldet `ris.object.depublished` (Operation `delete`) die Rücknahme mit
  Grund `nichtoeffentlich`; mit der Sitzung auch ihr Ort und ihre öffentlichen TOPs. Die Änderung selbst
  geht als `nichtoeffentlich` an die übrigen Empfänger.
- Gelöscht: Öffentliches als `ris.object.depublished` (`quelle_geloescht`), Nichtöffentliches als
  `ris.agendaitem.changed` (`deleted`, nur `nichtoeffentlich`) – wie beim Ingestor (`hub.ris.retraction`).
- Ein öffentliches Ereignis nennt keine Kennung eines nichtöffentlichen Objekts, etwa die Vorlage eines
  TOP nur, solange sie veröffentlicht ist, und von einer Station in einer nichtöffentlichen Sitzung weder
  Gremium noch Sitzung (wie die Schnittstelle: öffentlich bleibt nur, dass die Vorlage dort beraten wird).
- Eine Vorlage ist veröffentlicht, wenn sie öffentlich und freigegeben ist; ihre Stationen sind es mit ihr,
  ihre Anlagen, wenn sie selbst öffentlich sind.

## Freischaltung der Schnittstelle

Die Freischaltung selbst meldet nichts. Öffentliche Abnehmer steigen danach über den Snapshot der
Session-Schnittstelle ein (`…/api/oparl/body/snapshot/`, mit Cursor für den Feed). Ereignisse von vorher sind
`nichtoeffentlich` und erscheinen im öffentlichen Feed nicht. Auch die Rücknahme der Freischaltung meldet
nichts; die Schnittstelle samt Feed antwortet dann wieder mit 404 (möglich nur, solange der Mandant nicht im
Bürgerportal veröffentlicht).

## Übergang

Bis der RIS-Projektor den Spiegel ablöst (#488), gleicht der Ingestor die Session-Schnittstelle weiter ab
und meldet mit `INGESTOR_EVENTS_ENABLED` dieselben Objekte unter dem Mandanten `source:<uuid>`. Der
Änderungsfeed nennt ein Objekt dann zweimal (sofort aus Session, später aus dem Abgleich). Das ist für
Abnehmer unschädlich: Einträge sind Hinweise zum Nachlesen, `upsert` und `delete` sind idempotent.
Wer das vermeiden will, nimmt die Quelle des Mandanten mit `sync_config["events_enabled"] = false` von
den Ereignissen des Ingestors aus, sobald Session `aktiv` meldet.

## Reihenfolge

Innerhalb einer Änderung: Sitzungen, Vorlagen, Tagesordnungspunkte in der Reihenfolge der Tagesordnung,
Beratungen je Vorlage in der Reihenfolge der Stationen, Anlagen, dann ausdrücklich gemeldete Vorgänge
(Ladung). Der Sequenzierer übernimmt die Reihenfolge des Schreibens.

## Tests

- `apps/session/tests/test_drehscheibe_sitzungen.py`: je Fachfunktion, Schalter, Sichtbarkeit, Reihenfolge,
  Schattenbetrieb und Atomarität, Ankunft im Änderungsfeed der Session-Schnittstelle.
- `apps/session/tests/test_drehscheibe_vorlagen.py`: Vorlagen, Freigabelauf, Rücknahme samt Stationen und
  Anlagen, Antrag, Beratungsfolge, Terminieren, Anlagen.
- `hub/ris/tests/test_session_events.py`: Übergänge der Sichtbarkeit ohne Datenbank.
- Jedes Ereignis prüft `publish()` in Tests gegen seinen Vertrag (`EVENTS_VALIDATE_CONTRACTS`).
