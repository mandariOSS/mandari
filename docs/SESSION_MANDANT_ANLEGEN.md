# Mandant anlegen, deaktivieren und Bürgerportal je Körperschaft (Session)

Stand: 09/2026 · Issue #317 (Teil A)

Eine mandari-Installation kann das Ratsinformationssystem für mehrere Körperschaften zugleich
betreiben – etwa für die Bezirke eines Stadtstaats. Jede Körperschaft ist ein eigener
Session-Mandant mit eigenen Gremien, eigenem Nummernkreis und eigenem Schlüssel für verschlüsselte
Felder. Diese Notiz beschreibt, wie Sie einen Mandanten anlegen, wie das Deaktivieren auf das
Bürgerportal wirkt und wie jede Körperschaft ihren eigenen Einstieg ins Bürgerportal bekommt.

In NRW sind Bezirksvertretungen dagegen Gremien innerhalb einer Stadt (§ 37 GO NRW): dort legen
Sie einen Mandanten für die Stadt an und die Bezirksvertretungen als Gremien.

## Mandant anlegen

Ein Aufruf macht einen Mandanten arbeitsfähig:

| Schritt | Ergebnis |
|---|---|
| Stammdaten | Name, URL-Kürzel (Slug), optional Kurzname, Körperschaftstyp und Amtlicher Gemeindeschlüssel (AGS) |
| Standardrollen | Administrator, Sachbearbeiter, Protokollant, Lesezugriff sowie die Kontrollrollen Revision und Datenschutz (#221) |
| Nummernkreis | Preset aus `docs/SESSION_NUMMERNKREISE.md`, z. B. `stadtstaat_bezirk` (Drucksache `22-0001`) oder `nrw_verwaltung_politik` |
| Wahlperiode | aktuelle Wahlperiode mit Name, Beginn, Ende und – für Nummern je Wahlperiode – ihrer Nummer |
| Landesprofil | aus dem Profil, wenn der Mandant noch keines hat (z. B. `NI`, Issue #757) |
| Gremien | optional aus einer Gremienvorlage, z. B. Rat, Hauptausschuss, Finanzausschuss – mit gesetzlicher Ausschussart, Ladungsfrist, Vorbelegung der Geschäftsordnung und Standard-TOPs |
| Administrator | vorhandenes Konto wird Mitglied mit der Rolle Administrator; sonst Einladung per E-Mail |
| Schlüssel | Schlüssel für verschlüsselte Felder, eingepackt mit dem Hauptschlüssel |

Körperschaftstyp und AGS erscheinen in der OParl-API des Mandanten als `classification` und
`ags` des Body (ohne Angabe wie bisher `classification: "Kommune"`).

Ein neuer Mandant startet mit **gesperrter OParl-Schnittstelle** (404): Testdaten und Schulung
bleiben intern, bis die Verwaltung die Schnittstelle unter Einstellungen → OParl-Schnittstelle
freischaltet (Issue #319, `docs/SESSION_OPARL_API.md`). Erst danach lässt sich im Bürgerportal
veröffentlichen.

### Befehl

```bash
# Profile, Gremienvorlagen, Nummernkreis-Presets und Körperschaftstypen anzeigen
python manage.py session_create_tenant --list-presets

# Bezirk eines Stadtstaats nach Profil – zuerst als Prüflauf, dann ohne --dry-run
python manage.py session_create_tenant --profile stadtstaat_bezirk \
    --name "Bezirksversammlung Musterbezirk" --slug musterbezirk \
    --admin-email sitzungsdienst@example.org --dry-run

# Stadt in NRW mit AGS, eigener Wahlperiode und ohne Gremienvorlage
python manage.py session_create_tenant --profile nrw_stadt \
    --name "Stadt Musterstadt" --slug musterstadt --ags 05999000 \
    --admin-email rat@example.org \
    --term-name "Wahlperiode 2025–2030" --term-start 2025-11-01 --term-end 2030-10-31 \
    --no-committees
```

| Option | Bedeutung |
|---|---|
| `--name`, `--slug`, `--admin-email` | Pflicht. Der Slug besteht aus Kleinbuchstaben, Ziffern und Bindestrichen; `static`, `api`, `health`, `invite` und `leitstelle` sind reserviert (dieselbe Liste wie `RESERVED_SLUGS` in `apps/session/middleware.py`). |
| `--profile` | Profil aus der Preset-Datei; liefert Körperschaftstyp, Nummernkreis, Wahlperiode und Gremienvorlage als Vorbelegung. |
| `--short-name`, `--kind`, `--ags` | Kurzname (höchstens 50 Zeichen), Körperschaftstyp (z. B. `stadt`, `bezirk`, `gemeinde`), AGS mit 2, 3, 5 oder 8 Ziffern. |
| `--numbering` | Nummernkreis-Preset, überschreibt das Profil. |
| `--term-name`, `--term-number`, `--term-start`, `--term-end` | Eigene Wahlperiode (Datum als JJJJ-MM-TT). Mit `--term-name` gelten nur die ausdrücklichen Angaben; ohne ergänzen `--term-number`, `--term-start` und `--term-end` das Profil. |
| `--committees`, `--no-committees` | Andere Gremienvorlage bzw. keine Gremien. |
| `--preset-file` | Eigene Preset-Datei im Aufbau der mitgelieferten. |
| `--dry-run` | Prüflauf: Alle Schritte laufen und werden zurückgerollt; keine E-Mail. |

### Profile und Gremienvorlagen

Die mitgelieferte Datei `mandari/apps/session/presets/mandanten.json` enthält:

| Profil | Körperschaftstyp | Nummernkreis | Wahlperiode | Gremien |
|---|---|---|---|---|
| `nrw_stadt` | Stadt | `nrw_verwaltung_politik` (`1344/2026`, `AN/1492/2026`) | „Wahlperiode 2025–2030“, 01.11.2025 bis 31.10.2030 | Rat, Hauptausschuss, Finanzausschuss |
| `stadtstaat_bezirk` | Bezirk | `stadtstaat_bezirk` (`22-0593`) | „22. Wahlperiode“ (Nr. 22), 09.06.2024 bis 30.06.2029 | Bezirksversammlung, Hauptausschuss |
| `ni_samtgemeinde` | Samtgemeinde | `standard` | „Wahlperiode 2026–2031“, 01.11.2026 bis 31.10.2031 | Samtgemeinderat, Samtgemeindeausschuss, Finanzausschuss |
| `ni_mitgliedsgemeinde` | Mitgliedsgemeinde | `standard` | wie oben | Rat, Verwaltungsausschuss |
| `ni_mitgliedsgemeinde_ohne_va` | Mitgliedsgemeinde | `standard` | wie oben | Rat |
| `ni_mitgliedsgemeinde_gd`, `ni_mitgliedsgemeinde_ohne_va_gd` | Mitgliedsgemeinde mit Gemeindedirektor | `standard` | wie oben | Rat (und Verwaltungsausschuss); Niederschrift unterzeichnet die Gemeindedirektorin bzw. der Gemeindedirektor mit |
| `ni_stadt_ortsraete` | Stadt | `standard` | wie oben | Rat, Verwaltungsausschuss, Finanzausschuss, Ortsrat (Ortschaft benennen) |
| `ni_landkreis` | Landkreis | `standard` | wie oben | Kreistag, Kreisausschuss, Finanzausschuss, Jugendhilfeausschuss |

Die Profile für Niedersachsen (Issue #757) setzen das Landesprofil `NI`, legen den Hauptausschuss mit der
gesetzlichen Ausschussart „Hauptausschuss“ und seinem Namen nach Körperschaftstyp an (§ 7 Abs. 2 NKomVG),
belegen die Geschäftsordnung der Körperschaft vor (Ladungsfrist 7 Kalendertage ab Bereitstellung, Eilfall 3 Tage
mit Pflichthinweis, Anträge 14 und Anfragen 5 Tage vorher, Unterzeichnende der Niederschrift, Abstimmungsarten,
öffentliche Ausschüsse, Einwohnerfragestunde 30 Minuten) und legen im Rat den Standard-TOP
„Einwohnerfragestunde“ an. Die Werte sind eine Vorbelegung nach dem Muster des NLT und verbreiteter Praxis;
bitte an die örtliche Geschäftsordnung anpassen (Einstellungen → Sitzungsformate → Ortsrecht).

**Weitere Körperschaften:** Führt die Verwaltung mehrere Körperschaften (z. B. eine Samtgemeinde mit ihren
Mitgliedsgemeinden), lässt sich beim Anlegen einer Körperschaft unter Einstellungen → Körperschaften dieselbe
Gremienvorlage wählen: Gremien, Geschäftsordnung und Standard-TOPs entstehen dann in dieser Körperschaft.

**Konstituierende Sitzung:** Für Mandanten mit Landesprofil `NI` bietet die Tagesordnung einer noch nicht
geladenen Sitzung die Vorlagen „Konstituierende Sitzung der Vertretung“ und „… des Rates einer Mitgliedsgemeinde“
(`mandari/apps/session/presets/tagesordnungen.json`): Verpflichtung, Wahl des Vorsitzes und der Stellvertretungen
(als Wahl gekennzeichnet), Fraktionen und Gruppen, Hauptausschuss, Ausschüsse, Stellvertretungen der bzw. des
HVB, Geschäftsordnung. Die Sitzung wird dabei auf Präsenz gestellt, weil jedes Mitglied eine geheime Wahl verlangen
kann (§ 67 Satz 2 NKomVG) und diese mit Zugeschalteten unzulässig wäre. Der Assistent für den
Wahlperiodenwechsel (#155) baut darauf auf.

Die Daten der Wahlperioden sind Vorschläge; bitte prüfen Sie sie vor dem Anlegen. Eine eigene
Datei hat denselben Aufbau:

```json
{
  "profile": {
    "gemeinde_klein": {
      "bezeichnung": "Kleine Gemeinde",
      "koerperschaftstyp": "gemeinde",
      "nummernkreis": "gemeinde",
      "wahlperiode": {"name": "Wahlperiode 2025–2030", "nummer": null, "beginn": "2025-11-01", "ende": "2030-10-31"},
      "gremienvorlage": "rat"
    }
  },
  "gremienvorlagen": {
    "rat": {"bezeichnung": "Gemeinderat", "gremien": [{"name": "Gemeinderat", "kurzname": "GR", "art": "council"}]}
  }
}
```

Erlaubte Gremienarten: `council`, `committee`, `local_council` (Ortsrat bzw. Stadtbezirksrat), `advisory`,
`youth_council` (Jugendbeteiligungsgremium), `commission`, `department`, `faction`, `group` (Gruppe), `other`.
Je Gremium optional `ausschussart` (`main`, `finance`, `audit`, `special` für Ausschüsse nach besonderen
Rechtsvorschriften, `ordinary`) und `ladungsfrist` (Tage). Je Gremienvorlage optional `geschaeftsordnung`
(Schlüssel wie im Ortsrecht, z. B. `invitation_days`, `question_days`, `minutes_signers`) und `standard_tops`
(`name`, `art` z. B. `residents_questions`, `gremium` als Gremienart). Je Profil optional `landesprofil`
(Länderkürzel).

### Erster Administrator

- **Konto vorhanden** (E-Mail ohne Rücksicht auf Groß- und Kleinschreibung): Das Konto wird
  Mitglied des Mandanten mit der Rolle Administrator; die Rollenvergabe steht im Protokoll.
- **Kein Konto:** Es entsteht eine Einladung über den bestehenden Einladungsweg (#239) mit der
  Rolle Administrator, gültig sieben Tage. Die Person bekommt den Link nur per E-Mail. Scheitert
  der Versand, meldet der Befehl das; die Einladung lässt sich im Sitzungsdienst unter
  Einstellungen → Benutzer erneut senden.

Der Befehl und der Assistent geben nie ein Passwort, einen Einladungslink oder ein Token aus.
Administratoren richten bei der ersten Anmeldung einen zweiten Faktor ein, sofern die
Zwei-Faktor-Pflicht aktiv ist (`docs/ACCOUNT_SECURITY.md`).

### Erneuter Lauf und Prüflauf

Ein zweiter Lauf mit demselben Slug ergänzt, was fehlt: fehlende Standardrollen (auch bei
Bestandsmandanten, denen Revision und Datenschutz noch fehlen), leere Stammdaten, Gremien der
Vorlage, den Schlüssel. Bestehendes bleibt unverändert, der Name eines vorhandenen Mandanten
ebenso. Angeglichen werden nur das Nummernkreis-Preset (vergebene Nummern bleiben, siehe
`docs/SESSION_NUMMERNKREISE.md`) und Nummer, Beginn und Ende der genannten Wahlperiode. Ist eine
Einladung noch offen, entsteht keine zweite.

Jeder Lauf schreibt einen Eintrag ins Protokoll des Mandanten (Hash-Kette, #221) mit Anlass,
Nummernkreis, Wahlperiode, Gremienvorlage und dem Weg zum Administrator.

### Admin-Assistent

Im Django-Admin unter **Session RIS → Mandant anlegen** (`/admin/session/sessiontenant/anlegen/`).
„Hinzufügen“ in der Mandantenliste führt ebenfalls dorthin; das bisherige Formular legte Mandanten
ohne Rollen an. Schritt 1 belegt die Angaben aus einem Profil vor, Schritt 2 zeigt alle Angaben
zum Prüfen; „Nur prüfen“ entspricht `--dry-run`. Der Assistent nutzt denselben Service wie der
Befehl und steht Staff-Konten mit dem Recht „Session-Mandant hinzufügen“ offen (Superuser immer).

## Mandant deaktivieren und reaktivieren

Ein deaktivierter Mandant ist im Sitzungsdienst und über seine OParl-API nicht mehr erreichbar.
Zusätzlich nimmt mandari seine **Bürgerportal-Quelle** zurück:

1. Die Quelle (seine eigene OParl-API) wird inaktiv, der Ingestor ruft sie nicht mehr ab.
2. Die Kommune erscheint nicht mehr in Auswahl, Listen und Sitemaps; ihr Einstieg
   `/insight/k/<slug>/` antwortet mit 404.
3. Alle gespiegelten Einträge (Sitzungen, TOPs, Vorlagen, Anlagen, Beratungen, Gremien,
   Personen, Mitgliedschaften, Wahlperioden) werden wie bei einer Ö→NÖ-Umstellung
   zurückgenommen: Sie verschwinden aus Suche und Listen, Detailseiten zeigen keinen Inhalt.

Das Reaktivieren stellt genau diesen Stand wieder her, sofern der Mandant im Bürgerportal
veröffentlicht: Quelle aktiv, Kommune wieder gelistet (wenn sie es vorher war), Einträge zurück –
außer denen, die in Session inzwischen gelöscht oder nichtöffentlich sind. Ist die
Veröffentlichung aus, bleibt die Rücknahme bestehen, bis der Mandant wieder veröffentlicht.

Die OParl-Schnittstelle folgt beim Reaktivieren ihrer Freischaltung (Issue #319): Mandanten, die bei
der Einführung der Freischaltung inaktiv waren und nicht im Bürgerportal veröffentlichten, sind
gesperrt – anders als vor ihrer Deaktivierung antwortet ihre Schnittstelle mit 404, bis die
Verwaltung sie freischaltet. Admin-Aktion und Formular melden das, das Protokoll des Mandanten
vermerkt es beim Eintrag „Aktiv“.

Wege: Admin-Aktionen „Mandanten deaktivieren“ bzw. „aktivieren“ in der Mandantenliste und der
Schalter „Aktiv“ im Formular. Beide speichern jeden Mandanten einzeln; das Protokoll des
Mandanten nennt, wer gehandelt hat (Änderung von „Aktiv“ sowie „Veröffentlichung
zurückgenommen“ bzw. „Veröffentlicht“ mit Anzahl der Einträge). Mandanten, die vor dieser
Änderung per Sammelaktion deaktiviert wurden, lassen sich mit der Aktion „deaktivieren“
nachträglich zurücknehmen. Bei sehr großen Datenbeständen dauert das einige Zeit, weil jeder
Eintrag einzeln gespeichert und aus der Suche entfernt wird.

## Bürgerportal je Körperschaft

### Einstieg

Jede gelistete Kommune hat einen eigenen Einstieg:

```
https://<host>/insight/k/<slug>/
```

`<slug>` ist der Slug der Kommune im Bürgerportal. Für Session-Mandanten funktioniert auch ihr
Mandanten-Slug; er führt auf die Kommune ihrer eigenen OParl-Quelle (nach Veröffentlichung und
erstem Abgleich).

Kommunen aus fremden Ratsinformationssystemen bekommen ihren Slug im Admin (Feld „Slug“) oder
gesammelt per Befehl – mit eindeutiger Zuordnung über die ID, idempotent:

```bash
python manage.py set_body_slugs                                  # gelistete Kommunen, IDs, Vorschlag
python manage.py set_body_slugs <id>=muenster <id>=koeln --dry-run
python manage.py set_body_slugs <id>=muenster <id>=koeln
```

Erlaubt sind Kleinbuchstaben, Ziffern und einzelne Bindestriche; `index` ist reserviert (Sitemap-Index),
ebenso Slugs, die auf `-vorgaenge-<Zahl>` oder `-sitzungen-<Zahl>` enden (nummerierte Sitemaps).
Der Slug eines Session-Mandanten bleibt dessen Portal: Admin und Befehl lehnen ihn für eine andere
Kommune ab. Einen schon gesetzten Slug ersetzt der Befehl nur mit `--replace`, weil alte Links dann ins
Leere führen. Das Verzeichnis der Kommune im Dokument-Cache ändert sich mit dem Slug nicht; wo es als
Adresse passt, den Slug trotzdem gleich dem Cache-Verzeichnis wählen (Rückfall, siehe
`docs/FILE_CACHE.md`). Der Sitemap-Index (`/sitemap-insight-index.xml`) führt jede gelistete Kommune,
ohne Slug unter ihrer ID: die Grund-Sitemap (`/sitemap-insight-<slug>.xml` mit Stadtseite, Gremien,
Personen mit laufender Mitgliedschaft und Ratsfragen) und alle Vorgänge und Sitzungen in nummerierten
Dateien mit höchstens 10.000 Einträgen (`/sitemap-insight-<slug>-vorgaenge-1.xml`,
`/sitemap-insight-<slug>-sitzungen-1.xml` …).

Im Einstieg gilt:

- **Name und Logo der Körperschaft** in Seitenleiste, Titel und Startseite. Name: Anzeigename der
  Kommune (sonst Kurzname, sonst Name). Logo: Logo der Kommune, sonst das Logo des
  Session-Mandanten.
- **Akzentfarbe**, sofern gesetzt: Feld „Akzentfarbe im eigenen Portal“ der Kommune im Admin,
  sonst die Primärfarbe des Session-Mandanten. Nur Werte im Format `#rrggbb` werden ausgegeben.
- **Kommunenauswahl festgelegt:** Kein Wechsel der Kommune, alle Listen, die Suche und die
  Karte zeigen nur diese Körperschaft.
- **Links bleiben im Kontext:** Der Einstieg hält die Körperschaft für die Sitzung des Browsers
  fest; „Übersicht“ führt zurück zum Einstieg. Über „Alle Kommunen im Bürgerportal“ oder die
  Auswahl einer anderen Kommune verlassen Besucher den Einstieg.
- **Direkte Links auf Portalseiten:** `/insight/k/<slug>/termine/` hält die Körperschaft fest und
  führt auf `/insight/termine/`. Zulässig sind nur die öffentlichen Portalseiten (Übersicht,
  Sitzungen, Vorgänge, Gremien, Personen, Beschlüsse, Ratsfragen, Dokumente, Suche, Karte,
  Nachbarschaft, Merkliste, Benachrichtigungen, Chat). Parameter werden nie weitergegeben.

Nicht gelistete Kommunen (z. B. Demo und Piloten) haben keinen Einstieg. Detailseiten anderer
Kommunen bleiben über ihre direkte Adresse erreichbar, wie im gemeinsamen Portal.

### Eigener Host

Optional ordnet die Umgebungsvariable `PORTAL_HOSTS` einem eigenen Hostnamen fest eine
Körperschaft zu:

```bash
PORTAL_HOSTS=ratsinfo.bezirk-nord.example=bezirk-nord,ratsinfo.bezirk-sued.example=bezirk-sued
```

- Der Host muss zusätzlich in `ALLOWED_HOSTS` stehen. Andere Hosts wertet mandari nicht aus; ein
  Host aus `PORTAL_HOSTS` ohne Eintrag in `ALLOWED_HOSTS` bleibt wirkungslos, Anfragen dafür
  weist Django ab. `python manage.py check` meldet solche Einträge (`insight_core.W001`).
- Auf dem Host führt `/` zum Portal; die Körperschaft lässt sich dort nicht verlassen.
- Ist die Kommune nicht gelistet (etwa weil der Mandant deaktiviert ist), antworten die
  Portalseiten des Hosts mit 404 – der Host zeigt nie das gemeinsame Portal.

Einrichtung auf der Serverseite (nicht Teil des Codes):

1. **DNS:** Einen Eintrag für den Hostnamen auf den Reverse Proxy der Installation setzen, bei
   einer eigenen Domain der Körperschaft in deren DNS.
2. **Zertifikat:** Ein TLS-Zertifikat für den Hostnamen beziehen, z. B. automatisch über den
   Reverse Proxy (ACME).
3. **Reverse Proxy:** Den Host wie den Haupthost auf den mandari-Webdienst leiten, einschließlich
   `/static/` und `/media/` (Logos). Der Proxy reicht den Host-Header unverändert durch und setzt
   `X-Forwarded-Proto`.
4. **Umgebung des Webdienstes:** Den Host in `ALLOWED_HOSTS` ergänzen und `PORTAL_HOSTS` setzen
   (bei Compose-Installationen z. B. über eine Override-Datei), danach den Dienst neu starten.
   Nur falls ein vorgeschalteter Dienst den Origin umschreibt, zusätzlich `CSRF_TRUSTED_ORIGINS`.

Anmeldungen und Merklisten gelten je Host (Cookies ohne gemeinsame Domain).

## Sicherheit

- Anlegen nur über den Befehl oder den Admin-Assistenten (Staff mit Anlegerecht, Admin-Netze und
  zweiter Faktor wie im übrigen Admin).
- Keine Passwörter, Tokens oder Einladungslinks in Ausgaben, Meldungen oder Protokollen;
  Fehlermeldungen entstehen aus festen Texten, Ausnahmen stehen nur im Betriebsprotokoll.
- Mandantentrennung: Rollen, Einladung und Gremien gehören ausschließlich zum neuen Mandanten;
  die Rücknahme betrifft nur die eigene OParl-Quelle des Mandanten, nie verknüpfte fremde Quellen.
- Einstiege zeigen nur gelistete Kommunen und dieselben öffentlichen Daten wie das gemeinsame
  Portal. Weiterleitungen gehen nur auf geprüfte, relative Portalpfade.
- Der Host-Header wird nur über `request.get_host()` und nur für Hosts aus `ALLOWED_HOSTS`
  ausgewertet.

## Technische Umsetzung

| Teil | Code |
|---|---|
| Service Anlegen, Deaktivieren, Reaktivieren | `mandari/apps/session/services/tenant_provisioning.py` |
| Rücknahme und Wiederherstellung im Bürgerportal | `mandari/apps/session/services/insight_service.py` (`retract_source`, `restore_source`) |
| Befehl | `mandari/apps/session/management/commands/session_create_tenant.py` |
| Admin-Assistent | `mandari/apps/session/admin_provisioning.py`, Vorlage `templates/admin/session/sessiontenant/provision.html` |
| Presets | `mandari/apps/session/presets/mandanten.json` |
| Signal (auch Formular und `save()`) | `mandari/apps/session/signals.py` (`tenant_publication_post_save`) |
| Portal-Kontext, Host-Middleware, Systemprüfung | `mandari/insight_core/portal.py` |
| Einstieg | `mandari/insight_core/views/portal.py` |

Tests: `apps/session/tests/test_mandant_anlegen.py`, `apps/session/tests/test_mandant_deaktivieren.py`,
`insight_core/tests/test_portal_koerperschaft.py`.

Mandantengruppen mit Leitstelle und gemeinsame Sitzungen (Teil B von #317) beschreibt
`docs/SESSION_LEITSTELLE.md`. Ein deaktivierter Mandant erscheint nicht mehr in der
Leitstellen-Übersicht und -Suche seiner Gruppe.
