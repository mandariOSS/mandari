# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Insight-Durchstich: Session-Mandanten als OParl-Quelle im Bürgerportal
(Issue #36).

Eine Session-Kommune erscheint automatisch im Insight-Bürgerportal, sobald
der Mandant die Veröffentlichung aktiviert (SessionTenant.insight_publish):

1. Die spec-konforme OParl-API des Mandanten (Issue #35) wird als ganz
   normale OParlSource registriert.
2. Der Ingestor (Daemon) bzw. der lokale Sync-Befehl
   ``manage.py sync_session_insight`` spiegelt die öffentlichen Daten in
   die Insight-Modelle — inkl. modified_since-Inkrementen und Tombstones.

Wird die Veröffentlichung deaktiviert, wird die Quelle inaktiv gesetzt
(kein weiterer Sync; bereits gespiegelte Daten bleiben, bis die Kommune
eine Löschung beauftragt — Muster purge_deleted, siehe docs/OPARL_API.md).

Wird der Mandant selbst deaktiviert (Issue #317), nimmt ``retract_source`` die
Bürgerportal-Quelle vollständig zurück: Quelle inaktiv, Kommune nicht mehr
gelistet, alle gespiegelten Einträge als zurückgenommen markiert (wie
``oparl_publication.retract_from_portal``). ``restore_source`` macht genau das
beim Reaktivieren rückgängig – ohne Einträge, die in Session inzwischen gelöscht
oder nichtöffentlich sind (Tombstone).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings as django_settings
from django.db.models import Max, Q
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)


def oparl_system_url(tenant, base_url: str | None = None) -> str:
    """Absolute System-URL der Mandanten-OParl-API (Einstiegspunkt für den Ingestor)."""
    base = (base_url or getattr(django_settings, "SITE_URL", "http://localhost:8000")).rstrip("/")
    path = reverse("session:oparl_system", kwargs={"tenant_slug": tenant.slug})
    return f"{base}{path}"


def register_source(tenant, base_url: str | None = None):
    """
    OParl-Quelle für den Mandanten anlegen/aktivieren (idempotent).

    Returns:
        (OParlSource, created)
    """
    from insight_core.models import OParlSource

    url = oparl_system_url(tenant, base_url)
    source, created = OParlSource.objects.get_or_create(
        url=url,
        defaults={
            "name": f"Sitzungsdienst {tenant.name}",
            "sync_config": {
                "source_type": OParlSource.SOURCE_TYPE_OPARL,
                "session_tenant": tenant.slug,
            },
        },
    )
    changed = created
    if not source.is_active:
        source.is_active = True
        changed = True
    sync_config = source.sync_config if isinstance(source.sync_config, dict) else {}
    if sync_config.get("session_tenant") != tenant.slug:
        sync_config["session_tenant"] = tenant.slug
        sync_config.setdefault("source_type", OParlSource.SOURCE_TYPE_OPARL)
        source.sync_config = sync_config
        changed = True
    if changed and not created:
        source.save(update_fields=["is_active", "sync_config", "updated_at"])
    if created:
        logger.info("[Insight] OParl-Quelle für Session-Mandant '%s' registriert: %s", tenant.slug, url)
    return source, created


def deactivate_source(tenant, base_url: str | None = None):
    """Quelle deaktivieren (kein weiterer Sync). Returns True, wenn eine Quelle betroffen war."""
    from insight_core.models import OParlSource

    url = oparl_system_url(tenant, base_url)
    updated = OParlSource.objects.filter(url=url, is_active=True).update(is_active=False)
    if updated:
        logger.info("[Insight] OParl-Quelle für Session-Mandant '%s' deaktiviert.", tenant.slug)
    return bool(updated)


def sync_publication_state(tenant, base_url: str | None = None):
    """Quellen-Registrierung an den Veröffentlichungs-Schalter angleichen."""
    if tenant.insight_publish and tenant.is_active:
        register_source(tenant, base_url)
        # Eine Rücknahme aus der Zeit der Deaktivierung endet mit der Veröffentlichung
        restore_source(tenant)
    else:
        deactivate_source(tenant, base_url)


# =============================================================================
# Rücknahme beim Deaktivieren des Mandanten (Issue #317)
# =============================================================================

#: Merker in ``OParlSource.sync_config``: Zeitpunkt der Rücknahme und vorher gelistete Kommunen
RETRACTION_KEY = "session_retraction"


@dataclass
class PortalChange:
    """Was Rücknahme bzw. Wiederherstellung im Bürgerportal bewirkt haben (Ausgabe und Audit)."""

    sources: int = 0
    bodies: int = 0
    entries: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"quellen": self.sources, "kommunen": self.bodies, "eintraege": self.entries}


def session_sources(tenant: Any) -> Any:
    """
    Bürgerportal-Quellen des Mandanten: seine eigene OParl-API (URL ``…/session/<slug>/api/oparl/``)
    bzw. als Session-Quelle registriert (``sync_config.session_tenant``). Fremde Quellen, die der
    Mandant nur verknüpft hat (``oparl_body``), gehören nicht dazu.
    """
    from insight_core.models import OParlSource

    marker = f"/session/{tenant.slug}/api/oparl/"
    return OParlSource.objects.filter(Q(url__contains=marker) | Q(sync_config__session_tenant=tenant.slug))


def _entry_querysets(body_ids: list[Any]) -> list[Any]:
    """Alle gespiegelten Einträge der Kommunen – über indizierte Fremdschlüssel, nicht über URLs."""
    from insight_core.models import (
        OParlAgendaItem,
        OParlConsultation,
        OParlFile,
        OParlLegislativeTerm,
        OParlMeeting,
        OParlMembership,
        OParlOrganization,
        OParlPaper,
        OParlPerson,
    )

    return [
        OParlMeeting.objects.filter(body__in=body_ids),
        OParlAgendaItem.objects.filter(meeting__body__in=body_ids),
        OParlPaper.objects.filter(body__in=body_ids),
        OParlConsultation.objects.filter(Q(body__in=body_ids) | Q(paper__body__in=body_ids)).distinct(),
        OParlFile.objects.filter(
            Q(body__in=body_ids) | Q(paper__body__in=body_ids) | Q(meeting__body__in=body_ids)
        ).distinct(),
        OParlOrganization.objects.filter(body__in=body_ids),
        OParlPerson.objects.filter(body__in=body_ids),
        OParlMembership.objects.filter(Q(organization__body__in=body_ids) | Q(person__body__in=body_ids)).distinct(),
        OParlLegislativeTerm.objects.filter(body__in=body_ids),
    ]


def retract_source(tenant: Any) -> PortalChange:
    """
    Bürgerportal-Quelle des Mandanten zurücknehmen (Deaktivieren, Issue #317); idempotent.

    Quelle inaktiv (kein Sync mehr), Kommune nicht mehr gelistet (kein Einstieg, keine Auswahl),
    jeder gespiegelte Eintrag per ``mark_deleted`` zurückgenommen – Einzel-Speichern, damit auch
    Suchindex und Signale folgen. Alle Einträge tragen denselben Zeitpunkt; mit den vorher
    gelisteten Kommunen steht er an der Quelle, damit ``restore_source`` genau das zurückholt.
    """
    from insight_core.models import OParlBody

    change = PortalChange()
    for source in session_sources(tenant):
        config = dict(source.sync_config) if isinstance(source.sync_config, dict) else {}
        record = config.get(RETRACTION_KEY)
        bodies = list(OParlBody.objects.filter(source=source))
        listed = {str(body.pk) for body in bodies if body.is_listed}
        frueher = _parse_stamp(record.get("at")) if isinstance(record, dict) else None
        if isinstance(record, dict):
            # Schon zurückgenommen: ursprünglichen Zeitpunkt behalten, Listung zusammenführen
            listed |= set(record.get("listed_bodies") or [])
        when = frueher or _retraction_stamp([body.pk for body in bodies])
        config[RETRACTION_KEY] = {"at": when.isoformat(), "listed_bodies": sorted(listed)}
        source.is_active = False
        source.sync_config = config
        source.save(update_fields=["is_active", "sync_config", "updated_at"])
        change.sources += 1
        for body in bodies:
            if body.is_listed:
                body.is_listed = False
                body.save(update_fields=["is_listed", "updated_at"])
                change.bodies += 1
        for queryset in _entry_querysets([body.pk for body in bodies]):
            for obj in queryset.filter(deleted=False).iterator(chunk_size=500):
                obj.mark_deleted(when)
                change.entries += 1
    if change.sources:
        logger.info("[Insight] Bürgerportal-Quelle für Session-Mandant '%s' zurückgenommen.", tenant.slug)
    return change


def restore_source(tenant: Any) -> PortalChange:
    """
    Rücknahme aus ``retract_source`` aufheben (Reaktivieren bzw. erneutes Veröffentlichen); idempotent.

    Nur für aktive, veröffentlichende Mandanten. Zurück kommen genau die Einträge mit dem Zeitpunkt
    der Rücknahme, außer denen, die in Session inzwischen einen Tombstone haben (gelöscht oder
    nichtöffentlich). Die Kommune wird wieder gelistet, sofern sie es vorher war.
    """
    from apps.session.models import SessionOParlTombstone
    from insight_core.models import OParlBody

    change = PortalChange()
    if not (tenant.is_active and tenant.insight_publish):
        return change
    tombstoned = {
        f"{kind}/{object_id}"
        for kind, object_id in SessionOParlTombstone.objects.filter(tenant=tenant).values_list(
            "oparl_type", "object_id"
        )
    }
    now = timezone.now()
    for source in session_sources(tenant):
        config = dict(source.sync_config) if isinstance(source.sync_config, dict) else {}
        record = config.pop(RETRACTION_KEY, None)
        if not isinstance(record, dict):
            continue
        when = _parse_stamp(record.get("at"))
        listed = set(record.get("listed_bodies") or [])
        bodies = list(OParlBody.objects.filter(source=source))
        for body in bodies:
            if str(body.pk) in listed and not body.is_listed:
                body.is_listed = True
                body.save(update_fields=["is_listed", "updated_at"])
                change.bodies += 1
        if when is not None:
            for queryset in _entry_querysets([body.pk for body in bodies]):
                for obj in queryset.filter(deleted=True, deleted_at=when).iterator(chunk_size=500):
                    if _oparl_tail(obj.external_id) in tombstoned:
                        continue
                    obj.deleted, obj.deleted_at, obj.oparl_modified = False, None, now
                    obj.save(update_fields=["deleted", "deleted_at", "oparl_modified", "updated_at"])
                    change.entries += 1
        source.is_active = True
        source.sync_config = config
        source.save(update_fields=["is_active", "sync_config", "updated_at"])
        change.sources += 1
    if change.sources:
        logger.info("[Insight] Bürgerportal-Quelle für Session-Mandant '%s' wiederhergestellt.", tenant.slug)
    return change


def _retraction_stamp(body_ids: list[Any]) -> datetime:
    """
    Zeitpunkt der Rücknahme, echt später als jede frühere Rücknahme eines Eintrags dieser Kommunen.

    ``restore_source`` erkennt die Einträge der Rücknahme an diesem Zeitpunkt; ein zufällig
    gleicher Zeitpunkt (grobe Systemuhr) darf früher Zurückgenommenes nicht wiederbeleben.
    """
    stamp = timezone.now()
    for queryset in _entry_querysets(body_ids):
        latest = queryset.filter(deleted=True).aggregate(latest=Max("deleted_at"))["latest"]
        if latest is not None and latest >= stamp:
            stamp = latest + timedelta(microseconds=1)
    return stamp


def _parse_stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _oparl_tail(external_id: str) -> str:
    """``…/api/oparl/meeting/<id>/`` → ``meeting/<id>`` (Abgleich mit Session-Tombstones)."""
    return "/".join((external_id or "").rstrip("/").split("/")[-2:])
