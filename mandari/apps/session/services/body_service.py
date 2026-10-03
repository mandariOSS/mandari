# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Körperschaften im Mandanten (Issue #756).

Der Session-Mandant ist die Verwaltung, die den Sitzungsdienst führt; die Körperschaft (``SessionBody``) ist
die rechtliche Einheit, deren Gremien tagen – etwa eine Samtgemeinde und ihre Mitgliedsgemeinden in einem
Mandanten. Gremien, Vorlagen und Nummernkreise gehören zur Körperschaft, Sitzungen über ihr Gremium.
Entscheidung und Abgrenzung: docs/adr/20261002-koerperschaften-im-mandanten.md.

- **Standardkörperschaft:** Jeder Mandant hat genau eine (``default_body``). Sie entsteht beim Anlegen des
  Mandanten bzw. bei Bedarf aus Name, Kurzname, Art und AGS des Mandanten.
- **Abwärtskompatibel:** Die Fremdschlüssel sind in der Datenbank nullbar. Was ein älteres Image auf dem
  neuen Schema anlegt, hat keine Körperschaft und gilt beim Lesen als Teil der Standardkörperschaft
  (``body_q``); jeder ``migrate``-Lauf ordnet solche Nachzügler zu (``assign_missing``).
- **Oberfläche nur bei Bedarf:** Auswahl, Filter und Verwaltung erscheinen erst ab der zweiten aktiven
  Körperschaft (``has_multiple``).
