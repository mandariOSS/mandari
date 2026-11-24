# Anbindung an einen Protokolldienst

Aufnahme, Transkript und Entwurf der Niederschrift entstehen in einem eigenen Dienst außerhalb von mandari, dem
Protokolldienst. Session meldet ihm den Ablauf der Sitzung aus dem Cockpit und nimmt seine freigegebenen Entwürfe als
Vorschlag für die Niederschrift zurück. Die Kopplung läuft nur über Schnittstellen:

| Richtung | Weg | Inhalt |
|---|---|---|
| Session → Protokolldienst | OParl je Mandant (lesend, [SESSION_OPARL_API.md](SESSION_OPARL_API.md)) | Sitzungen und öffentliche TOPs; der Dienst übernimmt sie und kennt sie unter ihrer OParl-Kennung |
| Session → Protokolldienst | Ereignis-Schnittstelle des Dienstes (`POST /api/v1/meetings/{id}/events`) | TOP-Aufruf, Unterbrechung, Abstimmungsergebnis aus dem Cockpit |
| Protokolldienst → Session | Bereich `protokolldienst` der Schnittstelle (`/api/mandari/v1/protokolldienst/`) | Entwurf je TOP: Protokolltext, Beschlusstext, Abstimmungsergebnis |
| Anmeldung | OpenID Connect ([schnittstelle/anmeldung.md](schnittstelle/anmeldung.md)) | Konten des Dienstes über das mandari-Konto (E-Mail, verifiziert) |

Code: `apps/session/protokolldienst/`, Einstellungen `apps/session/views/protokolldienst.py`, Modelle
`SessionProtokolldienst`, `SessionProtokolldienstMeldung`, `SessionNiederschriftVorschlag` (Migration
`session.0071`), Tests `apps/session/tests/test_protokolldienst*.py`.

## Einrichten

1. Beim Protokolldienst einen API-Schlüssel für den Mandanten anlegen, mit den Rechten `meetings:read` und
   `events:write`. Der Dienst übernimmt die Sitzungen über die OParl-Schnittstelle des Mandanten; die muss dafür
   freigeschaltet sein.
2. In Session unter Einstellungen › Datenschutz und Zugänge › Protokolldienst (Recht „Einstellungen verwalten“) die
   Adresse des Dienstes und den Schlüssel eintragen, einschalten, „Verbindung prüfen“.
3. Dort „Schlüssel erzeugen“: Der Rückgabeschlüssel erscheint genau einmal. Beim Protokolldienst mit der Adresse der
   Installation eintragen. Ein neuer Schlüssel macht den bisherigen ungültig.

Der API-Schlüssel des Dienstes liegt mit dem Mandantenschlüssel verschlüsselt in `api_key_encrypted`
([KRYPTOKONZEPT.md](KRYPTOKONZEPT.md)); vom Rückgabeschlüssel speichert Session nur den SHA-256 (wie bei
Einreichungs-Zugängen). Jede Änderung steht im Prüfprotokoll des Mandanten, nie ein Schlüssel.

**Adresse:** nur `https` und ein öffentlicher Host. Für einen Dienst im eigenen Netz trägt der Betrieb dessen Host in
`PROTOKOLLDIENST_ERLAUBTE_HOSTS` ein (kommagetrennt); ist die Liste gesetzt, sind nur diese Hosts zulässig, dann auch
mit `http`. Beim Abruf verbindet der Client nur mit geprüften öffentlichen Adressen (`safe_fetch`, streng) und folgt
keinen Weiterleitungen; freigegebene Hosts sind davon ausgenommen.

**Worker:** Zugestellt wird im Auftrag `apps.session.protokolldienst.auftraege.zustellen` in der Warteschlange
`adapter`, die der Dienst `worker` bedient. Ohne Worker bleiben Meldungen wartend; das Cockpit läuft trotzdem.

## Hinweg: Meldungen aus dem Cockpit

Das Cockpit (`cockpit_service`) legt in der Transaktion seiner Aktion Zeilen im Postausgang an
(`protokolldienst.meldungen`) und reiht nach dem Commit den Auftrag ein, immer im Journal der Aufträge, auch ohne
`TASKS_BACKEND=journal`. Es wartet nie auf den Dienst. Ohne eingeschaltete Anbindung und bei nichtöffentlichen
Sitzungen entsteht nichts.

| Aktion im Cockpit | Meldung | Nutzdaten |
|---|---|---|
| öffentlicher TOP aufgerufen | `agenda_item_changed` | `number`; die Zustellung ergänzt `agenda_item` (Kennung beim Dienst), wenn er die Nummer kennt |
| nichtöffentlicher TOP aufgerufen | `pause_started` | `reason: nichtoeffentlich`, keine Nummer |
| Einwohnerfragestunde aufgerufen | `agenda_item_changed`, dann `pause_started` | `number`, `reason: einwohnerfragestunde` |
| Sitzung unterbrochen | `pause_started` | `reason: unterbrechung` |
| Unterbrechung beendet, nächster öffentlicher TOP | `pause_ended` | Grund der offenen Pause |
| Abstimmung geschlossen, TOP vertagt | `vote_result` | `outcome` (`adopted`, `rejected`, `postponed`, …), bei angenommen oder abgelehnt `yes`, `no`, `abstain`, `unanimous`; nur öffentliche TOPs |

- **Nur öffentliche Angaben:** keine Namen, keine Texte, keine Einzelstimmen, nichts aus dem nichtöffentlichen Teil.
  Eine Rednerliste führt Session nicht; Rednerwechsel meldet es deshalb nicht.
