# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Betriebsmonitor: Gesundheit der OParl-Quellen und der Systemdienste.

Hintergrund: Die Bonner OParl-Quelle lieferte ab Februar 2026 nur noch HTTP 403
(Bot-Schutz). ``last_sync`` blieb stehen, aber niemand hat es bemerkt — es gab
weder eine sichtbare Bewertung im Admin noch eine Benachrichtigung. Dieses
Modul bewertet jede Quelle (ok / Warnung / kritisch), sammelt Systemchecks und
Handlungsbedarf und verschickt Alarme mit Entwarnung.
"""

import logging
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

STATUS_ORDER = {"critical": 0, "warning": 1, "never": 2, "ok": 3, "inactive": 4}
STATUS_META = {
    "ok": {"label": "OK", "color": "green"},
    "warning": {"label": "Warnung", "color": "amber"},
    "critical": {"label": "Kritisch", "color": "red"},
    "never": {"label": "Noch kein Sync", "color": "gray"},
    "inactive": {"label": "Inaktiv", "color": "gray"},
}
ALERT_REPEAT_DAYS = 7


def thresholds() -> tuple[int, int]:
    """(Warnschwelle in Stunden, kritische Schwelle in Tagen)."""
    return (
        int(getattr(settings, "INSIGHT_SOURCE_STALE_WARNING_HOURS", 48)),
        int(getattr(settings, "INSIGHT_SOURCE_STALE_CRITICAL_DAYS", 7)),
    )


def _site_url() -> str:
    return getattr(settings, "SITE_URL", "http://localhost:8000")


# =============================================================================
# Quellen
# =============================================================================


def evaluate_source(source, now=None) -> dict:
    """Bewertet eine Quelle anhand von letztem Erfolg, Fehlversuchen und Aktivität."""
    now = now or timezone.now()
    warning_hours, critical_days = thresholds()
    info = {
        "source": source,
        "status": "ok",
        "age_hours": None,
        "age_days": None,
        "last_error": source.last_error or "",
        "consecutive_failures": source.consecutive_failures or 0,
        "reasons": [],
    }

    if not source.is_active:
        info["status"] = "inactive"
    elif source.last_sync is None:
        since_created = now - (source.created_at or now)
        if since_created > timedelta(days=critical_days):
            info["status"] = "critical"
            info["reasons"].append(f"Seit Anlage vor {since_created.days} Tagen nie erfolgreich synchronisiert")
        else:
            info["status"] = "never"
            info["reasons"].append("Noch kein erfolgreicher Sync")
    else:
        age = now - source.last_sync
        info["age_hours"] = round(age.total_seconds() / 3600, 1)
        info["age_days"] = age.days
        if age > timedelta(days=critical_days):
            info["status"] = "critical"
            info["reasons"].append(f"Letzter erfolgreicher Sync vor {age.days} Tagen")
        elif age > timedelta(hours=warning_hours):
            info["status"] = "warning"
            info["reasons"].append(f"Letzter erfolgreicher Sync vor {int(age.total_seconds() // 3600)} Stunden")

    if source.is_active:
        failures = info["consecutive_failures"]
        if failures >= 3:
            info["status"] = "critical"
            info["reasons"].append(f"{failures} Fehlversuche in Folge")
        elif failures >= 1 and info["status"] in ("ok", "never"):
            info["status"] = "warning"
            info["reasons"].append("Letzter Sync-Versuch fehlgeschlagen")
        if info["last_error"]:
            when = f" ({source.last_error_at:%d.%m.%Y %H:%M})" if source.last_error_at else ""
            info["reasons"].append(f"Letzter Fehler{when}: {info['last_error']}")

    info.update(STATUS_META[info["status"]])
    return info


def collect_source_health(now=None) -> dict:
    """Alle Quellen bewertet, kritische zuerst; mit Zusammenfassung."""
    from ..models import OParlBody, OParlSource

    now = now or timezone.now()
    body_names: dict = {}
    for body in OParlBody.objects.filter(deleted=False).exclude(source__isnull=True).values("source_id", "name"):
        body_names.setdefault(body["source_id"], []).append(body["name"])

    items = []
    for source in OParlSource.objects.all().order_by("name"):
        item = evaluate_source(source, now)
        item["bodies"] = body_names.get(source.id, [])
        items.append(item)
    items.sort(key=lambda i: (STATUS_ORDER[i["status"]], i["source"].name))

    counts = Counter(i["status"] for i in items)
    if counts["critical"]:
        overall = "critical"
    elif counts["warning"] or counts["never"]:
        overall = "warning"
    else:
        overall = "ok"
    return {
        "items": items,
        "problems": [i for i in items if i["status"] in ("critical", "warning", "never")],
        "counts": dict(counts),
        "overall": overall,
        "overall_meta": STATUS_META[overall],
    }


# =============================================================================
# Systemdienste
# =============================================================================


def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail, **STATUS_META[status]}


def collect_system_health() -> list[dict]:
    """Datenbank, Cache, Elasticsearch, Ingestor-Daemon, Sync-Läufe."""
    checks = []

    # Datenbank
    try:
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        checks.append(_check("Datenbank", "ok", "PostgreSQL antwortet"))
    except Exception as exc:
        checks.append(_check("Datenbank", "critical", f"Keine Verbindung: {exc}"))

    # Datenbank-Verbindungen (Issue #86): pg_stat_activity gegen max_connections
    try:
        from django.db import connection

        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'")
                used = cursor.fetchone()[0]
                cursor.execute("SHOW max_connections")
                limit = int(cursor.fetchone()[0])
            ratio = used / limit if limit else 0
            status = "critical" if ratio >= 0.9 else "warning" if ratio >= 0.7 else "ok"
            checks.append(_check("DB-Verbindungen", status, f"{used} von {limit} belegt ({ratio:.0%})"))
        else:
            checks.append(_check("DB-Verbindungen", "inactive", "Nur für PostgreSQL"))
    except Exception as exc:
        checks.append(_check("DB-Verbindungen", "warning", f"Nicht ermittelbar: {exc}"))

    # Cache / Redis
    try:
        cache.set("monitoring-probe", "1", 10)
        if cache.get("monitoring-probe") == "1":
            checks.append(_check("Cache", "ok", "Schreiben/Lesen funktioniert"))
        else:
            checks.append(_check("Cache", "warning", "Wert nicht lesbar"))
    except Exception as exc:
        checks.append(_check("Cache", "critical", f"Fehler: {exc}"))

    # Elasticsearch
    es_url = getattr(settings, "ELASTICSEARCH_URL", "") or ""
    if es_url:
        try:
            import httpx

            response = httpx.get(es_url, timeout=2.0)
            if response.status_code == 200:
                checks.append(_check("Elasticsearch", "ok", "Erreichbar"))
            else:
                checks.append(_check("Elasticsearch", "warning", f"HTTP {response.status_code}"))
        except Exception as exc:
            checks.append(_check("Elasticsearch", "critical", f"Nicht erreichbar: {type(exc).__name__}"))
    else:
        checks.append(_check("Elasticsearch", "inactive", "Nicht konfiguriert"))

    # Dokument-Cache (Issue #87): Abdeckung + freier Speicher am Cache-Verzeichnis
    try:
        from .file_cache import cache_stats

        stats = cache_stats()
        free_gb = stats["disk_free_bytes"] / 1024**3
        if free_gb < 2:
            status = "critical"
        elif free_gb < stats["min_free_gb"]:
            status = "warning"
        else:
            status = "ok"
        checks.append(
            _check(
                "Dokument-Cache",
                status,
                f"{stats['ok']} von {stats['total']} Dokumenten lokal ({stats['coverage']} %), "
                f"{stats['cached_gb']} GB belegt, {free_gb:.0f} GB frei",
            )
        )
    except Exception as exc:
        checks.append(_check("Dokument-Cache", "warning", f"Status nicht ermittelbar: {exc}"))

    # Ingestor-Daemon + Sync-Läufe
    try:
        from insight_sync import daemon
        from insight_sync.models import SyncLog

        last_log = SyncLog.objects.order_by("-started_at").first()
        if daemon.is_ingestor_active():
            checks.append(_check("Ingestor-Daemon", "ok", "Letzter Lauf vor weniger als 30 Minuten"))
        elif last_log is None:
            checks.append(_check("Ingestor-Daemon", "warning", "Noch kein Sync-Lauf protokolliert"))
        else:
            age = timezone.now() - last_log.started_at
            status = "critical" if age > timedelta(hours=6) else "warning"
            hours = int(age.total_seconds() // 3600)
            checks.append(_check("Ingestor-Daemon", status, f"Letzter Lauf vor {hours} Stunden"))

        day_ago = timezone.now() - timedelta(hours=24)
        runs = SyncLog.objects.filter(started_at__gte=day_ago)
        total = runs.count()
        failed = runs.filter(status=SyncLog.Status.FAILED).count()
        if total == 0:
            checks.append(_check("Sync-Läufe (24 h)", "warning", "Keine Läufe in den letzten 24 Stunden"))
        elif failed == 0:
            checks.append(_check("Sync-Läufe (24 h)", "ok", f"{total} Läufe, keine Fehler"))
        elif failed < total:
            checks.append(_check("Sync-Läufe (24 h)", "warning", f"{failed} von {total} Läufen fehlgeschlagen"))
        else:
            checks.append(_check("Sync-Läufe (24 h)", "critical", f"Alle {total} Läufe fehlgeschlagen"))
    except Exception as exc:
        checks.append(_check("Ingestor-Daemon", "warning", f"Status nicht ermittelbar: {exc}"))

    return checks


# =============================================================================
# Handlungsbedarf
# =============================================================================


def collect_action_items() -> list[dict]:
    """Offene Aufgaben mit Zahl und Link in den Admin — nur belegte Einträge."""
    from django.urls import reverse

    from ..models import OParlFile, OParlPerson, PublicQuestion

    items = []

    def add(label, count, url, level="warning"):
        if count:
            items.append(
                {"label": label, "count": count, "url": url, "level": level, "color": STATUS_META[level]["color"]}
            )

    add(
        "Ratsfragen warten auf Freigabe",
        PublicQuestion.objects.filter(status="pending").count(),
        reverse("admin:insight_core_publicquestion_changelist") + "?status__exact=pending",
    )
    add(
        "Antworten warten auf Freigabe",
        PublicQuestion.objects.filter(answer_status="pending").count(),
        reverse("admin:insight_core_publicquestion_changelist") + "?answer_status__exact=pending",
    )
    add(
        "Ratsfragen seit über 14 Tagen unbeantwortet",
        PublicQuestion.objects.filter(
            status="published", answer_status="none", published_at__lt=timezone.now() - timedelta(days=14)
        ).count(),
        reverse("admin:insight_core_publicquestion_changelist") + "?status__exact=published&answer_status__exact=none",
        level="ok",
    )
    try:
        from apps.common.models import ProblemReport

        add(
            "Offene Problemmeldungen",
            ProblemReport.objects.filter(status="open").count(),
            reverse("admin:common_problemreport_changelist") + "?status__exact=open",
        )
    except Exception:  # App nicht installiert
        pass
    add(
        "Dateien mit fehlgeschlagener Textextraktion",
        OParlFile.objects.filter(text_extraction_status="failed").count(),
        reverse("admin:insight_core_oparlfile_changelist") + "?text_extraction_status__exact=failed",
        level="ok",
    )
    add(
        "Dokumente mit Cache-Fehler",
        OParlFile.objects.filter(deleted=False, local_status="error").count(),
        reverse("admin:insight_core_oparlfile_changelist") + "?local_status__exact=error",
        level="ok",
    )
    add(
        "Personenfotos mit Abruf-Fehler",
        OParlPerson.objects.filter(photo_status="error").count(),
        reverse("admin:insight_core_oparlperson_changelist") + "?photo_status__exact=error",
        level="ok",
    )
    return items


def collect_health() -> dict:
    """Gesamtbild für Dashboard und Betriebsmonitor."""
    sources = collect_source_health()
    system = collect_system_health()
    system_statuses = [c["status"] for c in system]
    if sources["overall"] == "critical" or "critical" in system_statuses:
        overall = "critical"
    elif sources["overall"] == "warning" or "warning" in system_statuses:
        overall = "warning"
    else:
        overall = "ok"
    return {
        "sources": sources,
        "system": system,
        "actions": collect_action_items(),
        "overall": overall,
        "overall_meta": STATUS_META[overall],
        "thresholds": {"warning_hours": thresholds()[0], "critical_days": thresholds()[1]},
        "generated_at": timezone.now(),
    }


# =============================================================================
# Alarmierung
# =============================================================================


def get_alert_emails() -> list[str]:
    configured = [e for e in getattr(settings, "INSIGHT_ALERT_EMAILS", []) if e]
    if configured:
        return configured
    from .question_service import get_moderator_emails

    return get_moderator_emails()


def _send_health_mail(kind: str, subject: str, context: dict) -> bool:
    from apps.common.email import send_template_email

    recipients = get_alert_emails()
    if not recipients:
        logger.warning("Betriebsmonitor: keine Alarm-Empfänger konfiguriert.")
        return False
    context = {"kind": kind, "site_url": _site_url(), "monitoring_url": f"{_site_url()}/admin/monitoring/", **context}
    return send_template_email(
        subject=subject,
        template_name="emails/monitoring/source_health",
        context=context,
        to=recipients,
        fail_silently=True,
    )


def send_health_alerts(dry_run: bool = False) -> dict:
    """
    Alarm bei kritischen Quellen (einmalig, Wiederholung nach 7 Tagen), Entwarnung
    bei Erholung; Alarm bei stehendem Ingestor-Daemon (max. einmal je 24 h).
    """
    now = timezone.now()
    result = {"alerts": [], "recoveries": [], "daemon_alert": False}

    for item in collect_source_health(now)["items"]:
        source = item["source"]
        if item["status"] == "critical":
            due = source.health_alert_sent_at is None or source.health_alert_sent_at < now - timedelta(
                days=ALERT_REPEAT_DAYS
            )
            if not due:
                continue
            result["alerts"].append(source.name)
            if dry_run:
                continue
            _send_health_mail(
                "alert",
                f"[mandari] Quelle kritisch: {source.name}",
                {
                    "item": item,
                    "source": source,
                    "admin_url": f"{_site_url()}/admin/insight_core/oparlsource/{source.id}/change/",
                },
            )
            source.health_alert_sent_at = now
            source.save(update_fields=["health_alert_sent_at"])
        elif item["status"] == "ok" and source.health_alert_sent_at:
            result["recoveries"].append(source.name)
            if dry_run:
                continue
            _send_health_mail(
                "recovered",
                f"[mandari] Quelle wieder erreichbar: {source.name}",
                {
                    "item": item,
                    "source": source,
                    "admin_url": f"{_site_url()}/admin/insight_core/oparlsource/{source.id}/change/",
                },
            )
            source.health_alert_sent_at = None
            source.save(update_fields=["health_alert_sent_at"])

    daemon_check = next((c for c in collect_system_health() if c["name"] == "Ingestor-Daemon"), None)
    if daemon_check and daemon_check["status"] == "critical":
        if dry_run:
            result["daemon_alert"] = True
        elif cache.add("health-alert-daemon", "1", 24 * 3600):
            result["daemon_alert"] = True
            _send_health_mail(
                "daemon",
                "[mandari] Ingestor-Daemon liefert keine Sync-Läufe mehr",
                {"item": daemon_check, "source": None, "admin_url": f"{_site_url()}/admin/monitoring/"},
            )
    return result
