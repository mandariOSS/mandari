# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vier-Augen-Prinzip für Freigaben und Genehmigungen (Issue #222).

Je Mandant und Vorgangsart schaltbar (Einstellungen → Vier-Augen-Prinzip), „selbst“ ist:

- Vorlagenfreigabe (Standard aus, wahlweise nur bei finanziellen Auswirkungen oder immer):
  Ersteller:in und letzte:r inhaltliche:r Bearbeiter:in (Text, Anlagen)
- Genehmigung der Niederschrift (Standard aus): Ersteller:in und letzte:r inhaltliche:r
  Bearbeiter:in (allgemeiner Teil, Protokoll der TOPs, Beschlussergebnisse)
- Sitzungsgeld und Pauschalen (Standard an): wer den Abrechnungs- bzw. Monatslauf erzeugt hat
- Übergabe von Beschlussauszügen (Standard aus): Ersteller:in und letzte:r inhaltliche:r
  Bearbeiter:in der Niederschrift der Sitzung

Warum „Ersteller:in und letzte:r inhaltliche:r Bearbeiter:in“: Wer eine Vorlage erstellt, trägt
die fachliche Verantwortung; wer sie zuletzt inhaltlich geändert hat, hat genau den Stand
geschaffen, der zur Freigabe steht. Beide prüfen sonst ihre eigene Arbeit. Frühere
Zwischenbearbeitungen sperren bewusst nicht dauerhaft – ihr Ergebnis hat danach eine andere
Person weiterbearbeitet, und sonst würde schon eine Tippfehler-Korrektur die Freigabe durch
diese Person für immer ausschließen. Wer in der Prüfung selbst ändert, gibt danach nicht frei.

Die Prüfung läuft auf Service-Ebene (:func:`authorize`) – die Oberfläche blendet nur zusätzlich
aus. In einer Vertretung gilt sie für die handelnde **und** die vertretene Person: Niemand gibt
über eine Vertretung eigene Vorlagen frei, und auch nicht die der vertretenen Person.

Wer zuletzt inhaltlich bearbeitet hat, halten die Signal-Hooks ``track_*`` fest (angebunden in
``apps/session/signals.py``), damit der Bearbeitungscode der Vorlagen unverändert bleibt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from apps.session import audit
from apps.session.permissions import role_permissions
from apps.session.services import delegation_service

if TYPE_CHECKING:
    from apps.session.models import SessionTenant, SessionUser

_audit = cast(Any, audit)

PROCESS_PAPER = "paper"
PROCESS_PROTOCOL = "protocol"
PROCESS_ALLOWANCE = "allowance"
PROCESS_FORWARDING = "forwarding"

#: Vorgangsarten mit Beschriftung für die Einstellungen
PROCESSES: dict[str, str] = {
    PROCESS_PAPER: "Vorlagenfreigabe",
    PROCESS_PROTOCOL: "Genehmigung der Niederschrift",
    PROCESS_ALLOWANCE: "Sitzungsgeld und Pauschalen",
    PROCESS_FORWARDING: "Übergabe von Beschlussauszügen",
}

#: Wer je Vorgangsart als „selbst“ gilt (Erläuterung in den Einstellungen)
SELF_DEFINITION: dict[str, str] = {
    PROCESS_PAPER: "Wer die Vorlage erstellt oder zuletzt inhaltlich bearbeitet hat (Text, Anlagen).",
    PROCESS_PROTOCOL: (
        "Wer die Niederschrift erstellt oder zuletzt inhaltlich bearbeitet hat "
        "(allgemeiner Teil, Protokoll der TOPs, Beschlussergebnisse)."
    ),
    PROCESS_ALLOWANCE: "Wer den Abrechnungs- bzw. Monatslauf erzeugt hat.",
    PROCESS_FORWARDING: "Wer die Niederschrift der Sitzung erstellt oder zuletzt inhaltlich bearbeitet hat.",
}

