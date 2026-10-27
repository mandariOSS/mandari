# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Provisioning-API v2: Soll-Zustand einer Work-Organisation prüfen und anwenden (Issue #1132).

``parse_work_spec`` prüft Typen und Formate des JSON-Körpers, ``put_work_organization`` legt an, übernimmt
(``adopt``) oder ändert. Nicht mitgeschickte Felder bleiben unverändert. Eine Wiederholung mit demselben
Inhalt schreibt nichts und verschickt keine weitere Einladung. Nutzerdaten werden nie gelöscht oder
verändert: „gesperrt“ und „beendet“ schalten nur ``is_active`` ab.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from apps.tenants.models import (
    PROVISIONING_STATUS_ACTIVE,
    PROVISIONING_STATUS_CHOICES,
    Organization,
    PartyGroup,
    UserInvitation,
)

from ..services import (
    create_organization_with_admin_invitation,
    find_system_inviter,
    invitation_accept_url,
    send_admin_invitation,
)

logger = logging.getLogger(__name__)

REF_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
# Neue Slugs wie in v1; bestehende (übernommene) Slugs dürfen davon abweichen
NEW_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")
ANY_SLUG_RE = re.compile(r"[-a-zA-Z0-9_]{1,100}")
# Plan als technische Kennung; welche Pläne es gibt, entscheidet das aufrufende System
PLAN_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,49}")
# Gemeinden haben acht Stellen, höhere Ebenen den gekürzten Schlüssel (Land 2, Bezirk 3, Kreis 5)
AGS_RE = re.compile(r"\d{2}|\d{3}|\d{5}|\d{8}")
MAX_LIMIT = 2_147_483_647
DEFAULT_PLAN = "hosted"
STATUSES = tuple(value for value, _label in PROVISIONING_STATUS_CHOICES)
PARTY_NAME_MAX = 100


class ProvisioningError(Exception):
    """Fehler mit HTTP-Status, fester Kennung, optional Fehlern je Feld und fester Meldung (nie Ausnahmetexte)."""

    def __init__(self, status: int, code: str, fields: Mapping[str, str] | None = None, *, message: str = "") -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.fields = dict(fields or {})
        self.message = message

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {"error": self.code}
        if self.fields:
            data["fields"] = self.fields
        if self.message:
            data["message"] = self.message
        return data


def validation_error(fields: Mapping[str, str]) -> ProvisioningError:
    return ProvisioningError(400, "validation", fields)


