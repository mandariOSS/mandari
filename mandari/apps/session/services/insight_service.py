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
"""

import logging

from django.conf import settings as django_settings
from django.urls import reverse

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
    else:
        deactivate_source(tenant, base_url)
