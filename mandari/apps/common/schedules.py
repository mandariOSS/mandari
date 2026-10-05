# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wiederkehrende Verwaltungsbefehle als Zeitpläne im Worker (Issue #516) – die frühere Host-Crontab.

Jeder Eintrag ruft ``manage.py <befehl>`` zu denselben Zeiten wie die bisherige Beispiel-Crontab in
``DEPLOYMENT.md`` (und in ``docs/FILE_CACHE.md``), als eigenen Prozess im Worker
(``apps.events.verwaltungsbefehle``). Zeiten in ``TIME_ZONE``. Ein verpasster Termin wird einmal
nachgeholt. Abschalten je Befehl: ``EVENTS_SCHEDULES_DISABLED=befehl:<name>``.

Die Liste steht an einer Stelle, damit sie sich mit einer bestehenden Crontab vergleichen lässt; die
Befehle selbst gehören den Fachmodulen. Auf dem Host bleiben nur Aufgaben des Betriebssystems
(Datensicherung, Journal-Alarm, Neustart ungesunder Container).

Dazu ``postausgang_aufraeumen``: täglich um 03:55 Uhr Zeilen des Postausgangs des Mail-Dienstes nach
ihrer Frist löschen (``apps.common.mail.outbox.purge``, Issue #528).
"""

from __future__ import annotations

from django.conf import settings
from django.tasks import task

from apps.events.schedule import ScheduleRegistry, cron
from apps.events.verwaltungsbefehle import befehl_als_zeitplan


def registrieren(ziel: ScheduleRegistry | None = None) -> None:
    """Registriert die Zeitpläne nach den aktuellen Einstellungen (ohne ``ziel``: Register des Prozesses)."""
    # Erinnerungen und Pflege
    befehl_als_zeitplan("send_session_reminders", crontab="0 7 * * *", zeitgrenze=1800, ziel=ziel)
    befehl_als_zeitplan("send_task_due_reminders", crontab="15 7 * * *", zeitgrenze=1800, ziel=ziel)
    befehl_als_zeitplan("send_question_reminders", crontab="30 7 * * *", zeitgrenze=1800, ziel=ziel)
    befehl_als_zeitplan("fetch_person_photos", crontab="0 3 * * 1", zeitgrenze=3000, ziel=ziel)
    # Amtliche Umringe von Bebauungsplänen abrufen und Vorlagen zuordnen (Issue #598)
    befehl_als_zeitplan("sync_plan_boundaries", crontab="50 4 * * *", zeitgrenze=3000, ziel=ziel)
    # Verwaiste Konten nach Frist löschen (Issue #238)
    befehl_als_zeitplan("cleanup_orphaned_accounts", crontab="45 3 * * *", zeitgrenze=1800, ziel=ziel)

    # Dokument-Cache (docs/FILE_CACHE.md): stündlich die neuesten fehlenden Dateien nachladen. Die
    # Zeitgrenze liegt unter dem Abstand, damit ein abgebrochener Lauf den nächsten nicht sperrt.
    befehl_als_zeitplan("cache_files", crontab="40 * * * *", argumente=["--limit", "400"], zeitgrenze=3000, ziel=ziel)
    # Löschabgleich mit den Quellen (Issue #787): stündlich; Zeitgrenze und Sperre wie bei cache_files
    befehl_als_zeitplan("loeschabgleich", crontab="15 * * * *", zeitgrenze=3000, ziel=ziel)
    # Dokumentablage (Issue #788): verwaiste Inhalte löschen, Zwischenspeicher begrenzen. Mit Objektspeicher
    # (und Ablage nach SHA-256) vorher hochladen; den Zwischenspeicher verlassen nur hochgeladene Inhalte.
    ablage = ["--aufraeumen"]
    layout = str(getattr(settings, "FILE_STORE_LAYOUT", "") or "").strip().lower()
    if getattr(settings, "OBJ_ENABLED", False) and layout != "kommune":
        ablage.insert(0, "--hochladen")
    befehl_als_zeitplan("dokumentablage", crontab="50 * * * *", argumente=ablage, zeitgrenze=3000, ziel=ziel)

    # Abos zu Themen und Orten im Bürgerportal (Issue #460): nur, wenn sie eingeschaltet sind. Beide
    # Befehle sind wiederholbar (Benachrichtigung je Abonnent und Vorgang einmal).
    if getattr(settings, "INSIGHT_SUBSCRIPTIONS_ENABLED", False):
        befehl_als_zeitplan("generate_alerts", crontab="45 7 * * *", zeitgrenze=1800, ziel=ziel)
        befehl_als_zeitplan("send_digest", crontab="0 8 * * 1", zeitgrenze=1800, ziel=ziel)

    # Betrieb (Issue #231): Quellen stündlich, Service-Level täglich
    befehl_als_zeitplan("check_source_health", crontab="15 * * * *", zeitgrenze=600, ziel=ziel)
    befehl_als_zeitplan("check_service_levels", crontab="30 6 * * *", zeitgrenze=600, ziel=ziel)
    # Verfügbarkeitsbericht des Vormonats nach REPORTS_ROOT; nur mit Statusseite, sonst endete jeder
    # Lauf mit Fehler
    if getattr(settings, "GATUS_URL", ""):
        befehl_als_zeitplan(
            "availability_report",
            crontab="15 0 1 * *",
            argumente=["--out", f"{settings.REPORTS_ROOT}/"],
            zeitgrenze=600,
            ziel=ziel,
        )

    # Protokollierung (Issue #221): Hash-Ketten täglich prüfen (ein Befund lässt den Auftrag scheitern
    # und erscheint in mandari_tasks_dead), Sicherheitsprotokoll nach Frist löschen, DSGVO-Löschlauf
    befehl_als_zeitplan("verify_audit_chain", crontab="20 4 * * *", zeitgrenze=3000, ziel=ziel)
    befehl_als_zeitplan("purge_security_audit_log", crontab="40 4 * * *", zeitgrenze=1800, ziel=ziel)
    befehl_als_zeitplan("session_privacy_purge", crontab="0 5 1 * *", zeitgrenze=3000, ziel=ziel)

    # Sitzungsmappen (Issue #218): Anforderungen aus der Oberfläche minütlich abarbeiten
    befehl_als_zeitplan(
        "build_meeting_packages",
        minuten=1,
        argumente=["--limit", "5", "--max-seconds", "240"],
        zeitgrenze=300,
        ziel=ziel,
    )


registrieren()


@cron("55 3 * * *")
@task
def postausgang_aufraeumen() -> int:
    """Löscht Zeilen des Postausgangs nach ihrer Frist (versendet 14, fehlgeschlagen 90 Tage)."""
    from apps.common.mail.outbox import purge

    return purge()