#: Einstellungsfeld am Mandanten je Vorgangsart
TENANT_FIELDS: dict[str, str] = {
    PROCESS_PAPER: "four_eyes_papers",
    PROCESS_PROTOCOL: "four_eyes_protocols",
    PROCESS_ALLOWANCE: "four_eyes_allowances",
    PROCESS_FORWARDING: "four_eyes_forwardings",
}

#: Recht, das der Schritt verlangt (Vertretung nur für Rechte aus DELEGABLE_PERMISSIONS)
PERMISSIONS: dict[str, str] = {
    PROCESS_PAPER: "approve_papers",
    PROCESS_PROTOCOL: "approve_protocols",
    PROCESS_ALLOWANCE: "manage_allowances",
    PROCESS_FORWARDING: "edit_meetings",
}

_SUBJECT = {
    PROCESS_PAPER: "diese Vorlage",
    PROCESS_PROTOCOL: "diese Niederschrift",
    PROCESS_ALLOWANCE: "diese Position",
    PROCESS_FORWARDING: "die Niederschrift dieser Sitzung",
}
_CONSEQUENCE = {
    PROCESS_PAPER: "Freigeben muss eine andere Person.",
    PROCESS_PROTOCOL: "Genehmigen muss eine andere Person.",
    PROCESS_ALLOWANCE: "Genehmigen muss eine andere Person.",
    PROCESS_FORWARDING: "Den Beschlussauszug übergibt eine andere Person.",
}

# Felder, deren Änderung als inhaltliche Bearbeitung zählt
# (verschlüsselte Felder zuletzt: Sie werden nur entschlüsselt, wenn sonst nichts geändert ist)
PAPER_CONTENT_FIELDS = (
    "name",
    "paper_type",
    "main_text",
    "resolution_text",
    "has_financial_impact",
    "financial_impact_note",
    "confidential_text_encrypted",
)
PROTOCOL_CONTENT_FIELDS = ("content", "chair_name", "recorder_name", "content_encrypted")
AGENDA_PROTOCOL_FIELDS = (
    "protocol_note",
    "resolution_text",
    "vote_result",
    "votes_yes",
    "votes_no",
    "votes_abstain",
    "protocol_note_encrypted",
    "resolution_text_encrypted",
)
FILE_CONTENT_FIELDS = ("file", "name", "version")

EDITED = "zuletzt inhaltlich bearbeitet"


class ApprovalError(Exception):
    """Freigabe nicht zulässig; die Meldung ist für die Oberfläche formuliert."""


@dataclass(frozen=True)
class Decision:
    """Ergebnis der Prüfung: erlaubt, ggf. in Vertretung für, sonst mit Begründung."""

    allowed: bool
    on_behalf_of: SessionUser | None = None
    reason: str = ""


def required(tenant: SessionTenant, process: str, obj: Any = None) -> bool:
    """Gilt das Vier-Augen-Prinzip für diesen Vorgang?"""
    if process == PROCESS_PAPER:
        mode = tenant.four_eyes_papers
        return mode == "always" or (mode == "financial" and getattr(obj, "has_financial_impact", None) is True)
    if process == PROCESS_PROTOCOL:
        return bool(tenant.four_eyes_protocols)
    if process == PROCESS_ALLOWANCE:
        return bool(tenant.four_eyes_allowances)
    if process == PROCESS_FORWARDING:
        return bool(tenant.four_eyes_forwardings)
    return False


def responsible(process: str, obj: Any) -> dict[Any, str]:
    """„Selbst“ je Vorgang: Session-Nutzer-ID → Grund (für die Meldung)."""
    result: dict[Any, str] = {}

    def add(pk: Any, grund: str) -> None:
        if pk is not None:
            result.setdefault(pk, grund)

    if process in (PROCESS_PAPER, PROCESS_PROTOCOL):
        add(obj.created_by_id, "erstellt")
        add(obj.content_edited_by_id, EDITED)
    elif process == PROCESS_ALLOWANCE:
        add(obj.created_by_id, "erzeugt")
    elif process == PROCESS_FORWARDING:
        from apps.session.models import SessionProtocol

        protocol = (
            SessionProtocol.objects.filter(meeting_id=obj.meeting_id)
            .values("created_by_id", "content_edited_by_id")
            .first()
        )
        if protocol:
            add(protocol["created_by_id"], "erstellt")
            add(protocol["content_edited_by_id"], EDITED)
    return result