- **Pausen überlappen nie.** Es gibt höchstens eine offene Pause. Sie beginnt mit einer Unterbrechung oder einem
  geschützten TOP und endet erst, wenn die Sitzung nicht unterbrochen ist und ein öffentlicher TOP aufgerufen wird.
  Das Ende eines geschützten TOP allein beendet sie nicht. Der Dienst verarbeitet Pausen nie, auch wenn er den TOP
  nicht kennt.
- **Zuordnung:** Die Zustellung sucht die Sitzung beim Dienst über ihre OParl-Kennung
  (`GET /api/v1/meetings` drei Tage um den Beginn) und die TOPs über ihre Nummer (`GET /api/v1/meetings/{id}`).
  Jede Meldung trägt Quelle `session` und `external_id` `mandari-session:<Kennung der Meldung>`; Wiederholungen
  speichert der Dienst nur einmal.
- **Wiederholung:** Kennt der Dienst die Sitzung noch nicht, ist er nicht erreichbar oder überlastet, wiederholt der
  Runner den Auftrag (zwölf Versuche, zusammen etwa sechs Stunden). Lehnt er den Schlüssel ab (401, 403) oder die
  Meldung (400, 413, 422), ist sie fehlgeschlagen. Ein Ergebnis zu einem TOP, den der Dienst nicht kennt, ebenfalls
  (`top_unbekannt`). Den Stand (letzte Zustellung, letzter Fehler, wartende Meldungen) zeigt die Einstellungsseite.
- **Aufbewahrung:** Zugestellte Meldungen löscht der Auftrag nach 30 Tagen; mit der Sitzung gehen alle.

Das Cockpit hat dafür die Aktionen „Sitzung unterbrechen“ und „Unterbrechung beenden“ (`SessionMeeting.interrupted_at`);
ein TOP-Aufruf und das Schließen der Sitzung beenden die Unterbrechung, während der Unterbrechung wird nicht
abgestimmt ([SESSION.md](SESSION.md#3-sitzungscockpit)).

## Rückweg: Entwürfe als Vorschlag

`POST /api/mandari/v1/protokolldienst/{tenant_slug}/niederschrift/vorschlaege/` mit
`Authorization: Bearer <Rückgabeschlüssel>` nimmt den Befehl `session.minutes_draft.propose` an
([EREIGNISKATALOG.md](EREIGNISKATALOG.md)); die Beschreibung steht in `docs/schnittstelle/openapi.json` (Bereich
„Protokolldienst“). `GET /api/mandari/v1/protokolldienst/{tenant_slug}/` prüft die Verbindung.

```json
{
  "meeting": "https://…/session/<mandant>/api/oparl/meeting/<kennung>/",
  "source_document": "0b8f4c1e-2d3a-4e5f-9a6b-7c8d9e0f1a2b",
  "revision": 2,
  "released_at": "2026-11-24T10:15:00+01:00",
  "items": [
    {"agenda_item": "https://…/session/<mandant>/api/oparl/agendaitem/<kennung>/",
     "draft_text": "…", "resolution_text": "…",
     "vote": {"yes": 21, "no": 3, "abstain": 1, "unanimous": false, "outcome": "adopted"}},
    {"number": "7", "draft_text": "…"}
  ]
}
```

- Der Schlüssel gilt nur für den Mandanten in der Adresse und nur, solange die Anbindung eingeschaltet ist; sonst 401.
  Höchstens 60 Anfragen je Minute und Mandant.
- Sitzung und TOPs über ihre OParl-Kennung, TOPs ohne Kennung (etwa nichtöffentliche) über die Nummer. Nicht
  zuzuordnende TOPs nennt die Antwort unter `unassigned`; ist keiner zuzuordnen, 422. Eine Sitzung eines anderen
  Mandanten: 404. Genehmigte Niederschrift: 409.
- `Idempotency-Key` ist empfohlen; ohne ihn gilt `protokolldienst.<Dokument>.<Fassung>`. Dieselbe Fassung mit anderem
  Inhalt ergibt 422. Eine neuere Fassung ersetzt offene Vorschläge desselben TOP.
- Die Quittung (201) nennt Eingangsnummer, Eingangszeit, Inhalts-Hash und die Zahl der Vorschläge.

**Vorschläge ändern die Niederschrift nicht.** Sobald eine Niederschrift angelegt ist, erscheinen sie auf ihrer Seite
unter „Vorschläge des Protokolldienstes“ (nichtöffentliche TOPs nur mit dem NÖ-Recht), gekennzeichnet als mit KI
erstellt ([KI.md](KI.md)). Mit dem Recht „Protokolle bearbeiten“ und solange die Niederschrift im Entwurf oder in der
Prüfung ist:

- **Übernehmen** schreibt den Entwurf als Protokolltext und den Beschlusstext in den TOP, beim nichtöffentlichen TOP in
  die nichtöffentlichen Felder. Das Abstimmungsergebnis nur, solange noch keins festgestellt ist, und mit denselben
  Prüfungen wie beim Bearbeiten (Zahl der Stimmberechtigten, Ergebnisregel des Landesprofils, Sperre bei
  Zugeschalteten); bei offener oder namentlicher Abstimmung bleiben die Summen aus den Einzelstimmen. Vorhandene Texte
  ersetzt die Übernahme. Unter der Sperre von Sitzung und Niederschrift, wie Cockpit und Bearbeiten.
- **Verwerfen** lässt die Niederschrift unverändert.

Beides steht im Prüfprotokoll des TOP, ohne Inhalte. Die Texte der Vorschläge sind mit dem Mandantenschlüssel
verschlüsselt, auch bei öffentlichen TOPs. Nach der Entscheidung, und wenn eine neuere Fassung einen offenen Vorschlag
ersetzt, löscht Session die Texte; es bleiben Fassung, Zeitpunkt und wer entschieden hat.