- **Geltungsbereich für Rechte (#759):** ``bodies_in_scope`` liefert bis dahin alle Körperschaften des
  Mandanten – der Geltungsbereich „Körperschaft“ wirkt wie „Mandant“.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.apps import apps as django_apps
from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Q, QuerySet, Subquery
from django.utils.text import slugify

if TYPE_CHECKING:
    from apps.session.models import SessionBody, SessionOrganization, SessionPaper, SessionTenant, SessionUser

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Standardkörperschaft
# ---------------------------------------------------------------------------


def free_slug(body_model: Any, tenant_id: Any, base: str, *, exclude_pk: Any = None) -> str:
    """Kurzkennung, die im Mandanten noch frei ist: ``base``, ``base-2``, ``base-3`` …"""
    base = (slugify(base) or "koerperschaft")[:90]
    taken = set(
        body_model.objects.filter(tenant_id=tenant_id, slug__startswith=base)
        .exclude(pk=exclude_pk)
        .values_list("slug", flat=True)
    )
    slug, n = base, 1
    while slug in taken:
        n += 1
        slug = f"{base}-{n}"
    return slug


def default_fields(tenant: Any) -> dict[str, Any]:
    """Angaben der Standardkörperschaft aus dem Mandanten (Anlage und Migration)."""
    return {
        "name": tenant.name,
        "short_name": tenant.short_name or "",
        "body_type": tenant.body_type or "",
        "ags": tenant.ags or "",
    }


def default_body(tenant: SessionTenant) -> SessionBody:
    """Standardkörperschaft des Mandanten; fehlt sie (Mandant aus einem älteren Image), wird sie angelegt."""
    from apps.session.models import SessionBody

    body = SessionBody.objects.filter(tenant_id=tenant.pk, is_default=True).first()
    if body is not None:
        return body
    try:
        with transaction.atomic():
            # Ohne Signale: Die Standardkörperschaft ist Teil der Mandantenanlage (die Bereitstellung protokolliert
            # sie), keine Handlung einer Person. Ein eigener Eintrag stünde sonst vor allen anderen in der Kette
            # des Mandanten und verschöbe deren Archivierung.
            (created,) = SessionBody.objects.bulk_create(
                [
                    SessionBody(
                        tenant=tenant,
                        is_default=True,
                        slug=free_slug(SessionBody, tenant.pk, tenant.slug),
                        **default_fields(tenant),
                    )
                ]
            )
            return created
    except IntegrityError:
        # Gleichzeitig angelegt: höchstens eine Standardkörperschaft je Mandant (Datenbankregel)
        return SessionBody.objects.get(tenant_id=tenant.pk, is_default=True)


# ---------------------------------------------------------------------------
# Lesen
# ---------------------------------------------------------------------------


def bodies(tenant: SessionTenant, *, include_inactive: bool = False) -> QuerySet[SessionBody]:
    """Körperschaften des Mandanten, Standardkörperschaft zuerst."""
    from apps.session.models import SessionBody

    qs = SessionBody.objects.filter(tenant_id=tenant.pk)
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return qs.order_by("-is_default", "name")


def has_multiple(tenant: SessionTenant) -> bool:
    """Führt der Mandant mehr als eine aktive Körperschaft? Erst dann zeigt die Oberfläche Auswahl und Filter."""
    return bodies(tenant)[:2].count() > 1


def body_q(body: SessionBody, prefix: str = "") -> Q:
    """
    Filter „gehört zu dieser Körperschaft“ mit Präfix (Gremium: ``""``, Sitzung: ``"organization__"``).

    Objekte ohne Körperschaft (angelegt von einem älteren Image) zählen zur Standardkörperschaft.
    """
    condition = Q(**{f"{prefix}body": body})
    if body.is_default:
        condition |= Q(**{f"{prefix}body__isnull": True})
    return condition


def effective_body_id(obj: Any, default_id: Any) -> Any:
    """Körperschaft eines Gremiums bzw. einer Vorlage; ohne Angabe die Standardkörperschaft."""
    return obj.body_id if obj.body_id is not None else default_id


def body_for_paper(paper: SessionPaper) -> SessionBody:
    """Körperschaft einer Vorlage ohne Angabe: die des federführenden Gremiums, sonst die Standardkörperschaft."""
    organization = paper.main_organization if paper.main_organization_id else None
    body = organization.body if organization is not None and organization.body_id is not None else None
    return body if body is not None else default_body(paper.tenant)


def bodies_in_scope(session_user: SessionUser) -> QuerySet[SessionBody]:
    """
    Körperschaften im Geltungsbereich einer Person (Schnittstelle für Rechte mit Geltungsbereich, #759).

    Bis zur Umstellung der Rechte wirkt der Geltungsbereich „Körperschaft“ wie „Mandant“: alle Körperschaften
    des Mandanten der Person.
    """
    from apps.session.models import SessionBody

    return SessionBody.objects.filter(tenant_id=session_user.tenant_id)


# ---------------------------------------------------------------------------
# Prüfungen
# ---------------------------------------------------------------------------


def same_body(lead: SessionOrganization, others: QuerySet[SessionOrganization]) -> bool:
    """
    Gehören alle ``others`` zur Körperschaft von ``lead``? Ohne Angabe zählt die Standardkörperschaft.

    Gemeinsame Sitzungen bleiben innerhalb einer Körperschaft (``joint_meeting_service``).
    """
    from apps.session.models import SessionBody

    default_id = (
        SessionBody.objects.filter(tenant_id=lead.tenant_id, is_default=True).values_list("pk", flat=True).first()
    )
    lead_body = effective_body_id(lead, default_id)
    return all(
        (body_id if body_id is not None else default_id) == lead_body
        for body_id in others.values_list("body_id", flat=True)
    )


# ---------------------------------------------------------------------------
# Standard festlegen
# ---------------------------------------------------------------------------


def set_default(body: SessionBody) -> None:
    """
    ``body`` wird Standardkörperschaft ihres Mandanten (Verwaltung der Körperschaften). Die bisherige verliert
    das Kennzeichen; beide Änderungen stehen über die Signale im Prüfprotokoll des Mandanten.
    """
    from apps.session.models import SessionBody

    with transaction.atomic():
        # Alle Körperschaften des Mandanten sperren, nicht nur die bisherige Standardkörperschaft: Ein
        # gleichzeitiger Wechsel wartet so auf diesen und liest danach dessen neue Standardkörperschaft. Wer nur
        # die bisherige sperrt, findet nach dem Warten keine mehr (sie ist es nicht mehr) und verletzt die
        # Datenbankregel „höchstens eine“. Feste Reihenfolge, damit sich zwei Wechsel nicht gegenseitig sperren.
        locked = list(SessionBody.objects.select_for_update().filter(tenant_id=body.tenant_id).order_by("pk"))
        for old in locked:
            if old.pk == body.pk or not old.is_default:
                continue
            old.is_default = False
            old.save(update_fields=["is_default", "updated_at"])
        body.is_default = True
        body.is_active = True
        body.save(update_fields=["is_default", "is_active", "updated_at"])


# ---------------------------------------------------------------------------
# Nachzügler zuordnen (post_migrate)
# ---------------------------------------------------------------------------


def assign_missing(registry: Any = None) -> dict[str, int]:
    """
    Mandanten ohne Standardkörperschaft ergänzen und Gremien und Vorlagen ohne Körperschaft zuordnen.

    Idempotent und ohne Änderungszeitpunkt (``update()``): OParl-Abnehmer sehen keine Änderung. Gremien gehen an
    die Standardkörperschaft, Vorlagen an die Körperschaft ihres federführenden Gremiums, sonst ebenfalls an die
    Standardkörperschaft. ``registry``: App-Registry (beim Migrieren der Stand nach dem Lauf); fehlt das Modell
    dort (Migration zurückgesetzt), geschieht nichts.
    """
    registry = registry or django_apps
    try:
        tenant_model = registry.get_model("session", "SessionTenant")
        body_model = registry.get_model("session", "SessionBody")
        organization_model = registry.get_model("session", "SessionOrganization")
        paper_model = registry.get_model("session", "SessionPaper")
    except LookupError:
        return {}

    counts = {"bodies": 0, "organizations": 0, "papers": 0}
    with_default = body_model.objects.filter(is_default=True).values("tenant_id")
    for tenant in tenant_model.objects.exclude(pk__in=with_default).iterator():
        # Ohne Signale wie in default_body(): Teil der Mandantenanlage, kein Eintrag im Prüfprotokoll
        body_model.objects.bulk_create(
            [
                body_model(
                    tenant_id=tenant.pk,
                    is_default=True,
                    slug=free_slug(body_model, tenant.pk, tenant.slug),
                    **default_fields(tenant),
                )
            ]
        )
        counts["bodies"] += 1

    defaults = dict(body_model.objects.filter(is_default=True).values_list("tenant_id", "pk"))
    for tenant_id in set(organization_model.objects.filter(body__isnull=True).values_list("tenant_id", flat=True)):
        counts["organizations"] += organization_model.objects.filter(tenant_id=tenant_id, body__isnull=True).update(
            body_id=defaults[tenant_id]
        )

    lead_body = organization_model.objects.filter(pk=OuterRef("main_organization_id")).values("body_id")[:1]
    counts["papers"] += paper_model.objects.filter(body__isnull=True, main_organization__body__isnull=False).update(
        body_id=Subquery(lead_body)
    )
    for tenant_id in set(paper_model.objects.filter(body__isnull=True).values_list("tenant_id", flat=True)):
        counts["papers"] += paper_model.objects.filter(tenant_id=tenant_id, body__isnull=True).update(
            body_id=defaults[tenant_id]
        )
    return counts


def post_migrate_assign(sender: Any, apps: Any = None, **kwargs: Any) -> None:
    """Nach jedem ``migrate``: Nachzügler eines älteren Images zuordnen (Rückfall, Containerwechsel)."""
    try:
        counts = assign_missing(apps)
    except Exception:
        # Ein Fehler hier darf den Deploy nicht abbrechen; der nächste Lauf versucht es erneut
        logger.exception("Körperschaften: Zuordnung nach migrate fehlgeschlagen.")
        return
    if any(counts.values()):
        logger.info(
            "Körperschaften zugeordnet: %s neue Standardkörperschaften, %s Gremien, %s Vorlagen.",
            counts["bodies"],
            counts["organizations"],
            counts["papers"],
        )
