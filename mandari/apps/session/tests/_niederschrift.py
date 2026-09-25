# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gemeinsame Testwelt für die Niederschrift nach der Genehmigung (Issue #318)."""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionProtocol,
    SessionRole,
    SessionTenant,
    SessionUser,
)

#: Kennzeichen nichtöffentlicher Inhalte – dürfen in keiner öffentlichen Ausgabe auftauchen
GEHEIM = (
    "GEHEIMTOP",
    "GEHEIMBESCHLUSS",
    "GEHEIMNOTIZ",
    "GEHEIMVERSCHL",
    "GEHEIMALLGEMEIN",
    "GEHEIMUNTERPUNKT",
    "GEHEIMGRUND",
)


@dataclass
class Welt:
    tenant: SessionTenant
    gremium: SessionOrganization
    sitzung: SessionMeeting
    folge: SessionMeeting
    folge_top: SessionAgendaItem
    top: SessionAgendaItem
    top_noe: SessionAgendaItem
    protokoll: SessionProtocol
    stimmberechtigt: list[SessionPerson]
    beratend: SessionPerson
    gast: SessionPerson
    abwesend: SessionPerson
    protokollant: SessionUser
    genehmiger: SessionUser
    zweite: SessionUser
    leser: SessionUser


def nutzer(tenant: SessionTenant, name: str, *perms: str) -> SessionUser:
    flags = {f"can_{perm}": True for perm in perms}
    role = SessionRole.objects.create(tenant=tenant, name=f"r-{name}", **flags)
    user = cast(Any, UserFactory)(email=f"{name}@{tenant.slug}.example.org")
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(role)
    return session_user


def client(session_user: SessionUser) -> Client:
    result = Client()
    result.force_login(session_user.user)
    return result


def person(tenant: SessionTenant, name: str) -> SessionPerson:
    return SessionPerson.objects.create(tenant=tenant, given_name="P", family_name=name)


def welt(slug: str = "nord", *, status: str = "approved", four_eyes: bool = False, **tenant_kwargs: Any) -> Welt:
    """Sitzung mit Ö- und NÖ-TOP, vollständiger Anwesenheit und (standardmäßig) genehmigter Niederschrift."""
    tenant = SessionTenant.objects.create(
        name=f"Stadt {slug}", slug=slug, four_eyes_protocols=four_eyes, **tenant_kwargs
    )
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss")
    start = timezone.now() - timedelta(days=2)
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Bauausschuss", organization=gremium, start=start, is_public=True
    )
    folge = SessionMeeting.objects.create(
        tenant=tenant, name="Folgesitzung", organization=gremium, start=start + timedelta(days=28), is_public=True
    )
    folge_top = SessionAgendaItem.objects.create(
        meeting=folge, number="1", order=1, name="Genehmigung der Niederschrift"
    )

    stimmberechtigt = [person(tenant, name) for name in ("Amsel", "Buche", "Carl")]
    beratend = person(tenant, "Dachs")
    gast = person(tenant, "Esche")
    abwesend = person(tenant, "Fink")
    for p in [*stimmberechtigt, abwesend]:
        SessionOrganizationMembership.objects.create(organization=gremium, person=p, has_voting_rights=True)
    SessionOrganizationMembership.objects.create(
        organization=gremium, person=beratend, has_voting_rights=False, role="advisor"
    )
    for p in stimmberechtigt:
        SessionAttendance.objects.create(meeting=sitzung, person=p, status="present")
    SessionAttendance.objects.create(meeting=sitzung, person=abwesend, status="excused")
    SessionAttendance.objects.create(
        meeting=sitzung, person=beratend, status="present", role="expert", has_voting_rights=False
    )
    SessionAttendance.objects.create(meeting=sitzung, person=gast, status="present", role="guest")

    top = SessionAgendaItem.objects.create(
        meeting=sitzung,
        number="1",
        order=1,
        name="Radweg Hauptstraße",
        resolution_text="Der Ausschuss beschließt den Radweg.",
        protocol_note="Aussprache zum Radweg.",
        vote_result="approved",
        votes_yes=2,
        votes_no=1,
    )
    top_noe = SessionAgendaItem(
        meeting=sitzung,
        number="N1",
        order=2,
        name="GEHEIMTOP Grundstück",
        is_public=False,
        resolution_text="GEHEIMBESCHLUSS",
        protocol_note="GEHEIMNOTIZ",
        vote_result="approved",
        votes_yes=3,
    )
    cast(Any, top_noe).set_protocol_note_encrypted("GEHEIMVERSCHL Wortbeitrag")
    top_noe.save()

    protokollant = nutzer(tenant, "protokoll", "view_meetings", "view_protocols", "edit_protocols")
    genehmiger = nutzer(
        tenant,
        "genehmiger",
        "view_meetings",
        "view_protocols",
        "approve_protocols",
        "view_non_public_meetings",
        "edit_meetings",
    )
    zweite = nutzer(
        tenant, "zweite", "view_meetings", "view_protocols", "approve_protocols", "view_non_public_meetings"
    )
    leser = nutzer(tenant, "leser", "view_meetings", "view_protocols")

    protokoll = SessionProtocol(
        meeting=sitzung,
        content="Allgemeiner Teil der Sitzung.",
        status=status,
        created_by=protokollant,
        chair_name="Vera Vorsitz",
    )
    cast(Any, protokoll).set_content_encrypted("GEHEIMALLGEMEIN")
    if status in ("approved", "published"):
        protokoll.approved_by = genehmiger
        protokoll.approved_at = timezone.now()
    protokoll.save()
    return Welt(
        tenant=tenant,
        gremium=gremium,
        sitzung=sitzung,
        folge=folge,
        folge_top=folge_top,
        top=top,
        top_noe=top_noe,
        protokoll=protokoll,
        stimmberechtigt=stimmberechtigt,
        beratend=beratend,
        gast=gast,
        abwesend=abwesend,
        protokollant=protokollant,
        genehmiger=genehmiger,
        zweite=zweite,
        leser=leser,
    )


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(bytes(data)))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def base(w: Welt) -> str:
    return f"/session/{w.tenant.slug}"
