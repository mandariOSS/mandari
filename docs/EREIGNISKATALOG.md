<!-- Erzeugt aus dem Vertragsregister. Nicht von Hand ändern, sondern neu erzeugen:
     python scripts/check_event_contracts.py --write-catalog -->

# Ereigniskatalog

Alle Ereignisse und Befehle der Datendrehscheibe mit Version, Eigentümer, Sichtbarkeit, Feldern und
Beispielen. Quelle sind die Schemas unter [`mandari/hub/contracts/schemas/`](../mandari/hub/contracts/schemas/);
Regeln für Namen, Sichtbarkeit und Änderungen stehen in
[Verträge für Ereignisse und Befehle](adr/20260929-ereignisvertraege.md). Eine bestehende Version
ändert sich nur ergänzend (neue optionale Felder, neue Codes); alles andere ergibt eine neue Version.
Die CI prüft das und erzeugt diese Datei neu (`scripts/check_event_contracts.py`).

## Übersicht

| Typ | Art | Versionen | Eigentümer | Sichtbarkeit | Titel |
|---|---|---|---|---|---|
| `attendance.respond` | Befehl | 1 | `apps.session` | personenbezogen | Zu- oder Absage zur Sitzung |
| `attendance.response_recorded` | Ereignis | 1 | `apps.session` | personenbezogen | Zu- oder Absage erfasst |
| `core.membership.changed` | Ereignis | 1 | `apps.tenants` | personenbezogen | Mitgliedschaft geändert |
| `core.user.registered` | Ereignis | 1 | `apps.accounts` | personenbezogen | Konto angelegt oder bestätigt |
| `invitation.acknowledge` | Befehl | 1 | `apps.session` | personenbezogen | Empfang einer Ladung bestätigen |
| `ris.agendaitem.changed` | Ereignis | 1 | `hub.ris` | nichtoeffentlich, oeffentlich | Tagesordnung geändert |
| `ris.consultation.changed` | Ereignis | 1 | `hub.ris` | nichtoeffentlich, oeffentlich | Beratungsfolge geändert |
| `ris.file.changed` | Ereignis | 1 | `hub.ris` | nichtoeffentlich, oeffentlich | Anlage geändert |
| `ris.file.text_extracted` | Ereignis | 1 | `hub.ris` | intern | Text einer Anlage erkannt |
| `ris.meeting.changed` | Ereignis | 1 | `hub.ris` | nichtoeffentlich, oeffentlich | Sitzung geändert |
| `ris.meeting.invited` | Ereignis | 1 | `hub.ris` | nichtoeffentlich | Ladung versendet |
| `ris.meeting.scheduled` | Ereignis | 1 | `hub.ris` | nichtoeffentlich, oeffentlich | Sitzung angesetzt |
| `ris.object.depublished` | Ereignis | 1 | `hub.ris` | oeffentlich | Objekt zurückgenommen |
| `ris.organization.changed` | Ereignis | 1 | `hub.ris` | oeffentlich | Gremium geändert |
| `ris.paper.changed` | Ereignis | 1 | `hub.ris` | nichtoeffentlich, oeffentlich | Vorlage geändert |
| `ris.paper.created` | Ereignis | 1 | `hub.ris` | nichtoeffentlich | Vorlage angelegt |
| `ris.paper.released` | Ereignis | 1 | `hub.ris` | nichtoeffentlich, oeffentlich | Vorlage freigegeben |
| `ris.person.changed` | Ereignis | 1 | `hub.ris` | oeffentlich | Person geändert |
| `ris.protocol.approved` | Ereignis | 1 | `hub.ris` | nichtoeffentlich | Niederschrift genehmigt |
| `ris.protocol.published` | Ereignis | 1 | `hub.ris` | oeffentlich | Niederschrift veröffentlicht |
| `ris.resolution.adopted` | Ereignis | 1 | `hub.ris` | nichtoeffentlich, oeffentlich | Beschluss gefasst |
| `ris.resolution.implementation_changed` | Ereignis | 1 | `hub.ris` | oeffentlich | Umsetzungsstand geändert |
| `ris.source.published` | Ereignis | 1 | `hub.ris` | oeffentlich | Kommune im Bürgerportal veröffentlicht |
| `ris.voting.recorded` | Ereignis | 1 | `hub.ris` | nichtoeffentlich, oeffentlich | Abstimmung erfasst |
| `session.allowance.approved` | Ereignis | 1 | `apps.session` | personenbezogen | Sitzungsgeld genehmigt |
| `session.payment.exported` | Ereignis | 1 | `apps.session` | personenbezogen | Zahlungsdatei erzeugt |
| `submission.received` | Ereignis | 1 | `apps.session` | nichtoeffentlich | Einreichung eingegangen |
| `submission.status_changed` | Ereignis | 1 | `apps.session` | nichtoeffentlich | Bearbeitungsstand einer Einreichung geändert |
| `submission.submit` | Befehl | 1 | `apps.session` | nichtoeffentlich | Antrag einreichen |
| `submission.withdraw` | Befehl | 1 | `apps.session` | nichtoeffentlich | Einreichung zurückziehen |
| `work.document.status_changed` | Ereignis | 1 | `apps.work` | intern | Dokumentstatus in Work geändert |
| `work.factionmeeting.invited` | Ereignis | 1 | `apps.work` | intern | Einladung zur Fraktionssitzung versendet |
| `work.task.assigned` | Ereignis | 1 | `apps.work` | intern | Aufgabe zugewiesen |

## Ereignishülle v1

**Ereignishülle.** Hülle jedes Ereignisses im Journal (docs/adr/20260929-ereignistechnik-postgres.md). Die Nutzlast prüft das Schema des Ereignistyps in der angegebenen Version. Die Hülle enthält nur Kennungen: keine Namen, Mailadressen oder Inhalte.

