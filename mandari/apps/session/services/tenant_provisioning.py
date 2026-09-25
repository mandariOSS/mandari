# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mandanten anlegen, deaktivieren und reaktivieren (Issue #317, Teil A).

**Anlegen:** ``provision_tenant`` macht einen Session-Mandanten mit einem Aufruf arbeitsfähig –
Standardrollen (auch „Revision“ und „Datenschutz“), Nummernkreis-Preset, aktuelle Wahlperiode,
optional eine Gremienvorlage, erster Administrator und Schlüssel für verschlüsselte Felder. Der
Befehl ``session_create_tenant`` und der Admin-Assistent „Mandant anlegen“ rufen denselben Service
auf. Der Lauf ist idempotent: Ein zweiter Aufruf mit demselben Slug ergänzt Fehlendes, verändert
Bestehendes aber nicht; nur Nummernkreis-Preset und Angaben der genannten Wahlperiode werden
angeglichen. Ein Prüflauf (``dry_run``) führt alle Schritte aus und rollt sie zurück.

Profile und Gremienvorlagen stehen in ``apps/session/presets/mandanten.json``; eine eigene Datei
gleicher Struktur lässt sich angeben (``load_presets``).

**Deaktivieren und Reaktivieren:** ``set_tenant_active`` speichert den Mandanten einzeln; das
Signal ruft ``on_active_changed``, das seine Bürgerportal-Quelle zurücknimmt bzw. wiederherstellt
(``insight_service.retract_source``/``restore_source``) und beides im Audit-Log des Mandanten
festhält. So greift die Rücknahme auch beim Speichern im Admin-Formular.

Sicherheit: Ausgaben und Protokolle enthalten nie Passwörter oder Tokens. Neue Administratoren
kommen über die bestehende Einladung (Link nur per E-Mail); vorhandene Konten werden direkt
Mitglied. Fehlermeldungen entstehen aus bekannten Werten (``ProvisioningError``), nie aus
Ausnahmetexten.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

# Reservierte Slugs aus derselben Quelle wie die Middleware, damit beide nicht auseinanderlaufen
from apps.session.middleware import RESERVED_SLUGS

if TYPE_CHECKING:
    from apps.session.models import SessionInvitation, SessionTenant
    from apps.session.services.insight_service import PortalChange

logger = logging.getLogger(__name__)

#: Mitgelieferte Profile und Gremienvorlagen
PRESET_FILE = Path(__file__).resolve().parent.parent / "presets" / "mandanten.json"
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
#: AGS: 2 (Land), 3 (Regierungsbezirk), 5 (Kreis) oder 8 Stellen (Gemeinde)
AGS_RE = re.compile(r"\d{2}(?:\d|\d{3}|\d{6})?")
ADMIN_STATUS_LABELS = {
    "member": "vorhandenes Konto als Administrator aufgenommen",
    "already_admin": "vorhandenes Konto ist bereits Administrator",
    "invited": "Einladung angelegt",
    "invitation_pending": "offene Einladung besteht bereits",
}


class ProvisioningError(ValueError):
    """Angaben unvollständig oder unzulässig; ``messages`` sind fertige, ausgabesichere Sätze."""

    def __init__(self, messages: list[str]) -> None:
        super().__init__("; ".join(messages))
        self.messages = messages


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Gremium:
    name: str
    short_name: str
    organization_type: str


@dataclass(frozen=True)
class GremienVorlage:
    key: str
    label: str
    gremien: tuple[Gremium, ...]


@dataclass(frozen=True)
class Profil:
    key: str
    label: str
    body_type: str
    numbering: str
    term_name: str
    term_number: int | None
    term_start: date
    term_end: date | None
    committees: str


@dataclass(frozen=True)
class PresetKatalog:
    profiles: dict[str, Profil]
    committee_templates: dict[str, GremienVorlage]


def _organization_types() -> set[str]:
    from apps.session.models import SessionOrganization

    return {str(key) for key, _ in SessionOrganization._meta.get_field("organization_type").choices or []}


def _body_types() -> set[str]:
    from apps.session.models import SessionTenant

    return {key for key, _ in SessionTenant.BODY_TYPE_CHOICES}


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError
    return date.fromisoformat(value)


