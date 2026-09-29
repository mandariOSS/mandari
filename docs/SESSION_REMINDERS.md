# Session: Fristen-Erinnerungen (Issue #83)

Der Sitzungsdienst wird per E-Mail an ablaufende Fristen erinnert. Der Lauf ist
idempotent: jede Erinnerung wird pro Objekt und Frist genau einmal versendet
(Protokoll in `session_reminder_logs`).

## Erinnerungstypen

| Typ | Empfänger | Standard-Vorlauf |
|-----|-----------|------------------|
| Ladungsfrist läuft ab | Benutzer mit `edit_meetings` | 3 Tage |
| Ladungsfrist verstrichen | Benutzer mit `edit_meetings` | sofort |
| Vorlagenfrist läuft ab (Status Entwurf/In Prüfung) | Benutzer mit `edit_papers` | 3 Tage |
| Erinnerung zur Ladung (nach Ladungsversand, siehe unten) | eingeladene Person (mit persönlichem Rückmeldelink; nicht bei Zustellweg Brief) | 5 Tage |
| Wiedervorlage Beschlusskontrolle (Frist naht/überfällig) | Benutzer mit `edit_meetings` | 7 Tage |

Vorlaufzeiten und An/Aus je Typ konfiguriert jeder Mandant unter
**Einstellungen → Fristen-Erinnerungen**. Wird eine Erledigungsfrist in der
Beschlusskontrolle verschoben, wird für die neue Frist erneut erinnert.

## Betrieb

Täglicher Lauf (z. B. Cron auf dem Host, werktags morgens):

```bash
# alle aktiven Mandanten
docker compose exec -T mandari python manage.py send_session_reminders

# nur ein Mandant / Testlauf ohne Versand
docker compose exec -T mandari python manage.py send_session_reminders --tenant stadt-musterstadt
docker compose exec -T mandari python manage.py send_session_reminders --dry-run
```

Beispiel-Crontab (07:00 Uhr, Container `mandari`):

```cron
0 7 * * * cd /opt/mandari && docker compose exec -T mandari python manage.py send_session_reminders >> /var/log/mandari-reminders.log 2>&1
```

Mehrfaches Ausführen am selben Tag erzeugt keine doppelten E-Mails.

## Erinnerung zur Ladung (Issues #225, #619)

Pflicht ist nur die Ladung; die Anwesenheit wird vor Ort per Unterschrift nachgewiesen. Ob und
wie an Rückmeldungen erinnert wird, stellt deshalb jeder Mandant unter **Einstellungen →
Fristen-Erinnerungen → Erinnerung zur Ladung** ein:

| Einstellung | Möglichkeiten | Standard |
|-------------|---------------|----------|
| An/aus | Aus: weder täglicher Lauf noch Knopf; die Ladung selbst bleibt unberührt | an |
| Anlass | Zu- oder Absage fehlt · Empfangsbestätigung fehlt (und keine Rückmeldung) · eines von beiden fehlt | Zu- oder Absage fehlt |
| Empfängerkreis | alle Geladenen · Mitglieder der Gremien (ohne Gäste) · nur stimmberechtigte Mitglieder und ihre Vertretungen | alle Geladenen |
| Zeitpunkte | bis zu drei, z. B. „7, 2“ Tage vor der Sitzung | 5 |

**Eine Regel für beide Wege** (`apps/session/services/rsvp_reminders.py`): Der tägliche Lauf und
der Knopf „Jetzt erinnern“ in der Übersicht *Sitzung → Ladung → Rückmeldungen* erinnern dieselben
Personen. Der Knopf sendet sofort (ohne Zeitpunkte); die Übersicht nennt Anlass, Empfängerkreis
und die Zahl der zu Erinnernden. Die Mail nennt, was fehlt, und enthält den persönlichen
Rückmeldelink aus dem jüngsten Versand; Personen nur auf der Anwesenheitsliste (ohne Versand)
erhalten eine Textmail mit Verweis auf den Sitzungsdienst.

**Zeitpunkte im täglichen Lauf:** Je Person und Zeitpunkt wird höchstens einmal erinnert. Fällig
ist der kleinste Zeitpunkt, den die Sitzung erreicht hat – bei „7, 2“ ab sieben Tagen vorher die
erste, ab zwei Tagen die zweite Erinnerung; wird die Ladung erst spät versandt, geht nur die
jeweils fällige raus. Erinnerungen aus der Zeit vor den Zeitpunkten gelten für den ersten weiter.

Gegenüber früher gilt die Regel jetzt auch für den Knopf: Mit dem Standard-Anlass erinnert er alle
ohne Zu- oder Absage (bisher nur, wer auch den Erhalt nicht bestätigt hatte); das frühere
Verhalten des Knopfs entspricht dem Anlass „Empfangsbestätigung fehlt“.

Für die automatische Erinnerung genügt der bestehende Cron-Eintrag oben; ein zusätzlicher ist
nicht nötig.
