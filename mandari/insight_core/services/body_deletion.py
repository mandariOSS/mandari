# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Speicherschonende Löschung einer Kommune (OParlBody) samt aller RIS-Daten.

Djangos Standard-Delete sammelt ALLE abhängigen Objekte im RAM (Collector) —
bei einer Kommune mit hunderttausenden agenda_items/files führt das zum
OOM-Kill des Web-Containers. Dieser Service löscht stattdessen in kleinen
Batches über das ORM (Kaskaden und Signale bleiben intakt, Speicher bleibt
begrenzt) und läuft als Background-Task.
"""

import logging

from django.tasks import task

logger = logging.getLogger(__name__)

BATCH_SIZE = 2000


def _chunked_delete(queryset, batch_size: int = BATCH_SIZE) -> int:
    """Löscht ein Queryset in Batches. Gibt die Gesamtzahl gelöschter Zeilen zurück."""
    model = queryset.model
    total = 0
    while True:
        pks = list(queryset.values_list("pk", flat=True)[:batch_size])
        if not pks:
            return total
        deleted, _ = model.objects.filter(pk__in=pks).delete()
        total += deleted


def delete_body_data(body_id: str) -> dict:
    """
    Löscht alle Daten einer Kommune in Kind-zuerst-Reihenfolge.

    Reihenfolge ist wichtig: große Kind-Tabellen zuerst, damit der finale
    body.delete() nur noch eine kleine Kaskade übrig hat.
    """
    from insight_core.models import (
        OParlAgendaItem,
        OParlBody,
        OParlConsultation,
        OParlFile,
        OParlLegislativeTerm,
        OParlLocation,
        OParlMeeting,
        OParlMembership,
        OParlOrganization,
        OParlPaper,
        OParlPerson,
    )

    try:
        body = OParlBody.objects.get(id=body_id)
    except OParlBody.DoesNotExist:
        logger.warning(f"[BodyDeletion] Body {body_id} existiert nicht (mehr).")
        return {"deleted": 0, "body": None}

    body_name = body.name
    logger.info(f"[BodyDeletion] Starte Löschung von '{body_name}' ({body_id})")

    counts = {}
    steps = [
        ("files", OParlFile.objects.filter(body=body)),
        ("consultations", OParlConsultation.objects.filter(body=body)),
        ("agenda_items", OParlAgendaItem.objects.filter(meeting__body=body)),
        ("memberships", OParlMembership.objects.filter(organization__body=body)),
        ("meetings", OParlMeeting.objects.filter(body=body)),
        ("papers", OParlPaper.objects.filter(body=body)),
        ("persons", OParlPerson.objects.filter(body=body)),
        ("organizations", OParlOrganization.objects.filter(body=body)),
        ("locations", OParlLocation.objects.filter(body=body)),
        ("legislative_terms", OParlLegislativeTerm.objects.filter(body=body)),
    ]
    for name, qs in steps:
        counts[name] = _chunked_delete(qs)
        logger.info(f"[BodyDeletion] {body_name}: {counts[name]} {name} gelöscht")

    # Restliche kleine Kaskade (Subscriber, LocationMappings, …) über den
    # normalen Delete — jetzt speicherunkritisch.
    deleted, detail = body.delete()
    counts["body_cascade"] = deleted
    total = sum(counts.values())
    logger.info(f"[BodyDeletion] '{body_name}' vollständig gelöscht ({total} Zeilen): {counts}")
    return {"deleted": total, "body": body_name, "detail": counts}


@task
def delete_body_task(body_id: str):
    """Background-Task-Wrapper für die Kommune-Löschung."""
    try:
        return delete_body_data(body_id)
    except Exception:
        logger.exception(f"[BodyDeletion] Löschung von Body {body_id} fehlgeschlagen")
        raise