def load_presets(path: Path | None = None) -> PresetKatalog:
    """Profile und Gremienvorlagen lesen und prüfen; ``ProvisioningError`` bei ungültiger Datei."""
    from apps.session.services import numbering_service

    quelle = path or PRESET_FILE
    try:
        daten = json.loads(quelle.read_text(encoding="utf-8"))
    except OSError:
        raise ProvisioningError(["Die Preset-Datei ließ sich nicht lesen."]) from None
    except ValueError:
        raise ProvisioningError(["Die Preset-Datei ist kein gültiges JSON."]) from None
    if not isinstance(daten, dict):
        raise ProvisioningError(["Die Preset-Datei muss ein JSON-Objekt sein."])

    arten = _organization_types()
    vorlagen: dict[str, GremienVorlage] = {}
    for key, eintrag in (daten.get("gremienvorlagen") or {}).items():
        gremien = eintrag.get("gremien") if isinstance(eintrag, dict) else None
        if not isinstance(gremien, list) or not gremien:
            raise ProvisioningError([f"Gremienvorlage „{key}“: Liste „gremien“ fehlt."])
        liste = []
        for gremium in gremien:
            name = str((gremium or {}).get("name") or "").strip()
            art = str((gremium or {}).get("art") or "committee")
            if not name or len(name) > 500 or art not in arten:
                raise ProvisioningError([f"Gremienvorlage „{key}“: Gremium ohne Namen oder mit unbekannter Art."])
            liste.append(Gremium(name, str(gremium.get("kurzname") or "")[:100], art))
        vorlagen[str(key)] = GremienVorlage(str(key), str(eintrag.get("bezeichnung") or key), tuple(liste))

    profile: dict[str, Profil] = {}
    for key, eintrag in (daten.get("profile") or {}).items():
        if not isinstance(eintrag, dict):
            raise ProvisioningError([f"Profil „{key}“ ist kein Objekt."])
        term = eintrag.get("wahlperiode") or {}
        try:
            start = _parse_date(term.get("beginn"))
            ende = _parse_date(term.get("ende"))
            nummer = term.get("nummer")
            nummer = int(nummer) if nummer is not None else None
        except (TypeError, ValueError):
            raise ProvisioningError([f"Profil „{key}“: Wahlperiode mit ungültigem Datum oder Nummer."]) from None
        profil = Profil(
            key=str(key),
            label=str(eintrag.get("bezeichnung") or key),
            body_type=str(eintrag.get("koerperschaftstyp") or ""),
            numbering=str(eintrag.get("nummernkreis") or "standard"),
            term_name=str(term.get("name") or ""),
            term_number=nummer,
            term_start=start or date.min,
            term_end=ende,
            committees=str(eintrag.get("gremienvorlage") or ""),
        )
        fehler = []
        if start is None or not profil.term_name:
            fehler.append(f"Profil „{key}“: Wahlperiode braucht Name und Beginn.")
        if profil.numbering not in numbering_service.PRESETS:
            fehler.append(f"Profil „{key}“: unbekanntes Nummernkreis-Preset.")
        if profil.body_type and profil.body_type not in _body_types():
            fehler.append(f"Profil „{key}“: unbekannter Körperschaftstyp.")
        if profil.committees and profil.committees not in vorlagen:
            fehler.append(f"Profil „{key}“: unbekannte Gremienvorlage.")
        if fehler:
            raise ProvisioningError(fehler)
        profile[str(key)] = profil
    return PresetKatalog(profiles=profile, committee_templates=vorlagen)


# ---------------------------------------------------------------------------
# Angaben
# ---------------------------------------------------------------------------


@dataclass
class TenantSpec:
    """Was angelegt werden soll; leere Texte bedeuten „nicht angegeben“."""

    name: str
    slug: str
    admin_email: str
    short_name: str = ""
    body_type: str = ""
    ags: str = ""
    numbering: str = "standard"
    term_name: str = ""
    term_number: int | None = None
    term_start: date | None = None
    term_end: date | None = None
    committees: str = ""


