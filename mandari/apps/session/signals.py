# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Signal-Registrierung für das Session RIS.

Verbindet die Audit-Receiver (apps/session/audit.py) mit den zentralen
Session-Models. Wird über SessionConfig.ready() geladen.
"""

from django.db.models.signals import post_delete, post_save, pre_delete, pre_save

from apps.session import audit, oparl_publication
from apps.session.models import (
    SessionAgendaItem,
    SessionApplication,
    SessionAttendance,
    SessionConsultation,
    SessionFile,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
    SessionProtocol,
    SessionTenant,
    SessionUser,
)

# Zentrale Models, deren Änderungen revisionssicher protokolliert werden
AUDITED_MODELS = [
    SessionMeeting,
    SessionAgendaItem,
    SessionPaper,
    SessionApplication,
    SessionProtocol,
    SessionPerson,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionAttendance,
    SessionConsultation,
    SessionLegislativeTerm,
    SessionFile,
    SessionUser,
]

for _model in AUDITED_MODELS:
    _uid = f"session_audit_{_model.__name__}"
    pre_save.connect(audit.audit_pre_save, sender=_model, dispatch_uid=f"{_uid}_pre_save")
    post_save.connect(audit.audit_post_save, sender=_model, dispatch_uid=f"{_uid}_post_save")
    post_delete.connect(audit.audit_post_delete, sender=_model, dispatch_uid=f"{_uid}_post_delete")

# Issue #56: Mandanten-Kaskadenlöschung erkennen — während des Löschens eines
# SessionTenant dürfen die Audit-Receiver keine neuen Log-Zeilen für den
# verschwindenden Mandanten anlegen (IntegrityError/hängende Fremdschlüssel).
pre_delete.connect(audit.tenant_pre_delete, sender=SessionTenant, dispatch_uid="session_audit_tenant_pre_delete")
post_delete.connect(audit.tenant_post_delete, sender=SessionTenant, dispatch_uid="session_audit_tenant_post_delete")


# =============================================================================
# OParl-Tombstones (Issue #35): Löschungen/Ö->NÖ-Wechsel nachhalten
# =============================================================================

for _model in oparl_publication.KIND_BY_MODEL:
    _uid = f"session_oparl_tombstone_{_model.__name__}"
    post_save.connect(oparl_publication.tombstone_post_save, sender=_model, dispatch_uid=f"{_uid}_post_save")
    post_delete.connect(oparl_publication.tombstone_post_delete, sender=_model, dispatch_uid=f"{_uid}_post_delete")


# =============================================================================
# Beratungsfolge (Issue #34): Beschlussergebnis an die Station zurückschreiben
# =============================================================================


def sync_consultation_result(sender, instance, **kwargs):
    """
    Schreibt das Abstimmungsergebnis eines TOP an die verknüpfte
    Beratungsstation zurück (Issue #34).

    Wird das Ergebnis am TOP erfasst (Niederschrift/Beschlussregister,
    Issues #31/#32), spiegelt die Station der Beratungsfolge den Stand —
    so ist z. B. das Vorberatungsergebnis in der Ratssitzung sichtbar.
    """
    if kwargs.get("raw"):
        return
    try:
        consultation = instance.consultation
    except SessionConsultation.DoesNotExist:
        return
    if consultation.result != instance.vote_result:
        consultation.result = instance.vote_result
        consultation.save(update_fields=["result", "updated_at"])


post_save.connect(
    sync_consultation_result,
    sender=SessionAgendaItem,
    dispatch_uid="session_consultation_result_sync",
)
