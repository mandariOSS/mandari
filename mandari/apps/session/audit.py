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

import logging
import threading
import time
from contextlib import contextmanager
from typing import Any

from apps.common import audit_core

logger = logging.getLogger(__name__)

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

# Handlungen aus einer Vertretung (Issue #222)
_vertretung = threading.local()


@contextmanager
def in_vertretung(principal):
    """
    Audit-Einträge in diesem Block als Handlung „in Vertretung für …“ kennzeichnen.

    Gilt auch für Einträge, die Model-Signale beim Speichern erzeugen (z. B. die Freigabe).
    Ohne vertretene Person (``None``) bleibt alles wie bisher.
    """
    previous = getattr(_vertretung, "principal", None)
    _vertretung.principal = principal
    try:
        yield
    finally:
        _vertretung.principal = previous


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
    """post_delete(SessionTenant): Kaskadenlöschung abgeschlossen, Kettenkopf entfernen (Issue #221)."""
    from apps.common import audit_chain

    unmark_tenant_deleting(instance.pk)
    audit_chain.drop_head(audit_chain.SESSION, instance.pk)


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


def log_event(
    action: str,
    instance: Any,
    *,
    tenant: Any = None,
    user: Any = None,
    changes: Any = None,
    request: Any = None,
    on_behalf_of: Any = None,
    object_repr: str | None = None,
) -> Any:
    """
    Audit-Eintrag schreiben.

    Args:
        action: Aktion aus den SessionAuditLog-Choices
        instance: Betroffenes Model-Objekt
        tenant: SessionTenant (sonst aus instance abgeleitet)
        user: SessionUser (sonst aus dem aktuellen Request abgeleitet)
        changes: Optionaler Änderungs-Diff (dict)
        request: Optionaler Request (sonst Thread-Local)
        on_behalf_of: Vertretene Person (sonst aus :func:`in_vertretung`), Issue #222
        object_repr: Objekt-Beschreibung statt ``str(instance)`` (Lesezugriffe: nur Referenz, Issue #221)
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

    if on_behalf_of is None:
        on_behalf_of = getattr(_vertretung, "principal", None)
    if on_behalf_of is not None and on_behalf_of.tenant_id != tenant.pk:
        on_behalf_of = None

    # Schreiben über die Hash-Kette des Mandanten (SessionAuditLog.save, Issue #221)
    return SessionAuditLog.objects.create(
        tenant=tenant,
        user=user,
        on_behalf_of=on_behalf_of,
        ip_address=ip_address,
        user_agent=user_agent,
        action=action,
        model_name=instance.__class__.__name__,
        object_id=instance.pk,
        object_repr=(str(instance) if object_repr is None else object_repr)[:500],
        changes=changes or {},
    )


def log_role_assignment(
    session_user: Any, old_roles: Any, new_roles: Any, *, request: Any = None, user: Any = None, reason: str = ""
) -> Any:
    """
    Rollenzuweisung eines Nutzers protokollieren (Issue #221): hinzugefügte und entzogene Rollen.

    Die M2M-Zuordnung löst kein ``post_save`` aus; ohne diesen direkten Eintrag bliebe eine
    Rechteausweitung unsichtbar. Ohne Unterschied entsteht kein Eintrag.
    """
    old = {role.pk: role.name for role in old_roles}
    new = {role.pk: role.name for role in new_roles}
    added = sorted(new[pk] for pk in new.keys() - old.keys())
    removed = sorted(old[pk] for pk in old.keys() - new.keys())
    if not added and not removed:
        return None
    changes = {"hinzugefuegt": added, "entzogen": removed}
    if reason:
        changes["anlass"] = reason
    return log_event(
        "roles_changed", session_user, tenant=session_user.tenant, user=user, request=request, changes=changes
    )


# =============================================================================
# Lesezugriffe (Issue #221)
# =============================================================================
#
# Lesezugriffe auf nichtöffentliche Inhalte werden datensparsam protokolliert: wer, wann,
# welches Objekt – nie der Inhalt. Wiederholte Aufrufe desselben Objekts durch dieselbe Person
# innerhalb von READ_DEDUP_SECONDS fasst der Eintrag des ersten Aufrufs zusammen. Der Merker
# liegt in der ohnehin geladenen und gespeicherten Login-Session: Ein wiederholter Aufruf kostet
# keine Abfrage, ein neuer Eintrag die drei Abfragen der Hash-Kette. Scheitert das
# Protokollieren, wird der Fehler geloggt und die Seite trotzdem ausgeliefert.

#: Zeitfenster, in dem wiederholte Lesezugriffe zusammengefasst werden
READ_DEDUP_SECONDS = 600
#: Session-Schlüssel des Merkers und dessen Obergrenze (älteste Einträge fallen heraus)
READ_SESSION_KEY = "audit_read_seen"
READ_SESSION_MAX = 200


def _meeting_date(meeting: Any) -> str:
    from django.utils import timezone

    start = getattr(meeting, "start", None)
    return timezone.localtime(start).strftime("%d.%m.%Y") if start else "ohne Datum"


def read_reference(instance: Any) -> str:
    """Neutrale Objekt-Referenz für Lesezugriffe – ohne Betreff oder Titel (Datensparsamkeit)."""
    name = instance.__class__.__name__
    if name == "SessionPaper":
        return f"Vorlage {instance.reference or instance.pk}"
    if name == "SessionMeeting":
        return f"Sitzung vom {_meeting_date(instance)}"
    if name == "SessionAgendaItem":
        return f"TOP {instance.number or '?'}, Sitzung vom {_meeting_date(instance.meeting)}"
    if name == "SessionProtocol":
        return f"Niederschrift, Sitzung vom {_meeting_date(instance.meeting)}"
    if name == "SessionTenant":
        return "Protokoll"
    return f"{name} {instance.pk}"


def read_logging_enabled(tenant: Any) -> bool:
    """Protokolliert der Mandant Lesezugriffe auf nichtöffentliche Inhalte? (Standard: ja)."""
    privacy = (tenant.settings or {}).get("privacy", {})
    if not isinstance(privacy, dict):
        return True
    return bool(privacy.get("read_logging", True))


def _read_seen(request: Any, key: str, now: float) -> bool:
    session = getattr(request, "session", None)
    if session is None:
        return False
    seen = session.get(READ_SESSION_KEY)
    if not isinstance(seen, dict):
        return False
    stamp = seen.get(key)
    return isinstance(stamp, int | float) and now - stamp < READ_DEDUP_SECONDS


def _remember_read(request: Any, key: str, now: float) -> None:
    session = getattr(request, "session", None)
    if session is None:
        return
    seen = session.get(READ_SESSION_KEY)
    fresh = {
        k: v
        for k, v in (seen.items() if isinstance(seen, dict) else [])
        if isinstance(v, int | float) and now - v < READ_DEDUP_SECONDS
    }
    fresh[key] = now
    if len(fresh) > READ_SESSION_MAX:
        fresh = dict(sorted(fresh.items(), key=lambda item: item[1])[-READ_SESSION_MAX:])
    session[READ_SESSION_KEY] = fresh


def log_read(
    request: Any,
    instance: Any,
    *,
    tenant: Any,
    user: Any = None,
    action: str = "view",
    changes: Any = None,
    respect_setting: bool = True,
    dedup_suffix: str = "",
) -> Any:
    """
    Lesezugriff protokollieren – datensparsam, zusammengefasst, fehlertolerant.

    Args:
        request: aktueller Request (für Nutzer, Session-Merker und IP)
        instance: gelesenes Objekt (nur die Referenz wird gespeichert)
        tenant: Mandant
        user: SessionUser (sonst aus dem Request)
        action: ``view`` (Ansicht), ``download`` (erzeugtes Dokument) oder ``audit_view``
        changes: knappe Angaben zum Umfang, nie Inhalte
        respect_setting: False für Zugriffe, die immer protokolliert werden (Protokoll-Einsicht)
        dedup_suffix: unterscheidet Zugriffe auf dasselbe Objekt (z. B. verschiedene Filter)

    Returns: der neue Eintrag oder ``None`` (abgeschaltet, zusammengefasst oder Fehler)
    """
    if respect_setting and not read_logging_enabled(tenant):
        return None
    now = time.time()
    key = f"{tenant.pk}:{action}:{instance.__class__.__name__}:{instance.pk}:{dedup_suffix}"
    if _read_seen(request, key, now):
        return None
    try:
        entry = log_event(
            action,
            instance,
            tenant=tenant,
            user=user,
            changes=changes,
            request=request,
            object_repr=read_reference(instance),
        )
    except Exception:
        # Die Seite darf am Protokoll nicht scheitern; der Fehler landet im Betriebslog
        logger.exception("Lesezugriff konnte nicht protokolliert werden")
        return None
    _remember_read(request, key, now)
    return entry


# =============================================================================
# Signal-Receiver (in signals.py registriert)
# =============================================================================


#: Entschädigungen mit sprechenden Aktionen je Posten (Sitzungsgeld, Monatspauschalen, Issue #221)
ALLOWANCE_MODELS = frozenset({"SessionAllowance", "SessionMonthlyAllowance"})
_ALLOWANCE_STATUS_ACTIONS = {
    "approved": "allowance_approved",
    "paid": "allowance_paid",
    "cancelled": "allowance_cancelled",
}


def _special_action(old_instance, new_instance) -> str | None:
    """Spezial-Ereignisse aus Statuswechseln ableiten."""
    model_name = new_instance.__class__.__name__

    old_status = getattr(old_instance, "status", None)
    new_status = getattr(new_instance, "status", None)
    if old_status != new_status:
        if model_name in ALLOWANCE_MODELS and new_status in _ALLOWANCE_STATUS_ACTIONS:
            return _ALLOWANCE_STATUS_ACTIONS[new_status]
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
        created_action = "allowance_created" if instance.__class__.__name__ in ALLOWANCE_MODELS else "create"
        log_event(created_action, instance)
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