def conflict(process: str, actor: SessionUser, principal: SessionUser | None, selbst: dict[Any, str]) -> str:
    """Begründung, falls Handelnde oder vertretene Person „selbst“ sind – sonst leer."""
    if actor.pk in selbst:
        return f"Vier-Augen-Prinzip: Sie haben {_SUBJECT[process]} {selbst[actor.pk]}. {_CONSEQUENCE[process]}"
    if principal is not None and principal.pk in selbst:
        return (
            f"Vier-Augen-Prinzip: Die vertretene Person ({principal.user.email}) hat {_SUBJECT[process]} "
            f"{selbst[principal.pk]}. Auch in Vertretung gilt: {_CONSEQUENCE[process]}"
        )
    return ""


def visible_to(process: str, obj: Any, person: SessionUser) -> bool:
    """Darf die vertretene Person den Vorgang überhaupt sehen? (nie mehr als sie selbst darf)"""
    perms = role_permissions(person)
    if process == PROCESS_PAPER:
        return "view_papers" in perms and (obj.is_public or "view_non_public_papers" in perms)
    if process == PROCESS_PROTOCOL:
        return "view_protocols" in perms and (obj.meeting.is_public or "view_non_public_meetings" in perms)
    return True


def evaluate(process: str, obj: Any, actor: SessionUser, *, four_eyes: bool = True) -> Decision:
    """
    Darf ``actor`` diesen Schritt ausführen – im eigenen Namen oder in Vertretung?

    Eigene Rechte gehen vor; sonst zählen aktive Vertretungen mit Umfang „Freigaben und
    Genehmigungen“, deren vertretene Person das Recht aus eigenen Rollen hat und den Vorgang
    sehen darf. Mit ``four_eyes=False`` (Zurückweisen, Veröffentlichen) entfällt nur die
    Vier-Augen-Prüfung; Rechte und Vertretung werden trotzdem ermittelt.
    """
    permission = PERMISSIONS[process]
    candidates: list[SessionUser | None] = []
    if permission in role_permissions(actor):
        candidates.append(None)
    candidates.extend(
        principal
        for principal in delegation_service.principals_with_permission(actor, permission)
        if visible_to(process, obj, principal)
    )
    if not candidates:
        return Decision(False, reason="Für diesen Schritt fehlt die Berechtigung – auch aus einer Vertretung.")
    if not four_eyes or not required(actor.tenant, process, obj):
        return Decision(True, candidates[0])
    selbst = responsible(process, obj)
    first_reason = ""
    for principal in candidates:
        reason = conflict(process, actor, principal, selbst)
        if not reason:
            return Decision(True, principal)
        first_reason = first_reason or reason
    return Decision(False, reason=first_reason)


def authorize(process: str, obj: Any, actor: SessionUser, *, four_eyes: bool = True) -> SessionUser | None:
    """
    Schritt freigeben oder mit :class:`ApprovalError` ablehnen.

    Returns:
        Die vertretene Person, wenn der Schritt aus einer Vertretung erfolgt, sonst None.
    """
    decision = evaluate(process, obj, actor, four_eyes=four_eyes)
    if not decision.allowed:
        raise ApprovalError(decision.reason)
    return decision.on_behalf_of


# =============================================================================
# Letzte inhaltliche Bearbeitung festhalten (Signal-Hooks)
# =============================================================================


def _request_user(tenant_id: Any) -> SessionUser | None:
    """Handelnde Person aus dem laufenden Request – nur im selben Mandanten."""
    request = _audit.get_current_request()
    session_user = getattr(request, "session_user", None) if request is not None else None
    if session_user is None or session_user.tenant_id != tenant_id:
        return None
    return cast("SessionUser", session_user)