class _FieldError(Exception):
    """Ungültiger Wert eines Feldes; ``field`` weicht ab, wenn der Fehler zu einem anderen Feld gehört."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


def is_valid_ref(ref: str) -> bool:
    """1–64 Zeichen ``[A-Za-z0-9._-]``; „.“ und „..“ lassen sich in einer URL nicht adressieren."""
    return REF_RE.fullmatch(ref) is not None and ref not in {".", ".."}


# ---------------------------------------------------------------------------
# Eingabe prüfen
# ---------------------------------------------------------------------------


def _text(value: object, max_length: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise _FieldError("Zeichenkette erwartet")
    value = value.strip()
    if required and not value:
        raise _FieldError("darf nicht leer sein")
    if len(value) > max_length:
        raise _FieldError(f"höchstens {max_length} Zeichen")
    return value


def _limit(value: object, minimum: int, *, nullable: bool) -> int | None:
    if value is None and nullable:
        return None
    # bool ist in Python ein int; true/false sind hier Typfehler
    if not isinstance(value, int) or isinstance(value, bool):
        raise _FieldError("ganze Zahl oder null erwartet" if nullable else "ganze Zahl erwartet")
    if not minimum <= value <= MAX_LIMIT:
        raise _FieldError(f"Zahl von {minimum} bis {MAX_LIMIT} erwartet")
    return value


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _parse_name(value: object) -> str:
    return _text(value, 200, required=True)


def _parse_slug(value: object) -> str:
    if not isinstance(value, str):
        raise _FieldError("Zeichenkette erwartet")
    if ANY_SLUG_RE.fullmatch(value) is None:
        raise _FieldError("nur a-z, A-Z, 0-9, Bindestrich und Unterstrich, höchstens 100 Zeichen")
    return value


def _parse_admin_email(value: object) -> str:
    email = _text(value, 254, required=True).lower()
    try:
        validate_email(email)
    except ValidationError as exc:
        raise _FieldError("gültige E-Mail-Adresse erwartet") from exc
    return email


def _parse_body_ags(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or AGS_RE.fullmatch(value) is None:
        raise _FieldError("Amtlicher Gemeindeschlüssel aus 8 Ziffern erwartet (Kreis 5, Bezirk 3, Land 2)")
    return value


def _parse_body_id(value: object) -> uuid.UUID | None:
    if value is None:
        return None
    parsed = _as_uuid(value) if isinstance(value, str) else None
    if parsed is None:
        raise _FieldError("UUID erwartet")
    return parsed


def _parse_party_group(value: object) -> str | None:
    if value is None:
        return None
    party = _text(value, PARTY_NAME_MAX, required=True)
    if _as_uuid(party) is None and not slugify(party):
        raise _FieldError("Name oder Kennung der Partei erwartet")
    return party


def _parse_status(value: object) -> str:
    if not isinstance(value, str) or value not in STATUSES:
        raise _FieldError(f"erlaubt: {', '.join(STATUSES)}")
    return value


def _parse_member_limit(value: object) -> int | None:
    return _limit(value, 1, nullable=True)


def _parse_guest_limit(value: object) -> int | None:
    return _limit(value, 0, nullable=False)


def _parse_billing_reference(value: object) -> str:
    return _text(value, 100)


def _parse_plan(value: object) -> str:
    if not isinstance(value, str) or PLAN_RE.fullmatch(value) is None:
        raise _FieldError("Kennung aus a-z, 0-9, Punkt, Unterstrich und Bindestrich erwartet, höchstens 50 Zeichen")
    return value


def _parse_adopt(value: object) -> bool:
    if not isinstance(value, bool):
        raise _FieldError("true oder false erwartet")
    return value


_PARSERS: dict[str, Callable[[object], Any]] = {
    "name": _parse_name,
    "slug": _parse_slug,
    "admin_email": _parse_admin_email,
    "body_ags": _parse_body_ags,
    "body_id": _parse_body_id,
    "party_group": _parse_party_group,
    "status": _parse_status,
    "member_limit": _parse_member_limit,
    "guest_limit": _parse_guest_limit,
    "billing_reference": _parse_billing_reference,
    "plan": _parse_plan,
    "adopt": _parse_adopt,
}


@dataclass(frozen=True)
class WorkSpec:
    """Geprüfter Soll-Zustand. ``sent`` nennt die mitgeschickten Felder; alle anderen bleiben unverändert."""

    sent: frozenset[str]
    name: str = ""
    slug: str = ""
    admin_email: str = ""
    body_ags: str | None = None
    body_id: uuid.UUID | None = None
    party_group: str | None = None
    status: str = PROVISIONING_STATUS_ACTIVE
    member_limit: int | None = None
    guest_limit: int | None = None
    billing_reference: str = ""
    plan: str = DEFAULT_PLAN
    adopt: bool = False


def parse_work_spec(data: object) -> WorkSpec:
    """JSON-Körper prüfen: unbekannte Felder und falsche Typen sind Fehler, nichts wird still übergangen."""
    if not isinstance(data, dict):
        raise validation_error({"__all__": "JSON-Objekt erwartet"})
    errors: dict[str, str] = {}
    values: dict[str, Any] = {}
    for key, value in data.items():
        parser = _PARSERS.get(key)
        if parser is None:
            errors[str(key)] = "unbekanntes Feld"
            continue
        try:
            values[key] = parser(value)
        except _FieldError as exc:
            errors[key] = exc.message
    if errors:
        raise validation_error(errors)
    return WorkSpec(sent=frozenset(values), **values)


# ---------------------------------------------------------------------------
# Kommune und Partei auflösen
# ---------------------------------------------------------------------------


def _body_queryset() -> models.QuerySet[Any]:
    """
    Kommunen (OParlBody) über die Beziehung ``Organization.body``.

    Die Plattform-Schicht importiert ``insight_core`` nicht (Schichtregel in pyproject.toml); das Modell
    kommt deshalb aus dem Fremdschlüssel der Organisation.
    """
    field: Any = Organization._meta.get_field("body")
    model = field.related_model
    if not (isinstance(model, type) and issubclass(model, models.Model)):
        raise RuntimeError("Organization.body verweist auf kein Modell")
    return model._default_manager.filter(deleted=False)


def _apply_body(organization: Organization, spec: WorkSpec) -> bool:
    """Heimatkommune nach ``body_id`` bzw. ``body_ags`` setzen; True bei Änderung."""
    body_id = spec.body_id if "body_id" in spec.sent else None
    ags = spec.body_ags if "body_ags" in spec.sent else None
    current = organization.body if organization.body_id else None

    if body_id is not None:
        if current is not None and current.pk == body_id:
            if ags is not None and current.ags != ags:
                raise _FieldError("passt nicht zur Kommune aus body_id", "body_ags")
            return False
        found = list(_body_queryset().filter(pk=body_id).values_list("ags", flat=True)[:1])
        if not found:
            raise _FieldError("keine Kommune mit dieser Kennung", "body_id")
        if ags is not None and found[0] != ags:
            raise _FieldError("passt nicht zur Kommune aus body_id", "body_ags")
        organization.body_id = body_id
        return True

    if ags is None:
        # Ausdrücklich null: Heimatkommune entfernen (bei aktiven Organisationen lehnt clean() das ab)
        if current is None:
            return False
        organization.body = None
        return True

    if current is not None and current.ags == ags:
        return False
    candidates = list(_body_queryset().filter(ags=ags).values_list("pk", "is_listed"))
    if not candidates:
        raise _FieldError("keine Kommune mit diesem Gemeindeschlüssel", "body_ags")
    if len(candidates) > 1:
        # Mehrere Einträge mit demselben Schlüssel: eindeutig nur, wenn genau einer öffentlich gelistet ist
        listed = [pk for pk, is_listed in candidates if is_listed]
        if len(listed) != 1:
            raise _FieldError("Gemeindeschlüssel ist mehrdeutig; bitte body_id angeben", "body_ags")
        organization.body_id = listed[0]
        return True
    organization.body_id = candidates[0][0]
    return True


def _party_matches(party: PartyGroup, value: str) -> bool:
    parsed = _as_uuid(value)
    if parsed is not None:
        return bool(party.pk == parsed)
    return party.name.casefold() == value.casefold() or party.slug == slugify(value)[:100]


def _resolve_party(value: str) -> PartyGroup:
    """Partei nach Kennung, Name oder Slug; unbekannte Namen werden wie in v1 angelegt."""
    parsed = _as_uuid(value)
    if parsed is not None:
        party = PartyGroup.objects.filter(pk=parsed).first()
        if party is None:
            raise _FieldError("keine Partei mit dieser Kennung", "party_group")
        return party
    slug = slugify(value)[:100]
    matches = list(PartyGroup.objects.filter(Q(name__iexact=value) | Q(slug=slug))[:2])
    if len(matches) > 1:
        raise _FieldError("Name ist mehrdeutig; bitte die Kennung (id) angeben", "party_group")
    if matches:
        return matches[0]
    return PartyGroup.objects.create(name=value, slug=slug)


def _apply_party(organization: Organization, value: str | None) -> bool:
    current = organization.party_group if organization.party_group_id else None
    if value is None:
        if current is None:
            return False
        organization.party_group = None
        return True
    if current is not None and _party_matches(current, value):
        return False
    organization.party_group = _resolve_party(value)
    return True


# ---------------------------------------------------------------------------
# Soll-Zustand anwenden
# ---------------------------------------------------------------------------

_CLEAN_FIELDS = {"body": "body_ags", "party_group": "party_group"}


def _validate(organization: Organization) -> None:
    """Dieselben Regeln wie im Admin (``Organization.clean``): aktive Organisationen brauchen Kommune und Partei."""
    try:
        organization.clean()
    except ValidationError as exc:
        if hasattr(exc, "error_dict"):
            fields = {_CLEAN_FIELDS.get(key, key): " ".join(messages) for key, messages in exc.message_dict.items()}
        else:
            fields = {"__all__": " ".join(exc.messages)}
        raise validation_error(fields) from exc


def _apply_status(organization: Organization, status: str) -> list[str]:
    """aktiv → is_active, gesperrt/beendet → nicht aktiv; Zeitpunkt nur bei einer echten Änderung."""
    changed: list[str] = []
    is_active = status == PROVISIONING_STATUS_ACTIVE
    if organization.is_active != is_active:
        organization.is_active = is_active
        changed.append("is_active")
    if organization.provisioning_status != status:
        organization.provisioning_status = status
        changed.append("provisioning_status")
    if changed:
        organization.provisioning_status_changed_at = timezone.now()
        changed.append("provisioning_status_changed_at")
    return changed


def _apply_fields(organization: Organization, spec: WorkSpec) -> list[str]:
    """Mitgeschickte Felder auf die (gespeicherte oder neue) Organisation anwenden und prüfen."""
    changed: list[str] = []
    for name in ("name", "member_limit", "guest_limit", "billing_reference", "plan"):
        if name in spec.sent and getattr(organization, name) != getattr(spec, name):
            setattr(organization, name, getattr(spec, name))
            changed.append(name)
    if "status" in spec.sent:
        changed += _apply_status(organization, spec.status)

    errors: dict[str, str] = {}
    if spec.sent & {"body_ags", "body_id"}:
        try:
            if _apply_body(organization, spec):
                changed.append("body")
        except _FieldError as exc:
            errors[exc.field or "body_ags"] = exc.message
    if "party_group" in spec.sent:
        try:
            if _apply_party(organization, spec.party_group):
                changed.append("party_group")
        except _FieldError as exc:
            errors[exc.field or "party_group"] = exc.message
    if errors:
        raise validation_error(errors)
    _validate(organization)
    return changed


def _update(organization: Organization, spec: WorkSpec, *, also_changed: tuple[str, ...] = ()) -> list[str]:
    if "slug" in spec.sent and spec.slug != organization.slug:
        raise ProvisioningError(409, "slug_immutable")
    changed = [*also_changed, *_apply_fields(organization, spec)]
    if changed:
        organization.save(update_fields=[*changed, "updated_at"])
    return changed


def _required(spec: WorkSpec, names: tuple[str, ...]) -> None:
    missing = {name: "erforderlich" for name in names if name not in spec.sent}
    if missing:
        raise validation_error(missing)


def _create(ref: str, spec: WorkSpec) -> tuple[Organization, UserInvitation]:
    if spec.status != PROVISIONING_STATUS_ACTIVE:
        # Anlegen lädt die Admin-Person ein; eine gesperrte oder beendete Organisation darf das nicht
        raise ProvisioningError(
            409,
            "create_requires_active",
            message="Neue Organisationen lassen sich nur mit Status „aktiv“ anlegen (mit Einladung der "
            "Admin-Person). Erst anlegen, dann sperren oder beenden.",
        )
    if NEW_SLUG_RE.fullmatch(spec.slug) is None:
        raise validation_error({"slug": "3-40 Zeichen, nur a-z, 0-9 und Bindestrich"})
    inviter = find_system_inviter()
    if inviter is None:
        logger.error("[Provisioning v2] Kein Superuser als Einladungs-Absender vorhanden")
        raise ProvisioningError(500, "no_system_user")

    fields: dict[str, Any] = {
        "name": spec.name,
        "slug": spec.slug,
        "plan": spec.plan,  # ohne Angabe DEFAULT_PLAN wie in v1
        "billing_reference": spec.billing_reference,
        "member_limit": spec.member_limit,
        "provisioning_ref": ref,
    }
    if "guest_limit" in spec.sent:
        fields["guest_limit"] = spec.guest_limit
    # Status (Vorgabe „aktiv“), Kommune und Partei an einer ungespeicherten Organisation auflösen und
    # prüfen; angelegt wird erst danach über den gemeinsamen Weg mit v1
    candidate = Organization(**fields)
    _apply_status(candidate, spec.status)
    _apply_fields(candidate, spec)
    fields.update(
        is_active=candidate.is_active,
        provisioning_status=candidate.provisioning_status,
        provisioning_status_changed_at=candidate.provisioning_status_changed_at,
        body_id=candidate.body_id,
        party_group=candidate.party_group,
    )
    return create_organization_with_admin_invitation(admin_email=spec.admin_email, inviter=inviter, **fields)


@dataclass
class PutResult:
    organization: Organization
    changed: list[str]
    created: bool = False
    adopted: bool = False
    invitation_sent: bool | None = None


def _locked_by_ref(ref: str) -> Organization | None:
    """Organisation mit dieser Referenz, gesperrt bis zum Ende der Transaktion."""
    return Organization.objects.select_for_update().filter(provisioning_ref=ref).first()


def _adopt_or_create(ref: str, spec: WorkSpec) -> tuple[PutResult, UserInvitation | None]:
    # Übernahme braucht nur den Slug (keine Einladung), Anlegen auch Name und Admin-E-Mail
    _required(spec, ("slug",) if spec.adopt else ("name", "slug", "admin_email"))
    existing = Organization.objects.select_for_update().filter(slug=spec.slug).first()
    if existing is None:
        if spec.adopt:
            # Übernahme erwartet eine bestehende Organisation; ein Tippfehler legt nichts an und lädt niemanden ein
            raise ProvisioningError(409, "adopt_not_found")
        organization, invitation = _create(ref, spec)
        return PutResult(organization, changed=["created"], created=True), invitation
    if existing.provisioning_ref == ref:
        # Eine gleichzeitige Anfrage mit derselben Referenz war schneller (Übernahme oder Anlegen):
        # nach ihrem Commit trägt die Organisation die Referenz schon, also normal abgleichen
        return PutResult(existing, changed=_update(existing, spec)), None
    if not spec.adopt:
        raise ProvisioningError(409, "slug_taken")
    if existing.provisioning_ref not in (None, ref):
        # Schon unter einer anderen Referenz bereitgestellt
        raise ProvisioningError(409, "ref_conflict")
    existing.provisioning_ref = ref
    changed = _update(existing, spec, also_changed=("provisioning_ref",))
    return PutResult(existing, changed=changed, adopted=True), None


def put_work_organization(ref: str, spec: WorkSpec) -> PutResult:
    """
    Soll-Zustand unter ``ref`` herstellen: ändern, übernehmen (``adopt``) oder anlegen.

    Alles läuft in einer Transaktion (eine bei der Auflösung angelegte Partei verschwindet bei einem
    Fehler wieder). Die Einladung geht erst nach dem Commit hinaus und nur beim Anlegen.
    """
    invitation: UserInvitation | None = None
    try:
        with transaction.atomic():
            organization = _locked_by_ref(ref)
            if organization is not None:
                result = PutResult(organization, changed=_update(organization, spec))
            else:
                result, invitation = _adopt_or_create(ref, spec)
    except IntegrityError as exc:
        # Gleichzeitige Anfrage mit derselben Referenz oder demselben Slug; eine Wiederholung trifft den Bestand
        logger.warning("[Provisioning v2] Konflikt beim Schreiben von ref=%s", ref)
        raise ProvisioningError(409, "conflict") from exc

    if invitation is not None:
        result.invitation_sent = send_admin_invitation(
            result.organization, invitation, invitation_accept_url(invitation)
        )
    action = "angelegt" if result.created else "übernommen" if result.adopted else "abgeglichen"
    logger.info(
        "[Provisioning v2] ref=%s slug=%s %s, geändert: %s",
        ref,
        result.organization.slug,
        action,
        ", ".join(result.changed) or "nichts",
    )
    return result