def build_spec(
    *,
    name: str,
    slug: str,
    admin_email: str,
    catalog: PresetKatalog,
    profile: str = "",
    short_name: str = "",
    body_type: str | None = None,
    ags: str = "",
    numbering: str | None = None,
    term_name: str | None = None,
    term_number: int | None = None,
    term_start: date | None = None,
    term_end: date | None = None,
    committees: str | None = None,
) -> TenantSpec:
    """
    Angaben aus einem Profil und ausdrücklichen Werten zusammensetzen: ``None`` übernimmt den Wert
    des Profils, ein ausdrücklicher Wert gilt vor dem Profil. ``committees=""`` legt keine Gremien an.
    Mit eigenem ``term_name`` gilt eine eigene Wahlperiode: Nummer, Beginn und Ende kommen dann nur
    aus den ausdrücklichen Werten.
    """
    vorlage = catalog.profiles.get(profile) if profile else None
    if profile and vorlage is None:
        raise ProvisioningError([f"Unbekanntes Profil „{profile}“."])
    if term_name is not None or vorlage is None:
        periode = ((term_name or "").strip(), term_number, term_start, term_end)
    else:
        periode = (
            vorlage.term_name,
            term_number if term_number is not None else vorlage.term_number,
            term_start or vorlage.term_start,
            term_end or vorlage.term_end,
        )
    return TenantSpec(
        name=name.strip(),
        slug=slug.strip(),
        admin_email=admin_email.strip().lower(),
        short_name=short_name.strip(),
        body_type=body_type if body_type is not None else (vorlage.body_type if vorlage else ""),
        ags=ags.strip(),
        numbering=numbering or (vorlage.numbering if vorlage else "standard"),
        term_name=periode[0],
        term_number=periode[1],
        term_start=periode[2],
        term_end=periode[3],
        committees=committees if committees is not None else (vorlage.committees if vorlage else ""),
    )


def validate_spec(spec: TenantSpec, catalog: PresetKatalog) -> list[str]:
    """Fehlermeldungen zu den Angaben (leer = in Ordnung)."""
    from apps.session.services import numbering_service

    fehler: list[str] = []
    if not spec.name or len(spec.name) > 255:
        fehler.append("Bitte einen Namen mit höchstens 255 Zeichen angeben.")
    if not SLUG_RE.fullmatch(spec.slug or "") or len(spec.slug) > 100:
        fehler.append("Der Slug besteht aus Kleinbuchstaben, Ziffern und Bindestrichen (höchstens 100 Zeichen).")
    elif spec.slug in RESERVED_SLUGS:
        fehler.append("Dieser Slug ist für Systempfade reserviert.")
    if len(spec.short_name) > 50:
        fehler.append("Der Kurzname hat höchstens 50 Zeichen.")
    if spec.body_type and spec.body_type not in _body_types():
        fehler.append("Unbekannter Körperschaftstyp.")
    if spec.ags and not AGS_RE.fullmatch(spec.ags):
        fehler.append("Der Amtliche Gemeindeschlüssel hat 2, 3, 5 oder 8 Ziffern.")
    preset = numbering_service.PRESETS.get(spec.numbering)
    if preset is None:
        fehler.append("Unbekanntes Nummernkreis-Preset.")
    if not spec.term_name or len(spec.term_name) > 255:
        fehler.append("Bitte die aktuelle Wahlperiode mit Namen angeben.")
    if spec.term_start is None or spec.term_end is None:
        fehler.append("Die Wahlperiode braucht Beginn und Ende.")
    elif spec.term_end < spec.term_start:
        fehler.append("Das Ende der Wahlperiode liegt vor ihrem Beginn.")
    if spec.term_number is not None and not 1 <= spec.term_number <= 32767:
        fehler.append("Die Nummer der Wahlperiode liegt zwischen 1 und 32767.")
    if preset is not None and spec.term_number is None and any("{wp" in kreis.pattern for kreis in preset.kreise):
        fehler.append("Das Nummernkreis-Preset zählt je Wahlperiode: Bitte die Nummer der Wahlperiode angeben.")
    if spec.committees and spec.committees not in catalog.committee_templates:
        fehler.append("Unbekannte Gremienvorlage.")
    try:
        validate_email(spec.admin_email)
    except ValidationError:
        fehler.append("Bitte eine gültige E-Mail-Adresse für den ersten Administrator angeben.")
    return fehler


# ---------------------------------------------------------------------------
# Anlegen
# ---------------------------------------------------------------------------


@dataclass
class ProvisioningResult:
    """Ergebnis für Ausgabe und Admin; enthält keine Geheimnisse."""

    slug: str
    name: str
    created: bool
    dry_run: bool = False
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    admin_status: str = ""
    invitation_sent: bool | None = None
    tenant_id: Any = None
    _invitation: SessionInvitation | None = field(default=None, repr=False)