def _plain(obj: Any, name: str) -> Any:
    """Feldwert für den Vergleich; verschlüsselte Felder im Klartext (Neuverschlüsseln ändert nichts)."""
    value = getattr(obj, name)
    if not name.endswith("_encrypted"):
        return value
    if not value:
        return ""
    getter = getattr(obj, f"get_{name.removesuffix('_encrypted')}_decrypted", None)
    if callable(getter):
        try:
            return getter()
        except Exception:  # noqa: BLE001 – im Zweifel gilt der verschlüsselte Wert als Änderung
            return value
    return value


def _changed(instance: Any, fields: tuple[str, ...]) -> bool:
    """Hat sich eines der Felder gegenüber dem vom Audit geladenen Altzustand geändert?"""
    old = getattr(instance, "_audit_old", None)
    if old is None:
        return False
    # Beziehungen des gespeicherten Objekts mitnutzen – der Altzustand entschlüsselt ohne weitere Abfragen
    for relation in ("meeting", "tenant"):
        cached = instance._state.fields_cache.get(relation)
        if cached is not None and getattr(old, f"{relation}_id", None) == cached.pk:
            old._state.fields_cache[relation] = cached
    return any(_plain(old, name) != _plain(instance, name) for name in fields)


def track_paper_edit(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
    """post_save(SessionPaper): inhaltliche Änderung → letzte:r Bearbeiter:in."""
    if kwargs.get("raw"):
        return
    session_user = _request_user(instance.tenant_id)
    if session_user is None or instance.content_edited_by_id == session_user.pk:
        return
    if created or _changed(instance, PAPER_CONTENT_FIELDS):
        sender.objects.filter(pk=instance.pk).update(content_edited_by=session_user)
        instance.content_edited_by = session_user


def track_protocol_edit(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
    """post_save(SessionProtocol): allgemeiner Teil oder Unterschriften geändert."""
    if kwargs.get("raw"):
        return
    session_user = _request_user(instance.meeting.tenant_id)
    if session_user is None or instance.content_edited_by_id == session_user.pk:
        return
    if created or _changed(instance, PROTOCOL_CONTENT_FIELDS):
        sender.objects.filter(pk=instance.pk).update(content_edited_by=session_user)
        instance.content_edited_by = session_user


def track_agenda_protocol_edit(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
    """post_save(SessionAgendaItem): Protokolltext oder Beschlussergebnis eines TOP geändert."""
    from apps.session.models import SessionProtocol

    if kwargs.get("raw") or created or not _changed(instance, AGENDA_PROTOCOL_FIELDS):
        return
    request = _audit.get_current_request()
    session_user = getattr(request, "session_user", None) if request is not None else None
    if session_user is None:
        return
    SessionProtocol.objects.filter(
        meeting_id=instance.meeting_id,
        meeting__tenant_id=session_user.tenant_id,
        status__in=("draft", "review"),
    ).exclude(content_edited_by=session_user).update(content_edited_by=session_user)


def _track_file(instance: Any, *, deleted: bool, created: bool = False) -> None:
    from apps.session.models import SessionPaper

    if not instance.paper_id or _audit.is_tenant_deleting(instance.tenant_id):
        return
    if not (deleted or created or _changed(instance, FILE_CONTENT_FIELDS)):
        return
    session_user = _request_user(instance.tenant_id)
    if session_user is None:
        return
    SessionPaper.objects.filter(pk=instance.paper_id, tenant_id=session_user.tenant_id).exclude(
        content_edited_by=session_user
    ).update(content_edited_by=session_user)


def track_file_save(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
    """post_save(SessionFile): Anlage einer Vorlage hinzugefügt oder ersetzt."""
    if not kwargs.get("raw"):
        _track_file(instance, deleted=False, created=created)


def track_file_delete(sender: Any, instance: Any, **kwargs: Any) -> None:
    """post_delete(SessionFile): Anlage einer Vorlage entfernt."""
    _track_file(instance, deleted=True)
