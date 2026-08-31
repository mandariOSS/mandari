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
| Fehlende Rückmeldung zur Sitzung (nach Ladungsversand) | eingeladene Person | 5 Tage |
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
