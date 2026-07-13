# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Meeting preparation views for the Work module.

Org-weite Sitzungsvorbereitung mit 5 Sektionen pro TOP:
1. Position/Beschluss (org-weit)
2. Private Notizen (pro User)
3. Redebeitrag (pro User, teilbar)
4. Fraktionsdiskussion (org-weit)
5. Dokumente (org-weit)
"""

import re

from insight_core.models import OParlConsultation


def get_primary_paper_for_item(agenda_item):
    """Erste Vorlage eines TOPs (über OParlConsultation) oder None."""
    if not agenda_item.external_id:
        return None
    consultation = (
        OParlConsultation.objects.filter(agenda_item_external_id=agenda_item.external_id, paper__isnull=False)
        .select_related("paper")
        .first()
    )
    return consultation.paper if consultation else None


def is_pdf_file(mime_type, *names):
    """PDF-Erkennung wie im RIS-Bereich: mime_type, ersatzweise Dateiendung."""
    if (mime_type or "").split(";")[0].strip().lower() == "application/pdf":
        return True
    return any(str(n or "").lower().endswith(".pdf") for n in names)


def natural_sort_key(item):
    """Sort agenda items naturally: 1, 2, 10, 11 instead of 1, 10, 11, 2."""
    number = item.number or "999"
    parts = re.split(r"(\d+)", str(number))
    return [(0, int(p)) if p.isdigit() else (1, p.lower()) for p in parts if p]


def prefetch_papers_for_agenda_items(agenda_items):
    """
    Pre-fetch papers for a list of agenda items via consultations.
    Returns a dict mapping agenda_item.id to list of papers with their files.
    """
    from django.db.models import Count

    if not agenda_items:
        return {}

    external_ids = [item.external_id for item in agenda_items if item.external_id]
    if not external_ids:
        return {}

    consultations = (
        OParlConsultation.objects.filter(agenda_item_external_id__in=external_ids)
        .select_related("paper")
        .prefetch_related("paper__files", "paper__consultations")
    )

    paper_ids = set()
    for consultation in consultations:
        if consultation.paper:
            paper_ids.add(consultation.paper.id)

    paper_consultation_counts = {}
    if paper_ids:
        from insight_core.models import OParlPaper

        papers_with_counts = OParlPaper.objects.filter(id__in=paper_ids).annotate(
            consultation_count=Count("consultations")
        )
        paper_consultation_counts = {p.id: p.consultation_count for p in papers_with_counts}

    papers_by_ext_id = {}
    for consultation in consultations:
        if consultation.paper and consultation.agenda_item_external_id:
            ext_id = consultation.agenda_item_external_id
            if ext_id not in papers_by_ext_id:
                papers_by_ext_id[ext_id] = []
            if consultation.paper not in papers_by_ext_id[ext_id]:
                consultation.paper._prefetched_consultation_count = paper_consultation_counts.get(
                    consultation.paper.id, 0
                )
                papers_by_ext_id[ext_id].append(consultation.paper)

    papers_by_item_id = {}
    for item in agenda_items:
        if item.external_id and item.external_id in papers_by_ext_id:
            papers_by_item_id[item.id] = papers_by_ext_id[item.external_id]
        else:
            papers_by_item_id[item.id] = []

    return papers_by_item_id
