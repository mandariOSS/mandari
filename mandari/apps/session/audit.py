# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Audit-Logging für das Session RIS (Issue #23).

Revisionssichere Protokollierung aller relevanten Änderungen an den
zentralen Session-Models. Einträge entstehen automatisch über
Model-Signale (create/update/delete) sowie explizit über
:func:`log_event` für Spezial-Ereignisse (Freigabe, Veröffentlichung,
Einladungsversand, Absetzung, Datei-Ersetzung).

Die generischen Grundfunktionen (Thread-Local-Request, Feld-Diff,
Kaskadenlösch-Schutz) liegen seit Issue #66 im gemeinsamen Baustein
:mod:`apps.common.audit_core` und werden auch vom Work-Portal
(Fraktionssitzungen) genutzt. Dieses Modul behält seine bisherige
öffentliche API — das Session-Verhalten ändert sich nicht.

Sicherheit:
- Einträge sind unveränderbar (Save-/Delete-Guard auf dem Model).
- Verschlüsselte Felder werden niemals im Klartext protokolliert —
  nur die Tatsache, DASS sie geändert wurden.
- Der auslösende Nutzer wird über einen Thread-Local-Request ermittelt,
  den die SessionTenantMiddleware setzt.
"""

from apps.common import audit_core

# Öffentliche API (unverändert) — delegiert an den gemeinsamen Baustein
set_current_request = audit_core.set_current_request
clear_current_request = audit_core.clear_current_request
get_current_request = audit_core.get_current_request
build_changes = audit_core.build_changes

# Felder, die nie in den Änderungs-Diff aufgenommen werden
_SKIP_FIELDS = audit_core.DEFAULT_SKIP_FIELDS

# Maximale Länge protokollierter Werte
_MAX_VALUE_LENGTH = audit_core.MAX_VALUE_LENGTH

# Platzhalter für verschlüsselte/binäre Felder
_MASKED = audit_core.MASKED

_serialize_value = audit_core.serialize_value

# Kaskadenlösch-Schutz-Scope für Session-Mandanten (Issue #56)
_TENANT_SCOPE = "session_tenant"


# =============================================================================
# Mandanten-Kaskadenlöschung (Issue #56)
# =============================================================================
#
# Beim Löschen eines kompletten SessionTenant löscht Django alle abhängigen
# Objekte in einer Kaskade. Die post_delete-Receiver würden dabei NEUE
# SessionAuditLog-Zeilen für den gerade verschwindenden Mandanten anlegen —
# der Collector kennt diese Zeilen nicht, sie blieben mit hängendem
# Fremdschlüssel zurück und das abschließende DELETE des Mandanten schlägt
# mit IntegrityError fehl. Daher wird der Mandant während seiner Löschung
# markiert und das Protokollieren übersprungen (die Audit-Zeilen des
# Mandanten werden ohnehin mitkaskadiert — es geht keine Historie verloren).


def mark_tenant_deleting(tenant_pk):
    """Mandanten-PK als 'wird gerade kaskadengelöscht' markieren (pre_delete)."""
    audit_core.mark_root_deleting(_TENANT_SCOPE, tenant_pk)


def unmark_tenant_deleting(tenant_pk):
    """Markierung nach Abschluss der Kaskadenlöschung entfernen (post_delete)."""
    audit_core.unmark_root_deleting(_TENANT_SCOPE, tenant_pk)


def is_tenant_deleting(tenant_pk) -> bool:
    """Läuft für diesen Mandanten gerade eine Kaskadenlöschung?"""
    return audit_core.is_root_deleting(_TENANT_SCOPE, tenant_pk)


def tenant_pre_delete(sender, instance, **kwargs):
    """pre_delete(SessionTenant): Kaskadenlöschung beginnt."""
    mark_tenant_deleting(instance.pk)


def tenant_post_delete(sender, instance, **kwargs):
    """post_delete(SessionTenant): Kaskadenlöschung abgeschlossen."""
    unmark_tenant_deleting(instance.pk)


def resolve_tenant(instance):
    """Tenant eines Session-Objekts ermitteln (direkt oder über Relation)."""
    tenant = getattr(instance, "tenant", None)
    if tenant is not None:
        return tenant
    meeting = getattr(instance, "meeting", None)
    if meeting is not None:
        return meeting.tenant
    organization = getattr(instance, "organization", None)
    if organization is not None:
        return getattr(organization, "tenant", None)
    attendance = getattr(instance, "attendance", None)
    if attendance is not None:
        return attendance.meeting.tenant
    return None


def log_event(action, instance, *, tenant=None, user=None, changes=None, request=None):
    """
    Audit-Eintrag schreiben.

    Args:
        action: Aktion aus den SessionAuditLog-Choices
        instance: Betroffenes Model-Objekt
        tenant: SessionTenant (sonst aus instance abgeleitet)
        user: SessionUser (sonst aus dem aktuellen Request abgeleitet)
        changes: Optionaler Änderungs-Diff (dict)
        request: Optionaler Request (sonst Thread-Local)
    """
    from apps.session.models import SessionAuditLog

    tenant = tenant or resolve_tenant(instance)
    if tenant is None:
        return None

    # Issue #56: Während einer Mandanten-Kaskadenlöschung nichts protokollieren
    if is_tenant_deleting(tenant.pk):
        return None

    request = request or get_current_request()
    if request is not None and user is None:
        session_user = getattr(request, "session_user", None)
        if session_user is not None and session_user.tenant_id == tenant.pk:
            user = session_user
    ip_address, user_agent = audit_core.get_client_meta(request)

    return SessionAuditLog.objects.create(
        tenant=tenant,
        user=user,
        ip_address=ip_address,
        user_agent=user_agent,
        action=action,
        model_name=instance.__class__.__name__,
        object_id=instance.pk,
        object_repr=str(instance)[:500],
        changes=changes or {},
    )


# =============================================================================
# Signal-Receiver (in signals.py registriert)
# =============================================================================


def _special_action(old_instance, new_instance) -> str | None:
    """Spezial-Ereignisse aus Statuswechseln ableiten."""
    model_name = new_instance.__class__.__name__

    old_status = getattr(old_instance, "status", None)
    new_status = getattr(new_instance, "status", None)
    if old_status != new_status:
        if new_status == "approved":
            return "approve"
        if new_status == "published":
            return "publish"

    if model_name == "SessionMeeting":
        old_state = getattr(old_instance, "meeting_state", None)
        new_state = getattr(new_instance, "meeting_state", None)
        if old_state != new_state and new_state == "invitation_sent":
            return "invitation_sent"
        old_sent = getattr(old_instance, "invitation_sent_at", None)
        new_sent = getattr(new_instance, "invitation_sent_at", None)
        if old_sent is None and new_sent is not None:
            return "invitation_sent"

    if getattr(old_instance, "is_withdrawn", False) is False and getattr(new_instance, "is_withdrawn", False) is True:
        return "withdraw"

    return None


def audit_pre_save(sender, instance, **kwargs):
    """Alten Zustand für den Diff laden."""
    audit_core.capture_old_state(sender, instance)


def audit_post_save(sender, instance, created, **kwargs):
    """create/update (inkl. Spezial-Ereignisse) protokollieren."""
    if kwargs.get("raw"):
        return
    if created:
        log_event("create", instance)
        return

    old_instance = getattr(instance, "_audit_old", None)
    if old_instance is None:
        log_event("update", instance)
        return

    changes = build_changes(old_instance, instance)
    if not changes:
        return
    action = _special_action(old_instance, instance) or "update"
    log_event(action, instance, changes=changes)


def audit_post_delete(sender, instance, **kwargs):
    """delete protokollieren (übersprungen während Mandanten-Kaskadenlöschung)."""
    # Schneller Pfad ohne DB-Zugriff: direkte tenant_id-Objekte (Issue #56)
    tenant_id = getattr(instance, "tenant_id", None)
    if tenant_id is not None and is_tenant_deleting(tenant_id):
        return
    log_event("delete", instance)
