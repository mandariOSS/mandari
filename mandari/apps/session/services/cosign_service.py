# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mitzeichnungslauf für Vorlagen (Issue #81).

Beim Vorlegen einer Vorlage zur Freigabe wird aus den Mitzeichnungsregeln
des Mandanten die Stationskette erzeugt. Die Stationen zeichnen der Reihe
nach mit; erst wenn alle mitgezeichnet haben, darf die Vorlage freigegeben
werden. Eine Zurückweisung wirft die Vorlage zurück in den Entwurf.
"""

from django.db.models import Q

from ..models import SessionCosignature, SessionCosignatureRule, SessionPaper, SessionUser


def build_chain(paper: SessionPaper) -> int:
    """
    Mitzeichnungskette für eine Vorlage (neu) aufbauen.

    Bestehende Stationen werden verworfen — nach einer inhaltlichen
    Überarbeitung muss erneut mitgezeichnet werden (Historie bleibt im
    Audit-Log erhalten). Regeln mit „nur bei finanziellen Auswirkungen"
    greifen nur, wenn die Vorlage finanzielle Auswirkungen hat.
    """
    paper.cosignatures.all().delete()

    rules = (
        SessionCosignatureRule.objects.filter(tenant=paper.tenant)
        .filter(Q(paper_type="") | Q(paper_type=paper.paper_type))
        .filter(department__is_active=True)
        .select_related("department")
        .order_by("order", "created_at")
    )
    created = 0
    seen_departments = set()
    for rule in rules:
        if rule.only_financial and paper.has_financial_impact is not True:
            continue
        if rule.department_id in seen_departments:
            continue
        seen_departments.add(rule.department_id)
        SessionCosignature.objects.create(
            paper=paper,
            department=rule.department,
            order=rule.order,
        )
        created += 1
    return created


def pending_blockers(paper: SessionPaper):
    """Offene bzw. zurückgewiesene Stationen, die die Freigabe blockieren."""
    return paper.cosignatures.exclude(status="signed")


def is_actionable(cosignature: SessionCosignature) -> bool:
    """
    Stationen zeichnen der Reihe nach: Eine Station ist erst dran, wenn
    alle Stationen mit kleinerer Reihenfolge mitgezeichnet haben.
    """
    if cosignature.status != "pending":
        return False
    return not cosignature.paper.cosignatures.filter(order__lt=cosignature.order).exclude(status="signed").exists()


def can_decide(session_user: SessionUser, cosignature: SessionCosignature) -> bool:
    """Darf diese Person für die Station entscheiden? (Amts-Zuordnung oder Admin)"""
    if session_user.is_admin():
        return True
    return session_user.departments.filter(pk=cosignature.department_id).exists()


def my_pending_cosignatures(session_user: SessionUser):
    """
    Arbeitsvorrat „Meine Mitzeichnungen": offene Stationen der Ämter
    dieser Person für Vorlagen, die gerade in Prüfung sind.
    """
    qs = SessionCosignature.objects.filter(
        paper__tenant=session_user.tenant,
        paper__status="review",
        status="pending",
    )
    if not session_user.is_admin():
        qs = qs.filter(department__in=session_user.departments.all())
    return qs.select_related("paper__main_organization", "department").order_by("paper__created_at", "order")
