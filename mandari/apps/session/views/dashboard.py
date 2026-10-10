# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.
"""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from django.views.generic import (
    TemplateView,
)

from ..models import (
    SessionAgendaItem,
    SessionApplication,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
)
from ..permissions import SessionViewMixin
from ..rahmen import neues_design
from ..services import audit_log_service, joint_meeting_service

# =============================================================================
# DASHBOARD
# =============================================================================

#: Wegweiser für Rollen ohne Sitzungs-, Vorlagen- und Antragskacheln (Issue #708): Bereiche außerhalb des
#: Sitzungsdienstes, je mit genau den Rechten, die auch die Seite verlangt (eines genügt). Das Audit-Log hat
#: eine eigene Karte (Protokollkontrolle).
ROLE_AREAS = (
    (
        ("manage_allowances",),
        "session:allowances",
        "Sitzungsgelder",
        "banknote",
        "Sitzungsgelder abrechnen, genehmigen und auszahlen",
    ),
    (("manage_devices",), "session:devices", "Endgeräte", "tablet", "Endgeräte für die digitale Ratsarbeit"),
    (
        ("manage_settings", "manage_users"),
        "session:settings",
        "Einstellungen",
        "settings",
        "Benutzer, Rollen und Einstellungen des Mandanten",
    ),
)


def role_areas(permissions, tenant_slug: str) -> list[dict]:
    """Bereiche aus :data:`ROLE_AREAS`, die die Person öffnen darf – ohne zusätzliche Rechte."""
    return [
        {"url": reverse(url_name, kwargs={"tenant_slug": tenant_slug}), "label": label, "icon": icon, "text": text}
        for needed, url_name, label, icon, text in ROLE_AREAS
        if any(perm in permissions for perm in needed)
    ]


def _anzahl(zahl: int, einzahl: str, mehrzahl: str) -> str:
    return f"{zahl} {einzahl if zahl == 1 else mehrzahl}"


def start_satz(stats: dict, review_count: int | None) -> str:
    """
    Ein Satz zum Stand für das Kopfband des neuen Starts (Issue #944) statt Zählerkacheln: nur Zahlen, die die Person
    auch in den Listen sähe (``stats`` aus der Ansicht, ``review_count`` nur mit dem Prüfrecht).
    """
    teile = []
    if "meetings_upcoming" in stats:
        teile.append(_anzahl(stats["meetings_upcoming"], "anstehende Sitzung", "anstehende Sitzungen"))
    if "papers_draft" in stats:
        teile.append(_anzahl(stats["papers_draft"], "Vorlage in Bearbeitung", "Vorlagen in Bearbeitung"))
    if review_count is not None:
        teile.append(_anzahl(review_count, "Vorlage zur Prüfung", "Vorlagen zur Prüfung"))
    if "applications_pending" in stats:
        teile.append(_anzahl(stats["applications_pending"], "offener Antrag", "offene Anträge"))
    if not teile:
        return "Hier finden Sie die Bereiche Ihrer Rolle."
    if len(teile) == 1:
        return f"{teile[0]}."
    return f"{', '.join(teile[:-1])} und {teile[-1]}."


class DashboardView(SessionViewMixin, TemplateView):
    """Main dashboard for Session RIS."""

    template_name = "session/dashboard.html"
    permission_required = "view_dashboard"

    def get_template_names(self):
        # Neues Erscheinungsbild je Mandant (Issue #944); der bisherige Start bleibt unverändert
        if neues_design(self.session_tenant):
            return ["session/neu/start.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tenant = self.session_tenant
        today = timezone.localdate()  # Kalendertag in der Zeitzone der Kommune, wie die start__date-Filter
        permissions = self.session_permissions
        # Jede Kachel nur mit ihrem Fachrecht (Funktionstrennung, Issue #221): Das Dashboard-Recht
        # allein zeigt keine Sitzungen, Vorlagen oder Anträge
        can_meetings = "view_meetings" in permissions
        can_papers = "view_papers" in permissions
        can_applications = "view_applications" in permissions
        context.update(
            {
                "can_view_meetings": can_meetings,
                "can_view_papers": can_papers,
                "can_view_applications": can_applications,
            }
        )
        # Kontrollrollen (Revision, Datenschutz) sehen bewusst keine Fachinhalte; ohne Fachkachel bliebe das
        # Dashboard leer. Sie bekommen den Stand des Protokolls und Wegweiser in ihre Bereiche (Issue #708).
        if "view_audit_log" in permissions:
            context["audit_summary"] = audit_log_service.dashboard_summary(
                tenant, with_verify="export_audit_log" in permissions
            )
        if not (can_meetings or can_papers or can_applications or "approve_papers" in permissions):
            context["without_content_tiles"] = True
            context["role_areas"] = role_areas(permissions, tenant.slug)
        meetings = SessionMeeting.objects.filter(tenant=tenant).visible_to(permissions)
        papers = SessionPaper.objects.filter(tenant=tenant).visible_to(permissions)

        # Upcoming meetings (next 30 days) — Ö/NÖ nur für Berechtigte
        if can_meetings:
            upcoming = meetings.filter(
                start__date__gte=today,
                start__date__lte=today + timedelta(days=30),
                cancelled=False,
            )
            context["upcoming_meetings"] = upcoming.select_related("organization").order_by("start")[:5]

        # Fristwarnung Ladung (Issue #29): kommende Sitzungen ohne versandte
        # Einladung — „Ladung muss bis TT.MM. raus" (überfällige zuerst)
        if can_meetings:
            # Gemeinsame Sitzungen (Issue #317): längste Ladungsfrist der beteiligten Gremien
            pending_invitations = SessionMeeting.with_joint_flag(
                SessionMeeting.objects.filter(
                    tenant=tenant,
                    start__gte=timezone.now(),
                    cancelled=False,
                    invitation_sent_at__isnull=True,
                    meeting_state__in=["draft", "scheduled"],
                )
                .select_related("organization")
                .order_by("start")
            )
            if not self.has_permission("view_non_public_meetings"):
                pending_invitations = pending_invitations.filter(is_public=True)
            candidates = list(pending_invitations[:20])
            joint_meeting_service.prefetch_joint(candidates)
            warnings = sorted(candidates, key=lambda m: m.invitation_deadline)
            context["invitation_warnings"] = warnings
            context["invitation_overdue_count"] = sum(1 for m in warnings if m.invitation_overdue)

        # Beschlusskontrolle (Issue #37): überfällige Beschlüsse prominent warnen
        if can_meetings:
            overdue_resolutions = (
                SessionAgendaItem.objects.filter(
                    meeting__tenant=tenant,
                    vote_result="approved",
                    implementation_deadline__lt=today,
                )
                .exclude(implementation_status="done")
                .select_related("meeting__organization")
                .order_by("implementation_deadline")
            )
            if not self.has_permission("view_non_public_meetings"):
                overdue_resolutions = overdue_resolutions.filter(is_public=True, meeting__is_public=True)
            context["overdue_resolutions"] = list(overdue_resolutions[:5])
            context["overdue_resolutions_count"] = overdue_resolutions.count()

        # Recent papers — Ö/NÖ nur für Berechtigte
        if can_papers:
            context["recent_papers"] = papers.select_related("main_organization", "originator_organization").order_by(
                "-created_at"
            )[:5]

        # Pending applications
        open_applications = SessionApplication.objects.filter(
            tenant=tenant,
            status__in=["submitted", "received", "in_review"],
        )
        if can_applications:
            context["pending_applications"] = open_applications.order_by("-submitted_at")[:5]

        # Arbeitsvorrat „Meine zu prüfenden Vorlagen" (Issue #33)
        if "approve_papers" in permissions:
            review_papers = papers.filter(status="review")
            context["review_papers"] = review_papers.select_related("main_organization").order_by("created_at")[:5]

        # Statistics – nur Zahlen, die die Person auch in den Listen sähe
        stats = {}
        if can_meetings:
            stats["meetings_total"] = meetings.count()
            stats["meetings_upcoming"] = meetings.filter(start__date__gte=today, cancelled=False).count()
            stats["organizations_count"] = SessionOrganization.objects.filter(tenant=tenant, is_active=True).count()
            stats["persons_count"] = SessionPerson.objects.filter(tenant=tenant, is_active=True).count()
        if can_papers:
            stats["papers_total"] = papers.count()
            stats["papers_draft"] = papers.filter(status="draft").count()
        if can_applications:
            stats["applications_pending"] = open_applications.count()
        context["stats"] = stats
        if neues_design(tenant):
            context["start_satz"] = start_satz(
                stats, context.get("papers_review_count", 0) if "approve_papers" in permissions else None
            )

        return context
