# Provisioning-API (Betreiber)

Über die Provisioning-API legt ein vertrauenswürdiges System des Betreibers Work-Organisationen an und
hält sie aktuell, etwa ein Abrechnungs- oder ERP-System. Sie ist keine öffentliche Schnittstelle und ohne
Schlüssel vollständig abgeschaltet; bei selbst betriebenen Installationen entsteht keine Angriffsfläche.

Es gibt zwei Versionen nebeneinander:

| Version | Pfad | Schlüssel | Arbeitsweise |
|---------|------|-----------|--------------|
| v2 | `/api/provisioning/v2/work/` | `PROVISIONING_V2_API_KEY` | deklarativ: Soll-Zustand setzen, wiederholbar (Issue #1132) |
| v1 | `/api/provisioning/organizations/` | `PROVISIONING_API_KEY` | Befehle: anlegen (`POST`), ändern (`PATCH`) |

v1 bleibt unverändert, bis alle Aufrufer auf v2 umgestellt sind.

## Version 2

### Einschalten und Zugang

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
  einem anderen Proxy, der den Kopf nur ergänzt, ist die Prüfung umgehbar. Unabhängig davon gilt: Die Route
  gehört nicht ins offene Netz. Am besten erreicht nur das interne Netz `/api/provisioning/`, und der
  öffentliche Proxy sperrt den Pfad.
- CSRF-Prüfung entfällt (Server-zu-Server, Schlüssel statt Sitzung).

### Endpunkte

| Methode | Pfad | Zweck |
|---------|------|-------|
| `PUT` | `/api/provisioning/v2/work/<ref>` | Soll-Zustand setzen: anlegen, übernehmen oder ändern. Antwort 201 (angelegt) oder 200 |
| `GET` | `/api/provisioning/v2/work/<ref>` | Ist-Zustand lesen; unbekannte Referenz: 404 `{"error": "not_found"}` |
| `GET` | `/api/provisioning/v2/work/` | Alle Organisationen, auch solche ohne Referenz (`"ref": null`), für den vollständigen Abgleich |

`<ref>` ist die Kennung der Bereitstellung im aufrufenden System: 1 bis 64 Zeichen aus `A-Z`, `a-z`, `0-9`,
Punkt, Unterstrich und Bindestrich (nicht nur `.` oder `..`). mandari speichert sie an der Organisation
(`provisioning_ref`, eindeutig). Ein Schrägstrich am Ende ist erlaubt.

### Soll-Zustand (`PUT`)

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
| `status` | `aktiv`, `gesperrt`, `beendet` | Vorgabe `aktiv` | siehe Status |
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
   `admin_email` mit Administrator-Rolle, 14 Tage gültig). Antwort 201 mit `invitation_sent`.
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
zurück. Treffen zwei Anfragen mit derselben neuen Referenz gleichzeitig ein, erhält die zweite 409
`conflict`; ihre Wiederholung trifft dann den Bestand.

### Status

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

### Antwort

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

### Fehler

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
| 409 | `{"error": "conflict"}` | gleichzeitige Anfrage; wiederholen |
| 500 | `{"error": "no_system_user"}` | kein aktiver Superuser als Absender der Einladung |

### Mitglieder-Limit in Work

`member_limit` wird seit v2 durchgesetzt, wie das Gast-Limit:

- Keine neue Einladung, wenn aktive Mitglieder und offene Einladungen zusammen das Limit erreichen. Eine
  zurückgezogene Einladung gibt den Platz frei; eine abgelaufene belegt ihn erst wieder, wenn sie erneut
  gesendet wird.
- Eine Einladung lässt sich nicht annehmen, solange das Limit erreicht ist; sie bleibt gültig.
- Reaktivieren und Freischalten offener Registrierungsanfragen nur unterhalb des Limits.
- Selbstregistrierung mit sofortiger Freischaltung wird bei erreichtem Limit zur offenen Anfrage.
- Bestehende Mitglieder bleiben immer, auch über dem Limit. Gäste zählen nicht mit.
- `null` heißt unbegrenzt (Vorgabe, auch für selbst betriebene Installationen).

Die Meldungen in Work nennen das Limit und den Weg (Einladung zurückziehen, Mitglied deaktivieren oder
Limit erhöhen).

### Beispiel

```bash
curl -X PUT "https://mandari.example.org/api/provisioning/v2/work/vertrag-0815" \
  -H "Authorization: Bearer $PROVISIONING_V2_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "Fraktion Beispiel", "slug": "fraktion-beispiel", "admin_email": "vorsitz@example.org",
       "body_ags": "12345678", "party_group": "Beispielpartei", "status": "aktiv",
       "member_limit": 10, "guest_limit": 25, "billing_reference": "V-2026-001", "plan": "hosted"}'
```

### Noch nicht enthalten

- **Export** der Daten einer Organisation zum Vertragsende (`POST …/work/<ref>/export`) und
- **Löschen** nach Frist und Export (`DELETE …/work/<ref>`)

folgen in eigenen Schritten. Bis dahin endet ein Vertrag mit `beendet`; die Daten bleiben erhalten.

## Version 1

| Methode | Pfad | Zweck |
|---------|------|-------|
| `GET` | `/api/provisioning/organizations/` | Liste aller Organisationen mit `owner_email` |
| `POST` | `/api/provisioning/organizations/` | Anlegen mit Einladung; belegter Slug: 409 |
| `GET`, `PATCH` | `/api/provisioning/organizations/<slug>/` | Lesen bzw. `plan`, `is_active`, Limits, `billing_reference`, `parties` ändern |

Anlegen und Einladung laufen über denselben Code wie in v2 (`apps/provisioning/services.py`).

## Implementierung

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
