# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Audit-Logging für das Session RIS (Issue #23).

Revisionssichere Protokollierung aller relevanten Änderungen an den
zentralen Session-Models. Einträge entstehen automatisch über
Model-Signale (create/update/delete) sowie explizit über
:func:`log_event` für Spezial-Ereignisse (Freigabe, Veröffentlichung,
Einladungsversand, Absetzung, Datei-Ersetzung).

Sicherheit:
- Einträge sind unveränderbar (Save-/Delete-Guard auf dem Model).
- Verschlüsselte Felder werden niemals im Klartext protokolliert —
  nur die Tatsache, DASS sie geändert wurden.
- Der auslösende Nutzer wird über einen Thread-Local-Request ermittelt,
  den die SessionTenantMiddleware setzt.
"""

import threading

from django.db import models

_thread_state = threading.local()

# Felder, die nie in den Änderungs-Diff aufgenommen werden
_SKIP_FIELDS = {"id", "created_at", "updated_at", "last_access", "joined_at", "submitted_at"}

# Maximale Länge protokollierter Werte
_MAX_VALUE_LENGTH = 300

# Platzhalter für verschlüsselte/binäre Felder
_MASKED = "[verschlüsselt geändert]"


def set_current_request(request):
    """Aktuellen Request für die Audit-Attribution merken (Middleware)."""
    _thread_state.request = request


def clear_current_request():
    """Thread-Local-Request wieder entfernen (Middleware, Response/Exception)."""
    _thread_state.request = None


def get_current_request():
    """Aktuellen Request abrufen (oder None außerhalb eines Requests)."""
    return getattr(_thread_state, "request", None)


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


def _serialize_value(field, value):
    """Feldwert für das Änderungsprotokoll aufbereiten (nie Klartext-Sensitives)."""
    if isinstance(field, models.BinaryField):
        # Deckt EncryptedTextField ab: niemals Inhalte protokollieren
        return _MASKED if value else ""
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    if len(text) > _MAX_VALUE_LENGTH:
        text = text[:_MAX_VALUE_LENGTH] + "…"
    return text


def build_changes(old_instance, new_instance) -> dict:
    """Feld-Diff zwischen altem und neuem Zustand (verschlüsselte Werte maskiert)."""
    changes = {}
    for field in new_instance._meta.concrete_fields:
        if field.name in _SKIP_FIELDS:
            continue
        old_value = getattr(old_instance, field.attname, None)
        new_value = getattr(new_instance, field.attname, None)
        if old_value == new_value:
            continue
        if isinstance(field, models.BinaryField):
            # Nur die Tatsache der Änderung festhalten
            changes[field.name] = {"alt": _MASKED, "neu": _MASKED}
        else:
            changes[field.name] = {
                "alt": _serialize_value(field, old_value),
                "neu": _serialize_value(field, new_value),
            }
    return changes


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

    request = request or get_current_request()
    ip_address = None
    user_agent = ""
    if request is not None:
        if user is None:
            session_user = getattr(request, "session_user", None)
            if session_user is not None and session_user.tenant_id == tenant.pk:
                user = session_user
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            ip_address = x_forwarded_for.split(",")[0].strip()
        else:
            ip_address = request.META.get("REMOTE_ADDR") or None
        user_agent = request.META.get("HTTP_USER_AGENT", "")[:500]

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
    if instance.pk:
        try:
            instance._audit_old = sender.objects.get(pk=instance.pk)
        except sender.DoesNotExist:
            instance._audit_old = None
    else:
        instance._audit_old = None


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
    """delete protokollieren."""
    log_event("delete", instance)