Schema: [`envelope/v1.json`](../mandari/hub/contracts/envelope/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `event_id` | ja | Zeichenkette (uuid, Muster `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`) | Eindeutige Kennung des Ereignisses; Empfänger erkennen damit Doppelzustellungen. |
| `type` | ja | Zeichenkette (Muster `^[a-z][a-z0-9]*(?:_[a-z0-9]+)*(?:\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*){1,2}$`, höchstens 100 Zeichen) | Ereignistyp &lt;bereich&gt;.&lt;objekt&gt;.&lt;ereignis&gt;, z. B. ris.paper.released. |
| `version` | ja | Ganzzahl (1 bis 32767) | Schemaversion der Nutzlast (steht nie im Typnamen). |
| `aggregate_type` | ja | Zeichenkette (Muster `^[A-Z][A-Za-z0-9]*$`, höchstens 64 Zeichen) | Kanonischer Objekttyp, z. B. Paper. |
| `aggregate_id` | ja | Zeichenkette (uuid, Muster `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`) | Kanonische Kennung des Objekts. |
| `tenant_ref` | ja | Zeichenkette (Muster `^(session\|org\|source):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`) | Mandant: session:&lt;uuid&gt;, org:&lt;uuid&gt; oder source:&lt;uuid&gt;. |
| `body_id` | nein | Zeichenkette (uuid, Muster `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`) oder null | Kommune (kanonische Kennung), falls das Ereignis einer Kommune zugeordnet ist. |
| `visibility` | ja | Code: `oeffentlich`, `nichtoeffentlich`, `intern`, `personenbezogen` | Sichtbarkeit; der öffentliche Feed, das Bürgerportal und die Suche filtern auf oeffentlich. |
| `operation` | ja | Code: `upsert`, `delete`, `redact` | Operation im Änderungsfeed. |
| `occurred_at` | ja | Zeichenkette (date-time, Muster `^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z\|[+-][0-9]{2}:[0-9]{2})$`) | Zeitpunkt des fachlichen Geschehens, mit Zeitzone (RFC 3339). |
| `actor_ref` | nein | Zeichenkette (Muster `^(user:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\|system:[a-z][a-z0-9_.-]{0,63})$`) oder null | Auslöser als Kennung: user:&lt;uuid&gt; oder system:&lt;auftrag&gt;, nie ein Name. |
| `correlation_id` | ja | Zeichenkette (uuid, Muster `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`) | Kennung des Gesamtvorgangs (Anfrage, Auftrag), über alle Folgeereignisse gleich. |
| `causation_id` | nein | Zeichenkette (uuid, Muster `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`) oder null | event_id des auslösenden Ereignisses, falls es eines gibt. |
| `payload` | ja | Objekt | Nutzlast nach dem Schema des Typs: nur Kennungen und Namen geänderter Felder. |

## Ereignisse

### attendance.response_recorded v1

**Zu- oder Absage erfasst.** Eine geladene Person hat zu- oder abgesagt, über den Rückmeldelink, das Portal oder den Sitzungsdienst. Aggregat: Meeting. Der Grund einer Absage steht nie im Ereignis.

- Art: Ereignis
- Eigentümer: `apps.session`
- Sichtbarkeit: personenbezogen
- Schema: [`attendance.response_recorded/v1.json`](../mandari/hub/contracts/schemas/attendance.response_recorded/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `meeting` | ja | Zeichenkette (uuid) | Sitzung. |
| `person` | ja | Zeichenkette (uuid) | Geladene Person. |
| `response` | ja | Code: `confirmed`, `declined` | confirmed (Zusage) oder declined (Absage). |
| `substitute_requested` | nein | Wahrheitswert | Bei einer Absage: Vertretung erbeten. |
| `source` | nein | Code: `link`, `portal`, `staff` | Weg der Rückmeldung. |
| `attendance` | nein | Zeichenkette (uuid) | Kennung des Anwesenheitseintrags. |

Beispiel 1:

```json
{
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "person": "8e7d6c5b-4a39-4281-9f0e-1d2c3b4a5968",
  "response": "confirmed",
  "source": "link"
}
```

Beispiel 2:

```json
{
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "person": "8e7d6c5b-4a39-4281-9f0e-1d2c3b4a5968",
  "response": "declined",
  "substitute_requested": true,
  "attendance": "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e"
}
```

### core.membership.changed v1

**Mitgliedschaft geändert.** Eine Mitgliedschaft in einer Organisation wurde angelegt, geändert (Rollen, Rechte), deaktiviert, reaktiviert oder beendet. Aggregat: Membership.

- Art: Ereignis
- Eigentümer: `apps.tenants`
- Sichtbarkeit: personenbezogen
- Schema: [`core.membership.changed/v1.json`](../mandari/hub/contracts/schemas/core.membership.changed/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `membership` | ja | Zeichenkette (uuid) | Kennung der Mitgliedschaft. |
| `organization` | ja | Zeichenkette (uuid) | Organisation. |
| `user` | ja | Zeichenkette (uuid) | Konto der Person. |
| `change` | ja | Code: `added`, `roles_changed`, `permissions_changed`, `deactivated`, `reactivated`, `removed` | added, roles_changed, permissions_changed, deactivated, reactivated, removed. |
| `changed` | nein | Liste aus Zeichenkette (Muster `^[a-z][A-Za-z0-9_]{0,63}$`) (1 bis 64 Einträge) | Geänderte Felder der Mitgliedschaft. |

Beispiel 1:

```json
{
  "membership": "abcdef01-2345-4678-9abc-def012345678",
  "organization": "3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d",
  "user": "01234567-89ab-4cde-8f01-23456789abcd",
  "change": "added"
}
```

Beispiel 2:

```json
{
  "membership": "abcdef01-2345-4678-9abc-def012345678",
  "organization": "3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d",
  "user": "01234567-89ab-4cde-8f01-23456789abcd",
  "change": "roles_changed",
  "changed": [
    "roles"
  ]
}
```

### core.user.registered v1

**Konto angelegt oder bestätigt.** Ein Konto wurde angelegt oder seine E-Mail-Adresse bestätigt. Aggregat: User.

- Art: Ereignis
- Eigentümer: `apps.accounts`
- Sichtbarkeit: personenbezogen
- Schema: [`core.user.registered/v1.json`](../mandari/hub/contracts/schemas/core.user.registered/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `user` | ja | Zeichenkette (uuid) | Kennung des Kontos. |
| `step` | ja | Code: `created`, `confirmed` | created (angelegt) oder confirmed (bestätigt). |
| `via` | nein | Code: `invitation`, `self_registration`, `admin` | Weg: invitation (Einladung), self_registration (Selbstregistrierung), admin. |
| `organization` | nein | Zeichenkette (uuid) | Organisation, über die das Konto entstand. |

Beispiel 1:

```json
{
  "user": "01234567-89ab-4cde-8f01-23456789abcd",
  "step": "created",
  "via": "invitation",
  "organization": "3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"
}
```

Beispiel 2:

```json
{
  "user": "01234567-89ab-4cde-8f01-23456789abcd",
  "step": "confirmed"
}
```

### ris.agendaitem.changed v1

**Tagesordnung geändert.** Ein Tagesordnungspunkt wurde angelegt, geändert, abgesetzt, verschoben oder gelöscht. Aggregat: AgendaItem. Die Kennung des Punkts bleibt über Neuveröffentlichungen gleich. Die Nutzlast enthält nur Kennungen, Codes und Feldnamen; Inhalte liest der Empfänger über die Lese-Fassade des RIS-Bestands.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: nichtoeffentlich, oeffentlich
- Schema: [`ris.agendaitem.changed/v1.json`](../mandari/hub/contracts/schemas/ris.agendaitem.changed/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `agenda_item` | ja | Zeichenkette (uuid) | Kanonische Kennung des Tagesordnungspunkts. |
| `meeting` | ja | Zeichenkette (uuid) | Sitzung, zu der der Punkt (jetzt) gehört. |
| `change` | ja | Code: `added`, `changed`, `withdrawn`, `moved`, `deleted` | Art der Änderung: added (neu), changed (geändert), withdrawn (abgesetzt), moved (in eine andere Sitzung verschoben), deleted (gelöscht). |
| `changed` | nein | Liste aus Zeichenkette (Muster `^[a-z][A-Za-z0-9_]{0,63}$`) (1 bis 64 Einträge) | Geänderte Felder im kanonischen Modell (Namen wie in OParl, z. B. name, number). |
| `previous_meeting` | nein | Zeichenkette (uuid) | Sitzung vor dem Verschieben (bei change moved). |
| `paper` | nein | Zeichenkette (uuid) | Vorlage, die unter dem Punkt beraten wird. |

Beispiel 1:

```json
{
  "agenda_item": "9a6f2c3e-5b1d-5c7a-8e2f-3d4c5b6a7e8f",
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "change": "added",
  "paper": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c"
}
```

Beispiel 2:

```json
{
  "agenda_item": "9a6f2c3e-5b1d-5c7a-8e2f-3d4c5b6a7e8f",
  "meeting": "8d0f7780-8536-51ef-a55c-f18fd2fa1bf8",
  "change": "moved",
  "previous_meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7"
}
```

Beispiel 3:

```json
{
  "agenda_item": "9a6f2c3e-5b1d-5c7a-8e2f-3d4c5b6a7e8f",
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "change": "changed",
  "changed": [
    "name",
    "number"
  ]
}
```

### ris.consultation.changed v1

**Beratungsfolge geändert.** Eine Station der Beratungsfolge einer Vorlage wurde angelegt, terminiert, weitergeleitet, geändert oder entfernt. Aggregat: Consultation. Die Nutzlast enthält nur Kennungen, Codes und Feldnamen; Inhalte liest der Empfänger über die Lese-Fassade des RIS-Bestands.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: nichtoeffentlich, oeffentlich
- Schema: [`ris.consultation.changed/v1.json`](../mandari/hub/contracts/schemas/ris.consultation.changed/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `consultation` | ja | Zeichenkette (uuid) | Kanonische Kennung der Beratung. |
| `paper` | ja | Zeichenkette (uuid) | Vorlage, zu der die Beratung gehört. |
| `change` | ja | Code: `added`, `scheduled`, `forwarded`, `changed`, `removed` | Art der Änderung: added (angelegt), scheduled (einer Sitzung zugeordnet), forwarded (an ein weiteres Gremium weitergeleitet), changed (geändert), removed (entfernt). |
| `changed` | nein | Liste aus Zeichenkette (Muster `^[a-z][A-Za-z0-9_]{0,63}$`) (1 bis 64 Einträge) | Geänderte Felder im kanonischen Modell (Namen wie in OParl, z. B. role, authoritative). |
| `organization` | nein | Zeichenkette (uuid) | Beratendes Gremium. |
| `meeting` | nein | Zeichenkette (uuid) | Sitzung, in der beraten wird. |
| `agenda_item` | nein | Zeichenkette (uuid) | Tagesordnungspunkt der Beratung. |

Beispiel 1:

```json
{
  "consultation": "c3d4e5f6-a7b8-5c9d-8e0f-1a2b3c4d5e6f",
  "paper": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c",
  "change": "added",
  "organization": "1b4e28ba-2fa1-51d2-883f-0016d3cca427"
}
```

Beispiel 2:

```json
{
  "consultation": "c3d4e5f6-a7b8-5c9d-8e0f-1a2b3c4d5e6f",
  "paper": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c",
  "change": "scheduled",
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "agenda_item": "9a6f2c3e-5b1d-5c7a-8e2f-3d4c5b6a7e8f"
}
```

### ris.file.changed v1

**Anlage geändert.** Eine Datei (Anlage) wurde hinzugefügt, ersetzt, umbenannt oder entfernt. Aggregat: File. Die Zugehörigkeit nennt, woran die Datei hängt. Die Nutzlast enthält nur Kennungen, Codes und Feldnamen; Inhalte liest der Empfänger über die Lese-Fassade des RIS-Bestands.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: nichtoeffentlich, oeffentlich
- Schema: [`ris.file.changed/v1.json`](../mandari/hub/contracts/schemas/ris.file.changed/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `file` | ja | Zeichenkette (uuid) | Kanonische Kennung der Datei. |
| `change` | ja | Code: `added`, `replaced`, `renamed`, `removed` | Art der Änderung: added, replaced (neue Fassung), renamed, removed. |
| `paper` | nein | Zeichenkette (uuid) | Vorlage, an der die Datei hängt. |
| `meeting` | nein | Zeichenkette (uuid) | Sitzung, an der die Datei hängt. |
| `agenda_item` | nein | Zeichenkette (uuid) | Tagesordnungspunkt, an dem die Datei hängt. |

Beispiel 1:

```json
{
  "file": "f1e2d3c4-b5a6-5978-8a9b-0c1d2e3f4a5b",
  "change": "added",
  "paper": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c"
}
```

Beispiel 2:

```json
{
  "file": "f1e2d3c4-b5a6-5978-8a9b-0c1d2e3f4a5b",
  "change": "removed",
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7"
}
```

### ris.file.text_extracted v1

**Text einer Anlage erkannt.** Der Text einer Datei wurde extrahiert (Anreicherung). Es schreiben der OCR-Worker des Ingestors und der Auftrag file.extract_text der Anwendung, je nach TEXT_EXTRACTION_RUNNER, mit denselben Regeln. Aggregat: File. Den Text liest der Empfänger aus dem RIS-Bestand.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: intern
- Schema: [`ris.file.text_extracted/v1.json`](../mandari/hub/contracts/schemas/ris.file.text_extracted/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `file` | ja | Zeichenkette (uuid) | Kanonische Kennung der Datei. |
| `method` | ja | Zeichenkette (Muster `^[a-z][a-z0-9_]{0,31}$`) | Verfahren der Erkennung als Code, z. B. pypdf, tesseract, mistral. |
| `characters` | nein | Ganzzahl (0 bis …) | Länge des erkannten Texts in Zeichen. |

Beispiel 1:

```json
{
  "file": "f1e2d3c4-b5a6-5978-8a9b-0c1d2e3f4a5b",
  "method": "pypdf",
  "characters": 18342
}
```

Beispiel 2:

```json
{
  "file": "f1e2d3c4-b5a6-5978-8a9b-0c1d2e3f4a5b",
  "method": "tesseract"
}
```

### ris.meeting.changed v1

**Sitzung geändert.** Eine Sitzung wurde geändert, verlegt oder abgesagt. Aggregat: Meeting. changed nennt die geänderten Felder; cancelled zeigt eine Absage direkt an. Die Nutzlast enthält nur Kennungen, Codes und Feldnamen; Inhalte liest der Empfänger über die Lese-Fassade des RIS-Bestands.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: nichtoeffentlich, oeffentlich
- Schema: [`ris.meeting.changed/v1.json`](../mandari/hub/contracts/schemas/ris.meeting.changed/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `meeting` | ja | Zeichenkette (uuid) | Kanonische Kennung der Sitzung. |
| `changed` | ja | Liste aus Zeichenkette (Muster `^[a-z][A-Za-z0-9_]{0,63}$`) (1 bis 64 Einträge) | Geänderte Felder im kanonischen Modell (Namen wie in OParl, z. B. start, location). |
| `cancelled` | nein | Wahrheitswert | Die Sitzung ist nach der Änderung abgesagt. |
| `organizations` | nein | Liste aus Zeichenkette (uuid) (0 bis 50 Einträge) | Gremien, die die Sitzung abhalten (kanonische Kennungen). |

Beispiel 1:

```json
{
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "changed": [
    "start",
    "location"
  ]
}
```

Beispiel 2:

```json
{
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "changed": [
    "cancelled"
  ],
  "cancelled": true,
  "organizations": [
    "1b4e28ba-2fa1-51d2-883f-0016d3cca427"
  ]
}
```

### ris.meeting.invited v1

**Ladung versendet.** Zu einer Sitzung wurde eine Ladung versandt: Erstladung, Nachtrag oder Vertretungsanfrage. Aggregat: Meeting. Empfänger und Anschreiben liest nur, wer dazu berechtigt ist, beim Eigentümer. Die Nutzlast enthält nur Kennungen, Codes und Feldnamen; Inhalte liest der Empfänger über die Lese-Fassade des RIS-Bestands.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: nichtoeffentlich
- Schema: [`ris.meeting.invited/v1.json`](../mandari/hub/contracts/schemas/ris.meeting.invited/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `meeting` | ja | Zeichenkette (uuid) | Kanonische Kennung der Sitzung. |
| `dispatch` | ja | Zeichenkette (uuid) | Kennung des Versandvorgangs. |
| `dispatch_type` | ja | Code: `invitation`, `supplementary`, `substitution` | Art des Versands: invitation (Ladung), supplementary (Nachtrag), substitution (Vertretungsanfrage). |

Beispiel 1:

```json
{
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "dispatch": "d4c3b2a1-9f8e-4d7c-a6b5-4e3d2c1b0a9f",
  "dispatch_type": "invitation"
}
```

Beispiel 2:

```json
{
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "dispatch": "d4c3b2a1-9f8e-4d7c-a6b5-4e3d2c1b0a9f",
  "dispatch_type": "supplementary"
}
```

### ris.meeting.scheduled v1

**Sitzung angesetzt.** Eine Sitzung wurde angesetzt: in Session terminiert oder beim Abgleich eines fremden RIS neu erkannt. Aggregat: Meeting. Die Nutzlast enthält nur Kennungen, Codes und Feldnamen; Inhalte liest der Empfänger über die Lese-Fassade des RIS-Bestands.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: nichtoeffentlich, oeffentlich
- Schema: [`ris.meeting.scheduled/v1.json`](../mandari/hub/contracts/schemas/ris.meeting.scheduled/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `meeting` | ja | Zeichenkette (uuid) | Kanonische Kennung der Sitzung. |
| `organizations` | nein | Liste aus Zeichenkette (uuid) (0 bis 50 Einträge) | Gremien, die die Sitzung abhalten (kanonische Kennungen). |

Beispiel 1:

```json
{
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "organizations": [
    "1b4e28ba-2fa1-51d2-883f-0016d3cca427"
  ]
}
```

Beispiel 2:

```json
{
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7"
}
```

### ris.object.depublished v1

**Objekt zurückgenommen.** Ein Objekt des RIS-Bestands ist nicht mehr öffentlich: in der Quelle gelöscht, zurückgenommen, nichtöffentlich geworden oder aus Datenschutzgründen entfernt. Aggregat: das betroffene Objekt. Die Hülle trägt operation delete, beim Grund datenschutz redact (docs/adr/20260929-aenderungsfeed-format.md).

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: oeffentlich
- Schema: [`ris.object.depublished/v1.json`](../mandari/hub/contracts/schemas/ris.object.depublished/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `object_type` | ja | Code: `Body`, `Organization`, `Person`, `Membership`, `LegislativeTerm`, `Meeting`, `AgendaItem`, `Paper`, `Consultation`, `File`, `Location`, `Voting` | Kanonischer Typ des Objekts. |
| `object` | ja | Zeichenkette (uuid) | Kanonische Kennung des Objekts. |
| `reason` | ja | Code: `quelle_geloescht`, `zurueckgenommen`, `nichtoeffentlich`, `datenschutz` | Grund: quelle_geloescht, zurueckgenommen, nichtoeffentlich, datenschutz. |

Beispiel 1:

```json
{
  "object_type": "Paper",
  "object": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c",
  "reason": "nichtoeffentlich"
}
```

Beispiel 2:

```json
{
  "object_type": "File",
  "object": "f1e2d3c4-b5a6-5978-8a9b-0c1d2e3f4a5b",
  "reason": "datenschutz"
}
```

### ris.organization.changed v1

**Gremium geändert.** Ein Gremium wurde neu erkannt (added) oder geändert (changed), etwa Name, Art oder Zeitraum. Aggregat: Organization. Die Rücknahme meldet ris.object.depublished. Die Nutzlast enthält nur Kennungen, Codes und Feldnamen; Inhalte liest der Empfänger über die Lese-Fassade des RIS-Bestands.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: oeffentlich
- Schema: [`ris.organization.changed/v1.json`](../mandari/hub/contracts/schemas/ris.organization.changed/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `organization` | ja | Zeichenkette (uuid) | Kanonische Kennung des Gremiums. |
| `change` | ja | Code: `added`, `changed` | Art der Änderung: added (neu erkannt oder nach einer Rücknahme wieder geliefert), changed (geändert). |
| `changed` | nein | Liste aus Zeichenkette (Muster `^[a-z][A-Za-z0-9_]{0,63}$`) (1 bis 64 Einträge) | Geänderte Felder im kanonischen Modell (Namen wie in OParl, z. B. name, organizationType); bei change changed. |

Beispiel 1:

```json
{
  "organization": "3f2b8c1d-6e4a-5b7c-9d8e-1a2b3c4d5e6f",
  "change": "added"
}
```

Beispiel 2:

```json
{
  "organization": "3f2b8c1d-6e4a-5b7c-9d8e-1a2b3c4d5e6f",
  "change": "changed",
  "changed": [
    "name",
    "shortName"
  ]
}
```

### ris.paper.changed v1

**Vorlage geändert.** Eine Vorlage wurde geändert: Inhalt, Fassung oder Kennzeichen. Aggregat: Paper. changed nennt die geänderten Felder. Die Nutzlast enthält nur Kennungen, Codes und Feldnamen; Inhalte liest der Empfänger über die Lese-Fassade des RIS-Bestands.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: nichtoeffentlich, oeffentlich
- Schema: [`ris.paper.changed/v1.json`](../mandari/hub/contracts/schemas/ris.paper.changed/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `paper` | ja | Zeichenkette (uuid) | Kanonische Kennung der Vorlage. |
| `changed` | ja | Liste aus Zeichenkette (Muster `^[a-z][A-Za-z0-9_]{0,63}$`) (1 bis 64 Einträge) | Geänderte Felder im kanonischen Modell (Namen wie in OParl, z. B. name, paperType). |

Beispiel 1:

```json
{
  "paper": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c",
  "changed": [
    "name",
    "paperType"
  ]
}
```

### ris.paper.created v1

**Vorlage angelegt.** Eine Vorlage wurde angelegt, auch aus einem eingereichten Antrag. Aggregat: Paper. Noch nicht veröffentlicht; die Veröffentlichung meldet ris.paper.released. Die Nutzlast enthält nur Kennungen, Codes und Feldnamen; Inhalte liest der Empfänger über die Lese-Fassade des RIS-Bestands.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: nichtoeffentlich
- Schema: [`ris.paper.created/v1.json`](../mandari/hub/contracts/schemas/ris.paper.created/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `paper` | ja | Zeichenkette (uuid) | Kanonische Kennung der Vorlage. |
| `submission` | nein | Zeichenkette (uuid) | Eingereichter Antrag, aus dem die Vorlage entstanden ist. |

Beispiel 1:

```json
{
  "paper": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c"
}
```

Beispiel 2:

```json
{
  "paper": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c",
  "submission": "6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f"
}
```

### ris.paper.released v1

**Vorlage freigegeben.** Eine Vorlage wurde freigegeben bzw. veröffentlicht, in Session oder in einem fremden RIS. Aggregat: Paper. Das Ereignis kann öffentlich sein und nennt deshalb nicht die Einreichung, aus der die Vorlage entstanden ist; diesen Bezug meldet ris.paper.created. Die Nutzlast enthält nur Kennungen, Codes und Feldnamen; Inhalte liest der Empfänger über die Lese-Fassade des RIS-Bestands.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: nichtoeffentlich, oeffentlich
- Schema: [`ris.paper.released/v1.json`](../mandari/hub/contracts/schemas/ris.paper.released/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `paper` | ja | Zeichenkette (uuid) | Kanonische Kennung der Vorlage. |

Beispiel 1:

```json
{
  "paper": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c"
}
```

### ris.person.changed v1

**Person geändert.** Eine Person wurde neu erkannt (added) oder geändert (changed), etwa Name, Titel oder Mitgliedschaften. Aggregat: Person. Die Rücknahme meldet ris.object.depublished. Die Nutzlast enthält nur Kennungen, Codes und Feldnamen; Inhalte liest der Empfänger über die Lese-Fassade des RIS-Bestands.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: oeffentlich
- Schema: [`ris.person.changed/v1.json`](../mandari/hub/contracts/schemas/ris.person.changed/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `person` | ja | Zeichenkette (uuid) | Kanonische Kennung der Person. |
| `change` | ja | Code: `added`, `changed` | Art der Änderung: added (neu erkannt oder nach einer Rücknahme wieder geliefert), changed (geändert). |
| `changed` | nein | Liste aus Zeichenkette (Muster `^[a-z][A-Za-z0-9_]{0,63}$`) (1 bis 64 Einträge) | Geänderte Felder im kanonischen Modell (Namen wie in OParl, z. B. familyName, membership); bei change changed. |

Beispiel 1:

```json
{
  "person": "6d1e2f3a-4b5c-5d6e-8f7a-9b0c1d2e3f4a",
  "change": "added"
}
```

Beispiel 2:

```json
{
  "person": "6d1e2f3a-4b5c-5d6e-8f7a-9b0c1d2e3f4a",
  "change": "changed",
  "changed": [
    "familyName",
    "membership"
  ]
}
```

### ris.protocol.approved v1

**Niederschrift genehmigt.** Die Niederschrift einer Sitzung wurde genehmigt (Erweiterung des kanonischen Modells). Aggregat: Meeting. Veröffentlicht wird sie mit ris.protocol.published. Die Nutzlast enthält nur Kennungen, Codes und Feldnamen; Inhalte liest der Empfänger über die Lese-Fassade des RIS-Bestands.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: nichtoeffentlich
- Schema: [`ris.protocol.approved/v1.json`](../mandari/hub/contracts/schemas/ris.protocol.approved/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `protocol` | ja | Zeichenkette (uuid) | Kennung der Niederschrift. |
| `meeting` | ja | Zeichenkette (uuid) | Sitzung, deren Niederschrift genehmigt wurde. |
| `mode` | nein | Code: `follow_up`, `direct` | Genehmigungsweg: follow_up (in der Folgesitzung), direct (ohne Genehmigungsschritt). |
| `approved_in` | nein | Zeichenkette (uuid) | Sitzung, in der genehmigt wurde (bei mode follow_up). |

Beispiel 1:

```json
{
  "protocol": "2c3d4e5f-6a7b-5c8d-9e0f-a1b2c3d4e5f6",
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "mode": "follow_up",
  "approved_in": "8d0f7780-8536-51ef-a55c-f18fd2fa1bf8"
}
```

### ris.protocol.published v1

**Niederschrift veröffentlicht.** Die Niederschrift einer Sitzung wurde veröffentlicht, erneuert oder berichtigt. Aggregat: Meeting. Die Nutzlast enthält nur Kennungen, Codes und Feldnamen; Inhalte liest der Empfänger über die Lese-Fassade des RIS-Bestands.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: oeffentlich
- Schema: [`ris.protocol.published/v1.json`](../mandari/hub/contracts/schemas/ris.protocol.published/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `protocol` | ja | Zeichenkette (uuid) | Kennung der Niederschrift. |
| `meeting` | ja | Zeichenkette (uuid) | Sitzung der Niederschrift. |
| `change` | ja | Code: `published`, `renewed`, `corrected` | published (erstmals), renewed (neu erzeugt), corrected (berichtigt). |
| `file` | nein | Zeichenkette (uuid) | Veröffentlichte Datei der Niederschrift. |

Beispiel 1:

```json
{
  "protocol": "2c3d4e5f-6a7b-5c8d-9e0f-a1b2c3d4e5f6",
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "change": "published",
  "file": "f1e2d3c4-b5a6-5978-8a9b-0c1d2e3f4a5b"
}
```

### ris.resolution.adopted v1

**Beschluss gefasst.** Zu einem Tagesordnungspunkt wurde ein Beschluss gefasst oder die Beschlussnummer vergeben (Erweiterung des kanonischen Modells). Aggregat: AgendaItem. Die Nutzlast enthält nur Kennungen, Codes und Feldnamen; Inhalte liest der Empfänger über die Lese-Fassade des RIS-Bestands.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: nichtoeffentlich, oeffentlich
- Schema: [`ris.resolution.adopted/v1.json`](../mandari/hub/contracts/schemas/ris.resolution.adopted/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `agenda_item` | ja | Zeichenkette (uuid) | Tagesordnungspunkt mit dem Beschluss. |
| `result` | ja | Code: `approved`, `rejected`, `deferred`, `withdrawn`, `noted` | Ergebnis: approved (angenommen), rejected (abgelehnt), deferred (vertagt), withdrawn (zurückgezogen), noted (zur Kenntnis genommen). |
| `changed` | nein | Liste aus Zeichenkette (Muster `^[a-z][A-Za-z0-9_]{0,63}$`) (1 bis 64 Einträge) | Geänderte Beschlussfelder, z. B. resolutionNumber. |
| `meeting` | nein | Zeichenkette (uuid) | Sitzung des Beschlusses. |
| `paper` | nein | Zeichenkette (uuid) | Vorlage, über die beschlossen wurde. |
| `consultation` | nein | Zeichenkette (uuid) | Beratung, in der beschlossen wurde. |

Beispiel 1:

```json
{
  "agenda_item": "9a6f2c3e-5b1d-5c7a-8e2f-3d4c5b6a7e8f",
  "result": "approved",
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "paper": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c",
  "consultation": "c3d4e5f6-a7b8-5c9d-8e0f-1a2b3c4d5e6f"
}
```

Beispiel 2:

```json
{
  "agenda_item": "9a6f2c3e-5b1d-5c7a-8e2f-3d4c5b6a7e8f",
  "result": "approved",
  "changed": [
    "resolutionNumber"
  ]
}
```

### ris.resolution.implementation_changed v1

**Umsetzungsstand geändert.** Der Umsetzungsstand eines Beschlusses (Beschlusskontrolle) hat sich geändert. Aggregat: AgendaItem. Die Nutzlast enthält nur Kennungen, Codes und Feldnamen; Inhalte liest der Empfänger über die Lese-Fassade des RIS-Bestands.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: oeffentlich
- Schema: [`ris.resolution.implementation_changed/v1.json`](../mandari/hub/contracts/schemas/ris.resolution.implementation_changed/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `agenda_item` | ja | Zeichenkette (uuid) | Tagesordnungspunkt mit dem Beschluss. |
| `status` | ja | Code: `open`, `in_progress`, `done`, `deferred` | Umsetzungsstand: open, in_progress, done, deferred (zurückgestellt). |
| `previous_status` | nein | Code: `open`, `in_progress`, `done`, `deferred` | Umsetzungsstand vor der Änderung. |
| `paper` | nein | Zeichenkette (uuid) | Vorlage des Beschlusses. |

Beispiel 1:

```json
{
  "agenda_item": "9a6f2c3e-5b1d-5c7a-8e2f-3d4c5b6a7e8f",
  "status": "done",
  "previous_status": "in_progress",
  "paper": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c"
}
```

### ris.source.published v1

**Kommune im Bürgerportal veröffentlicht.** Eine Kommune wurde im Bürgerportal veröffentlicht oder ihre Veröffentlichung beendet. Aggregat: Body. end_mode sagt bei beendeter Veröffentlichung, was mit dem Bestand geschieht.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: oeffentlich
- Schema: [`ris.source.published/v1.json`](../mandari/hub/contracts/schemas/ris.source.published/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `body` | ja | Zeichenkette (uuid) | Kanonische Kennung der Kommune. |
| `published` | ja | Wahrheitswert | true: veröffentlicht, false: Veröffentlichung beendet. |
| `end_mode` | nein | Code: `paused`, `archived`, `withdrawn` | Bei beendeter Veröffentlichung: paused (vorübergehend), archived (als Archiv), withdrawn (dauerhaft zurückgenommen). |

Beispiel 1:

```json
{
  "body": "4d5e6f70-8192-5a3b-8c4d-5e6f708192a3",
  "published": true
}
```

Beispiel 2:

```json
{
  "body": "4d5e6f70-8192-5a3b-8c4d-5e6f708192a3",
  "published": false,
  "end_mode": "archived"
}
```

### ris.voting.recorded v1

**Abstimmung erfasst.** Zu einem Tagesordnungspunkt wurde eine Abstimmung erfasst (Erweiterung des kanonischen Modells). Aggregat: Voting. Stimmen einzelner Personen stehen nie im Ereignis. Die Nutzlast enthält nur Kennungen, Codes und Feldnamen; Inhalte liest der Empfänger über die Lese-Fassade des RIS-Bestands.

- Art: Ereignis
- Eigentümer: `hub.ris`
- Sichtbarkeit: nichtoeffentlich, oeffentlich
- Schema: [`ris.voting.recorded/v1.json`](../mandari/hub/contracts/schemas/ris.voting.recorded/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `voting` | ja | Zeichenkette (uuid) | Kanonische Kennung der Abstimmung. |
| `agenda_item` | ja | Zeichenkette (uuid) | Tagesordnungspunkt, zu dem abgestimmt wurde. |
| `meeting` | nein | Zeichenkette (uuid) | Sitzung der Abstimmung. |
| `paper` | nein | Zeichenkette (uuid) | Vorlage, über die abgestimmt wurde. |
| `method` | nein | Code: `summary`, `open`, `roll_call`, `secret` | Abstimmungsart: summary (nur Summen), open (offen, einzeln erfasst), roll_call (namentlich), secret (geheim). |
| `result` | nein | Code: `approved`, `rejected`, `deferred`, `withdrawn`, `noted` | Ergebnis der Abstimmung. |

Beispiel 1:

```json
{
  "voting": "0a1b2c3d-4e5f-5a6b-9c7d-8e9f0a1b2c3d",
  "agenda_item": "9a6f2c3e-5b1d-5c7a-8e2f-3d4c5b6a7e8f",
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "method": "summary",
  "result": "approved"
}
```

Beispiel 2:

```json
{
  "voting": "0a1b2c3d-4e5f-5a6b-9c7d-8e9f0a1b2c3d",
  "agenda_item": "9a6f2c3e-5b1d-5c7a-8e2f-3d4c5b6a7e8f"
}
```

### session.allowance.approved v1

**Sitzungsgeld genehmigt.** Ein Sitzungsgeld oder eine Monatspauschale wurde genehmigt. Aggregat: Allowance. Beträge und Bankdaten stehen nie im Ereignis.

- Art: Ereignis
- Eigentümer: `apps.session`
- Sichtbarkeit: personenbezogen
- Schema: [`session.allowance.approved/v1.json`](../mandari/hub/contracts/schemas/session.allowance.approved/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `allowance` | ja | Zeichenkette (uuid) | Kennung der Abrechnungsposition. |
| `person` | ja | Zeichenkette (uuid) | Empfangende Person. |
| `kind` | ja | Code: `meeting`, `monthly` | meeting (Sitzungsgeld je Sitzung) oder monthly (Monatspauschale). |
| `meeting` | nein | Zeichenkette (uuid) | Sitzung (bei kind meeting). |

Beispiel 1:

```json
{
  "allowance": "a9b8c7d6-e5f4-4a3b-9c2d-1e0f9a8b7c6d",
  "person": "8e7d6c5b-4a39-4281-9f0e-1d2c3b4a5968",
  "kind": "meeting",
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7"
}
```

Beispiel 2:

```json
{
  "allowance": "a9b8c7d6-e5f4-4a3b-9c2d-1e0f9a8b7c6d",
  "person": "8e7d6c5b-4a39-4281-9f0e-1d2c3b4a5968",
  "kind": "monthly"
}
```

### session.payment.exported v1

**Zahlungsdatei erzeugt.** Für genehmigte Positionen wurde eine Zahlungsdatei (SEPA) erzeugt. Aggregat: Export. Beträge, Namen und Bankdaten stehen nie im Ereignis.

- Art: Ereignis
- Eigentümer: `apps.session`
- Sichtbarkeit: personenbezogen
- Schema: [`session.payment.exported/v1.json`](../mandari/hub/contracts/schemas/session.payment.exported/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `reference` | ja | Zeichenkette (Muster `^SG-[0-9]{4}-[0-9]{4,6}$`) | Export-Referenz, z. B. SG-2026-0003. |
| `kind` | ja | Code: `sitzungsgeld`, `pauschale` | sitzungsgeld oder pauschale. |
| `allowances` | ja | Liste aus Zeichenkette (uuid) (0 bis 10000 Einträge) | Exportierte Abrechnungspositionen. |

Beispiel 1:

```json
{
  "reference": "SG-2026-0003",
  "kind": "sitzungsgeld",
  "allowances": [
    "a9b8c7d6-e5f4-4a3b-9c2d-1e0f9a8b7c6d",
    "b0c9d8e7-f6a5-4b4c-8d3e-2f1a0b9c8d7e"
  ]
}
```

### submission.received v1

**Einreichung eingegangen.** Ein Antrag oder eine Anfrage ist bei der Verwaltung eingegangen, egal über welchen Weg (Befehl submission.submit). Aggregat: Submission. Inhalte und Einreichende liest nur, wer berechtigt ist, beim Eigentümer.

- Art: Ereignis
- Eigentümer: `apps.session`
- Sichtbarkeit: nichtoeffentlich
- Schema: [`submission.received/v1.json`](../mandari/hub/contracts/schemas/submission.received/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `submission` | ja | Zeichenkette (uuid) | Kennung der Einreichung. |
| `application_type` | ja | Code: `motion`, `inquiry`, `resolution`, `urgent`, `amendment`, `other` | Art der Einreichung. |
| `submitting_organization` | nein | Zeichenkette (uuid) | Einreichende Organisation (Fraktion, Gruppe). |
| `target_organization` | nein | Zeichenkette (uuid) | Zuständiges Gremium, falls angegeben. |
| `content_hash` | nein | Zeichenkette (Muster `^[0-9a-f]{64}$`) | SHA-256 (hexadezimal) über den Inhalt der Einreichung, wie in der Quittung. |

Beispiel 1:

```json
{
  "submission": "6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f",
  "application_type": "motion",
  "submitting_organization": "3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d",
  "target_organization": "1b4e28ba-2fa1-51d2-883f-0016d3cca427",
  "content_hash": "3a7bd3e2360a3d29eea436fcfb7e44c735d117c42d1c1835420b6b9942dd4f1b"
}
```

Beispiel 2:

```json
{
  "submission": "6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f",
  "application_type": "inquiry"
}
```

### submission.status_changed v1

**Bearbeitungsstand einer Einreichung geändert.** Der Bearbeitungsstand einer Einreichung hat sich geändert (in Prüfung, angenommen, zur Vorlage gemacht, zurückgewiesen, zurückgezogen). Aggregat: Submission.

- Art: Ereignis
- Eigentümer: `apps.session`
- Sichtbarkeit: nichtoeffentlich
- Schema: [`submission.status_changed/v1.json`](../mandari/hub/contracts/schemas/submission.status_changed/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `submission` | ja | Zeichenkette (uuid) | Kennung der Einreichung. |
| `status` | ja | Code: `submitted`, `received`, `in_review`, `accepted`, `rejected`, `converted`, `withdrawn` | Neuer Bearbeitungsstand. |
| `previous_status` | nein | Code: `submitted`, `received`, `in_review`, `accepted`, `rejected`, `converted`, `withdrawn` | Bearbeitungsstand vor der Änderung. |
| `paper` | nein | Zeichenkette (uuid) | Vorlage, die aus der Einreichung entstanden ist (bei status converted). |
| `submitting_organization` | nein | Zeichenkette (uuid) | Einreichende Organisation. |

Beispiel 1:

```json
{
  "submission": "6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f",
  "status": "in_review",
  "previous_status": "received",
  "submitting_organization": "3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"
}
```

Beispiel 2:

```json
{
  "submission": "6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f",
  "status": "converted",
  "previous_status": "accepted",
  "paper": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c"
}
```

### work.document.status_changed v1

**Dokumentstatus in Work geändert.** Der Status eines Dokuments (Antrag, Anfrage …) in Work hat sich geändert. Aggregat: Document.

- Art: Ereignis
- Eigentümer: `apps.work`
- Sichtbarkeit: intern
- Schema: [`work.document.status_changed/v1.json`](../mandari/hub/contracts/schemas/work.document.status_changed/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `document` | ja | Zeichenkette (uuid) | Kennung des Dokuments. |
| `organization` | ja | Zeichenkette (uuid) | Organisation, der das Dokument gehört. |
| `status` | ja | Code: `draft`, `review`, `approved`, `submitted`, `completed`, `rejected`, `archived`, `deleted`, `internal_review`, `external_review`, `at_admin`, `on_agenda`, `adopted`, `withdrawn` | Neuer Status. |
| `previous_status` | nein | Code: `draft`, `review`, `approved`, `submitted`, `completed`, `rejected`, `archived`, `deleted`, `internal_review`, `external_review`, `at_admin`, `on_agenda`, `adopted`, `withdrawn` | Status vor der Änderung. |

Beispiel 1:

```json
{
  "document": "0f9e8d7c-6b5a-4938-a7b6-c5d4e3f2a1b0",
  "organization": "3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d",
  "status": "at_admin",
  "previous_status": "submitted"
}
```

### work.factionmeeting.invited v1

**Einladung zur Fraktionssitzung versendet.** Zu einer Fraktionssitzung wurde eingeladen oder die Einladung aktualisiert. Aggregat: FactionMeeting.

- Art: Ereignis
- Eigentümer: `apps.work`
- Sichtbarkeit: intern
- Schema: [`work.factionmeeting.invited/v1.json`](../mandari/hub/contracts/schemas/work.factionmeeting.invited/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `faction_meeting` | ja | Zeichenkette (uuid) | Kennung der Fraktionssitzung. |
| `organization` | ja | Zeichenkette (uuid) | Organisation der Sitzung. |
| `update` | nein | Wahrheitswert | Aktualisierte Einladung zu einer bereits eingeladenen Sitzung. |

Beispiel 1:

```json
{
  "faction_meeting": "12345678-9abc-4def-8123-456789abcdef",
  "organization": "3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"
}
```

Beispiel 2:

```json
{
  "faction_meeting": "12345678-9abc-4def-8123-456789abcdef",
  "organization": "3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d",
  "update": true
}
```

### work.task.assigned v1

**Aufgabe zugewiesen.** Eine Aufgabe wurde angelegt oder einer Mitgliedschaft zugewiesen. Aggregat: Task.

- Art: Ereignis
- Eigentümer: `apps.work`
- Sichtbarkeit: intern
- Schema: [`work.task.assigned/v1.json`](../mandari/hub/contracts/schemas/work.task.assigned/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `task` | ja | Zeichenkette (uuid) | Kennung der Aufgabe. |
| `organization` | ja | Zeichenkette (uuid) | Organisation der Aufgabe. |
| `assignee` | nein | Zeichenkette (uuid) | Mitgliedschaft, der die Aufgabe jetzt zugewiesen ist. |
| `created` | nein | Wahrheitswert | Die Aufgabe wurde mit dieser Zuweisung angelegt. |

Beispiel 1:

```json
{
  "task": "fedcba98-7654-4321-8fed-cba987654321",
  "organization": "3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d",
  "assignee": "abcdef01-2345-4678-9abc-def012345678",
  "created": true
}
```


## Befehle

### attendance.respond v1

**Zu- oder Absage zur Sitzung.** Eine geladene Person sagt zu oder ab, optional mit Vertretungswunsch. Handler: apps.session. Der Grund einer Absage ist Inhalt (x-content): Der Eigentümer speichert ihn verschlüsselt und zeigt ihn nur dem Sitzungsdienst.

- Art: Befehl
- Eigentümer: `apps.session`
- Sichtbarkeit: personenbezogen
- Schema: [`attendance.respond/v1.json`](../mandari/hub/contracts/schemas/attendance.respond/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `meeting` | ja | Zeichenkette (uuid) | Sitzung. |
| `person` | ja | Zeichenkette (uuid) | Geladene Person. |
| `response` | ja | Code: `confirmed`, `declined` | confirmed (Zusage) oder declined (Absage). |
| `substitute_requested` | nein | Wahrheitswert | Bei einer Absage: Vertretung erbeten. |
| `source` | nein | Code: `link`, `portal`, `staff` | Weg der Rückmeldung. |
| `reason` | nein, Inhalt | Zeichenkette (höchstens 2000 Zeichen) | Grund der Absage. |

Beispiel 1:

```json
{
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "person": "8e7d6c5b-4a39-4281-9f0e-1d2c3b4a5968",
  "response": "confirmed",
  "source": "portal"
}
```

Beispiel 2:

```json
{
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "person": "8e7d6c5b-4a39-4281-9f0e-1d2c3b4a5968",
  "response": "declined",
  "substitute_requested": true,
  "source": "link",
  "reason": "Dienstreise"
}
```

### invitation.acknowledge v1

**Empfang einer Ladung bestätigen.** Eine geladene Person bestätigt den Empfang einer Ladung; bloßes Öffnen zählt nicht. Handler: apps.session.

- Art: Befehl
- Eigentümer: `apps.session`
- Sichtbarkeit: personenbezogen
- Schema: [`invitation.acknowledge/v1.json`](../mandari/hub/contracts/schemas/invitation.acknowledge/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `recipient` | ja | Zeichenkette (uuid) | Kennung des Ladungsempfängers (aus dem Versand). |
| `meeting` | nein | Zeichenkette (uuid) | Sitzung der Ladung. |
| `source` | nein | Code: `link`, `portal`, `staff` | Weg der Bestätigung. |

Beispiel 1:

```json
{
  "recipient": "e5f6a7b8-c9d0-4e1f-a2b3-c4d5e6f7a8b9",
  "meeting": "7c9e6679-7425-50de-944b-e07fc1f90ae7",
  "source": "link"
}
```

Beispiel 2:

```json
{
  "recipient": "e5f6a7b8-c9d0-4e1f-a2b3-c4d5e6f7a8b9"
}
```

### submission.submit v1

**Antrag einreichen.** Eine Organisation reicht einen Antrag oder eine Anfrage bei der Verwaltung ein. Handler: apps.session. Die Quittung nennt Eingangsnummer, Eingangszeit und Inhalts-Hash. Felder mit x-content sind Inhalte: Der Eigentümer speichert sie, der Befehlsweg gibt sie nie in Logs, Fehlermeldungen oder Ereignisse weiter.

- Art: Befehl
- Eigentümer: `apps.session`
- Sichtbarkeit: nichtoeffentlich
- Schema: [`submission.submit/v1.json`](../mandari/hub/contracts/schemas/submission.submit/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `application_type` | ja | Code: `motion`, `inquiry`, `resolution`, `urgent`, `amendment`, `other` | Art der Einreichung. |
| `title` | ja, Inhalt | Zeichenkette (höchstens 500 Zeichen) | Titel. |
| `resolution_proposal` | ja, Inhalt | Zeichenkette (höchstens 50000 Zeichen) | Beschlussvorschlag. |
| `justification` | ja, Inhalt | Zeichenkette (höchstens 50000 Zeichen) | Begründung. |
| `financial_impact` | nein, Inhalt | Zeichenkette (höchstens 10000 Zeichen) | Finanzielle Auswirkungen. |
| `is_urgent` | nein | Wahrheitswert | Dringlich. |
| `urgency_reason` | nein, Inhalt | Zeichenkette (höchstens 5000 Zeichen) | Begründung der Dringlichkeit. |
| `deadline` | nein | Zeichenkette (date) | Gewünschter Beratungstermin. |
| `target_organization` | nein | Zeichenkette (uuid) | Zuständiges Gremium der Verwaltung. |
| `submitting_organization` | nein | Zeichenkette (uuid) | Einreichende Organisation. |
| `submitter` | ja, Inhalt | Objekt | Ansprechperson für Rückfragen der Verwaltung. |
| `submitter.name` | ja | Zeichenkette (höchstens 200 Zeichen) |  |
| `submitter.email` | ja | Zeichenkette (email, höchstens 254 Zeichen) |  |
| `submitter.phone` | nein | Zeichenkette (höchstens 50 Zeichen) |  |
| `co_signers` | nein, Inhalt | Liste aus Zeichenkette (höchstens 200 Zeichen) (0 bis 100 Einträge) | Mitunterzeichnende, je Eintrag ein Name. |

Beispiel 1:

```json
{
  "application_type": "motion",
  "title": "Mehr Bänke im Stadtpark",
  "resolution_proposal": "Die Verwaltung wird beauftragt, zehn zusätzliche Bänke aufzustellen.",
  "justification": "Ältere Menschen finden auf den Wegen zu wenige Sitzgelegenheiten.",
  "is_urgent": false,
  "deadline": "2026-11-15",
  "target_organization": "1b4e28ba-2fa1-51d2-883f-0016d3cca427",
  "submitting_organization": "3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d",
  "submitter": {
    "name": "Erika Mustermann",
    "email": "fraktion@example.org"
  },
  "co_signers": [
    "Max Mustermann"
  ]
}
```

### submission.withdraw v1

**Einreichung zurückziehen.** Die einreichende Organisation zieht eine Einreichung zurück, solange die Verwaltung sie noch nicht zur Vorlage gemacht hat. Handler: apps.session.

- Art: Befehl
- Eigentümer: `apps.session`
- Sichtbarkeit: nichtoeffentlich
- Schema: [`submission.withdraw/v1.json`](../mandari/hub/contracts/schemas/submission.withdraw/v1.json)

| Feld | Pflicht | Typ | Beschreibung |
|---|---|---|---|
| `submission` | ja | Zeichenkette (uuid) | Kennung der Einreichung (aus der Quittung). |
| `submitting_organization` | nein | Zeichenkette (uuid) | Einreichende Organisation. |

Beispiel 1:

```json
{
  "submission": "6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f",
  "submitting_organization": "3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"
}
```

Beispiel 2:

```json
{
  "submission": "6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f"
}
```
