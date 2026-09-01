# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Digitale Abstimmung und Umlaufbeschlüsse (Issue #41).

- Einzelstimmen-Erfassung je TOP (offen/namentlich) mit automatischer
  Summenbildung; Befangene (Mitwirkungsverbot) zählen nicht mit.
- Bei geheimer Abstimmung werden nur Summen gespeichert; Einzelstimmen
  werden verworfen, Befangenheits-Vermerke bleiben dokumentierbar.
- Umlaufbeschlüsse: Rücklauf-Erfassung, Auszählung gegen die
  stimmberechtigte Besetzung, Nummernvergabe U/<Jahr>/<lfd>.
"""

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import (
    SessionAgendaItem,
    SessionCircularResolution,
    SessionOrganizationMembership,
    SessionTenant,
    SessionVote,
)

INDIVIDUAL_METHODS = ("open", "roll_call")


def capture_votes(agenda_item: SessionAgendaItem, votes_by_person: dict, *, recorded_by) -> dict:
    """
    Einzelstimmen für einen TOP speichern und Summen neu berechnen.

    Args:
        votes_by_person: {person: vote_value} — leere Werte löschen die Stimme.

    Bei geheimer Abstimmung werden nur „befangen"-Vermerke gespeichert
    (der Ausschluss ist dokumentationspflichtig, das Stimmverhalten nicht).
    """
    secret = agenda_item.voting_method == "secret"
    valid_votes = {value for value, _ in SessionVote.VOTE_CHOICES}

    for person, vote_value in votes_by_person.items():
        if vote_value not in valid_votes:
            SessionVote.objects.filter(agenda_item=agenda_item, person=person).delete()
            continue
        if secret and vote_value not in ("excluded", "not_participating"):
            # Geheime Abstimmung: kein individuelles Stimmverhalten speichern
            SessionVote.objects.filter(agenda_item=agenda_item, person=person).delete()
            continue
        SessionVote.objects.update_or_create(
            agenda_item=agenda_item,
            person=person,
            defaults={"vote": vote_value, "recorded_by": recorded_by},
        )

    if not secret:
        recompute_sums(agenda_item)
    return tally(agenda_item)


def recompute_sums(agenda_item: SessionAgendaItem) -> None:
    """Summen (Ja/Nein/Enthaltung) aus den Einzelstimmen ableiten."""
    counts = {value: 0 for value in SessionVote.COUNTED_VOTES}
    for vote in agenda_item.votes.filter(vote__in=SessionVote.COUNTED_VOTES).values_list("vote", flat=True):
        counts[vote] += 1
    agenda_item.votes_yes = counts["yes"]
    agenda_item.votes_no = counts["no"]
    agenda_item.votes_abstain = counts["abstain"]
    agenda_item.save(update_fields=["votes_yes", "votes_no", "votes_abstain", "updated_at"])


def tally(agenda_item: SessionAgendaItem) -> dict:
    """Übersicht: Summen + Befangene/Nicht-Teilnehmende."""
    votes = list(agenda_item.votes.select_related("person"))
    return {
        "yes": agenda_item.votes_yes,
        "no": agenda_item.votes_no,
        "abstain": agenda_item.votes_abstain,
        "excluded": [v.person for v in votes if v.vote == "excluded"],
        "not_participating": [v.person for v in votes if v.vote == "not_participating"],
    }


# --- Umlaufbeschlüsse --------------------------------------------------------


def voting_members(circular: SessionCircularResolution):
    """Aktive stimmberechtigte Besetzung des Gremiums."""
    today = timezone.localdate()
    return (
        SessionOrganizationMembership.objects.filter(
            organization=circular.organization,
            has_voting_rights=True,
        )
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        .filter(Q(start_date__isnull=True) | Q(start_date__lte=today))
        .select_related("person")
        .order_by("person__family_name", "person__given_name")
    )


def circular_tally(circular: SessionCircularResolution) -> dict:
    """Auszählung eines Umlaufbeschlusses gegen die stimmberechtigte Besetzung."""
    votes = {v.person_id: v for v in circular.votes.select_related("person")}
    members = list(voting_members(circular))
    counts = {"yes": 0, "no": 0, "abstain": 0}
    outstanding = []
    for membership in members:
        vote = votes.get(membership.person_id)
        if vote is None:
            outstanding.append(membership.person)
        else:
            counts[vote.vote] += 1
    total = len(members)
    responded = total - len(outstanding)
    return {
        **counts,
        "total_members": total,
        "responded": responded,
        "outstanding": outstanding,
        "quorum_met": total > 0 and responded > total / 2,
        "suggestion": "adopted" if counts["yes"] > counts["no"] else "rejected",
    }


def assign_circular_number(circular: SessionCircularResolution) -> bool:
    """Umlauf-Nummer U/<Jahr>/<lfd> vergeben (idempotent, transaktionssicher)."""
    if circular.reference:
        return False

    year = timezone.localdate().year
    prefix = f"U/{year}/"
    with transaction.atomic():
        SessionTenant.objects.select_for_update().get(pk=circular.tenant_id)
        max_num = 0
        refs = SessionCircularResolution.objects.filter(
            tenant_id=circular.tenant_id, reference__startswith=prefix
        ).values_list("reference", flat=True)
        for ref in refs:
            try:
                max_num = max(max_num, int(ref.rsplit("/", 1)[-1]))
            except (TypeError, ValueError):
                continue
        circular.reference = f"{prefix}{max_num + 1:04d}"
        circular.save(update_fields=["reference", "updated_at"])
    return True
