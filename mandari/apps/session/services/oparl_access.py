# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Freischaltung der OParl-Schnittstelle je Mandant (Issue #319).

Offene Daten ab Werk sind gewollt – aber die Verwaltung bestimmt den Zeitpunkt: Ein neuer Mandant
(Einführung, Testdaten, Schulung, Umstieg aus einem Altsystem) ist über ``/session/<slug>/api/oparl/``
nicht abrufbar, bis sie die Schnittstelle freischaltet. Vorher antwortet jeder Endpunkt mit 404, anonyme
Lesezugriffe der Session-API ebenso, und das Bürgerportal registriert keine Quelle
(``insight_service.sync_publication_state``). Die Freischaltung steht mit Datum am Mandanten
(``SessionTenant.oparl_public_since``) und im Audit-Log.

Zurücknehmen lässt sich die Freischaltung nur, solange der Mandant nicht im Bürgerportal veröffentlicht:
Das Bürgerportal liest genau diese Schnittstelle. Wer dort veröffentlicht, beendet das zuerst mit einer
Entscheidung über den Bestand (``portal_publication``, Issue #618).
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone


class OParlAccessError(ValueError):
    """Freischaltung lässt sich so nicht ändern; die Meldung ist ein fertiger, ausgabesicherer Satz."""


LOCK_BLOCKED = (
    "Die OParl-Schnittstelle speist das Bürgerportal. Beenden Sie zuerst die Veröffentlichung im Bürgerportal."
)
RELEASE_REQUIRED = "Das Bürgerportal liest die OParl-Schnittstelle. Schalten Sie zuerst die OParl-Schnittstelle frei."


#: Felder, die Freischaltung und Bürgerportal gegenseitig prüfen – unter Zeilensperre frisch gelesen
LOCKED_FIELDS = ("oparl_public_since", "insight_publish", "insight_end_mode", "is_active")


def lock_row(tenant: Any) -> None:
    """
    Mandantenzeile sperren und die gegenseitig geprüften Felder frisch lesen (nur in ``transaction.atomic``).

    Freischaltung zurücknehmen prüft ``insight_publish``, Veröffentlichen prüft ``oparl_public_since``.
    Ohne Sperre könnten zwei gleichzeitige Anfragen jeweils den alten Stand sehen und einen Mandanten
    hinterlassen, der im Bürgerportal veröffentlicht, obwohl seine Schnittstelle gesperrt ist.
    """
    tenant.refresh_from_db(fields=list(LOCKED_FIELDS), from_queryset=type(tenant).objects.select_for_update())


def state_label(tenant: Any) -> str:
    """Stand in Worten (Audit, Oberfläche)."""
    if not tenant.oparl_public:
        return "nicht freigeschaltet"
    return f"öffentlich seit {timezone.localtime(tenant.oparl_public_since):%d.%m.%Y %H:%M}"


def release(tenant: Any, *, user: Any = None, request: Any = None) -> bool:
    """
    Schnittstelle freischalten (ab sofort). ``False``, wenn sie schon freigeschaltet ist.

    Veröffentlicht der Mandant bereits im Bürgerportal (Bestand vor dieser Regel), registriert der
    Signal-Hook die Quelle mit dem Speichern.
    """
    from apps.session import audit

    with transaction.atomic():
        lock_row(tenant)
        if tenant.oparl_public:
            return False
        vorher = state_label(tenant)
        tenant.oparl_public_since = timezone.now()
        tenant.save(update_fields=["oparl_public_since", "updated_at"])
        audit.log_event(
            "publish",
            tenant,
            tenant=tenant,
            user=user,
            request=request,
            changes={"oparl_schnittstelle": {"alt": vorher, "neu": state_label(tenant)}},
            object_repr="OParl-Schnittstelle",
        )
    return True


def lock(tenant: Any, *, user: Any = None, request: Any = None) -> bool:
    """
    Freischaltung zurücknehmen: Die Schnittstelle antwortet wieder mit 404. ``False``, wenn sie nicht
    freigeschaltet war; ``OParlAccessError``, solange der Mandant im Bürgerportal veröffentlicht.
    """
    from apps.session import audit

    with transaction.atomic():
        lock_row(tenant)
        if not tenant.oparl_public:
            return False
        if tenant.insight_publish:
            raise OParlAccessError(LOCK_BLOCKED)
        vorher = state_label(tenant)
        tenant.oparl_public_since = None
        tenant.save(update_fields=["oparl_public_since", "updated_at"])
        audit.log_event(
            "unpublish",
            tenant,
            tenant=tenant,
            user=user,
            request=request,
            changes={"oparl_schnittstelle": {"alt": vorher, "neu": state_label(tenant)}},
            object_repr="OParl-Schnittstelle",
        )
    return True