def provision_tenant(
    spec: TenantSpec,
    *,
    catalog: PresetKatalog | None = None,
    dry_run: bool = False,
    actor: str = "",
) -> ProvisioningResult:
    """Mandanten anlegen bzw. vervollständigen. Wirft ``ProvisioningError`` bei ungültigen Angaben."""
    katalog = catalog or load_presets()
    fehler = validate_spec(spec, katalog)
    if fehler:
        raise ProvisioningError(fehler)
    with transaction.atomic():
        result = _apply(spec, katalog, actor=actor)
        if dry_run:
            # Prüflauf: alle Schritte wirklich ausführen, dann zurückrollen (keine E-Mail, kein Rest)
            transaction.set_rollback(True)
            result.dry_run = True
    einladung, result._invitation = result._invitation, None
    if einladung is not None and not dry_run:
        from apps.session.services import user_invitations

        result.invitation_sent = user_invitations.send_user_invitation(einladung)
        if result.invitation_sent:
            result.steps.append(f"Einladungs-E-Mail an {einladung.email} versendet.")
        else:
            result.warnings.append(
                "Die Einladungs-E-Mail ließ sich nicht versenden. Bitte den Mailversand prüfen und die "
                "Einladung im Sitzungsdienst unter Einstellungen → Benutzer erneut senden."
            )
    return result


def _apply(spec: TenantSpec, catalog: PresetKatalog, *, actor: str) -> ProvisioningResult:
    from apps.session import audit
    from apps.session.models import SessionTenant

    tenant = SessionTenant.objects.filter(slug=spec.slug).first()
    result = ProvisioningResult(slug=spec.slug, name=spec.name, created=tenant is None)
    if tenant is None:
        tenant = SessionTenant.objects.create(
            name=spec.name,
            slug=spec.slug,
            short_name=spec.short_name,
            body_type=spec.body_type or None,
            ags=spec.ags or None,
        )
        result.steps.append(f"Mandant „{tenant.name}“ ({tenant.slug}) angelegt.")
    else:
        result.name = tenant.name
        result.steps.append(f"Mandant „{tenant.name}“ ({tenant.slug}) bestand bereits – Fehlendes wird ergänzt.")
        _fill_blank_fields(tenant, spec, result)
    result.tenant_id = tenant.pk
    if tenant.body_type or tenant.ags:
        teile = [tenant.get_body_type_display() if tenant.body_type else "", f"AGS {tenant.ags}" if tenant.ags else ""]
        result.steps.append("Körperschaft: " + ", ".join(t for t in teile if t) + ".")

    _ensure_roles(tenant, result)
    _ensure_term(tenant, spec, result)
    _ensure_numbering(tenant, spec, result)
    _ensure_committees(tenant, spec, catalog, result)
    _ensure_key(tenant, result)
    _ensure_admin(tenant, spec.admin_email, result)

    changes = {
        "anlass": "Mandant angelegt" if result.created else "Mandant vervollständigt",
        "durch": actor or "unbekannt",
        "nummernkreis": spec.numbering,
        "wahlperiode": spec.term_name,
        "gremienvorlage": spec.committees or "keine",
        "administrator": ADMIN_STATUS_LABELS.get(result.admin_status, result.admin_status),
    }
    audit.log_event("create" if result.created else "update", tenant, tenant=tenant, changes=changes)
    return result


def _fill_blank_fields(tenant: SessionTenant, spec: TenantSpec, result: ProvisioningResult) -> None:
    """Bestehenden Mandanten nicht umschreiben, nur leere Angaben ergänzen."""
    if tenant.name != spec.name:
        result.warnings.append(f"Der Name bleibt „{tenant.name}“; umbenennen lässt sich der Mandant im Admin.")
    ergaenzt = []
    for feld, wert in (("short_name", spec.short_name), ("body_type", spec.body_type), ("ags", spec.ags)):
        if wert and not getattr(tenant, feld):
            setattr(tenant, feld, wert)
            ergaenzt.append(feld)
    if ergaenzt:
        cast(Any, tenant).save(update_fields=[*ergaenzt, "updated_at"])
    if not tenant.is_active:
        result.warnings.append("Der Mandant ist deaktiviert. Reaktivieren im Admin: Aktion „Mandanten aktivieren“.")


def _ensure_roles(tenant: SessionTenant, result: ProvisioningResult) -> None:
    from apps.session.models import SessionRole

    neu = cast(Any, SessionRole).ensure_default_roles(tenant)
    if neu:
        result.steps.append("Standardrollen angelegt: " + ", ".join(role.name for role in neu.values()) + ".")
    else:
        result.steps.append("Standardrollen sind vollständig vorhanden.")


