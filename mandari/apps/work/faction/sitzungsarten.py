# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsarten (Issue #1231, Epic #1226): Jede Organisation hat genau eine Standardart „Fraktionssitzung“.

Bestehende Organisationen bekommen sie aus der Datenmigration, neue beim Anlegen (:func:`register`). Wo eine Art
fehlt, legt :meth:`FactionMeetingType.default_for <apps.work.faction.models.FactionMeetingType.default_for>` sie an,
ohne je doppelt anzulegen; neue Reihen, Sitzungen und Standard-TOPs ohne Art bekommen beim Speichern die Standardart.
"""

from __future__ import annotations

from typing import Any


def organization_created(sender: Any, instance: Any, created: bool, raw: bool = False, **kwargs: Any) -> None:
    """post_save(Organization): neue Organisation bekommt ihre Standardart (nicht beim Laden von Fixtures)."""
    if not created or raw:
        return
    from apps.work.faction.models import FactionMeetingType

    FactionMeetingType.default_for(instance)


def register() -> None:
    """Receiver registrieren (WorkConfig.ready)."""
    from django.db.models.signals import post_save

    from apps.tenants.models import Organization

    post_save.connect(organization_created, sender=Organization, dispatch_uid="faction_meeting_type_default")