def _ensure_term(tenant: SessionTenant, spec: TenantSpec, result: ProvisioningResult) -> None:
    from apps.session.models import SessionLegislativeTerm

    term, angelegt = SessionLegislativeTerm.objects.update_or_create(
        tenant=tenant,
        name=spec.term_name,
        defaults={"number": spec.term_number, "start_date": spec.term_start, "end_date": spec.term_end},
    )
    nummer = f" (Nr. {term.number})" if term.number else ""
    zeitraum = f"{_de(term.start_date)} bis {_de(term.end_date)}"
    result.steps.append(f"Wahlperiode „{term.name}“{nummer}, {zeitraum} {'angelegt' if angelegt else 'aktualisiert'}.")
    if not term.is_current:
        result.warnings.append("Die Wahlperiode umfasst das heutige Datum nicht. Bitte Beginn und Ende prüfen.")


def _ensure_numbering(tenant: SessionTenant, spec: TenantSpec, result: ProvisioningResult) -> None:
    from apps.session.services import numbering_service

    vorher = sorted(tenant.number_ranges.filter(is_active=True).values_list("pattern", flat=True))
    kreise = numbering_service.apply_preset(tenant, spec.numbering)
    nachher = sorted(kreis.pattern for kreis in kreise)
    preset = numbering_service.PRESETS[spec.numbering]
    beispiel = numbering_service.preview(kreise[0]) if kreise else "–"
    result.steps.append(f"Nummernkreis: {preset.label} – nächste Nummer {beispiel} („{preset.reference_label}“).")
    if not result.created and vorher != nachher and tenant.papers.exclude(reference="").exists():
        result.warnings.append("Nummernkreis gewechselt: Bereits vergebene Nummern bleiben unverändert.")


def _ensure_committees(
    tenant: SessionTenant, spec: TenantSpec, catalog: PresetKatalog, result: ProvisioningResult
) -> None:
    from apps.session.models import SessionOrganization

    if not spec.committees:
        return
    vorlage = catalog.committee_templates[spec.committees]
    neu = []
    for gremium in vorlage.gremien:
        _, angelegt = SessionOrganization.objects.get_or_create(
            tenant=tenant,
            name=gremium.name,
            defaults={
                "short_name": gremium.short_name,
                "organization_type": gremium.organization_type,
                "is_active": True,
            },
        )
        if angelegt:
            neu.append(gremium.name)
    if neu:
        result.steps.append(f"Gremien aus der Vorlage „{vorlage.label}“ angelegt: {', '.join(neu)}.")
    else:
        result.steps.append(f"Gremien der Vorlage „{vorlage.label}“ sind vorhanden.")


def _ensure_key(tenant: SessionTenant, result: ProvisioningResult) -> None:
    """Mandantenschlüssel wie bei jedem Mandanten über ``TenantEncryption`` – hier gleich beim Anlegen."""
    from apps.common.encryption import TenantEncryption

    vorhanden = bool(tenant.encryption_key)
    try:
        _ = cast(Any, TenantEncryption)(tenant).key  # erzeugt und speichert den Schlüssel, falls er fehlt
    except Exception:
        logger.exception("Mandantenschlüssel für %s ließ sich nicht anlegen oder lesen.", tenant.slug)
        raise ProvisioningError(
            ["Der Schlüssel für verschlüsselte Felder ließ sich nicht anlegen. Bitte ENCRYPTION_MASTER_KEY prüfen."]
        ) from None
    result.steps.append(
        "Schlüssel für verschlüsselte Felder ist vorhanden."
        if vorhanden
        else "Schlüssel für verschlüsselte Felder angelegt."
    )


def _ensure_admin(tenant: SessionTenant, email: str, result: ProvisioningResult) -> None:
    """Vorhandenes Konto aufnehmen oder über die bestehende Einladung einladen – nie ein Passwort."""
    from apps.session import audit
    from apps.session.models import SessionInvitation, SessionUser

    admin_role = tenant.roles.filter(is_admin=True).order_by("-priority").first()
    if admin_role is None:  # pragma: no cover - ensure_default_roles legt die Rolle an
        raise ProvisioningError(["Der Mandant hat keine Administrator-Rolle."])
    konto = get_user_model().objects.filter(email__iexact=email).first()
    if konto is not None:
        zugang, neu = SessionUser.objects.get_or_create(user=konto, tenant=tenant)
        if not zugang.is_active:
            zugang.is_active = True
            zugang.save(update_fields=["is_active"])
        alte_rollen = [] if neu else list(zugang.roles.all())
        if admin_role in alte_rollen:
            result.admin_status = "already_admin"
        else:
            zugang.roles.add(admin_role)
            audit.log_role_assignment(zugang, alte_rollen, [*alte_rollen, admin_role], reason="Mandant anlegen")
            result.admin_status = "member"
        if not getattr(konto, "is_active", True):
            result.warnings.append("Das Konto des Administrators ist gesperrt; bitte im Admin entsperren.")
        result.steps.append(f"Administrator {email}: {ADMIN_STATUS_LABELS[result.admin_status]}.")
        return

    offen = (
        SessionInvitation.objects.filter(
            tenant=tenant, email__iexact=email, accepted_at__isnull=True, expires_at__gt=timezone.now()
        )
        .order_by("-created_at")
        .first()
    )
    if offen is not None:
        if not offen.roles.filter(pk=admin_role.pk).exists():
            offen.roles.add(admin_role)
        result.admin_status = "invitation_pending"
        gueltig = timezone.localtime(offen.expires_at).strftime("%d.%m.%Y")
        result.steps.append(f"Administrator {email}: offene Einladung besteht bereits (gültig bis {gueltig}).")
        return
    einladung = cast(Any, SessionInvitation).create_for_tenant(tenant=tenant, email=email, roles=[admin_role])
    result.admin_status = "invited"
    result._invitation = einladung
    gueltig = timezone.localtime(einladung.expires_at).strftime("%d.%m.%Y")
    result.steps.append(f"Administrator {email}: Einladung angelegt (gültig bis {gueltig}, Link nur per E-Mail).")


def _de(tag: date | None) -> str:
    return tag.strftime("%d.%m.%Y") if tag else "offen"


# ---------------------------------------------------------------------------
# Deaktivieren und Reaktivieren
# ---------------------------------------------------------------------------


@dataclass
class LifecycleResult:
    changed: bool
    portal: PortalChange | None = None


def set_tenant_active(tenant: SessionTenant, active: bool, *, actor: str = "", request: Any = None) -> LifecycleResult:
    """
    Mandanten einzeln (de)aktivieren – nie per ``queryset.update()``, damit die Rücknahme der
    Bürgerportal-Quelle und das Audit-Log folgen. Deaktivieren eines schon inaktiven Mandanten holt
    eine fehlende Rücknahme nach (Bestand aus der Zeit vor Issue #317).
    """
    from apps.session import audit
    from apps.session.services import insight_service

    with transaction.atomic():
        if tenant.is_active == active:
            if active:
                return LifecycleResult(changed=False)
            portal = insight_service.retract_source(tenant)
            if portal.bodies or portal.entries:
                audit.log_event(
                    "unpublish",
                    tenant,
                    tenant=tenant,
                    request=request,
                    changes={"buergerportal": portal.as_dict(), "durch": actor or "unbekannt"},
                    object_repr="Bürgerportal-Quelle",
                )
            return LifecycleResult(changed=False, portal=portal)
        tenant.is_active = active
        tenant._lifecycle_actor = actor  # type: ignore[attr-defined]
        tenant._lifecycle_request = request  # type: ignore[attr-defined]
        cast(Any, tenant).save(update_fields=["is_active", "updated_at"])
        return LifecycleResult(changed=True, portal=getattr(tenant, "_lifecycle_portal", None))


def on_active_changed(tenant: SessionTenant) -> PortalChange:
    """Signal-Hook: ``is_active`` wurde gespeichert – Bürgerportal nachziehen und protokollieren."""
    from apps.session import audit
    from apps.session.services import insight_service

    actor = getattr(tenant, "_lifecycle_actor", "") or "unbekannt"
    request = getattr(tenant, "_lifecycle_request", None)
    if tenant.is_active:
        portal = insight_service.PortalChange()
        if tenant.insight_publish:
            insight_service.register_source(tenant)
            portal = insight_service.restore_source(tenant)
        aktion = "publish"
    else:
        portal = insight_service.retract_source(tenant)
        aktion = "unpublish"
    audit.log_event(
        "update",
        tenant,
        tenant=tenant,
        request=request,
        changes={"is_active": {"alt": not tenant.is_active, "neu": tenant.is_active}, "durch": actor},
    )
    if portal.sources:
        audit.log_event(
            aktion,
            tenant,
            tenant=tenant,
            request=request,
            changes={"buergerportal": portal.as_dict(), "durch": actor},
            object_repr="Bürgerportal-Quelle",
        )
    tenant._lifecycle_portal = portal  # type: ignore[attr-defined]
    return portal
